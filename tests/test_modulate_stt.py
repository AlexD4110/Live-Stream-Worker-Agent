"""Modulate speech-to-text. A stand-in Modulate server on this computer follows the documented protocol
(query parameters, message shapes, close codes), so the real client code runs without a key or internet."""
import asyncio
import json
import unittest
from urllib.parse import parse_qs, urlparse

from voice import config

try:
    import websockets
    from websockets.asyncio.server import serve
    from pipecat.frames.frames import ErrorFrame, InputAudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame
    from pipecat.pipeline.task import PipelineParams
    from pipecat.tests.utils import SleepFrame, run_test
    HAVE_PIPECAT = True
except ImportError:
    HAVE_PIPECAT = False
if HAVE_PIPECAT:
    from voice import modulate_stt as ms

KEY = "SECRETKEY123"


class UrlTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_PIPECAT, "install requirements.txt")
    def test_url_declares_the_audio_and_turns_endpointing_on(self):
        q = parse_qs(urlparse(ms.build_url(KEY, 16000)).query)
        self.assertEqual({k: v[0] for k, v in q.items()},
                         {"api_key": KEY, "audio_format": "s16le", "sample_rate": "16000",
                          "num_channels": "1", "endpointing": "true"})

    @unittest.skipUnless(HAVE_PIPECAT, "install requirements.txt")
    def test_url_uses_modulates_english_endpoint(self):
        self.assertTrue(ms.build_url(KEY, 16000).startswith("wss://platform.modulate.ai/api/velma-2-stt-streaming-english-v2?"))

    @unittest.skipUnless(HAVE_PIPECAT, "install requirements.txt")
    def test_the_key_can_be_hidden_for_logging(self):
        shown = ms.redact(ms.build_url(KEY, 16000))
        self.assertNotIn(KEY, shown)
        self.assertIn("sample_rate=16000", shown)


@unittest.skipUnless(HAVE_PIPECAT, "install requirements.txt")
class MessageTest(unittest.TestCase):
    def test_partial(self):
        m = json.dumps({"type": "partial_utterance", "partial_utterance": {"text": "hello how", "is_final": False}})
        self.assertEqual(ms.parse_message(m), ("partial", "hello how"))

    def test_final(self):
        m = json.dumps({"type": "utterance", "utterance": {"text": "Hello, how are you?", "is_final": True,
                                                            "start_ms": 0, "duration_ms": 900}})
        self.assertEqual(ms.parse_message(m), ("final", "Hello, how are you?"))

    def test_done_and_error(self):
        self.assertEqual(ms.parse_message(json.dumps({"type": "done", "duration_ms": 1234})), ("done", None))
        self.assertEqual(ms.parse_message(json.dumps({"type": "error", "error": "Invalid audio_format"})),
                         ("error", "Invalid audio_format"))

    def test_blank_unknown_and_broken_messages_are_ignored(self):
        blank = json.dumps({"type": "utterance", "utterance": {"text": "   ", "is_final": True}})
        for raw in (blank, "not json", json.dumps({"type": "something_new"}), json.dumps([1, 2]), b"\x00\x01", ""):
            self.assertEqual(ms.parse_message(raw)[0], "ignore", raw)

    def test_close_codes_become_plain_sentences(self):
        for code, word in [(4001, "API key"), (4003, "not permitted"), (4004, "access"), (4029, "credits"),
                           (4030, "at once"), (4031, "monthly"), (1013, "busy"), (4002, "audio")]:
            self.assertIn(word.lower(), ms.close_reason(code).lower(), code)
        self.assertTrue(ms.close_reason(1006))

    def test_which_closes_are_worth_retrying(self):
        for code in (1006, 1011, 1013, 1001):
            self.assertTrue(ms.retryable(code), code)
        for code in (4001, 4003, 4004, 4029, 4031, 4002, 1003):
            self.assertFalse(ms.retryable(code), code)


class FakeModulate:
    """Accepts connections, records what the client sends, and plays a script on the first audio of each connection."""

    def __init__(self, on_audio=None, close_after_accept=None):
        self.connections, self.on_audio, self.close_after_accept = [], on_audio, close_after_accept

    async def handler(self, ws):
        idx = len(self.connections)
        conn = {"query": {k: v[0] for k, v in parse_qs(urlparse(ws.request.path).query).items()},
                "path": urlparse(ws.request.path).path, "bytes": 0, "text": []}
        self.connections.append(conn)
        if self.close_after_accept is not None:
            await ws.close(code=self.close_after_accept)
            return
        first = True
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    conn["bytes"] += len(msg)
                    if first and self.on_audio:
                        first = False
                        await self.on_audio(ws, idx)
                else:
                    conn["text"].append(msg)
                    if msg == "":
                        await ws.send(json.dumps({"type": "done", "duration_ms": 1000}))
                        await ws.close()
        except websockets.ConnectionClosed:
            pass

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/api/velma-2-stt-streaming-english-v2"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


def audio(ms_long=20):
    return InputAudioRawFrame(audio=bytes(16000 * 2 * ms_long // 1000), sample_rate=16000, num_channels=1)


def say(msg_type, text):
    key = "utterance" if msg_type == "utterance" else "partial_utterance"
    return json.dumps({"type": msg_type, key: {"text": text, "is_final": msg_type == "utterance", "start_ms": 0, "duration_ms": 500}})


async def drive(fake, frames, **service_args):
    svc = ms.ModulateSTTService(api_key=KEY, base_url=fake.url, **service_args)
    down, up = await run_test(svc, frames_to_send=frames, pipeline_params=PipelineParams(audio_in_sample_rate=16000),
                              send_end_frame=True)
    return svc, down, up


@unittest.skipUnless(HAVE_PIPECAT, "install requirements.txt")
class ServiceTest(unittest.TestCase):
    def setUp(self):
        ms.ModulateSTTService.RECONNECT_DELAY = 0.0

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_audio_is_sent_with_the_declared_format_and_the_stream_is_closed_properly(self):
        async def go():
            async with FakeModulate() as fake:
                await drive(fake, [audio(), audio(), audio(), SleepFrame(sleep=0.3)])
                return fake.connections
        conns = self.run_async(go())
        self.assertEqual(len(conns), 1)
        c = conns[0]
        self.assertEqual(c["query"]["api_key"], KEY)
        self.assertEqual((c["query"]["audio_format"], c["query"]["sample_rate"], c["query"]["num_channels"],
                          c["query"]["endpointing"]), ("s16le", "16000", "1", "true"))
        self.assertEqual(c["bytes"], 3 * 640)
        self.assertEqual(c["text"], [""])                       # the documented end-of-audio signal

    def test_partials_and_finals_become_transcription_frames(self):
        async def on_audio(ws, idx):
            await ws.send(say("partial_utterance", "how many"))
            await ws.send(say("utterance", "How many gifters are there?"))
            await ws.send(say("utterance", "   "))               # blank: must be ignored

        async def go():
            async with FakeModulate(on_audio) as fake:
                return await drive(fake, [audio(), SleepFrame(sleep=0.4)])
        _, down, _ = self.run_async(go())
        interim = [f.text for f in down if isinstance(f, InterimTranscriptionFrame)]
        final = [f.text for f in down if isinstance(f, TranscriptionFrame)]
        self.assertEqual(interim, ["how many"])
        self.assertEqual(final, ["How many gifters are there?"])

    def test_audio_still_passes_downstream(self):
        async def go():
            async with FakeModulate() as fake:
                return await drive(fake, [audio(), SleepFrame(sleep=0.2)])
        _, down, _ = self.run_async(go())
        self.assertTrue(any(isinstance(f, InputAudioRawFrame) for f in down))

    def test_a_server_error_stops_the_service_without_leaking_the_key(self):
        async def on_audio(ws, idx):
            await ws.send(json.dumps({"type": "error", "error": "Invalid audio_format='xyz'."}))

        async def go():
            async with FakeModulate(on_audio) as fake:
                return await drive(fake, [audio(), SleepFrame(sleep=0.4)])
        svc, _, up = self.run_async(go())
        errors = [f for f in up if isinstance(f, ErrorFrame)]
        self.assertTrue(errors)
        self.assertFalse(svc.is_usable)
        self.assertNotIn(KEY, " ".join(f.error for f in errors))

    def test_a_rejected_key_is_explained_in_plain_words(self):
        async def go():
            async with FakeModulate(close_after_accept=4001) as fake:
                return await drive(fake, [audio(), SleepFrame(sleep=0.4)])
        svc, _, up = self.run_async(go())
        text = " ".join(f.error for f in up if isinstance(f, ErrorFrame))
        self.assertIn("API key", text)
        self.assertNotIn(KEY, text)
        self.assertFalse(svc.is_usable)

    def test_out_of_credits_is_explained(self):
        async def go():
            async with FakeModulate(close_after_accept=4029) as fake:
                return await drive(fake, [audio(), SleepFrame(sleep=0.4)])
        _, _, up = self.run_async(go())
        self.assertIn("credits", " ".join(f.error for f in up if isinstance(f, ErrorFrame)).lower())

    def test_a_dropped_connection_is_retried_and_the_call_carries_on(self):
        async def on_audio(ws, idx):
            if idx == 0:
                await ws.close(code=1011)                        # a blip on the first connection
            else:
                await ws.send(say("utterance", "Welcome back."))

        async def go():
            async with FakeModulate(on_audio) as fake:
                res = await drive(fake, [audio(), SleepFrame(sleep=0.4), audio(), SleepFrame(sleep=0.4)])
                return fake.connections, res
        conns, (svc, down, _) = self.run_async(go())
        self.assertEqual(len(conns), 2)
        self.assertTrue(svc.is_usable)
        self.assertEqual([f.text for f in down if isinstance(f, TranscriptionFrame)], ["Welcome back."])

    def test_it_gives_up_after_a_few_failed_reconnects(self):
        async def go():
            async with FakeModulate(close_after_accept=1011) as fake:
                res = await drive(fake, [audio(), SleepFrame(sleep=0.8)])
                return fake.connections, res
        conns, (svc, _, up) = self.run_async(go())
        self.assertEqual(len(conns), 1 + ms.MAX_RECONNECTS)
        self.assertFalse(svc.is_usable)
        self.assertTrue(any(isinstance(f, ErrorFrame) for f in up))

    def test_an_unreachable_server_ends_the_call_instead_of_hanging(self):
        async def go():
            svc = ms.ModulateSTTService(api_key=KEY, base_url="ws://127.0.0.1:9/nothing-here")
            return svc, *await run_test(svc, frames_to_send=[audio(), SleepFrame(sleep=0.8)],
                                        pipeline_params=PipelineParams(audio_in_sample_rate=16000))
        svc, _, up = self.run_async(go())
        self.assertFalse(svc.is_usable)
        self.assertNotIn(KEY, " ".join(f.error for f in up if isinstance(f, ErrorFrame)))


class ConfigAndWiringTest(unittest.TestCase):
    BASE = {"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d", "ALLOWED_CALLERS": "+15550100100"}

    def test_modulate_is_used_when_its_key_is_set(self):
        c = config.load({**self.BASE, "MODULATE_API_KEY": "m"}, env_file=None)
        self.assertEqual(c.stt_provider, "modulate")

    def test_deepgram_is_the_fallback_without_a_key(self):
        self.assertEqual(config.load(self.BASE, env_file=None).stt_provider, "deepgram")

    def test_it_can_be_switched_off(self):
        c = config.load({**self.BASE, "MODULATE_API_KEY": "m", "STT_PROVIDER": "deepgram"}, env_file=None)
        self.assertEqual(c.stt_provider, "deepgram")

    def test_choosing_modulate_without_a_key_is_a_problem(self):
        c = config.load({**self.BASE, "STT_PROVIDER": "modulate"}, env_file=None)
        self.assertTrue(any("MODULATE_API_KEY" in p for p in config.problems(c)))

    def test_the_key_is_hidden_from_repr(self):
        self.assertNotIn("'m'", repr(config.load({**self.BASE, "MODULATE_API_KEY": "m"}, env_file=None)))

    @unittest.skipUnless(HAVE_PIPECAT, "install requirements.txt")
    def test_the_pipeline_listens_with_modulate(self):
        import os
        import tempfile
        from unittest.mock import MagicMock
        from pipecat.runner.types import CallData
        from gifting import analysis, generate
        from voice import pipeline, tools
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        analyst = tools.Analyst(analysis.load(path))
        call = CallData(stream_id="MZ1", call_id="CA1", body={})
        with_m = pipeline.build(MagicMock(), call, config.load({**self.BASE, "MODULATE_API_KEY": "m"}, env_file=None), analyst)
        without = pipeline.build(MagicMock(), call, config.load(self.BASE, env_file=None), analyst)
        self.assertIn("ModulateSTTService", [type(p).__name__ for p in with_m.stages])
        self.assertEqual(type(without.stages[1]).__name__, "DeepgramSTTService")
        self.assertEqual(with_m.task._params.audio_in_sample_rate, 16000)
        self.assertEqual(with_m.task._params.audio_out_sample_rate, 8000)


if __name__ == "__main__":
    unittest.main()
