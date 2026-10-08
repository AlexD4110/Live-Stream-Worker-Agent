"""The dashboard's JavaScript must agree with the Python metrics, and its rules must find what was planted."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections import defaultdict

from gifting import generate, metrics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
END = generate.DAYS - 1


def run_node(data_js, flt=None):
    args = ["node", os.path.join(ROOT, "tests", "node_runner.js"), data_js] + ([json.dumps(flt)] if flt else [])
    return json.loads(subprocess.run(args, capture_output=True, text=True, check=True, cwd=ROOT).stdout)


@unittest.skipUnless(shutil.which("node"), "node is only needed to run these checks, not to use the dashboard")
class DashboardParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t, cls.truth = generate.build(seed=7)
        cls.dir = tempfile.mkdtemp()
        cls.data_js = os.path.join(cls.dir, "data.js")
        generate.write_dashboard_data(cls.t, cls.data_js)
        cls.out = run_node(cls.data_js)
        cls.s, cls.f = cls.out["summary"], {x["id"]: x for x in cls.out["findings"]}
        cls.gifts = cls.t["gifts"]
        cls.ring_g = set(cls.truth["ring_gifters"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    # --- the numbers match the Python reference ---------------------------------
    def test_total_coins(self):
        self.assertEqual(self.s["coins"], sum(g["coins"] for g in self.gifts))

    def test_unique_gifters_and_rows(self):
        self.assertEqual(self.s["gifters"], len({g["gifter_id"] for g in self.gifts}))
        self.assertEqual(self.s["giftRows"], len(self.gifts))

    def test_retention_matches_python_for_new_gifters(self):
        new = {x["gifter_id"] for x in self.t["gifters"] if x["signup_day"] >= 0}
        gifts = [g for g in self.gifts if g["gifter_id"] in new]
        self.assertAlmostEqual(self.s["ret7"], metrics.retention(gifts, 7, END), places=9)
        self.assertAlmostEqual(self.s["ret30"], metrics.retention(gifts, 30, END), places=9)

    def test_top_one_percent_share(self):
        self.assertAlmostEqual(self.s["top1"], metrics.top_share(self.gifts, 0.01), places=9)

    def test_week_over_week(self):
        daily = metrics.daily_sum(self.gifts, "coins")
        self.assertAlmostEqual(self.s["wow"], metrics.wow_change(daily, END), places=9)

    def test_decomposition_multiplies_back_to_the_change(self):
        d = self.s["decomp"]
        product = 1
        for k in ("viewers", "sendRate", "giftsPerGifter", "coinsPerGift"):
            product *= 1 + d[k]
        self.assertAlmostEqual(product - 1, self.s["wow"], places=9)

    def test_region_filter(self):
        region = {g["gifter_id"]: g["region"] for g in self.t["gifters"]}
        want = sum(g["coins"] for g in self.gifts if region[g["gifter_id"]] == "US-East")
        got = run_node(self.data_js, {"region": "US-East"})["summary"]["coins"]
        self.assertEqual(got, want)

    def test_platform_filter(self):
        plat = {g["gifter_id"]: g["platform"] for g in self.t["gifters"]}
        want = sum(g["coins"] for g in self.gifts if plat[g["gifter_id"]] == "ios")
        self.assertEqual(run_node(self.data_js, {"platform": "ios"})["summary"]["coins"], want)

    def test_platform_filter_does_not_invent_viewer_numbers(self):
        s = run_node(self.data_js, {"platform": "android"})["summary"]
        self.assertIsNone(s["sendRate"])
        d = s["decomp"]
        self.assertNotIn("sendRate", d)
        product = 1
        for k in ("gifters", "giftsPerGifter", "coinsPerGift"):
            product *= 1 + d[k]
        self.assertAlmostEqual(product - 1, s["wow"], places=9)

    def test_campaign_lift_excludes_the_detected_ring(self):
        c = self.t["campaigns"][0]
        s, e = c["start_day"], c["end_day"]
        tr, ho = c["treatment_regions"].split("|"), c["holdout_regions"].split("|")
        region = {g["gifter_id"]: g["region"] for g in self.t["gifters"]}

        def coins(rs, lo, hi):
            return sum(g["coins"] for g in self.gifts if lo <= g["day"] <= hi
                       and region[g["gifter_id"]] in rs and g["gifter_id"] not in self.ring_g)
        tp, hp = coins(tr, s - 14, s - 1) / 2, coins(ho, s - 14, s - 1) / 2
        self.assertAlmostEqual(self.s["campaign"]["rawLift"], coins(tr, s, e) / tp - 1, places=9)
        self.assertAlmostEqual(self.s["campaign"]["trueLift"],
                               metrics.did_lift(tp, coins(tr, s, e), hp, coins(ho, s, e)), places=9)
        self.assertAlmostEqual(self.s["campaign"]["afterLift"],
                               metrics.did_lift(tp, coins(tr, e + 1, e + 7), hp, coins(ho, e + 1, e + 7)), places=9)


@unittest.skipUnless(shutil.which("node"), "node is only needed to run these checks")
class DashboardFindingsTest(DashboardParityTest):
    """Reuses the generated data; each rule must fire on its planted pattern and nothing is invented."""

    def test_every_planted_pattern_is_found(self):
        for key in ("payments_incident", "gift_ring", "early_churn", "whale_concentration", "campaign_overstated",
                    "creator_churn", "minors_gifting", "prize_issues"):
            self.assertIn(key, self.f, f"rule {key} did not fire")

    def test_every_finding_has_evidence_an_action_and_a_guardrail(self):
        for f in self.out["findings"]:
            for k in ("title", "severity", "evidence", "actions", "guardrail"):
                self.assertTrue(f[k], f"{f['id']} is missing {k}")
            self.assertIn(f["severity"], ("high", "medium", "info"))

    def test_findings_are_ranked_most_severe_first(self):
        order = {"high": 0, "medium": 1, "info": 2}
        ranks = [order[f["severity"]] for f in self.out["findings"]]
        self.assertEqual(ranks, sorted(ranks))

    def test_ring_found_exactly(self):
        accounts = set(self.f["gift_ring"]["accounts"])
        self.assertEqual(accounts, self.ring_g)

    def test_payments_incident_names_the_segment_and_version(self):
        p = self.f["payments_incident"]
        text = " ".join(p["evidence"])
        self.assertIn("Android", text)
        self.assertIn("US-East", text)
        self.assertIn(generate.BAD_VERSION, text)
        self.assertEqual(p["severity"], "high")

    def test_campaign_finding_reports_both_lifts(self):
        text = " ".join(self.f["campaign_overstated"]["evidence"])
        self.assertIn("holdout", text)
        self.assertIn("%", text)

    def test_prize_finding_flags_the_fraud_winner(self):
        self.assertTrue(self.f["prize_issues"]["suspectWinners"] >= 1)

    def test_no_finding_makes_a_recommendation_without_evidence(self):
        for f in self.out["findings"]:
            self.assertGreaterEqual(len(f["evidence"]), 1)
            self.assertGreaterEqual(len(f["actions"]), 1)

    def test_finding_text_never_states_unofficial_figures_as_policy(self):
        banned = ("50% take", "official rate", "TikTok pays", "adjudicated")
        blob = json.dumps(self.out["findings"])
        for b in banned:
            self.assertNotIn(b, blob)


if __name__ == "__main__":
    unittest.main()
