"""The conversation loop and the rules the AI model is given. A fake model stands in for Groq, so no key is needed."""
import json
import os
import tempfile
import unittest

from gifting import analysis, generate
from voice import brain, briefing, tools


def tool_call(name, args, call_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


class FakeModel:
    """Plays back a script of replies and remembers what it was sent."""

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def __call__(self, messages, tool_specs):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0)


class BrainBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.a = tools.Analyst(analysis.load(path))


class PromptTest(unittest.TestCase):
    def test_the_rules_the_agent_must_follow_are_in_the_prompt(self):
        p = brain.SYSTEM_PROMPT.lower()
        for must in ("only", "tool", "synthetic", "can't answer", "two sentences", "no markdown", "brief me",
                     "word for word", "trust and safety", "not a verdict", "benchmark", "allegation", "minor"):
            self.assertIn(must, p, f"prompt is missing: {must}")

    def test_prompt_forbids_the_things_it_must_not_do(self):
        p = brain.SYSTEM_PROMPT.lower()
        for rule in ("never calculate", "never state a number that did not come from a tool", "never read out account ids",
                     "never promise", "never encourage"):
            self.assertIn(rule, p, f"prompt is missing: {rule}")


class SchemaTest(unittest.TestCase):
    def test_openai_style_tool_list(self):
        listing = brain.openai_tools()
        self.assertEqual([t["function"]["name"] for t in listing], [s["name"] for s in tools.TOOL_SPECS])
        for t in listing:
            self.assertEqual(t["type"], "function")
            params = t["function"]["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertTrue(set(params["required"]) <= set(params["properties"]))

    def test_briefing_is_a_tool(self):
        self.assertIn("get_briefing", [s["name"] for s in tools.TOOL_SPECS])


class RequestTest(unittest.TestCase):
    def test_request_body(self):
        body = brain.build_request("some-model", [{"role": "user", "content": "hi"}])
        self.assertEqual(body["model"], "some-model")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"], brain.SYSTEM_PROMPT)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertLessEqual(body["temperature"], 0.3)
        self.assertLessEqual(body["max_tokens"], 400)
        self.assertEqual(len(body["tools"]), len(tools.TOOL_SPECS))


class ModelChoiceTest(unittest.TestCase):
    def test_reasoning_models_are_asked_to_think_briefly_and_given_room(self):
        body = brain.build_request("openai/gpt-oss-120b", [{"role": "user", "content": "hi"}])
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertGreaterEqual(body["max_tokens"], 500)          # thinking counts against the limit
        self.assertEqual(brain.build_request("qwen/qwen3.8-27b", [])["reasoning_effort"], "none")

    def test_other_models_get_no_reasoning_setting(self):
        body = brain.build_request("some-other-model", [])
        self.assertNotIn("reasoning_effort", body)
        self.assertLessEqual(body["max_tokens"], 400)

    def test_helpers_agree(self):
        self.assertEqual(brain.reasoning_effort("openai/gpt-oss-20b"), "low")
        self.assertIsNone(brain.reasoning_effort("llama-whatever"))
        self.assertGreater(brain.max_tokens_for("openai/gpt-oss-120b"), brain.max_tokens_for("llama-whatever"))

    def test_the_default_model_is_one_groq_currently_offers(self):
        self.assertEqual(brain.DEFAULT_MODEL, "openai/gpt-oss-120b")


class GroqErrorTest(unittest.TestCase):
    def fail_with(self, code):
        import io
        import urllib.error
        from unittest.mock import patch
        err = urllib.error.HTTPError("https://api.groq.com/x", code, "msg", {}, io.BytesIO(b"{}"))
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(brain.BrainError) as cm:
                brain._post(brain.GROQ_URL, "gsk_SECRET", {})
        return str(cm.exception)

    def test_a_missing_model_is_explained(self):
        msg = self.fail_with(404)
        self.assertIn("GROQ_MODEL", msg)
        self.assertIn("model", msg.lower())

    def test_other_errors_keep_their_hints_and_never_show_the_key(self):
        for code, word in [(401, "key"), (429, "limit")]:
            msg = self.fail_with(code)
            self.assertIn(word, msg)
            self.assertNotIn("SECRET", msg)


class LoopTest(BrainBase):
    def test_plain_answer_needs_no_tool(self):
        model = FakeModel(say("Hello."))
        reply, history = brain.run_turn(model, [], "hi", self.a)
        self.assertEqual(reply, "Hello.")
        self.assertEqual(history[-2:], [{"role": "user", "content": "hi"}, say("Hello.")])

    def test_tool_call_result_goes_back_to_the_model(self):
        model = FakeModel(tool_call("get_findings", {"limit": 1}), say("About 21 thousand people have gifted."))
        reply, history = brain.run_turn(model, [], "how many gifters?", self.a)
        self.assertEqual(reply, "About 21 thousand people have gifted.")
        tool_msg = next(m for m in history if m["role"] == "tool")
        self.assertEqual(tool_msg["tool_call_id"], "c1")
        self.assertEqual(json.loads(tool_msg["content"])["total_found"], self.a.get_findings(limit=1)["total_found"])
        self.assertEqual(len(model.seen), 2)               # asked, got a tool call, asked again with the result

    def test_a_bad_tool_call_is_reported_back_not_raised(self):
        model = FakeModel(tool_call("get_metric", {"metric": "vibes"}), say("I don't have that metric."))
        reply, history = brain.run_turn(model, [], "how are the vibes?", self.a)
        tool_msg = next(m for m in history if m["role"] == "tool")
        self.assertIn("error", json.loads(tool_msg["content"]))
        self.assertEqual(reply, "I don't have that metric.")

    def test_unparseable_arguments_are_reported_back(self):
        bad = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "get_metric", "arguments": "{not json"}}]}
        model = FakeModel(bad, say("Sorry, let me try that again."))
        reply, history = brain.run_turn(model, [], "x", self.a)
        self.assertIn("error", json.loads(next(m for m in history if m["role"] == "tool")["content"]))

    def test_several_tool_calls_in_one_reply(self):
        two = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "get_metric", "arguments": '{"metric": "gifters"}'}},
            {"id": "b", "type": "function", "function": {"name": "get_lapsed_gifters", "arguments": "{}"}}]}
        model = FakeModel(two)
        _, history = brain.run_turn(model, [], "x", self.a)
        self.assertEqual([m["tool_call_id"] for m in history if m["role"] == "tool"], ["a", "b"])

    def test_a_model_that_keeps_calling_tools_is_stopped(self):
        model = FakeModel(*[tool_call("get_findings", {"limit": 1}, f"c{i}") for i in range(20)])
        reply, _ = brain.run_turn(model, [], "loop forever", self.a)
        self.assertIn("can't", reply.lower())
        self.assertLessEqual(len(model.seen), brain.MAX_TOOL_ROUNDS + 1)

    def test_the_briefing_tool_returns_the_exact_script(self):
        model = FakeModel(tool_call("get_briefing", {}))
        _, history = brain.run_turn(model, [], "brief me", self.a)
        result = json.loads(next(m for m in history if m["role"] == "tool")["content"])
        self.assertEqual(result["script"], briefing.build(self.a.D)["script"])
        self.assertIn("word for word", result["instruction"].lower())

    def test_conversation_history_is_kept_between_turns(self):
        model = FakeModel(say("One."), say("Two."))
        _, h = brain.run_turn(model, [], "first", self.a)
        _, h = brain.run_turn(model, h, "second", self.a)
        self.assertEqual([m["content"] for m in h if m["role"] == "user"], ["first", "second"])


class DirectAnswerLoopTest(BrainBase):
    def test_a_direct_tool_is_spoken_without_a_second_model_call(self):
        model = FakeModel(tool_call("get_metric", {"metric": "gifters"}))      # a second reply would raise: none is scripted
        reply, history = brain.run_turn(model, [], "how many gifters?", self.a)
        self.assertEqual(reply, tools.speak_line(self.a.get_metric("gifters")["say"]))
        self.assertEqual(len(model.seen), 1)
        self.assertEqual(history[-1], {"role": "assistant", "content": reply})
        self.assertEqual(history[-2]["role"], "tool")                          # the result is still in the history

    def test_several_direct_tools_are_joined(self):
        two = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "get_metric", "arguments": '{"metric": "gifters"}'}},
            {"id": "b", "type": "function", "function": {"name": "get_lapsed_gifters", "arguments": "{}"}}]}
        reply, _ = brain.run_turn(FakeModel(two), [], "x", self.a)
        self.assertIn(tools.speak_line(self.a.get_metric("gifters")["say"]), reply)
        self.assertIn(tools.speak_line(self.a.get_lapsed_gifters()["say"]), reply)

    def test_the_same_line_is_never_spoken_twice(self):
        twice = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "get_lapsed_gifters", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "get_lapsed_gifters", "arguments": "{}"}}]}
        reply, history = brain.run_turn(FakeModel(twice), [], "x", self.a)
        line = tools.speak_line(self.a.get_lapsed_gifters()["say"])
        self.assertEqual(reply, line)
        self.assertEqual([m["tool_call_id"] for m in history if m["role"] == "tool"], ["a", "b"])    # both calls still answered

    def test_findings_still_go_back_to_the_model(self):
        model = FakeModel(tool_call("get_findings", {"limit": 1}), say("The top issue is the purchase bug."))
        reply, _ = brain.run_turn(model, [], "what should we do?", self.a)
        self.assertEqual(reply, "The top issue is the purchase bug.")
        self.assertEqual(len(model.seen), 2)

    def test_a_mix_goes_back_to_the_model(self):
        mixed = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "get_metric", "arguments": '{"metric": "gifters"}'}},
            {"id": "b", "type": "function", "function": {"name": "get_findings", "arguments": "{}"}}]}
        model = FakeModel(mixed, say("Combined answer."))
        self.assertEqual(brain.run_turn(model, [], "x", self.a)[0], "Combined answer.")

    def test_an_error_goes_back_to_the_model_to_explain(self):
        model = FakeModel(tool_call("get_metric", {"metric": "vibes"}), say("I don't have that metric."))
        self.assertEqual(brain.run_turn(model, [], "x", self.a)[0], "I don't have that metric.")
        self.assertEqual(len(model.seen), 2)

    def test_the_briefing_is_read_word_for_word_without_the_model(self):
        model = FakeModel(tool_call("get_briefing", {}))
        reply, _ = brain.run_turn(model, [], "brief me", self.a)
        self.assertEqual(reply, briefing.build(self.a.D)["script"])
        self.assertEqual(len(model.seen), 1)


class TrimTest(unittest.TestCase):
    def test_long_histories_are_trimmed_without_splitting_a_tool_exchange(self):
        h = []
        for i in range(30):
            h += [{"role": "user", "content": f"q{i}"}, tool_call("get_metric", {"metric": "gifters"}, f"c{i}"),
                  {"role": "tool", "tool_call_id": f"c{i}", "content": "{}"}, say(f"a{i}")]
        short = brain.trim_history(h, keep_turns=3)
        self.assertEqual(short[0]["role"], "user")
        self.assertEqual(sum(m["role"] == "user" for m in short), 3)
        ids = {m["tool_call_id"] for m in short if m["role"] == "tool"}
        called = {c["id"] for m in short if m.get("tool_calls") for c in m["tool_calls"]}
        self.assertEqual(ids, called)


if __name__ == "__main__":
    unittest.main()
