"""Metric math checked against tiny datasets worked out by hand."""
import unittest

from gifting import metrics


def gift(day, gifter, creator="c1", coins=10):
    return {"day": day, "gifter_id": gifter, "creator_id": creator, "coins": coins}


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.gifts = [
            gift(0, "g1"), gift(3, "g1"),     # back on day 3  -> D7 and D30
            gift(0, "g2"), gift(10, "g2"),    # back on day 10 -> D30 only
            gift(0, "g3"),                    # never back
            gift(0, "g3"),                    # same day again does not count
            gift(95, "g4"),                   # too new to judge
        ]

    def test_d7_counts_return_within_days_1_to_7(self):
        self.assertAlmostEqual(metrics.retention(self.gifts, 7, end_day=97), 1 / 3)

    def test_d30_counts_return_within_days_1_to_30(self):
        self.assertAlmostEqual(metrics.retention(self.gifts, 30, end_day=97), 2 / 3)

    def test_empty_cohort_is_zero(self):
        self.assertEqual(metrics.retention([gift(95, "g4")], 7, end_day=97), 0.0)


class ConcentrationTest(unittest.TestCase):
    def test_top_one_percent_share(self):
        gifts = [gift(0, "whale", coins=50)] + [gift(0, f"g{i}", coins=1) for i in range(99)]
        self.assertAlmostEqual(metrics.top_share(gifts, 0.01), 50 / 149)


class ChangeTest(unittest.TestCase):
    def test_diff_in_diff_lift(self):
        # treatment 100 -> 150, holdout 100 -> 120: 1.5 / 1.2 - 1
        self.assertAlmostEqual(metrics.did_lift(100, 150, 100, 120), 0.25)

    def test_week_over_week(self):
        daily = {d: 10 for d in range(7)}
        daily.update({d: 9 for d in range(7, 14)})
        self.assertAlmostEqual(metrics.wow_change(daily, end_day=13), -0.1)

    def test_daily_sum_groups_by_day(self):
        gifts = [gift(0, "a", coins=5), gift(0, "b", coins=7), gift(2, "a", coins=1)]
        self.assertEqual(metrics.daily_sum(gifts, "coins"), {0: 12, 2: 1})


if __name__ == "__main__":
    unittest.main()
