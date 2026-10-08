"""Cloned-voice check, using Modulate's synthetic voice detection over a WebSocket.

A copy of the caller's audio is streamed to Modulate while the call goes on as normal. Modulate scores each window of audio
as synthetic or human. One confident "synthetic" window closes the sensitive tools (the agent still gives headline numbers);
two end the call with a spoken reason. See voice/fraud.py for the decision rules.

If Modulate can't be reached the call carries on and a warning is logged, unless FRAUD_FAIL_CLOSED is on, in which case the
call ends. The key travels in the connection URL, as Modulate requires, so URLs are never logged unredacted.
"""
import asyncio
import logging
import re
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from pipecat.frames.frames import CancelFrame, EndFrame, EndWorkerFrame, InputAudioRawFrame, StartFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice import fraud
from voice.modulate_stt import _HTTP_AS_CLOSE, close_reason, retryable

log = logging.getLogger(__name__)

BASE_URL = "wss://platform.modulate.ai/api/velma-2-synthetic-voice-detection-streaming"
MAX_RECONNECTS = 2
QUEUE_MAX = 300                 # about six seconds of audio; if Modulate falls behind, the oldest audio is dropped
BLOCK_LINE = "I'm sorry, but the voice on this call sounds synthetic, so I have to end it. Please contact the team another way."
UNVERIFIABLE_LINE = "I can't verify the voice on this call right now, so I have to end it. Please contact the team another way."


def build_url(api_key, sample_rate, base_url=BASE_URL):
    return f"{base_url}?" + urlencode({"api_key": api_key, "audio_format": "s16le", "sample_rate": sample_rate, "num_channels": 1})


def redact(url):
    return re.sub(r"api_key=[^&]*", "api_key=hidden", url)


class SyntheticVoiceGuard(FrameProcessor):
    RECONNECT_DELAY = 0.5

    def __init__(self, api_key, analyst, *, monitor=None, base_url=BASE_URL, fail_closed=False, threshold=0.9, windows=2, **kwargs):
        super().__init__(**kwargs)
        self._key, self._analyst, self._base_url, self._fail_closed = api_key, analyst, base_url, fail_closed
        self.monitor = monitor or fraud.SyntheticVoiceMonitor(threshold, windows)
        self.available = True
        self._queue = asyncio.Queue()
        self._ws = None
        self._task = None
        self._sample_rate = 16000
        self._state = "ok"
        self._stopping = False
        self._ended = False

    # --- audio in ----------------------------------------------------------------------------------

    @property
    def backlog(self):
        return self._queue.qsize()

    def enqueue(self, chunk):
        if self._queue.qsize() >= QUEUE_MAX:
            self._queue.get_nowait()                          # drop the oldest rather than grow without limit
        self._queue.put_nowait(chunk)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._sample_rate = frame.audio_in_sample_rate or 16000
            self._task = self.create_task(self._session(), name="svd_session")
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._shutdown(graceful=isinstance(frame, EndFrame))
        elif isinstance(frame, InputAudioRawFrame):
            self.enqueue(frame.audio)
        await self.push_frame(frame, direction)

    # --- the connection ----------------------------------------------------------------------------

    async def _session(self):
        failures = 0
        while not self._stopping:
            code = None
            try:
                ws = await connect(build_url(self._key, self._sample_rate, self._base_url), open_timeout=10, max_size=None)
            except InvalidStatus as e:
                code = _HTTP_AS_CLOSE.get(e.response.status_code, 1011)
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("could not reach the voice check (%s)", type(e).__name__)
                code = 1006
            else:
                self._ws = ws
                sender = asyncio.create_task(self._send_loop(ws))
                try:
                    async for raw in ws:
                        if await self._handle(raw):
                            return
                except ConnectionClosed:
                    pass
                finally:
                    sender.cancel()
                code = ws.close_code
                self._ws = None
                if self._stopping:
                    return
            if not retryable(code):
                await self._unavailable(code)
                return
            failures += 1
            if failures > MAX_RECONNECTS:
                await self._unavailable(code)
                return
            await asyncio.sleep(self.RECONNECT_DELAY)

    async def _send_loop(self, ws):
        try:
            while True:
                await ws.send(await self._queue.get())
        except (ConnectionClosed, asyncio.CancelledError):
            pass

    async def _shutdown(self, graceful):
        self._stopping = True
        ws, task = self._ws, self._task
        if graceful and ws is not None:
            try:
                for _ in range(20):                           # let queued audio go out first
                    if self._queue.empty():
                        break
                    await asyncio.sleep(0.05)
                await ws.send("")                             # the documented end-of-audio signal
                if task:
                    await asyncio.wait_for(asyncio.shield(task), 2)
            except (ConnectionClosed, asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if task:
            await self.cancel_task(task)
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # --- what Modulate says ------------------------------------------------------------------------

    async def _handle(self, raw):
        """Returns True if the session should stop."""
        kind, a, b = fraud.parse_frame(raw)
        if kind == "frame":
            await self._apply(self.monitor.record(a, b), b)
        elif kind == "error":
            log.warning("the voice check reported an error: %s", a[:100])
            await self._unavailable(1011)
            return True
        return False

    async def _apply(self, state, confidence):
        if state == self._state:
            return
        self._state = state
        if state == "suspect":
            self._analyst.restricted = True
            fraud.log_event("synthetic_voice", confidence=confidence, action="restricted")
        elif state == "ok":
            self._analyst.restricted = False
            fraud.log_event("synthetic_voice", confidence=confidence, action="cleared")
        elif state == "block":
            self._analyst.restricted = True
            fraud.log_event("synthetic_voice", confidence=confidence, action="blocked")
            await self._end_call(BLOCK_LINE)

    async def _unavailable(self, code):
        self.available = False
        log.warning("the voice check is unavailable: %s", close_reason(code))
        if self._fail_closed:
            fraud.log_event("voice_check_unavailable", action="blocked")
            await self._end_call(UNVERIFIABLE_LINE)

    async def _end_call(self, line):
        if self._ended:
            return
        self._ended = True
        await self.push_frame(TTSSpeakFrame(line, append_to_context=False))
        await self.push_frame(EndWorkerFrame(reason="voice check"), FrameDirection.UPSTREAM)
