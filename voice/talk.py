"""python -m voice.talk   Talk to the agent from this computer's microphone, in a browser. No phone, Plivo or tunnel.

It is the same pipeline as the phone line (the same hearing, tools, guards and voice); only the audio comes from a web page.
Open the address it prints, click Connect, allow the microphone, and speak. The browser cancels echo, so no headphones needed.
It needs GROQ_API_KEY and DEEPGRAM_API_KEY (the agent's voice); MODULATE_API_KEY is used for hearing if set.
"""
import sys

from gifting import analysis
from voice import config, logs, pipeline, tools, web

BROWSER_HZ = 24000                  # a browser can play better quality than a phone line
_STATE = {}


def problems(cfg):
    """What is missing to run here. The phone settings (allow-list, Plivo) do not matter in the browser."""
    return [p for p in config.problems(cfg) if not p.startswith("ALLOWED_CALLERS")]


def assemble_for_browser(transport, cfg, analyst):
    return pipeline.assemble(transport, cfg, analyst, in_hz=pipeline.LISTEN_HZ, out_hz=BROWSER_HZ)


def _make_analyst(cfg):
    analyst = tools.Analyst(analysis.load(cfg.data_path), web=web.Tavily(cfg.tavily_api_key) if cfg.tavily_api_key else None)
    analyst.warm()
    return analyst


async def bot(runner_args):
    """Called by Pipecat's runner each time a browser connects."""
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.runner.utils import create_transport
    from pipecat.transports.base_transport import TransportParams

    cfg = config.load()
    analyst = _STATE.get("analyst") or _make_analyst(cfg)
    transport = await create_transport(runner_args, {"webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True)})
    session = assemble_for_browser(transport, cfg, analyst)
    try:
        await PipelineRunner(handle_sigint=runner_args.handle_sigint).run(session.task)
    finally:
        analyst.restricted = False


def _run_runner():
    from pipecat.runner.run import main as runner_main
    runner_main()


def main(cfg=None, run=None):
    cfg = cfg or config.load()
    found = problems(cfg)
    if found:
        print("Not ready yet:")
        for p in found:
            print("  -", p)
        print("\nFix these in .env, then run again.")
        sys.exit(1)
    logs.configure()
    _STATE["analyst"] = _make_analyst(cfg)
    print("Starting the browser voice test. Open the address below, click Connect, allow the microphone, and talk.")
    (run or _run_runner)()


if __name__ == "__main__":
    main()
