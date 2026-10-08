"""python -m voice.chat   Talk to the agent by typing. Uses the same brain and tools as the phone line.

Needs GROQ_API_KEY. Type 'brief me' for the briefing (no AI model involved) and 'quit' to leave."""
import sys

from gifting import analysis
from voice import brain, briefing, config, fraud, grounding, tools, web

BRIEF_WORDS = {"brief me", "briefing", "brief"}


def session(model, analyst, lines, say):
    history = []
    strikes = fraud.Strikes()
    facts = grounding.FactBook()                          # figures from tool results; the model's own numbers must match them
    say(briefing.GREETING)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("quit", "exit"):
            break
        hit = fraud.assess_text(line)                         # the same guard the phone line uses
        if hit:
            ended = strikes.add()
            say(f"  [guard] {hit['top']} request refused" + (" and the call would end here" if ended else ""))
            say(fraud.END_LINE if ended else hit["response"])
            if ended:
                break
            continue
        if line.lower().strip(".!? ") in BRIEF_WORDS:
            say(briefing.build(analyst.D)["script"])
            continue
        try:
            before = len(history)
            reply, history = brain.run_turn(model, history, line, analyst, facts=facts,
                                            on_event=lambda kind, category: say(f"  [guard] {kind}: {category}; sentence replaced"))
        except brain.BrainError as e:
            say(f"[problem] {e}")
            continue
        for m in history[before:]:
            for call in m.get("tool_calls") or []:
                say(f"  [tool] {call['function']['name']} {call['function'].get('arguments') or ''}")
        say(reply)


def _typed():
    while True:
        try:
            yield input("> ")
        except (EOFError, KeyboardInterrupt):
            return


def main():
    cfg = config.load()
    if not cfg.groq_api_key:
        sys.exit("GROQ_API_KEY is missing. Put it in .env (free key: console.groq.com/keys).")
    analyst = tools.Analyst(analysis.load(cfg.data_path), web=web.Tavily(cfg.tavily_api_key) if cfg.tavily_api_key else None)
    analyst.warm()
    session(brain.groq_chat(cfg.groq_api_key, cfg.groq_model), analyst, _typed(), print)


if __name__ == "__main__":
    main()
