"""Social-engineering guard: sits between speech-to-text and the AI model.

A transcript that asks for a payout, account details, an override of the rules or an action on an account is refused with a
fixed line and never reaches the model. Honest questions pass through untouched. The third refusal ends the call.
What was said is never logged; only the kind of request is.
"""
from pipecat.frames.frames import EndWorkerFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice import fraud


class SocialEngineeringGuard(FrameProcessor):
    def __init__(self, strikes=3, **kwargs):
        super().__init__(**kwargs)
        self._strikes = fraud.Strikes(strikes)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            hit = fraud.assess_text(frame.text)
            if hit:
                ended = self._strikes.add()
                fraud.log_event("social_engineering", category=hit["top"], action="ended_call" if ended else "refused")
                await self.push_frame(TTSSpeakFrame(fraud.END_LINE if ended else hit["response"]))
                if ended:
                    await self.push_frame(EndWorkerFrame(reason="repeated social engineering"), FrameDirection.UPSTREAM)
                return                                            # the model never sees it
        await self.push_frame(frame, direction)
