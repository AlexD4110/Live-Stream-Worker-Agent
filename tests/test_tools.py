"""The voice agent's fixed toolbox. Every answer comes from these functions; none of them guesses."""
import math
import os
import re
import tempfile
import unittest
from collections import defaultdict

from gifting import analysis, generate
from voice import briefing, tools

END = generate.DAYS - 1


class ToolsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t, cls.truth = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(cls.t, path)
        cls.D = analysis.load(path)
        cls.a = tools.Analyst(cls.D)


class SpokenFormTest(unittest.TestCase):
    def test_big_numbers_are_rounded_for_the_ear(self):
        self.assertEqual(tools.spoken_count(7583854), "about 7.6 million")
        self.assertEqual(tools.spoken_count(21298), "about 21 thousand")
        self.assertEqual(tools.spoken_count(541), "about 540")
        self.assertEqual(tools.spoken_count(12), "12")

    def test_percentages_say_percent(self):
        self.assertEqual(tools.spoken_pct(0.1812), "18 percent")
        self.assertEqual(tools.spoken_pct(0.0046), "0.5 percent")
        self.assertEqual(tools.spoken_pct(-0.1346), "13 percent")   # direction is stated in words, not with a sign

    def test_symbols_become_words(self):
        out = briefing.speakable("Gifting is -13% ($650) on Android US-East → fix it")
        for bad in ("%", "$", "→", "-13"):
            self.assertNotIn(bad, out)
        self.assertIn("percent", out)
        self.assertIn("650 dollars", out)


class MetricToolTest(ToolsBase):
    def test_every_listed_metric_returns_a_value_and_a_sentence(self):
        for m in self.a.METRICS:
            r = self.a.get_metric(m)
            if r.get("available") is False:
                continue
            self.assertIn("value", r, m)
            self.assertTrue(r["say"], m)

    def test_values_match_the_analysis_code(self):
        s = analysis.summary(self.D, {})
        self.assertEqual(self.a.get_metric("coins_gifted")["value"], s["coins"])
        self.assertAlmostEqual(self.a.get_metric("new_gifter_return_7d")["value"], s["ret7"])
        self.assertAlmostEqual(self.a.get_metric("top_1_percent_share")["value"], s["top1"])
        self.assertAlmostEqual(self.a.get_metric("coins_gifted")["change_vs_prior_week"], s["wow"])

    def test_filters_apply(self):
        east = self.a.get_metric("coins_gifted", region="US-East")
        self.assertEqual(east["value"], analysis.summary(self.D, {"region": "US-East"})["coins"])
        self.assertEqual(east["scope"], "US-East")
        self.assertEqual(self.a.get_metric("coins_gifted")["scope"], "all traffic")

    def test_send_rate_is_unavailable_for_a_platform_filter_and_says_why(self):
        r = self.a.get_metric("gift_send_rate", platform="android")
        self.assertFalse(r["available"])
        self.assertIn("viewer", r["reason"].lower())
        self.assertNotIn("value", r)

    def test_bad_input_returns_an_error_instead_of_raising(self):
        for kwargs in ({"metric": "revenue"}, {"metric": "coins_gifted", "region": "Mars"},
                       {"metric": "coins_gifted", "platform": "nokia"}):
            r = self.a.get_metric(**kwargs)
            self.assertIn("error", r)
            self.assertIn("options", r)


class DiagnosisToolTest(ToolsBase):
    def test_explain_change_names_the_biggest_mover(self):
        r = self.a.explain_change()
        parts = {p["name"]: p["change"] for p in r["parts"]}
        biggest = max(parts, key=lambda k: abs(parts[k]))
        self.assertEqual(r["biggest_mover"], biggest)
        self.assertAlmostEqual(r["total_change"], analysis.summary(self.D, {})["wow"])

    def test_explain_change_with_platform_uses_gifters_not_viewers(self):
        names = [p["name"] for p in self.a.explain_change(platform="android")["parts"]]
        self.assertIn("gifters", names)
        self.assertNotIn("viewers", names)

    def test_segment_changes_put_android_us_east_first(self):
        r = self.a.get_segment_changes()
        self.assertEqual((r["segments"][0]["platform"], r["segments"][0]["region"]), ("android", "US-East"))
        self.assertLess(r["segments"][0]["change"], -0.3)
        self.assertEqual(len(r["segments"]), 8)

    def test_findings_are_ranked_and_complete(self):
        r = self.a.get_findings(limit=2)
        self.assertEqual([f["id"] for f in r["findings"]], ["payments_incident", "gift_ring"])
        for f in r["findings"]:
            for k in ("title", "severity", "evidence", "next_steps", "guardrail"):
                self.assertTrue(f[k])
        self.assertEqual(r["total_found"], len(analysis.findings(self.D)))

    def test_findings_never_expose_account_ids(self):
        blob = str(self.a.get_findings(limit=20))
        self.assertNotRegex(blob, r"g\d{5}")

    def test_campaign_result_reports_the_holdout_and_a_caveat(self):
        r = self.a.get_campaign_result()
        s = analysis.summary(self.D, {})["campaign"]
        self.assertAlmostEqual(r["headline_lift"], s["rawLift"])
        self.assertAlmostEqual(r["lift_vs_holdout"], s["trueLift"])
        self.assertIn("holdout", r["caveat"])

    def test_creator_health(self):
        r = self.a.get_creator_health()
        self.assertEqual([t["tier"] for t in r["tiers"]], ["head", "mid", "emerging"])
        mid = r["tiers"][1]
        self.assertGreaterEqual(mid["stopped_last_4_weeks"], 2 * max(mid["stopped_previous_4_weeks"], 1))
        self.assertEqual(sum(t["creators"] for t in r["tiers"]), 300)

    def test_lapsed_gifters_match_an_independent_count(self):
        last, spend = {}, defaultdict(int)
        for g in self.t["gifts"]:
            last[g["gifter_id"]] = max(last.get(g["gifter_id"], -1), g["day"])
            spend[g["gifter_id"]] += g["coins"]
        ring = set(self.truth["ring_gifters"])
        lapsed = [g for g in last if END - last[g] >= 21 and g not in ring]
        r = self.a.get_lapsed_gifters()
        self.assertEqual(r["lapsed_gifters"], len(lapsed))
        self.assertEqual(r["coins_they_gifted_before"], sum(spend[g] for g in lapsed))
        self.assertGreater(r["lapsed_big_gifters"], 0)
        self.assertLessEqual(r["lapsed_big_gifters"], r["big_gifters"])


class WarmUpTest(ToolsBase):
    def test_warm_up_precomputes_so_the_first_answer_is_instant(self):
        fresh = tools.Analyst(self.D)
        self.assertEqual(fresh._summaries, {})
        fresh.warm()
        self.assertIn(("", ""), fresh._summaries)
        self.assertIsNotNone(fresh._findings)
        self.assertIs(fresh.get_metric("gifters")["value"], fresh._summary()["gifters"])


class DiagnoseTest(ToolsBase):
    def test_one_call_gives_what_drove_it_where_it_fell_and_the_top_issue(self):
        r = self.a.diagnose_change()
        self.assertEqual(r["biggest_mover"], self.a.explain_change()["biggest_mover"])
        self.assertEqual((r["worst_segment"]["platform"], r["worst_segment"]["region"]), ("android", "US-East"))
        self.assertEqual(r["top_finding"]["id"], "payments_incident")
        self.assertTrue(r["top_finding"]["next_step"])

    def test_the_spoken_answer_covers_all_three_and_reads_cleanly(self):
        say = self.a.diagnose_change()["say"]
        self.assertIn("percent", say)
        self.assertIn("biggest mover", say)
        self.assertIn("Android US East", say)
        self.assertIn("top issue", say.lower())
        say = tools.speak_line(say)                            # the form actually spoken
        self.assertFalse(any(ch in say for ch in "%$→-"), say)
        self.assertLess(len(say.split()), 80)

    def test_it_follows_the_filter_for_the_change_and_stays_honest_about_the_rest(self):
        r = self.a.diagnose_change(platform="android")
        self.assertIn("gifters", [p["name"] for p in r["parts"]])
        self.assertNotIn("viewers", [p["name"] for p in r["parts"]])

    def test_bad_filters_are_errors(self):
        self.assertIn("error", self.a.diagnose_change(region="Mars"))

    def test_it_closes_when_the_voice_is_suspect(self):
        a = tools.Analyst(self.D)
        a.restricted = True
        self.assertTrue(a.dispatch("diagnose_change", {}).get("restricted"))

    def test_the_prompt_and_descriptions_point_why_to_diagnose_and_where_to_segments(self):
        specs = {s["name"]: s["description"].lower() for s in tools.TOOL_SPECS}
        self.assertIn("why", specs["diagnose_change"])
        self.assertIn("where", specs["get_segment_changes"])
        from voice import brain
        p = brain.SYSTEM_PROMPT
        self.assertIn("diagnose_change", p)
        self.assertIn("get_segment_changes", p)


class RoutingTest(unittest.TestCase):
    """The model chooses a tool from its description alone, so the description must name the topics people ask about."""

    def test_the_findings_tool_names_the_topics_it_covers(self):
        d = next(s["description"] for s in tools.TOOL_SPECS if s["name"] == "get_findings").lower()
        for topic in ("fraud", "ring", "minors", "prizes", "chargebacks", "bug", "churn", "recommend"):
            self.assertIn(topic, d, topic)

    def test_the_prompt_sends_risk_questions_to_the_findings_tool(self):
        from voice import brain
        p = brain.SYSTEM_PROMPT.lower()
        self.assertIn("ring", p)
        self.assertIn("get_findings", p)
        self.assertIn("never say you can't answer without trying a tool", p)


class DirectAnswerTest(ToolsBase):
    """Tools that return a ready-to-speak line are spoken as they are, with no second model call."""

    def test_which_tools_are_direct(self):
        for name in ("get_metric", "explain_change", "get_segment_changes", "diagnose_change", "get_campaign_result",
                     "get_creator_health", "get_lapsed_gifters", "get_briefing"):
            self.assertIn(name, tools.DIRECT)
        for name in ("get_findings", "search_web"):
            self.assertNotIn(name, tools.DIRECT)
        self.assertTrue(tools.DIRECT <= {s["name"] for s in tools.TOOL_SPECS})

    def test_the_line_comes_from_the_tool_result(self):
        r = self.a.dispatch("get_metric", {"metric": "gifters"})
        self.assertEqual(tools.direct_say("get_metric", r), tools.speak_line(r["say"]))
        self.assertTrue(tools.direct_say("get_metric", r)[0].isupper())              # starts like a sentence

    def test_the_briefing_is_its_script(self):
        r = self.a.dispatch("get_briefing", {})
        self.assertEqual(tools.direct_say("get_briefing", r), r["script"])

    def test_no_direct_line_when_it_would_be_wrong_to_speak_one(self):
        self.assertIsNone(tools.direct_say("get_findings", self.a.dispatch("get_findings", {})))                   # needs the model
        self.assertIsNone(tools.direct_say("get_metric", {"error": "bad", "say": "x"}))                           # errors need explaining
        self.assertIsNone(tools.direct_say("get_metric", {"available": False, "reason": "no", "say": "x"}))
        self.assertIsNone(tools.direct_say("get_metric", {"value": 1}))
        self.assertIsNone(tools.direct_say("get_metric", {"say": "   "}))
        self.assertIsNone(tools.direct_say("get_metric", None))
        self.assertIsNone(tools.direct_say("delete_everything", {"say": "x"}))

    def test_a_restricted_answer_is_spoken_too(self):
        a = tools.Analyst(self.D)
        a.restricted = True
        r = a.dispatch("diagnose_change", {})
        self.assertIn("voice", tools.direct_say("diagnose_change", r).lower())

    def test_every_direct_tool_actually_produces_a_line(self):
        for name in tools.DIRECT:
            args = {"metric": "gifters"} if name == "get_metric" else {}
            self.assertTrue(tools.direct_say(name, self.a.dispatch(name, args)), name)


class NullOptionsTest(ToolsBase):
    """Models fill in every option, sending null for the ones they don't want. The schema must allow that, or the
    model provider rejects the call before our code runs."""

    def optional_props(self):
        for spec in tools.TOOL_SPECS:
            for name, p in spec["properties"].items():
                if name not in spec["required"]:
                    yield spec["name"], name, p

    def test_every_optional_option_is_declared_nullable(self):
        found = list(self.optional_props())
        self.assertTrue(found)
        for tool, name, p in found:
            types = p["type"] if isinstance(p["type"], list) else [p["type"]]
            self.assertIn("null", types, f"{tool}.{name} must allow null")
            if "enum" in p:
                self.assertIn(None, p["enum"], f"{tool}.{name} enum must allow null")

    def test_required_options_do_not_allow_null(self):
        for spec in tools.TOOL_SPECS:
            for name in spec["required"]:
                p = spec["properties"][name]
                types = p["type"] if isinstance(p["type"], list) else [p["type"]]
                self.assertNotIn("null", types, f"{spec['name']}.{name}")

    def test_null_means_no_filter(self):
        self.assertEqual(self.a.dispatch("explain_change", {"region": None, "platform": None}), self.a.explain_change())
        self.assertEqual(self.a.dispatch("get_metric", {"metric": "gifters", "region": None, "platform": None})["value"],
                         self.a.get_metric("gifters")["value"])

    def test_a_null_limit_uses_the_default_of_three(self):
        self.assertEqual(len(self.a.dispatch("get_findings", {"limit": None})["findings"]), 3)

    def test_a_null_required_option_is_still_an_error(self):
        self.assertIn("error", self.a.dispatch("get_metric", {"metric": None}))
        self.assertIn("error", self.a.dispatch("search_web", {"query": None}))

    def test_every_tool_accepts_all_its_options_set_to_null(self):
        for spec in tools.TOOL_SPECS:
            args = {name: (None if name not in spec["required"] else "gifters" if name == "metric" else "how do livestream gifts work")
                    for name in spec["properties"]}
            r = self.a.dispatch(spec["name"], args)
            self.assertNotIn("must be", str(r.get("error", "")), spec["name"])


class DispatchTest(ToolsBase):
    def test_every_spec_is_complete_and_has_a_matching_method(self):
        names = [s["name"] for s in tools.TOOL_SPECS]
        self.assertEqual(len(names), len(set(names)))
        for s in tools.TOOL_SPECS:
            self.assertTrue(s["description"])
            self.assertEqual(set(s["required"]) - set(s["properties"]), set())
            self.assertTrue(callable(getattr(self.a, s["name"])), s["name"])

    def test_dispatch_runs_a_tool(self):
        self.assertEqual(self.a.dispatch("get_metric", {"metric": "gifters"})["value"],
                         self.a.get_metric("gifters")["value"])

    def test_dispatch_rejects_unknown_tools_and_bad_arguments(self):
        self.assertIn("error", self.a.dispatch("delete_everything", {}))
        self.assertIn("error", self.a.dispatch("get_metric", {"metric": "gifters", "colour": "red"}))
        self.assertIn("error", self.a.dispatch("get_metric", {}))
        self.assertIn("error", self.a.dispatch("get_findings", {"limit": "lots"}))

    def test_dispatch_does_not_leak_python_errors(self):
        r = self.a.dispatch("get_findings", {"limit": -5})
        self.assertNotIn("Traceback", str(r))

    def test_results_are_json_safe(self):
        import json
        for s in tools.TOOL_SPECS:
            args = {"metric": "gifters"} if s["required"] else {}
            json.dumps(self.a.dispatch(s["name"], args))


class BriefingTest(ToolsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = briefing.build(cls.D)
        cls.words = len(cls.b["script"].split())

    def test_fits_in_about_a_minute(self):
        self.assertTrue(110 <= self.words <= 190, self.words)

    def test_leads_with_the_headline_number(self):
        s = analysis.summary(self.D, {})
        direction = "down" if s["wow"] < 0 else "up"
        headline = self.b["script"][:200]
        self.assertIn(direction, headline)
        self.assertIn(f"{math.floor(abs(s['wow']) * 100 + 0.5)} percent", headline)

    def test_names_the_top_concern_and_one_action(self):
        top = analysis.findings(self.D)[0]
        self.assertEqual(self.b["top_finding"], top["id"])
        self.assertIn("Android", self.b["script"])
        self.assertIn("next step", self.b["script"].lower())

    def test_reads_cleanly_aloud(self):
        for bad in ("%", "$", "→", "*", "#", "_", "http"):
            self.assertNotIn(bad, self.b["script"])
        self.assertIsNone(re.search(r"\bg\d{5}\b", self.b["script"]))

    def test_has_no_stuttering_repeated_words(self):
        self.assertIsNone(re.search(r"\b(\w+) \1\b", self.b["script"], re.IGNORECASE))
        for t in tools.TOOL_SPECS:
            for r in (self.a.get_metric("coins_gifted"), self.a.explain_change(), self.a.get_lapsed_gifters()):
                self.assertIsNone(re.search(r"\b(\w+) \1\b", r.get("say", ""), re.IGNORECASE), r["say"])

    def test_states_it_is_synthetic_and_an_ai(self):
        self.assertIn("synthetic", self.b["script"].lower())

    def test_ends_by_inviting_questions(self):
        self.assertTrue(self.b["script"].rstrip().endswith("?"))

    def test_is_deterministic(self):
        self.assertEqual(briefing.build(self.D)["script"], self.b["script"])

    def test_greeting_discloses_ai_and_offers_the_briefing(self):
        g = briefing.GREETING
        self.assertIn("AI", g)
        self.assertIn("briefing", g.lower())
        self.assertLess(len(g.split()), 45)


if __name__ == "__main__":
    unittest.main()
