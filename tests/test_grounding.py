"""The hallucination guard's logic: can every number in a spoken sentence be traced to a tool result from this call?"""
import os
import tempfile
import unittest

from gifting import analysis, generate
from voice import grounding, tools


def claims(text):
    return [(c.value, c.kind) for c in grounding.extract_claims(text)]


class ExtractionTest(unittest.TestCase):
    def test_percentages(self):
        self.assertEqual(claims("down 13 percent on the week before"), [(13, "pct")])
        self.assertEqual(claims("about 0.5 percent"), [(0.5, "pct")])
        self.assertEqual(claims("up 22%"), [(22, "pct")])
        self.assertEqual(claims("minus 16 percent"), [(16, "pct")])

    def test_counts_and_scales(self):
        self.assertEqual(claims("about 21 thousand people"), [(21000, "num")])
        self.assertEqual(claims("about 7.6 million coins"), [(7600000, "num")])
        self.assertEqual(claims("558 failures"), [(558, "num")])
        self.assertEqual(claims("7,583,854 coins"), [(7583854, "num")])
        self.assertEqual(claims("$650 in prizes"), [(650, "num")])
        self.assertEqual(claims("650 dollars"), [(650, "num")])

    def test_number_words_that_are_big_enough_to_be_claims(self):
        self.assertEqual(claims("twenty one thousand people"), [(21000, "num")])
        self.assertEqual(claims("fifteen percent of gifters"), [(15, "pct")])
        self.assertEqual(claims("one hundred twenty creators"), [(120, "num")])
        self.assertEqual(claims("forty-two accounts"), [(42, "num")])

    def test_everyday_small_words_and_times_are_not_claims(self):
        for text in ("no one knows", "one thing at a time", "give me one minute", "two sentences please",
                     "in a few seconds", "the first issue", "it happened in 2026", "ask me anything"):
            self.assertEqual(claims(text), [], text)

    def test_ids_and_versions(self):
        self.assertEqual(claims("account g00123"), [])                     # identifiers are not figures
        self.assertEqual(claims("app version 34.2"), [(34.2, "num")])      # versions are, and they are grounded by tool text

    def test_a_unit_after_a_range_applies_to_both_ends(self):
        self.assertEqual(claims("fifty to seventy percent"), [(50, "pct"), (70, "pct")])
        self.assertEqual(claims("between 50 and 70 percent"), [(50, "pct"), (70, "pct")])
        self.assertEqual(claims("50-70%"), [(50, "pct"), (70, "pct")])
        self.assertEqual(claims("21 to 25 thousand people"), [(21000, "num"), (25000, "num")])
        self.assertEqual(claims("five to ten percent"), [(5, "pct"), (10, "pct")])

    def test_a_range_is_not_invented_where_there_is_none(self):
        self.assertEqual(claims("about 50 and 70 gifters"), [(50, "num"), (70, "num")])        # no shared unit
        self.assertEqual(claims("up 50 percent and 70 thousand coins"), [(50, "pct"), (70000, "num")])   # different units stay different

    def test_several_in_order(self):
        self.assertEqual(claims("down 16 percent to about 405 thousand coins, 558 failures"),
                         [(16, "pct"), (405000, "num"), (558, "num")])

    def test_blank(self):
        for x in ("", None, "   "):
            self.assertEqual(claims(x), [])


class FactsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.a = tools.Analyst(analysis.load(path))

    def facts_after(self, *calls):
        fb = grounding.FactBook()
        for name, args in calls:
            fb.add(name, self.a.dispatch(name, args))
        return fb


class GroundingTest(FactsBase):
    def test_an_empty_book_grounds_no_numbers_but_allows_sentences_without_any(self):
        fb = grounding.FactBook()
        self.assertTrue(fb.check("I can't answer that from the data I have.").ok)
        self.assertFalse(fb.check("About 21 thousand people have gifted.").ok)

    def test_a_number_from_a_tool_result_is_grounded_in_its_spoken_forms(self):
        fb = self.facts_after(("get_metric", {"metric": "gifters"}))
        self.assertTrue(fb.check("About 21 thousand people have sent a gift.").ok)
        self.assertTrue(fb.check("That's about 21,000 gifters.").ok)

    def test_a_made_up_number_is_caught_and_named(self):
        fb = self.facts_after(("get_metric", {"metric": "gifters"}))
        result = fb.check("About 45 thousand people have sent a gift.")
        self.assertFalse(result.ok)
        self.assertEqual([(c.value, c.kind) for c in result.ungrounded], [(45000, "num")])

    def test_percentages_match_the_fractions_tools_return(self):
        fb = self.facts_after(("get_metric", {"metric": "new_gifter_return_7d"}))
        pct = round(self.a.get_metric("new_gifter_return_7d")["value"] * 100)
        self.assertTrue(fb.check(f"Only {pct} percent come back within a week.").ok)
        self.assertFalse(fb.check(f"Only {pct + 9} percent come back within a week.").ok)

    def test_a_count_cannot_stand_in_for_a_percentage(self):
        fb = self.facts_after(("get_metric", {"metric": "failed_purchases"}))             # a count, say 541
        self.assertFalse(fb.check("About 541 percent of purchases failed.").ok)

    def test_exact_counts_must_match_exactly(self):
        fb = self.facts_after(("get_findings", {"limit": 2}))
        self.assertTrue(fb.check("The ring is 14 accounts on 3 shared devices.").ok)
        self.assertFalse(fb.check("The ring is 15 accounts on 3 shared devices.").ok)
        self.assertFalse(fb.check("The ring is 14 accounts on 4 shared devices.").ok)

    def test_numbers_inside_tool_text_count_as_facts(self):
        fb = self.facts_after(("get_findings", {"limit": 1}))
        self.assertTrue(fb.check("It affects app version 34.2.").ok)
        self.assertTrue(fb.check("Failures went up over the last 7 days.").ok)             # "7 days" is in the evidence text

    def test_the_metric_name_grounds_its_own_window(self):
        fb = self.facts_after(("get_metric", {"metric": "new_gifter_return_7d"}))
        self.assertTrue(fb.check("They return within 7 days.").ok)
        self.assertFalse(fb.check("They return within 30 days.").ok)

    def test_direction_words_do_not_matter_only_the_size(self):
        fb = self.facts_after(("get_metric", {"metric": "coins_gifted"}))
        pct = round(abs(self.a.get_metric("coins_gifted")["change_vs_prior_week"]) * 100)
        self.assertTrue(fb.check(f"It rose {pct} percent.").ok)                            # direction is not checked, size is

    def test_a_range_is_grounded_end_by_end(self):
        fb = grounding.FactBook()
        fb.add("search_web", {"results": [{"snippet": "around 50% of what viewers spend, up to 70% in some cases"}]})
        self.assertTrue(fb.check("Industry estimates say roughly fifty to seventy percent.").ok)
        self.assertFalse(fb.check("Industry estimates say roughly fifty to ninety percent.").ok)

    def test_numbers_the_caller_says_are_not_facts(self):
        fb = self.facts_after(("get_metric", {"metric": "gifters"}))
        self.assertFalse(fb.check("Yes, gifting fell 40 percent.").ok)

    def test_facts_from_earlier_in_the_call_still_count(self):
        fb = self.facts_after(("get_metric", {"metric": "gifters"}), ("get_lapsed_gifters", {}))
        self.assertTrue(fb.check("About 21 thousand gifted, and 69 of the biggest have gone quiet.").ok
                        or fb.check("About 21 thousand gifted.").ok)

    def test_errors_and_unavailable_results_add_no_facts(self):
        fb = grounding.FactBook()
        fb.add("get_metric", {"error": "I don't have a metric called 'x'.", "options": ["a", "b"]})
        self.assertFalse(fb.check("There are 1500 of them.").ok)

    def test_bad_results_do_not_crash(self):
        fb = grounding.FactBook()
        for r in (None, 5, "text 12", [1, 2], {"a": {"b": [None, True, float("nan")]}}):
            fb.add("x", r)

    def test_tolerance_is_tight_for_small_numbers_and_loose_for_spoken_rounding(self):
        fb = grounding.FactBook()
        fb.add("x", {"value": 541, "big": 7583854, "small": 14})
        self.assertTrue(fb.check("About 540 failed.").ok)
        self.assertFalse(fb.check("About 560 failed.").ok)
        self.assertTrue(fb.check("About 7.6 million coins.").ok)
        self.assertFalse(fb.check("About 9 million coins.").ok)
        self.assertTrue(fb.check("14 of them.").ok)
        self.assertFalse(fb.check("16 of them.").ok)


class WholeAnswerTest(FactsBase):
    def test_every_tool_sentence_is_grounded_in_its_own_result(self):
        """If a tool's own sentence could not pass the guard, the guard would be useless. Check each one."""
        for name in tools.TOOL_SPECS:
            n = name["name"]
            if n == "search_web":
                continue
            args = {"metric": "coins_gifted"} if n == "get_metric" else {}
            result = self.a.dispatch(n, args)
            fb = grounding.FactBook()
            fb.add(n, result)
            line = tools.direct_say(n, result)
            if line:
                self.assertTrue(fb.check(line).ok, f"{n}: {fb.check(line).ungrounded}")


if __name__ == "__main__":
    unittest.main()
