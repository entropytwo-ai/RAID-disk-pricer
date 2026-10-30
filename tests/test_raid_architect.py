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


class TestDealsParsing(unittest.TestCase):
    """Pure helpers: capacity parsing, condition inference, multipack skip."""

    def test_parse_capacity_varied(self):
        self.assertEqual(ra.parse_capacity_tb("Seagate 22TB Exos"), 22.0)
        self.assertEqual(ra.parse_capacity_tb("WD 22 TB Recertified"), 22.0)
        self.assertEqual(ra.parse_capacity_tb("Toshiba 20tb NAS"), 20.0)
        self.assertEqual(ra.parse_capacity_tb("HGST 18.0 TB"), 18.0)

    def test_parse_capacity_rejects(self):
        self.assertIsNone(ra.parse_capacity_tb("Seagate 512GB SSD"))
        self.assertIsNone(ra.parse_capacity_tb("No capacity here"))
        self.assertIsNone(ra.parse_capacity_tb(""))
        self.assertIsNone(ra.parse_capacity_tb("Bogus 250TB bundle"))  # >100 rejected

    def test_infer_condition(self):
        self.assertEqual(ra.infer_condition("WD 22TB Recertified"), "Recertified")
        self.assertEqual(ra.infer_condition("WD 22TB Renewed"), "Recertified")
        self.assertEqual(ra.infer_condition("WD 22TB Refurbished"), "Recertified")
        self.assertEqual(ra.infer_condition("Seagate Exos 20TB Enterprise"), "Enterprise")
        self.assertEqual(ra.infer_condition("WD Red 18TB NAS"), "NAS")
        self.assertEqual(ra.infer_condition("Seagate 16TB New"), "New")
        self.assertEqual(ra.infer_condition("Seagate 16TB", default="Recertified"),
                         "Recertified")
        self.assertEqual(ra.infer_condition("Seagate 16TB"), "Unknown")

    def test_is_multipack(self):
        self.assertTrue(ra._is_multipack("Seagate 8TB 2-pack"))
        self.assertTrue(ra._is_multipack("WD 10TB bundle"))
        self.assertTrue(ra._is_multipack("HGST 12TB 2x drives"))
        self.assertFalse(ra._is_multipack("Seagate 22TB Exos"))

    def test_product_price_cheapest_available(self):
        prod = {"variants": [
            {"price": "330.00", "available": True},
            {"price": "320.00", "available": True},
            {"price": "300.00", "available": False},
        ]}
        self.assertAlmostEqual(ra._product_price(prod), 320.0)

    def test_product_price_none_when_out_of_stock(self):
        # Every variant is out of stock -> the listing must be dropped (None),
        # never surfaced at an out-of-stock price.
        prod = {"variants": [{"price": "300.00", "available": False}]}
        self.assertIsNone(ra._product_price(prod))

    def test_product_price_ignores_cheaper_out_of_stock(self):
        # A cheaper but out-of-stock variant must not win over an in-stock one.
        prod = {"variants": [
            {"price": "250.00", "available": False},
            {"price": "320.00", "available": True},
        ]}
        self.assertAlmostEqual(ra._product_price(prod), 320.0)

    def test_product_price_none_when_no_price(self):
        self.assertIsNone(ra._product_price({"variants": [{"price": "0"}]}))
        self.assertIsNone(ra._product_price({"variants": []}))


class TestDeals(unittest.TestCase):
    """Deals ranking, filtering, dedupe and graceful-empty via mocked network."""

    # Per-source Shopify-style fixtures keyed by a substring of the feed URL.
    SPD = {"products": [
        {"title": "Seagate 14TB Recertified", "handle": "spd-14",
         "variants": [{"price": "140.00", "available": True}]},   # $10.00/TB
        {"title": "WD 22TB Recertified", "handle": "spd-22",
         "variants": [{"price": "300.00", "available": True}]},   # $13.64/TB
        {"title": "Seagate 8TB 2-pack Recertified", "handle": "spd-8x2",
         "variants": [{"price": "120.00", "available": True}]},   # multipack -> skip
        {"title": "Samsung 512GB SSD", "handle": "spd-ssd",
         "variants": [{"price": "40.00", "available": True}]},    # no TB -> skip
    ]}
    GHD = {"products": [
        {"title": "HGST 18TB Enterprise", "handle": "ghd-18",
         "variants": [{"price": "279.00", "available": True}]},   # $15.50/TB
        {"title": "WD 22TB New", "handle": "ghd-22",
         "variants": [{"price": "330.00", "available": False},
                      {"price": "320.00", "available": True}]},   # $14.55/TB
    ]}

    def _fake_fetch(self, url, timeout=12):
        if "serverpartdeals" in url:
            return self.SPD
        if "waterpanther" in url:
            return self.GHD
        return None

    def setUp(self):
        ra.reset_deals_cache()
        self._orig = ra._fetch_products_json
        ra._fetch_products_json = self._fake_fetch

    def tearDown(self):
        ra._fetch_products_json = self._orig
        ra.reset_deals_cache()

    def test_ranking_ascending_by_price_per_tb(self):
        result = ra.get_deals()
        ppt = [round(d.price_per_tb, 2) for d in result.deals]
        self.assertEqual(ppt, sorted(ppt))
        # Best deal is the 14TB @ $140 = $10/TB, from ServerPartDeals.
        self.assertEqual(result.deals[0].capacity_tb, 14.0)
        self.assertAlmostEqual(result.deals[0].price_per_tb, 10.0)
        self.assertEqual(result.deals[0].retailer, "ServerPartDeals")

    def test_multipack_and_non_tb_skipped(self):
        titles = [d.title for d in ra.get_deals().deals]
        self.assertNotIn("Seagate 8TB 2-pack Recertified", titles)
        self.assertNotIn("Samsung 512GB SSD", titles)
        self.assertEqual(len(titles), 4)  # 2 SPD + 2 GHD

    def test_cheapest_available_variant_used(self):
        wd = next(d for d in ra.get_deals().deals if d.title == "WD 22TB New")
        self.assertAlmostEqual(wd.price, 320.0)

    def test_condition_inference_from_source(self):
        by_title = {d.title: d for d in ra.get_deals().deals}
        self.assertEqual(by_title["WD 22TB Recertified"].condition, "Recertified")
        self.assertEqual(by_title["WD 22TB New"].condition, "New")
        self.assertEqual(by_title["HGST 18TB Enterprise"].condition, "Enterprise")

    def test_capacity_filter(self):
        result = ra.get_deals(min_tb=18.0, max_tb=22.0)
        caps = sorted({d.capacity_tb for d in result.deals})
        self.assertEqual(caps, [18.0, 22.0])
        self.assertTrue(all(18.0 <= d.capacity_tb <= 22.0 for d in result.deals))

    def test_condition_filter(self):
        result = ra.get_deals(condition="recertified")
        self.assertTrue(result.deals)
        self.assertTrue(all(d.condition == "Recertified" for d in result.deals))

    def test_limit(self):
        self.assertEqual(len(ra.get_deals(limit=2).deals), 2)

    def test_dedupe(self):
        # Two identical SPD listings collapse to one.
        dup = {"products": self.SPD["products"] + [self.SPD["products"][0]]}
        ra._fetch_products_json = lambda url, timeout=12: (
            dup if "serverpartdeals" in url else self.GHD)
        ra.reset_deals_cache()
        titles = [d.title for d in ra.get_deals().deals]
        self.assertEqual(titles.count("Seagate 14TB Recertified"), 1)

    def test_out_of_stock_products_excluded(self):
        # A feed where the whole catalog is sold out -> zero deals surfaced.
        oos = {"products": [
            {"title": "Seagate 16TB Recertified", "handle": "oos-16",
             "variants": [{"price": "150.00", "available": False}]},
            {"title": "WD 20TB Recertified", "handle": "oos-20",
             "variants": [{"price": "200.00", "available": False},
                          {"price": "190.00", "available": False}]},
        ]}
        ra._fetch_products_json = lambda url, timeout=12: (
            oos if "serverpartdeals" in url else None)
        ra.reset_deals_cache()
        result = ra.get_deals()
        self.assertEqual(result.deals, [])
        # The source still fetched fine; it just had nothing in stock.
        self.assertTrue({s.name: s for s in result.sources}["ServerPartDeals"].ok)

    def test_mixed_stock_keeps_only_in_stock(self):
        mixed = {"products": [
            {"title": "Seagate 16TB Recertified", "handle": "m-16",
             "variants": [{"price": "150.00", "available": False}]},   # OOS -> drop
            {"title": "WD 20TB Recertified", "handle": "m-20",
             "variants": [{"price": "200.00", "available": True}]},    # in stock
        ]}
        ra._fetch_products_json = lambda url, timeout=12: (
            mixed if "serverpartdeals" in url else None)
        ra.reset_deals_cache()
        titles = [d.title for d in ra.get_deals().deals]
        self.assertEqual(titles, ["WD 20TB Recertified"])
        self.assertTrue(all(d.in_stock for d in ra.get_deals().deals))

    def test_pagination_merges_pages(self):
        # Page 1 and page 2 return different in-stock drives; both are merged.
        page1 = {"products": [
            {"title": "Seagate 14TB Recertified", "handle": "p1-14",
             "variants": [{"price": "140.00", "available": True}]}]}
        page2 = {"products": [
            {"title": "Seagate 18TB Recertified", "handle": "p2-18",
             "variants": [{"price": "180.00", "available": True}]}]}

        def paged(url, timeout=12):
            if "serverpartdeals" not in url:
                return None
            return page2 if "page=2" in url else page1
        ra._fetch_products_json = paged
        ra.reset_deals_cache()
        titles = sorted(d.title for d in ra.get_deals().deals)
        self.assertEqual(titles,
                         ["Seagate 14TB Recertified", "Seagate 18TB Recertified"])

    def test_source_status_reported(self):
        result = ra.get_deals()
        status = {s.name: s for s in result.sources}
        self.assertTrue(status["ServerPartDeals"].ok)
        self.assertTrue(status["Water Panther"].ok)
        self.assertEqual(status["ServerPartDeals"].count, 2)

    def test_graceful_empty_when_sources_unavailable(self):
        ra._fetch_products_json = lambda url, timeout=12: None
        ra.reset_deals_cache()
        result = ra.get_deals()
        self.assertEqual(result.deals, [])
        d = result.to_dict()
        self.assertEqual(d["count"], 0)
        self.assertFalse(d["any_source_ok"])
        self.assertTrue(all(not s["ok"] for s in d["sources"]))

    def test_never_raises_on_bad_payload(self):
        ra._fetch_products_json = lambda url, timeout=12: {"garbage": True}
        ra.reset_deals_cache()
        result = ra.get_deals()
        self.assertEqual(result.deals, [])
        self.assertTrue(all(s.ok for s in result.sources))  # dict ok, just no products

    def test_to_dict_shape(self):
        d = ra.get_deals(limit=1).to_dict()
        self.assertEqual(set(d.keys()),
                         {"version", "count", "any_source_ok", "sources", "deals"})
        deal = d["deals"][0]
        self.assertEqual(set(deal.keys()),
                         {"title", "capacity_tb", "price", "price_per_tb",
                          "condition", "retailer", "url", "in_stock"})

    def test_get_best_deals_wrapper(self):
        deals = ra.get_best_deals(limit=3)
        self.assertIsInstance(deals, list)
        self.assertLessEqual(len(deals), 3)
        self.assertIsInstance(deals[0], ra.DealListing)


class TestDealsWeb(unittest.TestCase):
    """/api/deals endpoint over mocked network."""

    def _fake_fetch(self, url, timeout=12):
        if "serverpartdeals" in url:
            return TestDeals.SPD
        if "waterpanther" in url:
            return TestDeals.GHD
        return None

    def setUp(self):
        self.client = webapp.app.test_client()
        ra.reset_deals_cache()
        self._orig = ra._fetch_products_json
        ra._fetch_products_json = self._fake_fetch

    def tearDown(self):
        ra._fetch_products_json = self._orig
        ra.reset_deals_cache()

    def test_api_deals_returns_ranked_json(self):
        resp = self.client.get("/api/deals?limit=5")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("max-age", resp.headers.get("Cache-Control", ""))
        payload = resp.get_json()
        self.assertIn("deals", payload)
        self.assertIn("sources", payload)
        self.assertIn("count", payload)
        self.assertTrue(payload["any_source_ok"])
        ppt = [d["price_per_tb"] for d in payload["deals"]]
        self.assertEqual(ppt, sorted(ppt))

    def test_api_deals_filters(self):
        resp = self.client.get("/api/deals?min=18TB&max=22TB&condition=recertified")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        for d in payload["deals"]:
            self.assertEqual(d["condition"], "Recertified")
            self.assertTrue(18.0 <= d["capacity_tb"] <= 22.0)

    def test_api_deals_graceful_empty(self):
        ra._fetch_products_json = lambda url, timeout=12: None
        ra.reset_deals_cache()
        resp = self.client.get("/api/deals")
        self.assertEqual(resp.status_code, 200)  # not a 500
        payload = resp.get_json()
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["any_source_ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
