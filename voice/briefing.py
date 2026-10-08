"""The spoken briefing, built from the analysis results with a fixed template. No AI model writes it, so every
number in it is one the code produced."""
import re

from gifting import analysis
from voice.tools import Analyst, _direction, spoken_count, spoken_pct

GREETING = ("Hi, this is Gifting Pulse, an AI assistant for gifting analytics. "
            "Say brief me for a one minute briefing, or just ask me a question.")


def speakable(text):
    """Make written text read cleanly aloud: no symbols a voice would spell out or skip."""
    t = re.sub(r"\+(\d+)\s*%", r"\1 percent", text)
    t = re.sub(r"-(\d+(?:\.\d+)?)\s*%", r"minus \1 percent", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"\1 percent", t)
    t = re.sub(r"\$(\d[\d,]*)", r"\1 dollars", t)
    t = re.sub(r"(?<=\d)\+", " plus", t)
    t = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", t)
    t = t.replace("→", " to ").replace("&", " and ")
    t = re.sub(r"[*#_`]", "", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _sentence(text):
    text = speakable(text).strip()
    return text if text.endswith((".", "?", "!")) else text + "."


def build(D):
    """Returns {"script": str, "top_finding": id}."""
    s = analysis.summary(D, {})
    found = analysis.findings(D)
    lapsed = Analyst(D).get_lapsed_gifters()
    top = found[0]
    others = [f for f in found[1:] if f["severity"] != "info"]

    parts = [
        "Here is your gifting briefing, based on synthetic demo data.",
        f"Headline: coins gifted are {_direction(s['wow'])} {spoken_pct(s['wow'])} this week, "
        f"{spoken_count(s['last7'])} coins.",
        "Three numbers to know.",
        f"First, only {spoken_pct(s['ret7'])} of new gifters come back within a week.",
        f"Second, the top 1 percent of gifters send {spoken_pct(s['top1'])} of all coins.",
        f"Third, {spoken_count(lapsed['lapsed_gifters'])} gifters have gone quiet for three weeks or more.",
        f"The biggest concern: {_sentence(top['title'])}",
        _sentence(top["evidence"][0]),
        f"The next step: {_sentence(top['actions'][0])}",
    ]
    if others:
        parts.append(f"I found {len(found) - 1} other items, starting with: {_sentence(others[0]['title'])}")
    parts.append("What would you like to dig into?")
    return {"script": " ".join(parts), "top_finding": top["id"]}
