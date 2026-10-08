"""The output guards: what the model writes is checked before it is spoken. Numbers must trace to tool results (hallucination
guard) and the wording must stay inside the rules (compliance guard)."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from gifting import analysis, generate
from voice import brain, chat, compliance, config, fraud, grounding, tools
from voice import review as rv

try:
    from pipecat.frames.frames import (InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
                                       TTSSpeakFrame)
    from pipecat.runner.types import CallData
    from pipecat.tests.utils import SleepFrame, run_test
    HAVE = True
except ImportError:
    HAVE = False
if HAVE:
    from voice import output_guard, pipeline


class ComplianceTest(unittest.TestCase):
    SHOULD_BLOCK = {
        "promise": ["I guarantee this will fix it.", "We promise gifting will recover.", "It is guaranteed to work.",
                    "This will definitely bring the numbers back.", "There is no risk in doing this.", "That's a sure thing."],
        "advice": ["You should buy the stock now.", "I recommend selling your shares.", "Hold your bitcoin until Friday.",
                   "Invest in the crypto before it moves."],
        "pressure_spend": ["Encourage the big gifters to spend more.", "Push whales to gift more this week.",
                           "Get heavy spenders to spend more.", "Nudge your top gifters to buy more coins."],
        "verdict": ["That is definitely a fraud ring.", "He is clearly a fraudster.", "This is confirmed money laundering.",
                    "They are criminals.", "The company is guilty."],
        "allegation": ["TikTok was found guilty of exploiting minors.", "The company broke the law.", "They were fined for gifting."],
        "official_rate": ["TikTok takes 50 percent of every gift.", "The platform keeps half of every gift."],
    }
    SHOULD_PASS = [
        "I can't give financial advice.", "We can't guarantee results.", "There's no guarantee it will work.",
        "The ring is a risk signal, not a verdict.", "It looks like a possible gift ring, so a person should review it.",
        "According to Digiday, the platform keeps about 50 percent, which is reported, not official.",
        "Consider holding payouts for the ring creators.", "Offer recognition to lapsed big gifters, not spending prompts.",
        "Reach out to the lapsed big gifters before offering anything.", "Test a second-gift nudge for new gifters.",
        "A person at trust and safety should review this.", "Those are allegations, not findings.",
        "The New Hampshire attorney general alleges exploitation.", "Gifting is down 16 percent this week.",
        "Invest in the onboarding flow to keep new gifters.", "The pattern is consistent with fraud, so escalate it.",
        "Hold payouts for the three receiving creators and send the cluster to review.", "I can't answer that from the data I have.",
        "Treat it as an incident and ask engineering to check the purchase flow.", "Some creators say it is a good fit for the event.",
    ]

    def test_each_kind_of_breach_is_caught_in_its_category(self):
        for category, sentences in self.SHOULD_BLOCK.items():
            for s in sentences:
                got = compliance.assess_output(s)
                self.assertIsNotNone(got, f"missed: {s}")
                self.assertEqual(got["category"], category, s)

    def test_honest_wording_passes(self):
        for s in self.SHOULD_PASS:
            self.assertIsNone(compliance.assess_output(s), f"false alarm: {s}")

    def test_blank_or_odd_input(self):
        for x in ("", None, "   ", 5):
            self.assertIsNone(compliance.assess_output(x))

    def test_the_replacement_lines_are_fixed_spoken_sentences(self):
        for category, line in compliance.RESPONSES.items():
            self.assertTrue(line.endswith("."), category)
            self.assertFalse(any(ch.isdigit() for ch in line), line)
        self.assertEqual(set(compliance.RESPONSES), set(self.SHOULD_BLOCK))


class ReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.a = tools.Analyst(analysis.load(path))

    def facts(self):
        fb = grounding.FactBook()
        fb.add("get_metric", self.a.dispatch("get_metric", {"metric": "gifters"}))
        return fb

    def test_a_grounded_clean_answer_is_untouched(self):
        r = rv.review("About 21 thousand people have sent a gift. Want more detail?", self.facts())
        self.assertEqual(r.text, "About 21 thousand people have sent a gift. Want more detail?")
        self.assertEqual(r.events, [])

    def test_an_invented_number_replaces_only_that_sentence(self):
        r = rv.review("About 45 thousand people have sent a gift. Want more detail?", self.facts())
        self.assertEqual(r.text, f"{grounding.UNGROUNDED_LINE} Want more detail?")
        self.assertEqual(r.events, [("hallucination", "ungrounded_number")])

    def test_a_compliance_breach_is_replaced(self):
        r = rv.review("I guarantee that will fix it.", self.facts())
        self.assertEqual(r.text, compliance.RESPONSES["promise"])
        self.assertEqual(r.events, [("compliance", "promise")])

    def test_wording_is_checked_before_numbers(self):
        r = rv.review("That is definitely a fraud ring of 99 accounts.", self.facts())
        self.assertEqual(r.text, compliance.RESPONSES["verdict"])

    def test_repeated_replacement_lines_are_said_once(self):
        r = rv.review("About 45 thousand gifted. About 50 thousand returned. About 60 thousand left.", self.facts())
        self.assertEqual(r.text, grounding.UNGROUNDED_LINE)
        self.assertEqual(len(r.events), 3)                       # all three were noted, only one line spoken

    def test_each_guard_can_be_switched_off(self):
        text = "About 45 thousand gifted. I guarantee that."
        only_numbers = rv.review(text, self.facts(), compliance_on=False)
        self.assertEqual(only_numbers.text, f"{grounding.UNGROUNDED_LINE} I guarantee that.")
        only_wording = rv.review(text, self.facts(), grounding_on=False)
        self.assertEqual(only_wording.text, f"About 45 thousand gifted. {compliance.RESPONSES['promise']}")

    def test_decimals_do_not_split_sentences(self):
        fb = grounding.FactBook()
        fb.add("x", {"say": "about 7.6 million coins"})
        self.assertEqual(rv.review("That is about 7.6 million coins. Fine.", fb).text, "That is about 7.6 million coins. Fine.")

    def test_odd_characters_are_made_voice_friendly(self):
        r = rv.review("The Android US\u2011East failures aren\u2019t fixed. \u201cNo\u201d change \u2013 yet.", grounding.FactBook())
        self.assertEqual(r.text, "The Android US East failures aren't fixed. \"No\" change - yet.")

    def test_empty(self):
        self.assertEqual(rv.review("", self.facts()).text, "")
        self.assertEqual(rv.review(None, self.facts()).text, "")

    def test_events_are_logged_without_the_words(self):
        with self.assertLogs("voice.fraud", level="INFO") as logs:
            rv.log_events([("hallucination", "ungrounded_number"), ("compliance", "promise")])
        text = " ".join(logs.output)
        self.assertIn("hallucination", text)
        self.assertIn("promise", text)


@unittest.skipUnless(HAVE, "install requirements.txt")
class StageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.a = tools.Analyst(analysis.load(path))

    def run_stage(self, frames):
        facts = grounding.FactBook()
        facts.add("get_metric", self.a.dispatch("get_metric", {"metric": "gifters"}))
        guard = output_guard.OutputGuard(facts)
        down, _ = asyncio.run(run_test(guard, frames_to_send=frames + [SleepFrame(sleep=0.15)]))
        return down

    @staticmethod
    def spoken(down):
        return "".join(f.text for f in down if isinstance(f, LLMTextFrame))

    def response(self, *chunks):
        return [LLMFullResponseStartFrame(), *[LLMTextFrame(c) for c in chunks], LLMFullResponseEndFrame()]

    def test_clean_text_streams_through_whole(self):
        down = self.run_stage(self.response("About 21 thou", "sand people ", "have sent a gift. ", "Want more?"))
        self.assertEqual(self.spoken(down).split(), "About 21 thousand people have sent a gift. Want more?".split())

    def test_an_invented_figure_never_reaches_the_voice(self):
        down = self.run_stage(self.response("About 21 thousand people gifted. ", "About 45 thou", "sand were new. ", "Anything else?"))
        said = self.spoken(down)
        self.assertNotIn("45", said)
        self.assertIn(grounding.UNGROUNDED_LINE, said)
        self.assertIn("About 21 thousand people gifted.", said)
        self.assertIn("Anything else?", said)

    def test_a_sentence_split_across_chunks_is_checked_whole(self):
        down = self.run_stage(self.response("About 45 ", "thousand ", "people gifted."))
        self.assertNotIn("45", self.spoken(down))

    def test_an_unfinished_last_sentence_is_still_checked_at_the_end(self):
        down = self.run_stage(self.response("About 45 thousand people gifted"))
        self.assertNotIn("45", self.spoken(down))

    def test_compliance_breaches_are_replaced(self):
        down = self.run_stage(self.response("I guarantee this works. ", "Shall we continue?"))
        said = self.spoken(down)
        self.assertNotIn("guarantee", said)
        self.assertIn(compliance.RESPONSES["promise"], said)

    def test_other_frames_pass_through_untouched(self):
        down = self.run_stage([TTSSpeakFrame("Here is the tool's own exact sentence, 16 percent.")] + self.response("Hello there."))
        self.assertEqual([f.text for f in down if isinstance(f, TTSSpeakFrame)], ["Here is the tool's own exact sentence, 16 percent."])
        self.assertTrue(any(isinstance(f, LLMFullResponseStartFrame) for f in down))
        self.assertTrue(any(isinstance(f, LLMFullResponseEndFrame) for f in down))

    def test_an_interruption_drops_a_half_written_sentence(self):
        down = self.run_stage([LLMFullResponseStartFrame(), LLMTextFrame("About 21 thousand peo"), InterruptionFrame(),
                               LLMFullResponseStartFrame(), LLMTextFrame("Fresh start."), LLMFullResponseEndFrame()])
        said = self.spoken(down)
        self.assertNotIn("peo", said)
        self.assertIn("Fresh start.", said)

    def test_the_guard_logs_the_kind_of_breach_never_the_words(self):
        with self.assertLogs("voice.fraud", level="INFO") as logs:
            self.run_stage(self.response("About 45 thousand people gifted."))
        text = " ".join(logs.output)
        self.assertIn("hallucination", text)
        self.assertNotIn("45", text)


class BrainIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.a = tools.Analyst(analysis.load(path))

    @staticmethod
    def model(*replies):
        it = iter(replies)
        return lambda messages, specs: next(it)

    CALL = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "get_findings", "arguments": '{"limit": 1}'}}]}

    def test_an_invented_number_in_the_models_answer_is_replaced(self):
        model = self.model(self.CALL, {"role": "assistant", "content": "There were 9,999 failed purchases."})
        reply, history = brain.run_turn(model, [], "what should we do?", self.a, facts=grounding.FactBook())
        self.assertEqual(reply, grounding.UNGROUNDED_LINE)
        self.assertEqual(history[-1]["content"], grounding.UNGROUNDED_LINE)         # the history keeps what was actually said

    def test_a_grounded_answer_passes(self):
        facts = grounding.FactBook()
        top = self.a.get_findings(limit=1)["findings"][0]
        said = f"{top['title']}."
        model = self.model(self.CALL, {"role": "assistant", "content": said})
        reply, _ = brain.run_turn(model, [], "what should we do?", self.a, facts=facts)
        self.assertEqual(reply, said)

    def test_an_answer_with_no_tool_cannot_contain_numbers(self):
        model = self.model({"role": "assistant", "content": "Gifting rose 40 percent."})
        reply, _ = brain.run_turn(model, [], "how is gifting?", self.a, facts=grounding.FactBook())
        self.assertEqual(reply, grounding.UNGROUNDED_LINE)

    def test_compliance_applies_in_chat_too(self):
        model = self.model({"role": "assistant", "content": "I guarantee it will recover."})
        reply, _ = brain.run_turn(model, [], "will it recover?", self.a, facts=grounding.FactBook())
        self.assertEqual(reply, compliance.RESPONSES["promise"])

    def test_direct_answers_are_not_second_guessed_and_feed_the_book(self):
        facts = grounding.FactBook()
        call = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "get_metric", "arguments": '{"metric": "gifters"}'}}]}
        reply, _ = brain.run_turn(self.model(call), [], "how many?", self.a, facts=facts)
        self.assertIn("thousand", reply)
        self.assertTrue(facts.check("About 21 thousand people.").ok)                   # the book learned from the tool

    def test_without_a_fact_book_nothing_changes(self):
        model = self.model({"role": "assistant", "content": "Gifting rose 40 percent."})
        self.assertEqual(brain.run_turn(model, [], "x", self.a)[0], "Gifting rose 40 percent.")

    def test_the_chat_session_checks_the_model(self):
        out = []
        model = self.model({"role": "assistant", "content": "Gifting rose 40 percent."})
        chat.session(model, self.a, iter(["how is gifting?", "quit"]), out.append)
        text = "\n".join(out)
        self.assertIn(grounding.UNGROUNDED_LINE, text)
        self.assertNotIn("40 percent", text)
        self.assertIn("[guard]", text)                                                    # the analyst sees that a guard acted


@unittest.skipUnless(HAVE, "install requirements.txt")
class WiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.a = tools.Analyst(analysis.load(path))

    def build(self, **env):
        cfg = config.load({"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d", "ALLOWED_CALLERS": "+15550100100", **env}, env_file=None)
        return pipeline.build(MagicMock(), CallData(stream_id="s", call_id="c", body={}), cfg, self.a)

    def test_the_guard_sits_between_the_model_and_the_voice(self):
        names = [type(p).__name__ for p in self.build().stages]
        self.assertEqual(names[names.index("GroqLLMService") + 1], "OutputGuard")
        self.assertEqual(names[names.index("OutputGuard") + 1], "DeepgramTTSService")

    def test_it_can_be_switched_off_for_troubleshooting(self):
        self.assertNotIn("OutputGuard", [type(p).__name__ for p in self.build(OUTPUT_GUARD="off").stages])

    def test_tool_results_feed_the_same_book_the_guard_reads(self):
        bot = self.build()
        got = []

        async def deliver(result, **kw):
            got.append(result)
        from unittest.mock import AsyncMock
        params = MagicMock(function_name="get_findings", arguments={"limit": 1}, result_callback=deliver)
        params.llm.push_frame = AsyncMock()
        asyncio.run(bot.handlers["get_findings"](params))
        self.assertIs(bot.facts, next(p for p in bot.stages if type(p).__name__ == "OutputGuard")._facts)
        self.assertTrue(bot.facts.check("It affects app version 34.2.").ok)

    def test_each_call_starts_with_an_empty_book(self):
        self.assertFalse(self.build().facts.check("About 21 thousand people.").ok)


class SettingsTest(unittest.TestCase):
    def test_on_by_default_and_switchable(self):
        base = {"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d"}
        self.assertTrue(config.load(base, env_file=None).output_guard)
        self.assertFalse(config.load({**base, "OUTPUT_GUARD": "off"}, env_file=None).output_guard)


if __name__ == "__main__":
    unittest.main()
