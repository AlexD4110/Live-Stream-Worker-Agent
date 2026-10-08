"""The voice pipeline is wired correctly. Built with placeholder keys; nothing connects to the internet."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from gifting import analysis, generate
from voice import brain, briefing, config, tools

try:
    from pipecat.runner.types import CallData
    HAVE_PIPECAT = True
except ImportError:                   # only the third-party package may be missing; errors in our own code must show
    HAVE_PIPECAT = False
if HAVE_PIPECAT:
    from voice import pipeline


@unittest.skipUnless(HAVE_PIPECAT, "install requirements.txt to run the pipeline tests")
class PipelineBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.analyst = tools.Analyst(analysis.load(path))

    def build(self, **env):
        base = {"GROQ_API_KEY": "placeholder", "DEEPGRAM_API_KEY": "placeholder", "ALLOWED_CALLERS": "+15550100100"}
        cfg = config.load({**base, **env}, env_file=None)
        return pipeline.build(MagicMock(), CallData(stream_id="MZ1", call_id="CA1", body={}), cfg, self.analyst)

    def test_the_stages_are_in_order(self):
        names = [type(p).__name__ for p in self.build().stages]
        self.assertEqual(names, ["FastAPIWebsocketInputTransport", "DeepgramSTTService", "SocialEngineeringGuard",
                                 "LLMUserAggregator", "GroqLLMService", "OutputGuard", "DeepgramTTSService",
                                 "FastAPIWebsocketOutputTransport", "LLMAssistantAggregator"])

    def test_with_modulate_the_cloned_voice_guard_listens_first(self):
        names = [type(p).__name__ for p in self.build(MODULATE_API_KEY="m").stages]
        self.assertEqual(names, ["FastAPIWebsocketInputTransport", "SyntheticVoiceGuard", "ModulateSTTService",
                                 "SocialEngineeringGuard", "LLMUserAggregator", "GroqLLMService", "OutputGuard",
                                 "DeepgramTTSService", "FastAPIWebsocketOutputTransport", "LLMAssistantAggregator"])

    def test_the_cloned_voice_guard_can_be_switched_off(self):
        names = [type(p).__name__ for p in self.build(MODULATE_API_KEY="m", FRAUD_GUARD="off").stages]
        self.assertNotIn("SyntheticVoiceGuard", names)
        self.assertIn("SocialEngineeringGuard", names)            # the local guard is always on

    def test_the_guard_shares_the_analyst_and_the_settings(self):
        bot = self.build(MODULATE_API_KEY="m", FRAUD_SYNTHETIC_THRESHOLD="0.8", FRAUD_SYNTHETIC_WINDOWS="3", FRAUD_FAIL_CLOSED="yes")
        guard = bot.stages[1]
        self.assertIs(guard._analyst, self.analyst)
        self.assertEqual((guard.monitor.threshold, guard.monitor.windows_to_block, guard._fail_closed), (0.8, 3, True))

    def test_a_new_call_never_starts_restricted(self):
        self.analyst.restricted = True                              # left over from an earlier call
        self.build()
        self.assertFalse(self.analyst.restricted)

    def test_the_model_gets_the_rules_and_every_tool(self):
        bot = self.build()
        self.assertEqual(bot.context.get_messages()[0]["content"], brain.SYSTEM_PROMPT)
        self.assertEqual([s.name for s in bot.context.tools.standard_tools], [s["name"] for s in tools.TOOL_SPECS])
        for spec in tools.TOOL_SPECS:
            self.assertIn(spec["name"], bot.llm._functions)

    def test_phone_audio_is_upsampled_for_listening_and_spoken_at_phone_rate(self):
        bot = self.build()
        self.assertEqual(bot.task._params.audio_in_sample_rate, 16000)    # what the speech-to-text and Silero hear
        self.assertEqual(bot.task._params.audio_out_sample_rate, 8000)    # what the phone line plays

    def test_the_phone_line_speaks_plivos_protocol(self):
        serializer = self.build().transport._params.serializer
        self.assertEqual(type(serializer).__name__, "PlivoFrameSerializer")

    def test_it_can_hang_up_the_call_itself_only_when_it_has_plivo_credentials(self):
        without = self.build().transport._params.serializer._params.auto_hang_up
        with_creds = self.build(PLIVO_AUTH_ID="id", PLIVO_AUTH_TOKEN="tok").transport._params.serializer._params.auto_hang_up
        self.assertFalse(without)
        self.assertTrue(with_creds)

    def test_the_greeting_discloses_the_ai(self):
        self.assertIs(self.build().greeting, briefing.GREETING)

    def test_a_reasoning_model_is_set_to_think_briefly(self):
        llm = self.build().llm
        self.assertEqual(llm._settings.model, "openai/gpt-oss-120b")
        self.assertEqual(llm._settings.reasoning_effort, "low")
        self.assertGreaterEqual(llm._settings.max_tokens, 500)

    def test_a_plain_model_gets_no_reasoning_setting(self):
        llm = self.build(GROQ_MODEL="some-plain-model").llm
        self.assertIsNone(llm._settings.reasoning_effort)          # None means "unset": Groq's own default applies
        self.assertLessEqual(llm._settings.max_tokens, 400)

    def test_config_choices_reach_the_services(self):
        bot = self.build(GROQ_MODEL="llama-3.1-8b-instant", DEEPGRAM_TTS_VOICE="aura-2-luna-en")
        self.assertEqual(bot.llm._settings.model, "llama-3.1-8b-instant")
        tts = next(p for p in bot.stages if type(p).__name__ == "DeepgramTTSService")
        self.assertEqual(tts._settings.voice, "aura-2-luna-en")

    def test_a_tool_handler_returns_the_analysts_answer(self):
        from unittest.mock import AsyncMock
        bot, got = self.build(), []

        async def deliver(result, **kw):
            got.append(result)
        params = MagicMock(function_name="get_findings", arguments={"limit": 1}, result_callback=deliver)
        params.llm.push_frame = AsyncMock()
        asyncio.run(bot.handlers["get_findings"](params))
        self.assertEqual(got, [self.analyst.get_findings(limit=1)])

    def test_the_call_ends_if_a_voice_service_dies(self):
        from unittest.mock import AsyncMock
        bot = self.build()
        bot.task.cancel = AsyncMock()
        dead = MagicMock()
        dead.processor.is_usable = False
        asyncio.run(bot.on_error(bot.task, dead))
        bot.task.cancel.assert_awaited_once()

    def test_a_minor_error_does_not_end_the_call(self):
        from unittest.mock import AsyncMock
        bot = self.build()
        bot.task.cancel = AsyncMock()
        minor = MagicMock()
        minor.processor.is_usable = True
        asyncio.run(bot.on_error(bot.task, minor))
        bot.task.cancel.assert_not_awaited()

    def test_a_direct_tool_is_spoken_straight_away_without_the_model(self):
        from unittest.mock import AsyncMock
        from pipecat.frames.frames import TTSSpeakFrame
        bot, calls = self.build(), []

        async def deliver(result, **kw):
            calls.append((result, kw))
        params = MagicMock(function_name="get_metric", arguments={"metric": "gifters"}, result_callback=deliver)
        params.llm.push_frame = AsyncMock()
        asyncio.run(bot.handlers["get_metric"](params))
        result, kw = calls[0]
        self.assertFalse(kw["properties"].run_llm)                             # do not ask the model to speak
        spoken = params.llm.push_frame.await_args.args[0]
        self.assertIsInstance(spoken, TTSSpeakFrame)
        self.assertEqual(spoken.text, tools.speak_line(result["say"]))

    def test_the_same_line_is_not_spoken_twice_if_the_model_repeats_a_call(self):
        from unittest.mock import AsyncMock
        bot = self.build()

        async def deliver(result, **kw):
            pass
        params = MagicMock(function_name="get_lapsed_gifters", arguments={}, result_callback=deliver)
        params.llm.push_frame = AsyncMock()

        async def twice():
            await bot.handlers["get_lapsed_gifters"](params)
            await bot.handlers["get_lapsed_gifters"](params)
        asyncio.run(twice())
        self.assertEqual(params.llm.push_frame.await_count, 1)

    def test_a_tool_that_needs_the_model_still_goes_to_it(self):
        from unittest.mock import AsyncMock
        bot, calls = self.build(), []

        async def deliver(result, **kw):
            calls.append((result, kw))
        params = MagicMock(function_name="get_findings", arguments={"limit": 1}, result_callback=deliver)
        params.llm.push_frame = AsyncMock()
        asyncio.run(bot.handlers["get_findings"](params))
        self.assertNotIn("properties", calls[0][1])
        params.llm.push_frame.assert_not_awaited()

    def test_a_bad_tool_call_gets_an_error_not_a_crash(self):
        bot, got = self.build(), []

        async def deliver(result, **kw):
            got.append(result)
        params = MagicMock(function_name="get_metric", arguments={"metric": "nope"}, result_callback=deliver)
        asyncio.run(bot.handlers["get_metric"](params))
        self.assertIn("error", got[0])


try:
    import loguru
    HAVE_LOGURU = True
except ImportError:
    HAVE_LOGURU = False


@unittest.skipUnless(HAVE_LOGURU, "install requirements.txt to run the logging test")
class LoggingTest(unittest.TestCase):
    def test_nothing_the_caller_says_can_reach_the_logs(self):
        import io
        from loguru import logger
        from voice import logs
        buf = io.StringIO()
        logs.configure(sink=buf)
        logger.debug("transcript: how many gifters")
        logger.info("tts: about 21 thousand people")
        logger.warning("a problem")
        self.assertNotIn("transcript", buf.getvalue())
        self.assertNotIn("21 thousand", buf.getvalue())
        self.assertIn("a problem", buf.getvalue())

    def test_spoken_text_in_warnings_is_hidden(self):
        import io
        from loguru import logger
        from voice import logs
        buf = io.StringIO()
        logs.configure(sink=buf)
        logger.warning("DeepgramTTSService#0: not speaking [the secret words the agent said] right now")
        out = buf.getvalue()
        self.assertIn("not speaking", out)
        self.assertNotIn("secret words", out)

    def test_long_messages_are_cut_short(self):
        import io
        from loguru import logger
        from voice import logs
        buf = io.StringIO()
        logs.configure(sink=buf)
        logger.error("x" * 5000)
        self.assertLess(len(buf.getvalue()), 400)


if __name__ == "__main__":
    unittest.main()
