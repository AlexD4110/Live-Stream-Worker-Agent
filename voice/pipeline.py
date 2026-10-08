"""One phone call, as a Pipecat pipeline:

  caller's voice -> speech to text (Deepgram) -> the model with tools (Groq) -> text to speech (Deepgram) -> caller

Silero detects when the caller starts and stops talking, so the agent can be interrupted. The model can only call
the tools in voice/tools.py. The pipeline is built by build(), which makes no network connection; run_bot() runs it.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from voice import brain, briefing, grounding, tools
from voice.output_guard import OutputGuard
from voice.input_guard import SocialEngineeringGuard
from voice.modulate_stt import ModulateSTTService
from voice.modulate_svd import SyntheticVoiceGuard

PHONE_HZ = 8000                      # phone audio is 8 kHz
LISTEN_HZ = 16000                    # what the pipeline listens at; the phone's 8 kHz is upsampled (Modulate's fastest path)
log = logging.getLogger(__name__)


@dataclass
class Bot:
    task: PipelineTask
    llm: Any
    context: LLMContext
    transport: Any
    stages: list
    handlers: dict = field(default_factory=dict)
    greeting: str = briefing.GREETING
    on_error: Any = None
    facts: Any = None


REPEAT_SECONDS = 5


def _tool_handler(analyst, facts):
    last = {"line": None, "at": 0.0}

    async def handler(params):
        # off the audio thread: the first lookups take a moment and must not make the call stutter
        result = await asyncio.to_thread(analyst.dispatch, params.function_name, dict(params.arguments or {}))
        facts.add(params.function_name, result)                  # the output guard checks the model's numbers against these
        line = tools.direct_say(params.function_name, result)
        if line:
            # the result already carries an exact spoken line: say it ourselves and skip the second model call
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
            now = time.monotonic()
            if line != last["line"] or now - last["at"] > REPEAT_SECONDS:       # a repeated call must not be said twice
                await params.llm.push_frame(TTSSpeakFrame(line))
            last["line"], last["at"] = line, now
        else:
            await params.result_callback(result)
    return handler


def build(websocket, call_data, cfg, analyst):
    """One phone call from Plivo."""
    can_hang_up = bool(cfg.plivo_auth_id and cfg.plivo_auth_token)
    serializer = PlivoFrameSerializer(
        stream_id=call_data.stream_id, call_id=call_data.call_id,
        auth_id=cfg.plivo_auth_id or None, auth_token=cfg.plivo_auth_token or None,
        params=PlivoFrameSerializer.InputParams(auto_hang_up=can_hang_up))
    transport = FastAPIWebsocketTransport(websocket=websocket, params=FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer))
    return assemble(transport, cfg, analyst, in_hz=LISTEN_HZ, out_hz=PHONE_HZ)


def assemble(transport, cfg, analyst, *, in_hz, out_hz):
    """The pipeline itself, for any audio transport: the phone line, or a browser on this computer for testing."""
    if cfg.stt_provider == "modulate":
        stt = ModulateSTTService(api_key=cfg.modulate_api_key)
    else:
        stt = DeepgramSTTService(api_key=cfg.deepgram_api_key,
                                 settings=DeepgramSTTService.Settings(model=cfg.stt_model, language=Language.EN))
    tts = DeepgramTTSService(api_key=cfg.deepgram_api_key, settings=DeepgramTTSService.Settings(voice=cfg.tts_voice))
    effort = {"reasoning_effort": brain.reasoning_effort(cfg.groq_model)} if brain.reasoning_effort(cfg.groq_model) else {}
    llm = GroqLLMService(api_key=cfg.groq_api_key, settings=GroqLLMService.Settings(
        model=cfg.groq_model, temperature=0.2, max_tokens=brain.max_tokens_for(cfg.groq_model), **effort))

    facts = grounding.FactBook()                               # every figure the tools return on this call
    handler = _tool_handler(analyst, facts)
    handlers = {}
    for spec in tools.TOOL_SPECS:
        llm.register_function(spec["name"], handler)
        handlers[spec["name"]] = handler

    schema = ToolsSchema(standard_tools=[FunctionSchema(name=s["name"], description=s["description"],
                                                        properties=s["properties"], required=s["required"])
                                         for s in tools.TOOL_SPECS])
    context = LLMContext(messages=[{"role": "system", "content": brain.SYSTEM_PROMPT}], tools=schema)
    agg = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))

    analyst.restricted = False                                 # a new call never inherits the last call's restrictions
    voice_guard = []
    if cfg.fraud_voice_check:                                  # listens to the caller's audio for a cloned voice
        voice_guard = [SyntheticVoiceGuard(cfg.modulate_api_key, analyst, fail_closed=cfg.fraud_fail_closed,
                                           threshold=cfg.synthetic_threshold, windows=cfg.synthetic_windows)]
    output_guard = [OutputGuard(facts)] if cfg.output_guard else []      # checks what the model writes before it is spoken
    stages = [transport.input(), *voice_guard, stt, SocialEngineeringGuard(), agg.user(), llm, *output_guard, tts,
              transport.output(), agg.assistant()]
    task = PipelineTask(Pipeline(stages), params=PipelineParams(audio_in_sample_rate=in_hz, audio_out_sample_rate=out_hz))

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        await task.queue_frames([TTSSpeakFrame(briefing.GREETING)])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        await task.cancel()

    async def on_error(_task, frame):
        # If speech recognition, the model or the voice stops working, hang up rather than leave the caller in silence.
        proc = getattr(frame, "processor", None)
        if proc is not None and not proc.is_usable:
            log.error("a voice service stopped working (%s); ending the call", type(proc).__name__)
            await task.cancel()

    task.event_handler("on_pipeline_error")(on_error)

    return Bot(task=task, llm=llm, context=context, transport=transport, stages=stages, handlers=handlers, on_error=on_error, facts=facts)


async def run_bot(websocket, call_data, cfg, analyst):
    bot = build(websocket, call_data, cfg, analyst)
    try:
        await PipelineRunner(handle_sigint=False).run(bot.task)
    finally:
        analyst.restricted = False
