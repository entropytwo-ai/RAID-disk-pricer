"""Test suite for raid_architect.

Runs under both ``pytest`` and the stdlib ``unittest``:

    pytest -q
    python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import raid_architect as ra  # noqa: E402


# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------
class TestParseSize(unittest.TestCase):
    def test_bare_number_is_tb(self):
        self.assertEqual(ra.parse_size("22"), 22.0)

    def test_tb_unit(self):
        self.assertEqual(ra.parse_size("22TB"), 22.0)
        self.assertEqual(ra.parse_size("22 tb"), 22.0)
        self.assertEqual(ra.parse_size("22T"), 22.0)

    def test_gb_unit(self):
        self.assertAlmostEqual(ra.parse_size("500GB"), 0.5)
        self.assertAlmostEqual(ra.parse_size("500 g"), 0.5)

    def test_pb_unit(self):
        self.assertEqual(ra.parse_size("2PB"), 2000.0)

    def test_decimal(self):
        self.assertAlmostEqual(ra.parse_size("1.5TB"), 1.5)

    def test_rejects_garbage(self):
        for bad in ("abc", "", "TB", "-5", "0", "22XB", "1.2.3"):
            with self.assertRaises(ra.RaidPricerError):
                ra.parse_size(bad)


# ---------------------------------------------------------------------------
# Level normalisation & fault-tolerance mapping
# ---------------------------------------------------------------------------
class TestLevelParsing(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(ra.normalize_level("5"), "5")
        self.assertEqual(ra.normalize_level("RAID6"), "6")
        self.assertEqual(ra.normalize_level("raid 10"), "10")

    def test_normalize_bad(self):
        with self.assertRaises(ra.RaidPricerError):
            ra.normalize_level("7")

    def test_fault_mapping(self):
        self.assertEqual(ra.level_from_fault_tolerance(0), "0")
        self.assertEqual(ra.level_from_fault_tolerance(1), "5")
        self.assertEqual(ra.level_from_fault_tolerance(2), "6")

    def test_fault_mapping_bad(self):
        with self.assertRaises(ra.RaidPricerError):
            ra.level_from_fault_tolerance(3)


# ---------------------------------------------------------------------------
# Capacity / parity / fault-tolerance math for every level
# ---------------------------------------------------------------------------
class TestLayoutMath(unittest.TestCase):
    SIZE = 10.0  # 10 TB drives keep the arithmetic obvious

    def test_raid0(self):
        lay = ra.compute_layout("0", self.SIZE, 4)
        self.assertEqual(lay.usable_capacity_tb, 40.0)
        self.assertEqual(lay.fault_tolerance, 0)
        self.assertAlmostEqual(lay.efficiency_pct, 100.0)

    def test_raid1(self):
        lay = ra.compute_layout("1", self.SIZE, 3)
        self.assertEqual(lay.usable_capacity_tb, 10.0)   # one drive's worth
        self.assertEqual(lay.fault_tolerance, 2)         # n-1
        self.assertAlmostEqual(lay.efficiency_pct, 100.0 / 3.0)

    def test_raid5(self):
        lay = ra.compute_layout("5", self.SIZE, 5)
        self.assertEqual(lay.usable_capacity_tb, 40.0)   # (n-1)*s
        self.assertEqual(lay.fault_tolerance, 1)

    def test_raid6(self):
        lay = ra.compute_layout("6", self.SIZE, 6)
        self.assertEqual(lay.usable_capacity_tb, 40.0)   # (n-2)*s
        self.assertEqual(lay.fault_tolerance, 2)

    def test_raid10(self):
        lay = ra.compute_layout("10", self.SIZE, 6)
        self.assertEqual(lay.usable_capacity_tb, 30.0)   # n/2*s
        self.assertEqual(lay.fault_tolerance, 1)
        self.assertAlmostEqual(lay.efficiency_pct, 50.0)

    def test_raid10_requires_even(self):
        with self.assertRaises(ra.RaidPricerError):
            ra.compute_layout("10", self.SIZE, 5)

    def test_raid50(self):
        # 6 drives, 2 groups of 3 -> loses 1 parity per group = 2 parity.
        lay = ra.compute_layout("50", self.SIZE, 6, groups=2)
        self.assertEqual(lay.groups, 2)
        self.assertEqual(lay.usable_capacity_tb, 40.0)   # (6-2)*s
        self.assertEqual(lay.fault_tolerance, 1)

    def test_raid60(self):
        # 8 drives, 2 groups of 4 -> loses 2 parity per group = 4 parity.
        lay = ra.compute_layout("60", self.SIZE, 8, groups=2)
        self.assertEqual(lay.groups, 2)
        self.assertEqual(lay.usable_capacity_tb, 40.0)   # (8-4)*s
        self.assertEqual(lay.fault_tolerance, 2)

    def test_min_drive_enforcement(self):
        for level, n in [("5", 2), ("6", 3), ("10", 2), ("50", 4), ("60", 6)]:
            with self.assertRaises(ra.RaidPricerError, msg=f"{level}/{n}"):
                ra.compute_layout(level, self.SIZE, n)

    def test_hot_spares_add_to_total_not_usable(self):
        lay = ra.compute_layout("6", self.SIZE, 6, hot_spares=2)
        self.assertEqual(lay.usable_capacity_tb, 40.0)
        self.assertEqual(lay.total_drives, 8)
        self.assertEqual(lay.hot_spares, 2)

    def test_group_size_validation(self):
        with self.assertRaises(ra.RaidPricerError):
            ra.compute_layout("50", self.SIZE, 7, group_size=3)  # 7 not divisible
        with self.assertRaises(ra.RaidPricerError):
            ra.compute_layout("60", self.SIZE, 8, group_size=3)  # group < 4


# ---------------------------------------------------------------------------
# Drive-count recommendation (capacity mode)
# ---------------------------------------------------------------------------
class TestDrivesForCapacity(unittest.TestCase):
    def test_raid0(self):
        self.assertEqual(ra.drives_for_capacity("0", 10.0, 100.0), 10)

    def test_raid5(self):
        self.assertEqual(ra.drives_for_capacity("5", 10.0, 100.0), 11)

    def test_raid6(self):
        self.assertEqual(ra.drives_for_capacity("6", 10.0, 100.0), 12)

    def test_raid10_even(self):
        n = ra.drives_for_capacity("10", 10.0, 100.0)
        self.assertEqual(n, 20)
        self.assertEqual(n % 2, 0)

    def test_round_trip_meets_target(self):
        # Whatever the recommendation, the resulting usable must meet the target.
        for level in ("0", "5", "6", "10", "50", "60"):
            n = ra.drives_for_capacity(level, 22.0, 100.0)
            lay = ra.compute_layout(level, 22.0, n)
            self.assertGreaterEqual(
                lay.usable_capacity_tb, 100.0 - 1e-9,
                msg=f"{level} gave {lay.usable_capacity_tb}",
            )

    def test_raid1_capacity_bounds(self):
        self.assertEqual(ra.drives_for_capacity("1", 10.0, 10.0), 2)
        with self.assertRaises(ra.RaidPricerError):
            ra.drives_for_capacity("1", 10.0, 100.0)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
class TestPricing(unittest.TestCase):
    def test_baseline_when_no_live(self):
        rates = ra.resolve_rates(10.0, want_live=False, manual={})
        self.assertEqual(rates.source, "baseline")
        self.assertEqual(rates.refurb, ra.HISTORICAL_RATES["Refurb"])

    def test_manual_override(self):
        rates = ra.resolve_rates(
            10.0, want_live=False,
            manual={"refurb": 12.0, "nas": None, "ent": None},
        )
        self.assertEqual(rates.refurb, 12.0)
        self.assertIn("manual", rates.source)

    def test_price_per_drive(self):
        rates = ra.resolve_rates(
            10.0, want_live=False, manual={}, price_per_drive=100.0,
        )
        self.assertEqual(rates.refurb, 10.0)  # 100/10
        self.assertAlmostEqual(rates.nas, 10.0 * ra.MARKUP_FROM_REFURB["NAS"])

    def test_price_layout_uses_total_drives(self):
        lay = ra.compute_layout("6", 10.0, 6, hot_spares=1)  # 7 total
        rates = ra.resolve_rates(10.0, want_live=False, manual={})
        pricing = ra.price_layout(lay, rates)
        refurb = next(q for q in pricing.quotes if q.tier == "Refurb")
        self.assertAlmostEqual(refurb.total, 7 * 10.0 * ra.HISTORICAL_RATES["Refurb"])

    def test_extract_price_median(self):
        data = {"products": [
            {"title": "WD 20TB Recert", "variants": [{"price": "200.00"}]},
            {"title": "Seagate 20TB", "variants": [{"price": "300.00"}]},
            {"title": "HGST 20 TB", "variants": [{"price": "250.00"}]},
            {"title": "Toshiba 18TB", "variants": [{"price": "100.00"}]},  # wrong size
        ]}
        self.assertEqual(ra._extract_price_for_size(data, 20.0), 250.0)

    def test_extract_price_no_match(self):
        data = {"products": [{"title": "WD 8TB", "variants": [{"price": "80"}]}]}
        self.assertIsNone(ra._extract_price_for_size(data, 20.0))

    def test_rejects_negative_rate(self):
        with self.assertRaises(ra.RaidPricerError):
            ra.resolve_rates(10.0, want_live=False,
                             manual={"refurb": -1.0, "nas": None, "ent": None})


# ---------------------------------------------------------------------------
# End-to-end CLI (run) — exercises arg handling & output
# ---------------------------------------------------------------------------
class TestCLI(unittest.TestCase):
    def _run(self, args):
        out, err = io.StringIO(), io.StringIO()
        code = ra.run(args, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_json_mode(self):
        code, out, _ = self._run(
            ["--size", "22TB", "--level", "6", "--drives", "7", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["layout"]["usable_capacity_tb"], 110.0)

    def test_capacity_mode(self):
        code, out, _ = self._run(
            ["--size", "10", "--level", "5", "--capacity", "100", "--color", "never"])
        self.assertEqual(code, 0)
        self.assertIn("RAID 5", out)

    def test_requires_mode(self):
        code, _, err = self._run(["--size", "10", "--level", "5"])
        self.assertEqual(code, 2)
        self.assertIn("planning mode", err)

    def test_both_modes_rejected(self):
        code, _, err = self._run(
            ["--size", "10", "--level", "5", "--capacity", "100", "--drives", "5"])
        self.assertEqual(code, 2)

    def test_compare_levels_json(self):
        code, out, _ = self._run(
            ["--size", "22TB", "--capacity", "100TB", "--compare-levels", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertGreater(len(payload["levels"]), 1)

    def test_bad_input_no_traceback(self):
        code, _, err = self._run(["--size", "nonsense", "--level", "5", "--drives", "4"])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_fault_derives_level(self):
        code, out, _ = self._run(
            ["--size", "10", "--fault", "2", "--drives", "6", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["layout"]["level"], "6")


# ---------------------------------------------------------------------------
# Web app: baseline-vs-live comparison block (build_comparison + /api/plan)
# ---------------------------------------------------------------------------
import webapp  # noqa: E402


class TestWebComparison(unittest.TestCase):
    """Exercise the web layer's baseline-vs-live comparison."""

    # A deterministic SPD-style catalog: 22TB recert priced so per-TB == 20.0.
    LIVE_FIXTURE = {"products": [
        {"title": "WD 22TB Recertified", "variants": [{"price": "440.00"}]},
    ]}

    def setUp(self):
        self.client = webapp.app.test_client()
        # Clear the cross-run live-price cache so each test is deterministic.
        ra._LIVE_CACHE.clear()
        self._orig_fetch = ra._fetch_products_json

    def tearDown(self):
        ra._fetch_products_json = self._orig_fetch
        ra._LIVE_CACHE.clear()

    def _layout(self):
        # RAID 6, 22TB drives, 340TB target — the task's sample plan.
        n = ra.drives_for_capacity("6", 22.0, 340.0)
        return ra.compute_layout("6", 22.0, n)

    # -- build_comparison (pure) -------------------------------------------

    def test_live_success_has_both_columns_and_deltas(self):
        ra._fetch_products_json = lambda *a, **k: self.LIVE_FIXTURE
        layout = self._layout()
        cmp, live_pricing = webapp.build_comparison(
            layout, 22.0, {"refurb": None, "nas": None, "ent": None}, None
        )
        self.assertTrue(cmp["live_succeeded"])
        self.assertFalse(cmp["live_has_manual_override"])
        self.assertFalse(cmp["live_equals_baseline"])
        self.assertEqual(cmp["tier_order"], ["Refurb", "NAS", "Ent"])
        # Baseline refurb stays the historical reference; live reflects fixture.
        self.assertEqual(cmp["baseline"]["rates_per_tb"]["Refurb"],
                         ra.HISTORICAL_RATES["Refurb"])
        self.assertAlmostEqual(cmp["live"]["rates_per_tb"]["Refurb"], 20.0)
        # Every tier carries baseline, live, and a delta == live - baseline.
        for t in cmp["tiers"]:
            for metric in ("per_tb", "per_drive", "total"):
                self.assertAlmostEqual(
                    t["delta"][metric], t["live"][metric] - t["baseline"][metric]
                )
            b_total = t["baseline"]["total"]
            expect_pct = (t["live"]["total"] - b_total) / b_total * 100.0
            self.assertAlmostEqual(t["delta"]["pct"], expect_pct)

    def test_fallback_when_live_fetch_fails(self):
        ra._fetch_products_json = lambda *a, **k: None
        layout = self._layout()
        cmp, _ = webapp.build_comparison(
            layout, 22.0, {"refurb": None, "nas": None, "ent": None}, None
        )
        self.assertFalse(cmp["live_succeeded"])
        self.assertTrue(cmp["live_equals_baseline"])
        for t in cmp["tiers"]:
            self.assertAlmostEqual(t["delta"]["total"], 0.0)
            self.assertAlmostEqual(t["baseline"]["total"], t["live"]["total"])

    def test_manual_override_flows_to_live_only(self):
        ra._fetch_products_json = lambda *a, **k: self.LIVE_FIXTURE
        layout = self._layout()
        cmp, _ = webapp.build_comparison(
            layout, 22.0, {"refurb": 12.5, "nas": None, "ent": None}, None
        )
        self.assertTrue(cmp["live_has_manual_override"])
        # Baseline column untouched by the override; live column honours it.
        self.assertEqual(cmp["baseline"]["rates_per_tb"]["Refurb"],
                         ra.HISTORICAL_RATES["Refurb"])
        self.assertAlmostEqual(cmp["live"]["rates_per_tb"]["Refurb"], 12.5)

    # -- /api/plan (full endpoint) -----------------------------------------

    def test_api_plan_includes_comparison(self):
        ra._fetch_products_json = lambda *a, **k: self.LIVE_FIXTURE
        resp = self.client.post("/api/plan", json={
            "capacity": "340TB", "size": "22TB", "level": "6",
        })
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("comparison", payload)
        self.assertIn("pricing", payload)  # primary field preserved
        cmp = payload["comparison"]
        self.assertTrue(cmp["live_succeeded"])
        self.assertEqual(len(cmp["tiers"]), 3)
        self.assertIn("baseline", cmp)
        self.assertIn("live", cmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
