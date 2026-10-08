"""Talk to the agent from the laptop's microphone in a browser: the same pipeline as the phone line, minus Plivo."""
import asyncio
import inspect
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from gifting import analysis, generate
from voice import config, tools

try:
    from pipecat.runner.types import CallData
    import aiortc  # noqa: F401  (the browser mode needs it)
    HAVE = True
except ImportError:                  # only third-party packages may be missing; errors in our own code must show
    HAVE = False
if HAVE:
    from voice import pipeline, talk


def names(bot):
    return [type(p).__name__ for p in bot.stages]


@unittest.skipUnless(HAVE, "install requirements.txt")
class SharedPipelineTest(unittest.TestCase):
    @staticmethod
    def fresh_transport():
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
        return FastAPIWebsocketTransport(websocket=MagicMock(), params=FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True))

    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.analyst = tools.Analyst(analysis.load(path))

    def cfg(self, **env):
        return config.load({"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d", "ALLOWED_CALLERS": "+15550100100", **env}, env_file=None)

    def test_the_phone_and_the_browser_share_one_assembly(self):
        phone = pipeline.build(MagicMock(), CallData(stream_id="s", call_id="c", body={}), self.cfg(MODULATE_API_KEY="m"), self.analyst)
        browser = pipeline.assemble(self.fresh_transport(), self.cfg(MODULATE_API_KEY="m"), self.analyst, in_hz=16000, out_hz=24000)
        self.assertEqual(names(browser), names(phone))

    def test_the_browser_hears_at_16k_and_speaks_at_24k_but_the_phone_speaks_at_8k(self):
        phone = pipeline.build(MagicMock(), CallData(stream_id="s", call_id="c", body={}), self.cfg(), self.analyst)
        browser = pipeline.assemble(self.fresh_transport(), self.cfg(), self.analyst, in_hz=16000, out_hz=24000)
        self.assertEqual((phone.task._params.audio_in_sample_rate, phone.task._params.audio_out_sample_rate), (16000, 8000))
        self.assertEqual((browser.task._params.audio_in_sample_rate, browser.task._params.audio_out_sample_rate), (16000, 24000))

    def test_the_guards_and_tools_come_along(self):
        browser = pipeline.build(MagicMock(), CallData(stream_id="s", call_id="c", body={}), self.cfg(MODULATE_API_KEY="m"), self.analyst)
        self.assertIn("SyntheticVoiceGuard", names(browser))
        self.assertIn("SocialEngineeringGuard", names(browser))
        self.assertEqual(sorted(browser.handlers), sorted(s["name"] for s in tools.TOOL_SPECS))


@unittest.skipUnless(HAVE, "install requirements.txt")
class TalkTest(unittest.TestCase):
    def test_the_runner_can_find_the_bot(self):
        self.assertTrue(inspect.iscoroutinefunction(talk.bot))

    def test_it_needs_the_brain_and_the_voice_but_not_the_phone(self):
        problems = " ".join(talk.problems(config.load({}, env_file=None)))
        self.assertIn("GROQ_API_KEY", problems)
        self.assertIn("DEEPGRAM_API_KEY", problems)
        self.assertNotIn("ALLOWED_CALLERS", problems)
        self.assertNotIn("PLIVO", problems)

    def test_ready_when_those_are_set(self):
        self.assertEqual(talk.problems(config.load({"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d"}, env_file=None)), [])

    def test_main_stops_with_a_clear_message_when_not_ready(self):
        with self.assertRaises(SystemExit) as cm:
            talk.main(config.load({}, env_file=None), run=lambda: self.fail("must not start"))
        self.assertNotEqual(cm.exception.code, 0)

    def test_main_starts_the_runner_when_ready(self):
        started = []
        talk.main(config.load({"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d"}, env_file=None), run=lambda: started.append(1))
        self.assertEqual(started, [1])

    def test_the_browser_session_uses_the_shared_assembly_and_does_not_leave_the_analyst_restricted(self):
        analyst = tools.Analyst({"days": 1})
        analyst.restricted = True
        bot = talk.assemble_for_browser(SharedPipelineTest.fresh_transport(), config.load({"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d"}, env_file=None), analyst)
        self.assertFalse(analyst.restricted)
        self.assertEqual(bot.task._params.audio_out_sample_rate, 24000)


if __name__ == "__main__":
    unittest.main()
