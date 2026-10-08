"""The synthetic dataset must contain every pattern the dashboard and agent are meant to find."""
import unittest
from collections import defaultdict

from gifting import generate, metrics

END = generate.DAYS - 1


class GeneratedDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t, cls.truth = generate.build(seed=7)
        cls.gifts = cls.t["gifts"]
        cls.ring_g = set(cls.truth["ring_gifters"])
        cls.ring_c = set(cls.truth["ring_creators"])

    # --- shape -------------------------------------------------------------
    def test_same_seed_same_data(self):
        again, _ = generate.build(seed=7)
        self.assertEqual(len(again["gifts"]), len(self.gifts))
        self.assertEqual(sum(g["coins"] for g in again["gifts"]), sum(g["coins"] for g in self.gifts))

    def test_sizes_fit_a_browser_dashboard(self):
        self.assertEqual(len(self.t["creators"]), 300)
        self.assertGreater(len(self.t["gifters"]), 5000)
        self.assertLess(len(self.gifts), 150_000)

    def test_ground_truth_is_not_in_the_tables(self):
        for name, rows in self.t.items():
            for key in rows[0]:
                self.assertNotIn("ring", key, f"{name}.{key} leaks the answer")

    # --- demand: gifters ---------------------------------------------------
    def test_early_churn_cliff(self):
        # Gifters who started before the window have no observed first gift, so only new ones count.
        new = {x["gifter_id"] for x in self.t["gifters"] if x["signup_day"] >= 0}
        gifts = [g for g in self.gifts if g["gifter_id"] in new]
        self.assertTrue(0.15 <= metrics.retention(gifts, 7, END) <= 0.35)
        self.assertLess(metrics.retention(gifts, 30, END), 0.5)

    def test_gifters_who_started_before_the_window_exist(self):
        self.assertTrue(any(x["signup_day"] < 0 for x in self.t["gifters"]))

    def test_whale_concentration(self):
        self.assertTrue(0.30 <= metrics.top_share(self.gifts, 0.01) <= 0.55)

    def test_lapsed_whales(self):
        by_gifter = defaultdict(int)
        last = {}
        for g in self.gifts:
            by_gifter[g["gifter_id"]] += g["coins"]
            last[g["gifter_id"]] = max(last.get(g["gifter_id"], 0), g["day"])
        top = sorted(by_gifter, key=by_gifter.get, reverse=True)[: len(by_gifter) // 100]
        lapsed = [g for g in top if END - last[g] >= 21 and g not in self.ring_g]
        self.assertGreaterEqual(len(lapsed) / len(top), 0.10)

    def test_weekend_spend_is_higher(self):
        daily = metrics.daily_sum(self.gifts, "coins")
        wkend = [v for d, v in daily.items() if generate.day_date(d).weekday() >= 5]
        wkday = [v for d, v in daily.items() if generate.day_date(d).weekday() < 5]
        self.assertGreater(sum(wkend) / len(wkend), 1.15 * sum(wkday) / len(wkday))

    def test_web_coin_share_grows(self):
        def web_share(lo, hi):
            rows = [p for p in self.t["purchases"] if lo <= p["day"] <= hi and p["status"] == "ok"]
            return sum(p["usd"] for p in rows if p["channel"] == "web") / sum(p["usd"] for p in rows)
        self.assertGreater(web_share(END - 27, END), web_share(0, 27) + 0.08)

    # --- campaign ------------------------------------------------------------
    def _region_coins(self, regions, lo, hi):
        region = {g["gifter_id"]: g["region"] for g in self.t["gifters"]}
        return sum(g["coins"] for g in self.gifts
                   if lo <= g["day"] <= hi and region[g["gifter_id"]] in regions
                   and g["gifter_id"] not in self.ring_g)

    def test_campaign_raw_lift_overstates_incremental_lift(self):
        c = self.t["campaigns"][0]
        s, e = c["start_day"], c["end_day"]
        tr, ho = c["treatment_regions"].split("|"), c["holdout_regions"].split("|")
        t_pre, t_ev = self._region_coins(tr, s - 14, s - 1) / 2, self._region_coins(tr, s, e)
        h_pre, h_ev = self._region_coins(ho, s - 14, s - 1) / 2, self._region_coins(ho, s, e)
        raw = t_ev / t_pre - 1
        did = metrics.did_lift(t_pre, t_ev, h_pre, h_ev)
        self.assertGreater(raw, 0.30)
        self.assertTrue(0.08 <= did <= 0.30)
        self.assertGreater(raw, did + 0.15)

    def test_campaign_pulls_spend_forward(self):
        c = self.t["campaigns"][0]
        s, e = c["start_day"], c["end_day"]
        tr, ho = c["treatment_regions"].split("|"), c["holdout_regions"].split("|")
        post = metrics.did_lift(self._region_coins(tr, s - 14, s - 1) / 2, self._region_coins(tr, e + 1, e + 7),
                                self._region_coins(ho, s - 14, s - 1) / 2, self._region_coins(ho, e + 1, e + 7))
        self.assertLess(post, 0)

    def test_prize_fulfillment_has_failures_and_a_fraud_winner(self):
        prizes = self.t["prizes"]
        self.assertTrue(any(p["status"] == "failed" for p in prizes))
        self.assertTrue(any(p["gifter_id"] in self.ring_g for p in prizes))

    # --- incident: last week's drop ---------------------------------------
    def test_last_week_drop_is_android_east_payments(self):
        daily = metrics.daily_sum(self.gifts, "coins")
        self.assertTrue(-0.18 <= metrics.wow_change(daily, END) <= -0.08)

        g = {x["gifter_id"]: x for x in self.t["gifters"]}
        def seg(lo, hi, hit):
            return sum(x["coins"] for x in self.gifts if lo <= x["day"] <= hi and hit(g[x["gifter_id"]]))
        is_ae = lambda x: x["platform"] == "android" and x["region"] == "US-East"
        total_drop = seg(END - 13, END - 7, lambda x: True) - seg(END - 6, END, lambda x: True)
        ae_drop = seg(END - 13, END - 7, is_ae) - seg(END - 6, END, is_ae)
        self.assertGreater(ae_drop / total_drop, 0.6)

        failed = [p for p in self.t["purchases"] if p["status"] == "failed"]
        self.assertGreater(len(failed), 50)
        self.assertGreater(sum(p["app_version"] == generate.BAD_VERSION for p in failed) / len(failed), 0.9)

    # --- trust & safety ----------------------------------------------------
    def test_fraud_ring_signals(self):
        def cb_rate(rows):
            return sum(p["chargeback"] for p in rows) / len(rows)
        ok = [p for p in self.t["purchases"] if p["status"] == "ok"]
        self.assertGreater(cb_rate([p for p in ok if p["gifter_id"] in self.ring_g]), 0.25)
        self.assertLess(cb_rate([p for p in ok if p["gifter_id"] not in self.ring_g]), 0.02)

        devices = {x["device_id"] for x in self.t["gifters"] if x["gifter_id"] in self.ring_g}
        self.assertLessEqual(len(devices), 4)

        for c in self.ring_c:
            rows = [x for x in self.gifts if x["creator_id"] == c]
            from_ring = sum(x["coins"] for x in rows if x["gifter_id"] in self.ring_g)
            self.assertGreater(from_ring / sum(x["coins"] for x in rows), 0.8)

    def test_flagged_minors_are_still_gifting(self):
        flagged = {x["gifter_id"] for x in self.t["gifters"] if x["age_signal"] == "flagged_minor"}
        self.assertTrue(flagged)
        self.assertTrue(any(x["gifter_id"] in flagged for x in self.gifts))

    # --- supply: creators --------------------------------------------------
    def test_mid_tier_creator_churn_accelerates(self):
        mids = [c for c in self.t["creators"] if c["tier"] == "mid" and c["churn_day"] != ""]
        early = sum(c["churn_day"] <= 27 for c in mids)
        late = sum(c["churn_day"] >= END - 27 for c in mids)
        self.assertGreaterEqual(late, 2 * max(early, 1))

    def test_many_small_creators_earn_nothing(self):
        recent = {g["creator_id"] for g in self.gifts if g["day"] > END - 28}
        def earning(tier):
            live = [c for c in self.t["creators"] if c["tier"] == tier and c["join_day"] <= END
                    and (c["churn_day"] == "" or c["churn_day"] > END - 28)]
            return sum(c["creator_id"] in recent for c in live) / len(live)
        self.assertGreaterEqual(earning("head"), 0.9)
        self.assertTrue(0.25 <= earning("emerging") <= 0.8)
        self.assertGreater(earning("mid"), earning("emerging"))

    def test_creators_without_sessions_after_churn(self):
        churned = {c["creator_id"]: c["churn_day"] for c in self.t["creators"] if c["churn_day"] != ""}
        for s in self.t["sessions"]:
            if s["creator_id"] in churned:
                self.assertLess(s["day"], churned[s["creator_id"]])


if __name__ == "__main__":
    unittest.main()
