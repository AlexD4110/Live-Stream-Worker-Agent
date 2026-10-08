"""Modulate (Velma) streaming speech-to-text for Pipecat.

Sends the caller's audio to Modulate over a WebSocket and turns what comes back into Pipecat transcription frames.
Protocol (from docs.modulate.ai): connect to the English streaming endpoint with the key and audio format in the
query string; send audio as binary frames; get `partial_utterance` (rolling text, replace don't append) and, with
endpointing on, one `utterance` per pause; end the stream with an empty text frame.

Needs MODULATE_API_KEY. The key travels in the URL as Modulate requires, so URLs are never logged unredacted.
"""
import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from pipecat.frames.frames import Frame, InterimTranscriptionFrame, StartFrame, TranscriptionFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

log = logging.getLogger(__name__)

BASE_URL = "wss://platform.modulate.ai/api/velma-2-stt-streaming-english-v2"
MODEL = "velma-2-stt-streaming-english-v2"
MAX_RECONNECTS = 3
# How long after the caller stops talking a final transcript usually arrives. An unmeasured starting guess:
# tune it after a real call (Pipecat uses it to time when the caller's turn is over).
MODULATE_TTFS_P99 = 1.0


def build_url(api_key, sample_rate, base_url=BASE_URL, endpointing=True):
    query = {"api_key": api_key, "audio_format": "s16le", "sample_rate": sample_rate, "num_channels": 1,
             "endpointing": "true" if endpointing else "false"}
    return f"{base_url}?{urlencode(query)}"


def redact(url):
    return re.sub(r"api_key=[^&]*", "api_key=hidden", url)


def parse_message(raw):
    """One server message as (kind, text): partial, final, done, error, or ignore."""
    if isinstance(raw, (bytes, bytearray)):
        return ("ignore", None)
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return ("ignore", None)
    if not isinstance(msg, dict):
        return ("ignore", None)
    kind = msg.get("type")
    if kind in ("partial_utterance", "utterance"):
        body = msg.get(kind)
        text = (body.get("text") if isinstance(body, dict) else "") or ""
        if not text.strip():
            return ("ignore", None)
        return ("partial" if kind == "partial_utterance" else "final", text.strip())
    if kind == "done":
        return ("done", None)
    if kind == "error":
        return ("error", str(msg.get("error") or "unknown error"))
    return ("ignore", None)


_REASONS = {
    4001: "Modulate rejected the API key (missing or invalid).",
    4002: "Modulate could not read the audio that was sent.",
    4003: "Modulate says this request is not permitted for the key.",
    4004: "The Modulate key does not have access to this speech model.",
    4029: "Modulate says the account is out of credits.",
    4030: "Modulate says there are too many connections open at once for this key.",
    4031: "Modulate says the monthly usage limit was reached.",
    1003: "Modulate rejected the connection settings.",
    1013: "Modulate is busy right now.",
    1011: "Modulate had a server error.",
}
_RETRYABLE = {None, 1000, 1001, 1006, 1011, 1013, 4030}
_HTTP_AS_CLOSE = {401: 4001, 403: 4003, 429: 4030}


def close_reason(code):
    return _REASONS.get(code, f"The connection to Modulate closed unexpectedly (code {code}).")


def retryable(code):
    return code in _RETRYABLE


@dataclass
class ModulateSTTSettings(STTSettings):
    pass


class ModulateSTTService(STTService):
    """Speech-to-text over Modulate's streaming API. Reconnects a few times if the connection drops, and marks
    itself unusable (so the call ends cleanly) if it can't keep working."""

    Settings = ModulateSTTSettings
    _settings: Settings
    RECONNECT_DELAY = 0.5

    def __init__(self, *, api_key, base_url=BASE_URL, sample_rate=None, endpointing=True,
                 ttfs_p99_latency=MODULATE_TTFS_P99, settings=None, **kwargs):
        defaults = self.Settings(model=MODEL, language=None)
        if settings is not None:
            defaults.apply_update(settings)
        super().__init__(sample_rate=sample_rate, ttfs_p99_latency=ttfs_p99_latency, settings=defaults, **kwargs)
        self._api_key, self._base_url, self._endpointing = api_key, base_url, endpointing
        self._ws = None
        self._session_task = None
        self._closing = False
        self._ready = asyncio.Event()

    def can_generate_metrics(self):
        return True

    # --- life cycle --------------------------------------------------------------------------------

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._closing = False
        self._session_task = self.create_task(self._session(), name="modulate_stt_session")
        try:
            await asyncio.wait_for(self._ready.wait(), 5)      # so the first words aren't lost
        except asyncio.TimeoutError:
            pass

    async def stop(self, frame):
        await super().stop(frame)
        await self._shutdown(graceful=True)

    async def cancel(self, frame):
        await super().cancel(frame)
        await self._shutdown(graceful=False)

    async def cleanup(self):
        await super().cleanup()
        await self._shutdown(graceful=False)

    async def _shutdown(self, graceful):
        self._closing = True
        ws, task = self._ws, self._session_task
        if graceful and ws is not None:
            try:
                await ws.send("")                              # the documented end-of-audio signal
                if task:
                    await asyncio.wait_for(asyncio.shield(task), 2)
            except (ConnectionClosed, asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if task:
            await self.cancel_task(task)
            self._session_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # --- audio in ----------------------------------------------------------------------------------

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(audio)
            except ConnectionClosed:
                pass                                           # the session loop notices and reconnects
        yield None

    # --- the connection ----------------------------------------------------------------------------

    async def _session(self):
        failures = 0
        while not self._closing:
            code = None
            try:
                self._ws = await connect(build_url(self._api_key, self.sample_rate, self._base_url, self._endpointing),
                                         max_size=None, open_timeout=10)
            except InvalidStatus as e:
                code = _HTTP_AS_CLOSE.get(e.response.status_code, 1011)
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("could not reach Modulate (%s)", type(e).__name__)
                code = 1006
            else:
                self._ready.set()
                await self._call_event_handler("on_connected")
                try:
                    async for raw in self._ws:
                        if await self._handle(raw):
                            return                             # a server error: already reported, stop for good
                except ConnectionClosed:
                    pass
                code = self._ws.close_code
                self._ws = None
                if self._closing:
                    return
            if not retryable(code):
                await self._fail(close_reason(code))
                return
            failures += 1
            if failures > MAX_RECONNECTS:
                await self._fail(close_reason(code) + " Giving up after several tries.")
                return
            log.warning("Modulate connection lost (code %s); retry %d of %d", code, failures, MAX_RECONNECTS)
            await asyncio.sleep(self.RECONNECT_DELAY)

    async def _handle(self, raw):
        """Act on one message. Returns True if the service has failed for good."""
        kind, text = parse_message(raw)
        if kind == "partial":
            await self.push_frame(InterimTranscriptionFrame(text, self._user_id, time_now_iso8601()))
        elif kind == "final":
            await self.emit_stt_usage_metrics()
            await self.push_frame(TranscriptionFrame(text, self._user_id, time_now_iso8601(), language=Language.EN))
        elif kind == "error":
            await self._fail(f"Modulate reported an error: {text}")
            return True
        return False

    async def _fail(self, message):
        self._ready.set()
        await self.push_error(error_msg=message, force_treat_as_permanent=True)
        await self._call_event_handler("on_connection_error", message)
