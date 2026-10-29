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


def _resolve_rates_from(data: dict, size_tb: float) -> ra.PricingRates:
    """Build pricing rates honouring the UI's source + override inputs."""
    source = str(data.get("pricing_source", "baseline")).lower()
    want_live = source == "live"
    manual = {
        "refurb": _opt_float(data, "rate_refurb"),
        "nas": _opt_float(data, "rate_nas"),
        "ent": _opt_float(data, "rate_ent"),
    }
    price_per_drive = _opt_float(data, "price_per_drive")
    return ra.resolve_rates(
        size_tb,
        want_live=want_live,
        manual=manual,
        price_per_drive=price_per_drive,
        verbose=False,
    )


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
        rates = _resolve_rates_from(data, size_tb)
        pricing = ra.price_layout(layout, rates)

        result = ra.build_json(layout, pricing)
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
