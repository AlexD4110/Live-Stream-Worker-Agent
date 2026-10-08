"""The output guard as a stage of the call pipeline: it sits between the AI model and the voice.

The model streams its answer a few words at a time. This stage collects the words into sentences, reviews each one (voice/review.py)
and passes on either the sentence or a fixed replacement. Nothing the model writes is spoken until its sentence has been checked.
Sentences the system speaks itself (a tool's exact line, the greeting) do not pass through here: they are exact by construction.
"""
import re

from pipecat.frames.frames import InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.review import log_events, review_sentence

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class OutputGuard(FrameProcessor):
    def __init__(self, facts, *, grounding_on=True, compliance_on=True, **kwargs):
        super().__init__(**kwargs)
        self._facts, self._grounding_on, self._compliance_on = facts, grounding_on, compliance_on
        self._buffer, self._last_replacement = "", None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._buffer, self._last_replacement = "", None
        elif isinstance(frame, InterruptionFrame):
            self._buffer = ""                                     # a half-written sentence is dropped, not spoken later
        elif isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            self._buffer += frame.text
            await self._emit(final=False)
            return
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._emit(final=True)
        await self.push_frame(frame, direction)

    async def _emit(self, final):
        parts = _SENTENCE.split(self._buffer)
        if final:
            ready, self._buffer = parts, ""
        else:
            ready, self._buffer = parts[:-1], parts[-1]
        for sentence in (s for s in ready if s.strip()):
            line, event = review_sentence(sentence.strip(), self._facts, grounding_on=self._grounding_on,
                                          compliance_on=self._compliance_on)
            if event:
                log_events([event])
                if line == self._last_replacement:
                    continue
                self._last_replacement = line
            await self.push_frame(LLMTextFrame(line + " "))
