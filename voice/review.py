"""Review what the model wrote, one sentence at a time, before it is spoken.

Each sentence goes through the compliance rules (voice/compliance.py) and then the number check (voice/grounding.py). A sentence
that breaks either is replaced with a fixed line; clean sentences pass untouched. This module needs no phone or audio code, so
the typed chat uses it too.
"""
import re
from collections import namedtuple

from voice import compliance, fraud, grounding

Review = namedtuple("Review", "text events")           # events: [("compliance" | "hallucination", category), ...]
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def voice_clean(text):
    """Characters a voice stumbles on: non-breaking hyphens, dashes and curly quotes become plain ones."""
    t = re.sub(r"(?<=\w)[\u2010\u2011](?=\w)", " ", text)
    t = t.replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    return t.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')


def split_sentences(text):
    return [s for s in _SENTENCE.split(text.strip()) if s]


def review_sentence(sentence, facts, *, grounding_on=True, compliance_on=True):
    """Returns (line to speak, event or None)."""
    if compliance_on:
        hit = compliance.assess_output(sentence)
        if hit:
            return hit["response"], ("compliance", hit["category"])
    if grounding_on and not facts.check(sentence).ok:
        return grounding.UNGROUNDED_LINE, ("hallucination", "ungrounded_number")
    return voice_clean(sentence), None


def review(text, facts, *, grounding_on=True, compliance_on=True):
    if not isinstance(text, str) or not text.strip():
        return Review("", [])
    out, events = [], []
    for sentence in split_sentences(text):
        line, event = review_sentence(sentence, facts, grounding_on=grounding_on, compliance_on=compliance_on)
        if event:
            events.append(event)
            if out and out[-1] == line:                  # the same fixed line is never said twice in a row
                continue
        out.append(line)
    return Review(" ".join(out), events)


def log_events(events):
    """Note that a guard acted. The words themselves are never logged."""
    for kind, category in events:
        fraud.log_event(kind, category=category, action="replaced")
