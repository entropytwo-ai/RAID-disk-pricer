#!/usr/bin/env python3
"""
webapp.py — Flask web UI for RAID Architect.

A thin HTTP layer over the existing ``raid_architect`` module. ALL RAID maths and
pricing live in ``raid_architect.py`` — this file only parses JSON requests,
calls the library functions, and returns structured JSON. There is exactly one
source of truth for the calculations.

Endpoints
---------
GET  /                 Single-page UI.
GET  /api/levels       Static metadata about the supported RAID levels & tiers.
POST /api/plan         Plan a single RAID level (layout + pricing).
POST /api/compare      Compare every applicable RAID level for a target.
GET  /healthz          Liveness probe.

Run locally:
    /opt/abacus-python/bin/python3 webapp.py           # dev server on :8137
Served in production behind nginx + systemd (see deploy/).
"""

from __future__ import annotations

import os
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory

import raid_architect as ra

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------

def _get_payload() -> dict:
    """Return the request body as a dict (JSON or form-encoded)."""
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict() if request.form else {}


def _opt_float(data: dict, key: str) -> Optional[float]:
    val = data.get(key)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        raise ra.RaidPricerError(f"'{key}' must be a number.")


def _opt_int(data: dict, key: str) -> Optional[int]:
    val = data.get(key)
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ra.RaidPricerError(f"'{key}' must be an integer.")


def _select_level(data: dict) -> str:
    """Mirror the CLI's level selection: explicit level, else fault, else RAID 5."""
    level = data.get("level")
    if level not in (None, ""):
        return ra.normalize_level(str(level))
    fault = _opt_int(data, "fault")
    if fault is not None:
        return ra.level_from_fault_tolerance(fault)
    return "5"


def _manual_from(data: dict) -> dict:
    """Extract per-tier manual $/TB overrides from the request."""
    return {
        "refurb": _opt_float(data, "rate_refurb"),
        "nas": _opt_float(data, "rate_nas"),
        "ent": _opt_float(data, "rate_ent"),
    }


def _resolve_rates_from(data: dict, size_tb: float) -> ra.PricingRates:
    """Build pricing rates honouring the UI's source + override inputs."""
    source = str(data.get("pricing_source", "live")).lower()
    want_live = source == "live"
    price_per_drive = _opt_float(data, "price_per_drive")
    return ra.resolve_rates(
        size_tb,
        want_live=want_live,
        manual=_manual_from(data),
        price_per_drive=price_per_drive,
        verbose=False,
    )


def _rates_block(rates: ra.PricingRates) -> dict:
    """Serialize a PricingRates into a small JSON block."""
    return {
        "source": rates.source,
        "note": rates.note,
        "rates_per_tb": {
            "Refurb": rates.refurb,
            "NAS": rates.nas,
            "Ent": rates.ent,
        },
    }


def build_comparison(layout, size_tb: float, manual: dict,
                     price_per_drive: Optional[float]):
    """Price *layout* under BOTH the static baseline and live pricing.

    Returns ``(comparison_dict, live_pricing)``:

    * ``comparison_dict`` — baseline block, live block, per-tier
      baseline/live/delta numbers, and flags telling the UI whether the live
      fetch actually succeeded or silently fell back to the baseline.
    * ``live_pricing`` — the live :class:`PricingResult` (what the user would
      pay), reused as the plan's primary ``pricing`` field for compatibility.

    The baseline column is the pure early-2026 reference
    (:data:`raid_architect.HISTORICAL_RATES`) with no overrides, so the delta is
    always measured against the same yardstick. Manual rate / per-drive
    overrides are applied on top of the LIVE column only, mirroring the CLI.
    """
    # Baseline reference: never live, never overridden.
    baseline_rates = ra.resolve_rates(
        size_tb, want_live=False, manual={}, price_per_drive=None, verbose=False
    )
    baseline_pricing = ra.price_layout(layout, baseline_rates)

    # Probe whether live pricing is actually reachable (cached within the run,
    # so this does not add a second network round-trip for the resolve below).
    live_succeeded = ra.get_live_per_tb(size_tb) is not None

    # Live column honours manual / per-drive overrides (auto-falls back to
    # baseline inside resolve_rates when the fetch fails).
    live_rates = ra.resolve_rates(
        size_tb, want_live=True, manual=manual,
        price_per_drive=price_per_drive, verbose=False,
    )
    live_pricing = ra.price_layout(layout, live_rates)

    has_manual = any(v is not None for v in manual.values()) or price_per_drive is not None

    bq = {q.tier: q for q in baseline_pricing.quotes}
    lq = {q.tier: q for q in live_pricing.quotes}
    tiers = []
    for tier in ra.TIER_ORDER:
        b, l = bq[tier], lq[tier]
        pct = ((l.total - b.total) / b.total * 100.0) if b.total else 0.0
        tiers.append({
            "tier": tier,
            "label": ra.TIER_LABELS[tier],
            "baseline": {"per_tb": b.per_tb, "per_drive": b.per_drive, "total": b.total},
            "live": {"per_tb": l.per_tb, "per_drive": l.per_drive, "total": l.total},
            "delta": {
                "per_tb": l.per_tb - b.per_tb,
                "per_drive": l.per_drive - b.per_drive,
                "total": l.total - b.total,
                "pct": pct,
            },
        })

    comparison = {
        "baseline": _rates_block(baseline_rates),
        "live": _rates_block(live_rates),
        "live_succeeded": live_succeeded,
        "live_has_manual_override": has_manual,
        # Columns are identical only when live failed AND nothing was overridden.
        "live_equals_baseline": (not live_succeeded) and (not has_manual),
        "tier_order": list(ra.TIER_ORDER),
        "tiers": tiers,
    }
    return comparison, live_pricing


# ---------------------------------------------------------------------------
# Static file / page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": ra.__version__})


@app.route("/api/levels")
def api_levels():
    """Expose level metadata + tier labels/links so the UI stays in sync."""
    levels = []
    for key in sorted(ra.RAID_LEVELS, key=lambda k: int(k)):
        lvl = ra.RAID_LEVELS[key]
        levels.append({
            "key": lvl.key,
            "name": lvl.name,
            "min_drives": lvl.min_drives,
            "grouped": lvl.grouped,
            "summary": lvl.summary,
        })
    return jsonify({
        "version": ra.__version__,
        "levels": levels,
        "tier_order": list(ra.TIER_ORDER),
        "tier_labels": ra.TIER_LABELS,
        "market_links": {
            tier: [{"label": lbl, "url": url} for lbl, url in links]
            for tier, links in ra.MARKET_LINKS.items()
        },
        "baseline_rates": ra.HISTORICAL_RATES,
    })


# ---------------------------------------------------------------------------
# Computation endpoints
# ---------------------------------------------------------------------------

@app.route("/api/plan", methods=["POST"])
def api_plan():
    """Plan a single RAID level. Reuses raid_architect computations verbatim."""
    try:
        data = _get_payload()

        size_raw = data.get("size")
        if size_raw in (None, ""):
            raise ra.RaidPricerError("A per-drive size is required (e.g. 22TB).")
        size_tb = ra.parse_size(str(size_raw))

        capacity_raw = data.get("capacity")
        drives = _opt_int(data, "drives")
        has_capacity = capacity_raw not in (None, "")
        has_drives = drives is not None

        if has_capacity and has_drives:
            raise ra.RaidPricerError(
                "Choose one planning mode: a target capacity OR a fixed drive count."
            )
        if not has_capacity and not has_drives:
            raise ra.RaidPricerError(
                "Provide a target usable capacity or a fixed drive count."
            )

        level_key = _select_level(data)
        hot_spares = _opt_int(data, "hot_spare") or 0
        groups = _opt_int(data, "groups")
        group_size = _opt_int(data, "group_size")

        if has_capacity:
            target = ra.parse_size(str(capacity_raw))
            n = ra.drives_for_capacity(
                level_key, size_tb, target, groups=groups, group_size=group_size
            )
        else:
            if drives <= 0:
                raise ra.RaidPricerError("Drive count must be a positive integer.")
            n = drives

        layout = ra.compute_layout(
            level_key, size_tb, n,
            hot_spares=hot_spares, groups=groups, group_size=group_size,
        )

        # Price the SAME layout under both the early-2026 baseline and live
        # pricing, with per-tier deltas. The live set (what the user pays, incl.
        # any overrides) stays as the primary `pricing` field for compatibility.
        manual = _manual_from(data)
        price_per_drive = _opt_float(data, "price_per_drive")
        comparison, live_pricing = build_comparison(
            layout, size_tb, manual, price_per_drive
        )

        result = ra.build_json(layout, live_pricing)
        result["comparison"] = comparison
        result["market_links"] = {
            tier: [{"label": lbl, "url": url} for lbl, url in links]
            for tier, links in ra.MARKET_LINKS.items()
        }
        result["tier_order"] = list(ra.TIER_ORDER)
        return jsonify(result)

    except ra.RaidPricerError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


@app.route("/api/compare", methods=["POST"])
def api_compare():
    """Compare every applicable RAID level for a target usable capacity."""
    try:
        data = _get_payload()

        size_raw = data.get("size")
        if size_raw in (None, ""):
            raise ra.RaidPricerError("A per-drive size is required (e.g. 22TB).")
        size_tb = ra.parse_size(str(size_raw))

        capacity_raw = data.get("capacity")
        if capacity_raw in (None, ""):
            raise ra.RaidPricerError("Comparison requires a target usable capacity.")
        target = ra.parse_size(str(capacity_raw))

        hot_spares = _opt_int(data, "hot_spare") or 0
        rates = _resolve_rates_from(data, size_tb)

        rows = []
        for key in sorted(ra.RAID_LEVELS, key=lambda k: int(k)):
            try:
                n = ra.drives_for_capacity(key, size_tb, target)
                layout = ra.compute_layout(key, size_tb, n, hot_spares=hot_spares)
                pricing = ra.price_layout(layout, rates)
                rows.append(ra.build_json(layout, pricing))
            except ra.RaidPricerError:
                continue  # skip levels that cannot satisfy the target

        if not rows:
            raise ra.RaidPricerError("No RAID level could satisfy that target.")

        return jsonify({
            "version": ra.__version__,
            "target_tb": target,
            "drive_size_tb": size_tb,
            "pricing_source": rows[0]["pricing"]["source"],
            "pricing_note": rows[0]["pricing"]["note"],
            "levels": rows,
        })

    except ra.RaidPricerError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("RAID_WEB_PORT", "8137"))
    app.run(host="127.0.0.1", port=port, debug=False)
