#!/usr/bin/env python3
"""
raid_architect.py — RAID planning & drive-pricing CLI.

A dependency-light, single-file tool that helps you plan a RAID array and price
the drives across three market tiers (Recertified, NAS Pro New, Enterprise New).

It works with ZERO third-party packages installed (falling back to a static
pricing baseline), and can optionally fetch *live* recertified-drive pricing
from ServerPartDeals when ``cloudscraper`` (or plain ``requests``/``urllib``)
is available.

Two planning modes are supported:

  * capacity mode  — give a target usable capacity; the tool recommends a drive
                     count for the chosen RAID level.
  * drives mode    — give a fixed number of drives; the tool reports the
                     resulting usable capacity.

Run ``raid_architect.py --help`` for usage and examples.

Author: entropytwo-ai
License: GPL-3.0
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from statistics import median
from typing import Callable, Optional

__version__ = "2.0.0"

# ---------------------------------------------------------------------------
# Optional dependency detection (graceful degradation is a core requirement).
# ---------------------------------------------------------------------------
try:
    import cloudscraper  # type: ignore
except ImportError:  # pragma: no cover - environment dependent
    cloudscraper = None  # type: ignore

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover - environment dependent
    requests = None  # type: ignore

HAVE_CLOUDSCRAPER = cloudscraper is not None
HAVE_REQUESTS = requests is not None


# ===========================================================================
# Constants
# ===========================================================================

#: Static per-TB baseline rates (USD) representing early-2026 market estimates.
HISTORICAL_RATES: dict[str, float] = {
    "Refurb": 15.50,
    "NAS": 22.50,
    "Ent": 25.00,
}

#: Multipliers used to derive the new-drive tiers from a scraped refurb price.
MARKUP_FROM_REFURB: dict[str, float] = {
    "NAS": 1.45,
    "Ent": 1.60,
}

#: Human-friendly tier labels for display.
TIER_LABELS: dict[str, str] = {
    "Refurb": "Recertified",
    "NAS": "NAS Pro (New)",
    "Ent": "Enterprise (New)",
}

#: Order in which tiers are displayed.
TIER_ORDER: tuple[str, ...] = ("Refurb", "NAS", "Ent")

#: Reference market links per tier (label, url).
MARKET_LINKS: dict[str, list[tuple[str, str]]] = {
    "Ent": [
        ("WD Gold", "https://www.westerndigital.com/products/internal-drives/wd-gold-sata-hdd"),
        ("Seagate Exos X24", "https://www.seagate.com/products/enterprise-drives/exos-hard-drives/"),
    ],
    "NAS": [
        ("WD Red Pro", "https://www.westerndigital.com/products/internal-drives/wd-red-pro-sata-hdd"),
        ("Seagate IronWolf Pro", "https://www.seagate.com/products/nas-drives/ironwolf-hard-drive/"),
    ],
    "Refurb": [
        ("ServerPartDeals", "https://serverpartdeals.com/collections/manufacturer-recertified-drives"),
        ("goHardDrive", "https://www.goharddrive.com/Recertified-Hard-Drives-s/25.htm"),
    ],
}

SPD_PRODUCTS_URL = (
    "https://serverpartdeals.com/collections/"
    "manufacturer-recertified-drives/products.json?limit=250"
)

#: Retailers with structured, polite-to-fetch Shopify ``products.json`` feeds,
#: used by the "best deals" scraper. Each source is fetched ONCE per run and
#: guarded independently — one failing feed never breaks the others. Keep this
#: list small to be a good citizen on the shared outbound IP.
DEAL_SOURCES: list[dict] = [
    {
        "name": "ServerPartDeals",
        "url": ("https://serverpartdeals.com/collections/"
                "manufacturer-recertified-drives/products.json?limit=250"),
        "base": "https://serverpartdeals.com",
        "default_condition": "Recertified",
    },
    {
        "name": "Water Panther",
        "url": "https://waterpanther.com/products.json?limit=250",
        "base": "https://waterpanther.com",
        "default_condition": None,
    },
]

#: Multipliers to translate TB-like units into TB.
UNIT_TO_TB: dict[str, float] = {
    "TB": 1.0,
    "T": 1.0,
    "GB": 1.0 / 1000.0,
    "G": 1.0 / 1000.0,
    "PB": 1000.0,
    "P": 1000.0,
    "MB": 1.0 / 1_000_000.0,
    "M": 1.0 / 1_000_000.0,
}


class RaidPricerError(Exception):
    """Raised for user-facing input/validation errors (no traceback shown)."""


# ===========================================================================
# ANSI colour / hyperlink helpers (auto-disabled for non-TTY / NO_COLOR).
# ===========================================================================


@dataclass
class Style:
    """Terminal styling that automatically degrades for pipes / NO_COLOR."""

    enabled: bool = True
    hyperlinks: bool = True

    # Raw ANSI codes (only emitted when enabled).
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    def paint(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return "".join(codes) + text + self.RESET

    def link(self, label: str, url: str) -> str:
        """Return an OSC 8 hyperlink, or a plain-text fallback."""
        if self.hyperlinks and self.enabled:
            return f"\033]8;;{url}\033\\{label}\033]8;;\033\\ ({url})"
        return f"{label} ({url})"


def resolve_style(stream, force_color: Optional[bool] = None,
                  no_color_env: Optional[bool] = None) -> Style:
    """Decide whether to emit ANSI/hyperlinks for *stream*.

    Honours the ``NO_COLOR`` convention and non-TTY output; ``force_color`` can
    override (e.g. ``--color always``).
    """
    if no_color_env is None:
        no_color_env = "NO_COLOR" in os.environ
    if force_color is True:
        enabled = True
    elif force_color is False:
        enabled = False
    else:
        enabled = stream.isatty() and not no_color_env
    return Style(enabled=enabled, hyperlinks=enabled)


# ===========================================================================
# Parsing helpers (robust replacements for fragile regex).
# ===========================================================================


def parse_size(value: str) -> float:
    """Parse a capacity string into a float number of **terabytes**.

    Accepts bare numbers (interpreted as TB) and values with units such as
    ``22TB``, ``22 tb``, ``500GB``, ``2PB``. Raises :class:`RaidPricerError`
    on malformed or non-positive input.
    """
    if value is None:
        raise RaidPricerError("A size value is required.")
    raw = str(value).strip().upper().replace(" ", "")
    if not raw:
        raise RaidPricerError("Empty size value.")

    # Split leading numeric part from a trailing unit.
    idx = 0
    seen_dot = False
    while idx < len(raw) and (raw[idx].isdigit() or (raw[idx] == "." and not seen_dot)):
        if raw[idx] == ".":
            seen_dot = True
        idx += 1

    number_part, unit_part = raw[:idx], raw[idx:]
    if not number_part:
        raise RaidPricerError(f"Could not read a number from size '{value}'.")

    try:
        number = float(number_part)
    except ValueError:
        raise RaidPricerError(f"Invalid numeric size '{value}'.")

    unit = unit_part or "TB"
    if unit not in UNIT_TO_TB:
        allowed = ", ".join(sorted(UNIT_TO_TB))
        raise RaidPricerError(
            f"Unknown size unit '{unit_part}' in '{value}'. Use one of: {allowed}."
        )

    tb = number * UNIT_TO_TB[unit]
    if tb <= 0:
        raise RaidPricerError(f"Size must be positive (got '{value}').")
    return tb


def fmt_tb(value: float) -> str:
    """Format a TB value cleanly (no trailing .0 for whole numbers)."""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value))} TB"
    return f"{value:.2f} TB".rstrip("0").rstrip(".") + ("" if value % 1 else " TB")


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


# ===========================================================================
# RAID level modelling.
# ===========================================================================


@dataclass(frozen=True)
class RaidLevel:
    """Static description of a RAID level and how it computes capacity."""

    key: str                      # canonical key, e.g. "5", "10", "60"
    name: str                     # display name, e.g. "RAID 5"
    min_drives: int
    grouped: bool                 # True for 50/60 (striped parity groups)
    parity_per_group: int         # parity drives lost per group (0 for mirror/stripe)
    read: str                     # qualitative read characterization
    write: str                    # qualitative write characterization
    summary: str                  # one-line description


RAID_LEVELS: dict[str, RaidLevel] = {
    "0": RaidLevel(
        key="0", name="RAID 0", min_drives=2, grouped=False, parity_per_group=0,
        read="Excellent (full striping across all drives)",
        write="Excellent (full striping, no parity)",
        summary="Striping only. Maximum speed & capacity, ZERO redundancy.",
    ),
    "1": RaidLevel(
        key="1", name="RAID 1", min_drives=2, grouped=False, parity_per_group=0,
        read="Very good (reads can be served from any mirror)",
        write="Good (every write goes to all mirrors)",
        summary="Mirroring. Every drive holds the same data; high redundancy, low efficiency.",
    ),
    "5": RaidLevel(
        key="5", name="RAID 5", min_drives=3, grouped=False, parity_per_group=1,
        read="Excellent (striped reads)",
        write="Moderate (single-parity read-modify-write penalty)",
        summary="Single distributed parity. Good balance; survives 1 drive failure.",
    ),
    "6": RaidLevel(
        key="6", name="RAID 6", min_drives=4, grouped=False, parity_per_group=2,
        read="Excellent (striped reads)",
        write="Moderate/Slow (double-parity write penalty)",
        summary="Dual distributed parity. Survives 2 drive failures; safer rebuilds.",
    ),
    "10": RaidLevel(
        key="10", name="RAID 10", min_drives=4, grouped=False, parity_per_group=0,
        read="Excellent (striped across mirrored pairs)",
        write="Very good (mirror writes, no parity math)",
        summary="Striped mirrors. Fast & resilient; 50% capacity cost.",
    ),
    "50": RaidLevel(
        key="50", name="RAID 50", min_drives=6, grouped=True, parity_per_group=1,
        read="Excellent (striped across RAID 5 groups)",
        write="Good (parity per group, parallel across groups)",
        summary="Striped RAID 5 groups. Survives 1 failure per group.",
    ),
    "60": RaidLevel(
        key="60", name="RAID 60", min_drives=8, grouped=True, parity_per_group=2,
        read="Excellent (striped across RAID 6 groups)",
        write="Moderate (double parity per group, parallel across groups)",
        summary="Striped RAID 6 groups. Survives 2 failures per group.",
    ),
}

#: Minimum drives-per-group for grouped levels.
MIN_GROUP_SIZE = {"50": 3, "60": 4}


def normalize_level(level: str) -> str:
    """Normalise a user-supplied level token to a canonical key."""
    key = str(level).strip().upper().replace("RAID", "").strip()
    if key in RAID_LEVELS:
        return key
    raise RaidPricerError(
        f"Unsupported RAID level '{level}'. Supported: "
        + ", ".join(sorted(RAID_LEVELS, key=lambda k: int(k)))
    )


def level_from_fault_tolerance(fault: int) -> str:
    """Map a desired fault tolerance to a sensible single-array RAID level."""
    mapping = {0: "0", 1: "5", 2: "6"}
    if fault not in mapping:
        raise RaidPricerError(
            "Fault tolerance must be 0 (RAID 0), 1 (RAID 5), or 2 (RAID 6). "
            "For higher tolerance pick RAID 10/50/60 explicitly with --level."
        )
    return mapping[fault]


# ===========================================================================
# Layout computation.
# ===========================================================================


@dataclass
class RaidLayout:
    """Computed RAID layout for a given drive count / size / level."""

    level: str
    level_name: str
    drive_size_tb: float
    data_drives: int                 # drives participating in the array
    hot_spares: int                  # reserved spares (cost only, no capacity)
    total_drives: int                # data_drives + hot_spares
    groups: Optional[int]            # for grouped levels, else None
    raw_capacity_tb: float           # data drives only
    usable_capacity_tb: float
    efficiency_pct: float            # usable / raw (data drives)
    fault_tolerance: int             # drives that can fail without data loss
    fault_tolerance_note: str
    read: str
    write: str
    summary: str
    min_drives: int

    def to_dict(self) -> dict:
        return asdict(self)


def _choose_groups(level: RaidLevel, n: int, requested_groups: Optional[int],
                   requested_group_size: Optional[int]) -> int:
    """Validate/choose a group count for grouped levels (50/60)."""
    min_gsize = MIN_GROUP_SIZE[level.key]

    if requested_groups is not None and requested_group_size is not None:
        raise RaidPricerError("Specify either --groups or --group-size, not both.")

    if requested_group_size is not None:
        if requested_group_size < min_gsize:
            raise RaidPricerError(
                f"{level.name} needs at least {min_gsize} drives per group."
            )
        if n % requested_group_size != 0:
            raise RaidPricerError(
                f"{n} drives cannot be split into groups of {requested_group_size}."
            )
        groups = n // requested_group_size
    elif requested_groups is not None:
        groups = requested_groups
    else:
        # Auto-pick the largest group count whose group size is still valid,
        # preferring 2 groups when possible for a sane default.
        groups = None
        for g in range(2, n // min_gsize + 1):
            if n % g == 0 and (n // g) >= min_gsize:
                groups = g
                break
        if groups is None:
            groups = 1 if n >= min_gsize else 0

    if groups < 2:
        raise RaidPricerError(
            f"{level.name} requires at least 2 groups (got {groups}). "
            f"Use more drives or adjust --groups/--group-size."
        )
    if n % groups != 0:
        raise RaidPricerError(
            f"{n} drives are not evenly divisible into {groups} groups."
        )
    if (n // groups) < min_gsize:
        raise RaidPricerError(
            f"{level.name} needs >= {min_gsize} drives per group; "
            f"{groups} groups of {n} drives gives {n // groups}."
        )
    return groups


def compute_layout(level_key: str, drive_size_tb: float, data_drives: int,
                   hot_spares: int = 0, groups: Optional[int] = None,
                   group_size: Optional[int] = None) -> RaidLayout:
    """Compute a full :class:`RaidLayout` from raw inputs (with validation)."""
    level = RAID_LEVELS[level_key]

    if data_drives < level.min_drives:
        raise RaidPricerError(
            f"{level.name} requires at least {level.min_drives} drives "
            f"(requested {data_drives})."
        )
    if hot_spares < 0:
        raise RaidPricerError("Hot spares cannot be negative.")

    chosen_groups: Optional[int] = None
    raw = data_drives * drive_size_tb

    if level.key == "0":
        usable = data_drives * drive_size_tb
        fault = 0
        fault_note = "None — any single drive failure loses the whole array."
    elif level.key == "1":
        usable = drive_size_tb
        fault = data_drives - 1
        fault_note = f"Survives up to {fault} failure(s) (all but one mirror)."
    elif level.key == "5":
        usable = (data_drives - 1) * drive_size_tb
        fault = 1
        fault_note = "Survives 1 drive failure."
    elif level.key == "6":
        usable = (data_drives - 2) * drive_size_tb
        fault = 2
        fault_note = "Survives 2 drive failures."
    elif level.key == "10":
        if data_drives % 2 != 0:
            raise RaidPricerError("RAID 10 requires an even number of drives.")
        usable = (data_drives // 2) * drive_size_tb
        fault = 1
        fault_note = (
            "Survives 1 guaranteed failure; up to 1 per mirror "
            f"({data_drives // 2} in the best case)."
        )
    elif level.key in ("50", "60"):
        chosen_groups = _choose_groups(level, data_drives, groups, group_size)
        parity = level.parity_per_group * chosen_groups
        usable = (data_drives - parity) * drive_size_tb
        fault = level.parity_per_group  # guaranteed across the array
        per_group = level.parity_per_group
        fault_note = (
            f"Survives {per_group} failure(s) per group guaranteed; "
            f"up to {parity} if spread across all {chosen_groups} groups."
        )
    else:  # pragma: no cover - guarded by normalize_level
        raise RaidPricerError(f"Unhandled RAID level '{level_key}'.")

    if usable <= 0:
        raise RaidPricerError(
            f"{level.name} with {data_drives} drives yields no usable capacity. "
            "Add more drives."
        )

    efficiency = (usable / raw * 100.0) if raw > 0 else 0.0

    return RaidLayout(
        level=level.key,
        level_name=level.name,
        drive_size_tb=drive_size_tb,
        data_drives=data_drives,
        hot_spares=hot_spares,
        total_drives=data_drives + hot_spares,
        groups=chosen_groups,
        raw_capacity_tb=raw,
        usable_capacity_tb=usable,
        efficiency_pct=efficiency,
        fault_tolerance=fault,
        fault_tolerance_note=fault_note,
        read=level.read,
        write=level.write,
        summary=level.summary,
        min_drives=level.min_drives,
    )


def drives_for_capacity(level_key: str, drive_size_tb: float, target_tb: float,
                        groups: Optional[int] = None,
                        group_size: Optional[int] = None) -> int:
    """Return the minimum data-drive count to reach *target_tb* usable."""
    level = RAID_LEVELS[level_key]
    if target_tb <= 0:
        raise RaidPricerError("Target capacity must be positive.")
    data_slots = math.ceil(target_tb / drive_size_tb)  # drives worth of data

    if level.key == "0":
        n = max(data_slots, level.min_drives)
    elif level.key == "1":
        if drive_size_tb < target_tb - 1e-9:
            raise RaidPricerError(
                f"RAID 1 usable capacity equals a single drive ({fmt_tb(drive_size_tb)}). "
                f"It cannot reach the target {fmt_tb(target_tb)}. Use a larger drive "
                "or a striped level (0/5/6/10/50/60)."
            )
        n = 2
    elif level.key == "5":
        n = max(data_slots + 1, level.min_drives)
    elif level.key == "6":
        n = max(data_slots + 2, level.min_drives)
    elif level.key == "10":
        n = max(data_slots * 2, level.min_drives)
        if n % 2:
            n += 1
    elif level.key in ("50", "60"):
        # Decide group count first (default 2 groups unless specified).
        if group_size is not None and groups is not None:
            raise RaidPricerError("Specify either --groups or --group-size, not both.")
        g = groups if groups is not None else 2
        if g < 2:
            raise RaidPricerError(f"{level.name} requires at least 2 groups.")
        parity = level.parity_per_group * g
        n = data_slots + parity
        min_gsize = MIN_GROUP_SIZE[level.key]
        # Round up to a multiple of g and respect min group size.
        n = max(n, g * min_gsize, level.min_drives)
        if n % g:
            n += g - (n % g)
    else:  # pragma: no cover
        raise RaidPricerError(f"Unhandled RAID level '{level_key}'.")

    return n


# ===========================================================================
# Pricing.
# ===========================================================================


@dataclass
class PricingRates:
    """Per-TB rates (USD) for each tier plus provenance metadata."""

    refurb: float
    nas: float
    ent: float
    source: str                 # "live", "baseline", "manual", "manual+baseline"
    note: str = ""

    def per_tb(self, tier: str) -> float:
        return {"Refurb": self.refurb, "NAS": self.nas, "Ent": self.ent}[tier]


@dataclass
class TierQuote:
    tier: str
    label: str
    per_tb: float
    per_drive: float
    total: float


@dataclass
class PricingResult:
    rates: PricingRates
    quotes: list[TierQuote] = field(default_factory=list)


# Simple in-run cache so repeated live lookups don't re-hit the network.
_LIVE_CACHE: dict[float, Optional[float]] = {}


def _fetch_products_json(url: str, timeout: int = 12) -> Optional[dict]:
    """Fetch & parse the products JSON using the best available fetcher.

    Order of preference: cloudscraper (bypasses Cloudflare) -> requests -> urllib.
    Returns ``None`` on any failure (network, HTTP, malformed JSON).
    """
    # 1) cloudscraper
    if HAVE_CLOUDSCRAPER:
        try:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass

    # 2) requests
    if HAVE_REQUESTS:
        try:
            resp = requests.get(url, timeout=timeout,
                                headers={"User-Agent": "raid-architect/" + __version__})
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass

    # 3) stdlib urllib
    try:
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "raid-architect/" + __version__}
        )
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return json.loads(fh.read().decode("utf-8"))
    except Exception:
        return None


def _extract_price_for_size(data: dict, size_tb: float) -> Optional[float]:
    """Return the median per-TB refurb price for drives matching *size_tb*.

    Scans all products/variants whose title mentions the size, collects their
    prices, and returns the median (robust against outliers / odd variants).
    """
    if not isinstance(data, dict):
        return None
    size_int = int(size_tb) if float(size_tb).is_integer() else size_tb
    needles = {f"{size_int}TB", f"{size_int} TB"}
    prices: list[float] = []
    for product in data.get("products", []):
        title = str(product.get("title", "")).upper()
        if not any(n.upper() in title for n in needles):
            continue
        for variant in product.get("variants", []):
            try:
                price = float(variant.get("price"))
            except (TypeError, ValueError):
                continue
            # Ignore obviously broken / placeholder prices.
            if price > 0:
                prices.append(price)
    if not prices:
        return None
    return median(prices)


def get_live_per_tb(size_tb: float, timeout: int = 12,
                    verbose: bool = False) -> Optional[float]:
    """Return a live per-TB refurb rate for *size_tb*, or ``None`` on failure."""
    if size_tb in _LIVE_CACHE:
        return _LIVE_CACHE[size_tb]

    data = _fetch_products_json(SPD_PRODUCTS_URL, timeout=timeout)
    if data is None:
        if verbose:
            print("  live pricing: fetch failed; using baseline.", file=sys.stderr)
        _LIVE_CACHE[size_tb] = None
        return None

    price = _extract_price_for_size(data, size_tb)
    if price is None:
        if verbose:
            print(f"  live pricing: no {fmt_tb(size_tb)} match; using baseline.",
                  file=sys.stderr)
        _LIVE_CACHE[size_tb] = None
        return None

    per_tb = price / size_tb
    _LIVE_CACHE[size_tb] = per_tb
    return per_tb


def resolve_rates(size_tb: float, *, want_live: bool,
                  manual: dict[str, Optional[float]],
                  price_per_drive: Optional[float] = None,
                  verbose: bool = False) -> PricingRates:
    """Resolve the per-TB rates for every tier, honouring overrides."""
    # Start from baseline.
    refurb = HISTORICAL_RATES["Refurb"]
    nas = HISTORICAL_RATES["NAS"]
    ent = HISTORICAL_RATES["Ent"]
    source = "baseline"
    note = "Static early-2026 estimates."

    # Live pricing (only if requested).
    if want_live:
        live = get_live_per_tb(size_tb, verbose=verbose)
        if live is not None:
            refurb = live
            nas = live * MARKUP_FROM_REFURB["NAS"]
            ent = live * MARKUP_FROM_REFURB["Ent"]
            source = "live"
            note = "Live ServerPartDeals recertified pricing (median match)."
        else:
            source = "baseline"
            note = "Live fetch unavailable; fell back to static baseline."

    # A flat per-drive price overrides everything for the refurb tier basis.
    if price_per_drive is not None:
        if price_per_drive <= 0:
            raise RaidPricerError("--price-per-drive must be positive.")
        pdt = price_per_drive / size_tb
        refurb = pdt
        nas = pdt * MARKUP_FROM_REFURB["NAS"]
        ent = pdt * MARKUP_FROM_REFURB["Ent"]
        source = "manual"
        note = f"Derived from --price-per-drive ({fmt_money(price_per_drive)}/drive)."

    # Explicit per-TB overrides win last, per tier.
    overridden = False
    if manual.get("refurb") is not None:
        refurb = manual["refurb"]; overridden = True
    if manual.get("nas") is not None:
        nas = manual["nas"]; overridden = True
    if manual.get("ent") is not None:
        ent = manual["ent"]; overridden = True
    if overridden:
        source = "manual" if source in ("manual", "baseline") else "manual+live"
        note = "Manual per-TB rate override(s) applied."

    for name, val in (("Refurb", refurb), ("NAS", nas), ("Ent", ent)):
        if val <= 0:
            raise RaidPricerError(f"{name} rate must be positive.")

    return PricingRates(refurb=refurb, nas=nas, ent=ent, source=source, note=note)


def price_layout(layout: RaidLayout, rates: PricingRates) -> PricingResult:
    """Produce per-tier quotes for a layout (priced on TOTAL drives incl. spares)."""
    result = PricingResult(rates=rates)
    drives = layout.total_drives
    size = layout.drive_size_tb
    for tier in TIER_ORDER:
        per_tb = rates.per_tb(tier)
        per_drive = per_tb * size
        total = per_drive * drives
        result.quotes.append(
            TierQuote(
                tier=tier,
                label=TIER_LABELS[tier],
                per_tb=per_tb,
                per_drive=per_drive,
                total=total,
            )
        )
    return result


# ===========================================================================
# Rendering.
# ===========================================================================


def _bar(pct: float, width: int = 20) -> str:
    filled = int(round(pct / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def render_report(layout: RaidLayout, pricing: PricingResult, st: Style) -> str:
    """Render the full human-readable report for a single layout."""
    lines: list[str] = []
    title = f" {layout.level_name} — RAID Plan & Pricing "
    lines.append(st.paint("═" * 62, st.CYAN))
    lines.append(st.paint(title.center(62), st.BOLD, st.CYAN))
    lines.append(st.paint("═" * 62, st.CYAN))
    lines.append("")

    lines.append(st.paint("Layout", st.BOLD, st.WHITE))
    lines.append(f"  {'Level':<22}{layout.level_name}  — {layout.summary}")
    lines.append(f"  {'Drive size':<22}{fmt_tb(layout.drive_size_tb)}")
    grp = f"  ({layout.groups} groups)" if layout.groups else ""
    lines.append(f"  {'Array drives':<22}{layout.data_drives}{grp}")
    if layout.hot_spares:
        lines.append(f"  {'Hot spares':<22}{layout.hot_spares}")
        lines.append(f"  {'Total drives':<22}{layout.total_drives}")
    lines.append(f"  {'Minimum drives':<22}{layout.min_drives}")
    lines.append("")

    lines.append(st.paint("Capacity", st.BOLD, st.WHITE))
    lines.append(f"  {'Raw capacity':<22}{fmt_tb(layout.raw_capacity_tb)}")
    lines.append(f"  {'Usable capacity':<22}"
                 + st.paint(fmt_tb(layout.usable_capacity_tb), st.GREEN, st.BOLD))
    eff = f"{layout.efficiency_pct:.1f}%"
    lines.append(f"  {'Efficiency':<22}{eff}  {st.paint(_bar(layout.efficiency_pct), st.CYAN)}")
    lines.append("")

    lines.append(st.paint("Resilience & Performance", st.BOLD, st.WHITE))
    ft = f"{layout.fault_tolerance} drive(s)"
    ft_color = st.GREEN if layout.fault_tolerance else st.RED
    lines.append(f"  {'Fault tolerance':<22}" + st.paint(ft, ft_color, st.BOLD))
    lines.append(f"  {'':<22}{st.paint(layout.fault_tolerance_note, st.DIM)}")
    lines.append(f"  {'Read':<22}{layout.read}")
    lines.append(f"  {'Write':<22}{layout.write}")
    lines.append("")

    # Pricing table.
    lines.append(st.paint("Pricing", st.BOLD, st.WHITE)
                 + st.paint(f"   (source: {pricing.rates.source})", st.DIM))
    lines.append(st.paint(f"  {pricing.rates.note}", st.DIM))
    header = f"  {'Tier':<18}{'$/TB':>10}{'$/drive':>13}{'Total':>16}"
    lines.append(st.paint(header, st.BOLD))
    lines.append("  " + st.paint("─" * 55, st.DIM))
    for q in pricing.quotes:
        row = (f"  {q.label:<18}{fmt_money(q.per_tb):>10}"
               f"{fmt_money(q.per_drive):>13}{fmt_money(q.total):>16}")
        color = {"Refurb": st.GREEN, "NAS": st.YELLOW, "Ent": st.MAGENTA}[q.tier]
        lines.append(st.paint(row, color))
    note = f"  Totals cover all {layout.total_drives} drive(s)"
    if layout.hot_spares:
        note += f" (incl. {layout.hot_spares} hot spare(s))"
    lines.append(st.paint(note + ".", st.DIM))
    lines.append("")

    # Market reference links.
    lines.append(st.paint("Market references", st.BOLD, st.WHITE))
    for tier in TIER_ORDER:
        label = TIER_LABELS[tier]
        refs = "   ".join(st.link(lbl, url) for lbl, url in MARKET_LINKS[tier])
        lines.append(f"  {label:<18}{refs}")

    return "\n".join(lines)


def render_compare(layouts: list[tuple[RaidLayout, PricingResult]], target_tb: float,
                   st: Style) -> str:
    """Render a side-by-side comparison of several RAID levels for one target."""
    lines: list[str] = []
    lines.append(st.paint("═" * 86, st.CYAN))
    heading = f" RAID Level Comparison — target usable ≈ {fmt_tb(target_tb)} "
    lines.append(st.paint(heading.center(86), st.BOLD, st.CYAN))
    lines.append(st.paint("═" * 86, st.CYAN))
    header = (f"{'Level':<9}{'Drives':>7}{'Usable':>12}{'Eff%':>7}"
              f"{'Tol':>5}{'Refurb $':>13}{'NAS $':>13}{'Ent $':>13}")
    lines.append(st.paint(header, st.BOLD))
    lines.append(st.paint("─" * 86, st.DIM))
    for layout, pricing in layouts:
        q = {x.tier: x for x in pricing.quotes}
        row = (f"{layout.level_name:<9}{layout.total_drives:>7}"
               f"{fmt_tb(layout.usable_capacity_tb):>12}"
               f"{layout.efficiency_pct:>6.0f}%"
               f"{layout.fault_tolerance:>5}"
               f"{fmt_money(q['Refurb'].total):>13}"
               f"{fmt_money(q['NAS'].total):>13}"
               f"{fmt_money(q['Ent'].total):>13}")
        lines.append(row)
    lines.append(st.paint("─" * 86, st.DIM))
    lines.append(st.paint(f"Pricing source: {layouts[0][1].rates.source} — "
                          f"{layouts[0][1].rates.note}", st.DIM))
    return "\n".join(lines)


def build_json(layout: RaidLayout, pricing: PricingResult) -> dict:
    return {
        "version": __version__,
        "layout": layout.to_dict(),
        "pricing": {
            "source": pricing.rates.source,
            "note": pricing.rates.note,
            "rates_per_tb": {
                "Refurb": pricing.rates.refurb,
                "NAS": pricing.rates.nas,
                "Ent": pricing.rates.ent,
            },
            "quotes": [asdict(q) for q in pricing.quotes],
        },
    }


# ===========================================================================
# Best hard-drive deals scraper.
#
# Reuses the SAME multi-stage fetcher (_fetch_products_json: cloudscraper ->
# requests -> urllib), User-Agent, timeouts and graceful degradation as the
# live-pricing path. Each retailer feed is fetched once per run, cached, and
# guarded independently so one failing source never breaks the others.
# ===========================================================================


@dataclass
class DealListing:
    """A single parsed drive listing, ranked by $/TB (lower is better value)."""

    title: str
    capacity_tb: float
    price: float
    price_per_tb: float
    condition: str          # Recertified / New / NAS / Enterprise / Unknown
    retailer: str
    url: str

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "capacity_tb": self.capacity_tb,
            "price": round(self.price, 2),
            "price_per_tb": round(self.price_per_tb, 4),
            "condition": self.condition,
            "retailer": self.retailer,
            "url": self.url,
        }


@dataclass
class SourceStatus:
    """Per-retailer fetch outcome, surfaced so the UI/CLI can note failures."""

    name: str
    ok: bool
    count: int = 0

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "count": self.count}


@dataclass
class DealsResult:
    """Ranked deals plus the status of each source they were drawn from."""

    deals: list[DealListing] = field(default_factory=list)
    sources: list[SourceStatus] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": __version__,
            "count": len(self.deals),
            "any_source_ok": any(s.ok for s in self.sources),
            "sources": [s.to_dict() for s in self.sources],
            "deals": [d.to_dict() for d in self.deals],
        }


#: Capacity like "22TB", "22 TB", "20tb" — captures the numeric TB value.
_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*TB\b", re.IGNORECASE)

#: In-run cache of (all parsed deals, source statuses) so filters are cheap and
#: the network is hit at most once per process.
_DEALS_CACHE: Optional[tuple[list[DealListing], list[SourceStatus]]] = None

_CONDITION_ALIASES: dict[str, str] = {
    "recert": "recertified", "recertified": "recertified",
    "refurb": "recertified", "refurbished": "recertified", "renewed": "recertified",
    "new": "new", "nas": "nas", "enterprise": "enterprise", "ent": "enterprise",
}


def parse_capacity_tb(title: str) -> Optional[float]:
    """Robustly parse a single-drive capacity in TB from a product title.

    Returns ``None`` when no sane single-drive TB value is present. Values above
    100 TB are rejected as implausible for one drive (likely a bundle/typo).
    """
    if not title:
        return None
    m = _CAPACITY_RE.search(title)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if val <= 0 or val > 100:
        return None
    return val


def _is_multipack(title: str) -> bool:
    """Heuristic: skip bundles / multi-packs (their $/TB would be misleading)."""
    t = title.lower()
    return ("-pack" in t or " pack" in t or "pack of" in t
            or "bundle" in t or " 2x" in t or " 4x" in t)


def infer_condition(title: str, default: Optional[str] = None) -> str:
    """Infer a drive's condition/tier from title keywords."""
    t = title.lower()
    if any(k in t for k in ("recertified", "recert", "refurbished", "refurb", "renewed")):
        return "Recertified"
    if "enterprise" in t:
        return "Enterprise"
    if "nas" in t:
        return "NAS"
    if "new" in t:
        return "New"
    return default or "Unknown"


def _product_price(product: dict) -> Optional[float]:
    """Return the cheapest available variant price (>0), or None."""
    available: list[float] = []
    any_price: list[float] = []
    for variant in product.get("variants", []):
        try:
            price = float(variant.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        any_price.append(price)
        if variant.get("available", True):
            available.append(price)
    pool = available or any_price
    return min(pool) if pool else None


def _parse_deals_from_products(data: dict, src: dict) -> list[DealListing]:
    """Parse a Shopify products.json payload into :class:`DealListing` rows."""
    if not isinstance(data, dict):
        return []
    base = src.get("base", "").rstrip("/")
    default_cond = src.get("default_condition")
    out: list[DealListing] = []
    for product in data.get("products", []):
        title = str(product.get("title", "")).strip()
        if not title or _is_multipack(title):
            continue
        cap = parse_capacity_tb(title)
        if cap is None:
            continue
        price = _product_price(product)
        if price is None:
            continue
        handle = product.get("handle")
        url = f"{base}/products/{handle}" if (base and handle) else (base or "")
        out.append(DealListing(
            title=title,
            capacity_tb=cap,
            price=price,
            price_per_tb=price / cap,
            condition=infer_condition(title, default_cond),
            retailer=src.get("name", "?"),
            url=url,
        ))
    return out


def _fetch_all_deals(timeout: int = 12,
                     verbose: bool = False) -> tuple[list[DealListing], list[SourceStatus]]:
    """Fetch & parse every configured source once; cache for the run.

    Never raises: a source that errors, times out, or returns malformed data is
    recorded as ``ok=False`` and simply contributes no listings.
    """
    global _DEALS_CACHE
    if _DEALS_CACHE is not None:
        return _DEALS_CACHE

    all_deals: list[DealListing] = []
    statuses: list[SourceStatus] = []
    for src in DEAL_SOURCES:
        name = src.get("name", "?")
        try:
            data = _fetch_products_json(src["url"], timeout=timeout)
            if not isinstance(data, dict):
                statuses.append(SourceStatus(name, False, 0))
                if verbose:
                    print(f"  deals: {name} unavailable.", file=sys.stderr)
                continue
            parsed = _parse_deals_from_products(data, src)
            all_deals.extend(parsed)
            statuses.append(SourceStatus(name, True, len(parsed)))
        except Exception:  # pragma: no cover - defensive; never crash
            statuses.append(SourceStatus(name, False, 0))
            if verbose:
                print(f"  deals: {name} errored.", file=sys.stderr)

    _DEALS_CACHE = (all_deals, statuses)
    return _DEALS_CACHE


def reset_deals_cache() -> None:
    """Clear the in-run deals cache (used by tests)."""
    global _DEALS_CACHE
    _DEALS_CACHE = None


def _normalize_condition_filter(condition: Optional[str]) -> Optional[str]:
    if not condition:
        return None
    key = str(condition).strip().lower()
    return _CONDITION_ALIASES.get(key, key)


def get_deals(min_tb: Optional[float] = None, max_tb: Optional[float] = None,
              limit: Optional[int] = 12, condition: Optional[str] = None,
              timeout: int = 12, verbose: bool = False) -> DealsResult:
    """Return ranked deals (best $/TB first) plus per-source status.

    Filters by capacity range and/or condition, de-duplicates obvious repeats,
    and sorts ascending by $/TB. Degrades to an empty result (never raises) when
    nothing is fetchable.
    """
    all_deals, statuses = _fetch_all_deals(timeout=timeout, verbose=verbose)
    cond = _normalize_condition_filter(condition)

    filtered: list[DealListing] = []
    seen: set = set()
    for d in all_deals:
        if min_tb is not None and d.capacity_tb < min_tb - 1e-9:
            continue
        if max_tb is not None and d.capacity_tb > max_tb + 1e-9:
            continue
        if cond is not None and d.condition.lower() != cond:
            continue
        key = (d.retailer, d.title.lower(), round(d.price, 2))
        if key in seen:
            continue
        seen.add(key)
        filtered.append(d)

    filtered.sort(key=lambda d: d.price_per_tb)
    if limit is not None and limit > 0:
        filtered = filtered[:limit]
    return DealsResult(deals=filtered, sources=statuses)


def get_best_deals(min_tb: Optional[float] = None, max_tb: Optional[float] = None,
                   limit: Optional[int] = 12, condition: Optional[str] = None,
                   timeout: int = 12) -> list[DealListing]:
    """Convenience wrapper returning just the ranked list of deals."""
    return get_deals(min_tb=min_tb, max_tb=max_tb, limit=limit,
                     condition=condition, timeout=timeout).deals


def _truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def render_deals(result: DealsResult, st: Style) -> str:
    """Render a beautiful, ranked deals table (OSC 8 links, colour-aware)."""
    lines: list[str] = []
    lines.append(st.paint("═" * 74, st.CYAN))
    lines.append(st.paint(" Best hard-drive deals — ranked by $/TB ".center(74),
                          st.BOLD, st.CYAN))
    lines.append(st.paint("═" * 74, st.CYAN))

    if result.sources:
        src_bits = ", ".join(f"{s.name} ({s.count})" if s.ok else f"{s.name} ✗"
                             for s in result.sources)
        lines.append(st.paint(f"  Sources: {src_bits}", st.DIM))
    failed = [s.name for s in result.sources if not s.ok]
    if failed:
        lines.append(st.paint(f"  Unavailable right now: {', '.join(failed)}", st.YELLOW))
    lines.append("")

    if not result.deals:
        lines.append(st.paint("  No live deals available right now.", st.YELLOW, st.BOLD))
        lines.append(st.paint("  (Retailer feeds may be unreachable, or nothing matched "
                              "your filters.)", st.DIM))
        return "\n".join(lines)

    header = (f"  {'#':<4}{'$/TB':>9}{'Price':>11}{'Cap':>8}  "
              f"{'Condition':<13}{'Retailer':<16}")
    lines.append(st.paint(header, st.BOLD))
    lines.append("  " + st.paint("─" * 70, st.DIM))
    for i, d in enumerate(result.deals, 1):
        row = (f"  {('#' + str(i)):<4}"
               + st.paint(f"{fmt_money(d.price_per_tb):>9}", st.GREEN, st.BOLD)
               + f"{fmt_money(d.price):>11}{fmt_tb(d.capacity_tb):>8}  "
               + f"{d.condition:<13}{d.retailer:<16}")
        lines.append(row)
        lines.append("      " + st.paint(st.link(_truncate(d.title, 60), d.url), st.DIM))
    lines.append("")
    lines.append(st.paint("  Prices move constantly — verify on the retailer's site "
                          "before buying.", st.DIM))
    return "\n".join(lines)


def render_deals_footer(size_tb: float, st: Style, timeout: int = 12,
                        n: int = 3) -> str:
    """Short 'top deals near <size>' footer for a normal plan (opt-in)."""
    lo, hi = size_tb * 0.85, size_tb * 1.15
    result = get_deals(min_tb=lo, max_tb=hi, limit=n, timeout=timeout)
    if not result.deals:
        return ""
    lines = [st.paint(f"Top deals near {fmt_tb(size_tb)} (by $/TB)", st.BOLD, st.WHITE)]
    for i, d in enumerate(result.deals, 1):
        lines.append("  "
                     + st.paint(f"#{i} {fmt_money(d.price_per_tb)}/TB", st.GREEN, st.BOLD)
                     + f"  {fmt_tb(d.capacity_tb)}  {fmt_money(d.price)}  "
                     + f"{d.condition}  {d.retailer}")
        lines.append("     " + st.paint(st.link(_truncate(d.title, 56), d.url), st.DIM))
    return "\n".join(lines)


# ===========================================================================
# Dependency help banner.
# ===========================================================================


def dependency_banner(st: Style, have_cloudscraper: bool = False) -> str:
    lines = []
    if have_cloudscraper:
        lines.append(st.paint(
            "⚠ Live pricing unavailable (network/fetch failed). Using baseline.",
            st.YELLOW))
    else:
        lines.append(st.paint(
            "⚠ Live pricing fell back to the static baseline.", st.YELLOW))
        lines.append("  For the most reliable live pricing, install cloudscraper:")
        lines.append(st.paint("    python3 -m venv ~/raid-env", st.DIM))
        lines.append(st.paint("    source ~/raid-env/bin/activate", st.DIM))
        lines.append(st.paint("    pip install cloudscraper", st.DIM))
    return "\n".join(lines)


# ===========================================================================
# CLI.
# ===========================================================================


EXAMPLES = """\
examples:
  # Plan for 100 TB usable on RAID 6 with 22 TB drives:
  raid_architect.py --size 22TB --level 6 --capacity 100TB

  # Fixed array: 8 x 20TB drives in RAID 10, resulting usable capacity:
  raid_architect.py --size 20 --level 10 --drives 8

  # Derive the level from a desired fault tolerance (2 -> RAID 6):
  raid_architect.py --size 18TB --fault 2 --capacity 60TB

  # RAID 60 across 2 groups with a hot spare, live pricing:
  raid_architect.py --size 22TB --level 60 --capacity 200TB --groups 2 --hot-spare 1 --compare

  # Compare every level for the same target, machine-readable:
  raid_architect.py --size 22TB --capacity 100TB --compare-levels --json

  # Override pricing manually (prices change!):
  raid_architect.py --size 24TB --level 5 --drives 6 --rate-refurb 12.5 --rate-nas 19
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="raid_architect.py",
        description="Plan a RAID array and price the drives across market tiers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    p.add_argument("--version", action="version",
                   version=f"raid_architect.py {__version__}")

    g_layout = p.add_argument_group("array")
    g_layout.add_argument("-s", "--size",
                          help="Per-drive size, e.g. 22TB, 20, 500GB, 2PB. "
                               "(Required for planning; not needed for --deals.)")
    g_layout.add_argument("-l", "--level",
                          help="RAID level: 0, 1, 5, 6, 10, 50, 60.")
    g_layout.add_argument("-f", "--fault", type=int,
                          help="Desired fault tolerance (0->RAID0, 1->RAID5, 2->RAID6) "
                               "when --level is not given.")
    g_layout.add_argument("--groups", type=int,
                          help="Number of parity groups for RAID 50/60.")
    g_layout.add_argument("--group-size", type=int,
                          help="Drives per parity group for RAID 50/60.")
    g_layout.add_argument("--hot-spare", type=int, default=0, metavar="N",
                          help="Reserve N hot-spare drives (cost only, no capacity).")

    g_mode = p.add_argument_group("planning mode (choose one)")
    g_mode.add_argument("-c", "--capacity",
                        help="Target USABLE capacity; recommends a drive count.")
    g_mode.add_argument("-d", "--drives", type=int,
                        help="Fixed array drive count; reports usable capacity.")

    g_price = p.add_argument_group("pricing")
    g_price.add_argument("--compare", action="store_true",
                         help="Deprecated: live pricing is now the default. Kept as a "
                              "no-op for backward compatibility.")
    g_price.add_argument("--historical", "--baseline", action="store_true",
                         help="Force the static baseline (never hit the network). "
                              "By default the tool fetches live pricing.")
    g_price.add_argument("--rate-refurb", type=float, metavar="USD",
                         help="Override Recertified $/TB.")
    g_price.add_argument("--rate-nas", type=float, metavar="USD",
                         help="Override NAS Pro $/TB.")
    g_price.add_argument("--rate-ent", type=float, metavar="USD",
                         help="Override Enterprise $/TB.")
    g_price.add_argument("--price-per-drive", type=float, metavar="USD",
                         help="Known per-drive price (refurb basis); derives tiers.")

    g_out = p.add_argument_group("output")
    g_out.add_argument("--compare-levels", action="store_true",
                       help="Tabulate ALL RAID levels for the same target.")
    g_out.add_argument("--json", action="store_true",
                       help="Emit machine-readable JSON instead of a table.")
    g_out.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                       help="Colour/hyperlink output (default: auto).")
    g_out.add_argument("--timeout", type=int, default=12,
                       help="Live-pricing network timeout in seconds (default: 12).")

    g_deals = p.add_argument_group("best deals")
    g_deals.add_argument("--deals", action="store_true",
                         help="Show the best current hard-drive deals "
                              "(ranked by $/TB) and exit.")
    g_deals.add_argument("--deals-size", metavar="TB",
                         help="Only deals at this exact capacity, e.g. 22TB.")
    g_deals.add_argument("--deals-min", metavar="TB",
                         help="Minimum capacity for deals, e.g. 16TB.")
    g_deals.add_argument("--deals-max", metavar="TB",
                         help="Maximum capacity for deals, e.g. 24TB.")
    g_deals.add_argument("--deals-limit", type=int, default=12, metavar="N",
                         help="Max number of deals to show (default: 12).")
    g_deals.add_argument("--deals-condition", metavar="COND",
                         help="Filter deals by condition: recertified, new, "
                              "nas, enterprise.")
    g_deals.add_argument("--with-deals", action="store_true",
                         help="Append a short 'top deals near <size>' footer to "
                              "a normal (non-JSON) plan.")

    return p


def _run_deals(args, st: Style, stdout) -> int:
    """Handle the ``--deals`` view (ranked best hard-drive deals)."""
    if args.deals_size:
        sz = parse_size(args.deals_size)
        min_tb = max_tb = sz
    else:
        min_tb = parse_size(args.deals_min) if args.deals_min else None
        max_tb = parse_size(args.deals_max) if args.deals_max else None
    result = get_deals(
        min_tb=min_tb, max_tb=max_tb, limit=args.deals_limit,
        condition=args.deals_condition, timeout=args.timeout,
        verbose=not args.json,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2), file=stdout)
    else:
        print(render_deals(result, st), file=stdout)
    return 0


def _select_level(args) -> str:
    if args.level is not None:
        return normalize_level(args.level)
    if args.fault is not None:
        return level_from_fault_tolerance(args.fault)
    return "5"  # sensible default


def run(argv: Optional[list[str]] = None, *,
        stdout=None, stderr=None) -> int:
    """Programmatic entry point. Returns a process exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    force_color = {"auto": None, "always": True, "never": False}[args.color]
    # JSON output is always plain.
    st = resolve_style(stdout, force_color=False if args.json else force_color)

    try:
        # ---- Best deals view (no planning inputs required) ----------------
        if args.deals:
            return _run_deals(args, st, stdout)

        if args.size is None:
            raise RaidPricerError(
                "A per-drive --size is required (e.g. 22TB). "
                "Tip: use --deals to browse the best current drive deals."
            )
        size_tb = parse_size(args.size)

        if args.capacity is None and args.drives is None:
            raise RaidPricerError(
                "Choose a planning mode: --capacity <TB> (target) "
                "or --drives <N> (fixed array)."
            )
        if args.capacity is not None and args.drives is not None:
            raise RaidPricerError("Use either --capacity or --drives, not both.")

        # Live pricing is the default; --historical forces the static baseline.
        want_live = not args.historical
        manual = {"refurb": args.rate_refurb, "nas": args.rate_nas, "ent": args.rate_ent}

        # ---- Compare-levels view ------------------------------------------
        if args.compare_levels:
            if args.capacity is None:
                raise RaidPricerError("--compare-levels requires --capacity <TB>.")
            target = parse_size(args.capacity)
            rates = resolve_rates(size_tb, want_live=want_live, manual=manual,
                                  price_per_drive=args.price_per_drive,
                                  verbose=True)
            rows: list[tuple[RaidLayout, PricingResult]] = []
            for key in sorted(RAID_LEVELS, key=lambda k: int(k)):
                try:
                    n = drives_for_capacity(key, size_tb, target)
                    layout = compute_layout(key, size_tb, n,
                                            hot_spares=args.hot_spare)
                    pricing = price_layout(layout, rates)
                    rows.append((layout, pricing))
                except RaidPricerError:
                    continue  # skip levels that can't satisfy the target
            if not rows:
                raise RaidPricerError("No RAID level could satisfy that target.")
            if args.json:
                payload = {
                    "version": __version__,
                    "target_tb": target,
                    "drive_size_tb": size_tb,
                    "levels": [build_json(l, p) for l, p in rows],
                }
                print(json.dumps(payload, indent=2), file=stdout)
            else:
                print(render_compare(rows, target, st), file=stdout)
            return 0

        # ---- Single-level plan --------------------------------------------
        level_key = _select_level(args)

        if args.capacity is not None:
            target = parse_size(args.capacity)
            n = drives_for_capacity(level_key, size_tb, target,
                                    groups=args.groups, group_size=args.group_size)
        else:
            n = args.drives
            if n is None or n <= 0:
                raise RaidPricerError("--drives must be a positive integer.")

        layout = compute_layout(level_key, size_tb, n,
                                hot_spares=args.hot_spare,
                                groups=args.groups, group_size=args.group_size)
        rates = resolve_rates(size_tb, want_live=want_live, manual=manual,
                              price_per_drive=args.price_per_drive, verbose=True)
        pricing = price_layout(layout, rates)

        if args.json:
            print(json.dumps(build_json(layout, pricing), indent=2), file=stdout)
        else:
            # Only warn when live pricing was wanted but we fell back to baseline.
            if want_live and pricing.rates.source == "baseline":
                print(dependency_banner(st, have_cloudscraper=HAVE_CLOUDSCRAPER),
                      file=stderr)
                print("", file=stdout)
            print(render_report(layout, pricing, st), file=stdout)
            if args.with_deals:
                footer = render_deals_footer(size_tb, st, timeout=args.timeout)
                if footer:
                    print("\n" + footer, file=stdout)
        return 0

    except RaidPricerError as exc:
        msg = f"error: {exc}"
        print(st.paint(msg, st.RED) if st.enabled else msg, file=stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("\nAborted.", file=stderr)
        return 130


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
