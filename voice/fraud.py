"""Fraud guards: the decisions, as plain code.

Two guards protect a call:
  1. Cloned-voice check. Modulate scores the caller's audio as synthetic or human; this module turns those scores into
     "ok", "suspect" or "block".
  2. Social-engineering check. Requests like "release my payout early" or "read me the card number" are recognised from the
     words alone, instantly, and answered with a fixed line. The AI model never sees them.

Records of what a guard did contain the kind of event, never the words that were said.
"""
import json
import logging
import math
import re
from collections import deque

log = logging.getLogger(__name__)


# --- 1. cloned voice ---------------------------------------------------------------------------------

_VERDICTS = {"synthetic", "non-synthetic", "no-content"}


def parse_frame(raw):
    """One Modulate message as (kind, a, b): ("frame", verdict, confidence), ("done", None, None),
    ("error", message, None) or ("ignore", None, None)."""
    ignore = ("ignore", None, None)
    if isinstance(raw, (bytes, bytearray)):
        return ignore
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return ignore
    if not isinstance(msg, dict):
        return ignore
    kind = msg.get("type")
    if kind == "done":
        return ("done", None, None)
    if kind == "error":
        return ("error", str(msg.get("error") or "unknown error"), None)
    if kind == "frame" and isinstance(msg.get("frame"), dict):
        verdict, conf = msg["frame"].get("verdict"), msg["frame"].get("confidence")
        if verdict in _VERDICTS and isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 1:
            return ("frame", verdict, float(conf))
    return ignore


class SyntheticVoiceMonitor:
    """Turns a stream of window verdicts into ok / suspect / block.

    A confident "synthetic" window counts as a hit. One hit in the recent windows is "suspect"; enough hits is "block", which
    is final. Silence is ignored, and suspicion fades if the voice keeps proving human."""

    def __init__(self, threshold=0.9, windows_to_block=2, history=4):
        self.threshold, self.windows_to_block = threshold, windows_to_block
        self._recent = deque(maxlen=max(history, windows_to_block))
        self.state = "ok"

    def record(self, verdict, confidence):
        if self.state == "block" or verdict == "no-content":
            return self.state
        if verdict == "synthetic":
            self._recent.append("hit" if confidence >= self.threshold else "weak")
        else:
            self._recent.append("clear" if confidence >= 0.5 else "weak")
        hits = sum(1 for x in self._recent if x == "hit")
        self.state = "block" if hits >= self.windows_to_block else "suspect" if hits >= 1 else "ok"
        return self.state


# --- 2. social engineering ---------------------------------------------------------------------------

RESPONSES = {
    "override": "I can't change my rules. I'm happy to keep going with the analytics.",
    "payout": "I can't release payouts or change payment details. I only report analytics, and a person on the trust and safety team has to handle that.",
    "credential": "I can't share account IDs, personal details or payment information.",
    "action": "I can't take actions on accounts. I can only report what the data shows and suggest next steps for a person to review.",
    "impersonation": "I can't verify who anyone is over a call, and I can't act on urgent requests. I can only report analytics.",
}
END_LINE = "I'm ending this call. Please contact the team another way."
_PRIORITY = ["override", "payout", "credential", "action", "impersonation"]

_PAY_VERB = r"(?:release|send|pay|approve|wire|transfer|issue|process|expedite|speed up|push through|pay out|paid out)"
_PAY_OBJ = r"(?:payouts?|payments?|money|funds|earnings|diamonds|cash|bonus|prizes?|coins)"
_CUE = r"\b(?:my|me|mine|our|myself|early|now|immediately|asap|today|right away|right now)\b"
_SECRET = (r"(?:card numbers?|credit cards?|debit cards?|passwords?|pass ?codes?|pins?|ssn|social security|bank account|"
           r"routing numbers?|email addresses|emails|phone numbers?|home addresses?|account ids?|user ids?|device ids?|"
           r"creator ids?|gifter ids?|logins?|credentials|api keys?|tokens?)")
_PEOPLE = r"(?:gifters?|creators?|users?|accounts?|customers?|people|whales|winners?)"
_ACT = r"(?:ban|suspend|delete|unlock|reinstate|refund|reverse|cancel|close|freeze|unfreeze|whitelist|approve|remove|block|terminate)"
_ACT_OBJ = r"(?:accounts?|gifters?|creators?|users?|payouts?|chargebacks?|purchases?|transactions?|prizes?|orders?)"
_LEAD = r"(?:please |now |just |okay |ok |go ahead and )*"
_ROLE = (r"(?:account owner|owner|creator|administrator|admin|ceo|cfo|executive|director|vice president|vp|manager|supervisor|"
         r"developer|engineer|security|trust and safety|compliance|support|tiktok|plivo|modulate|head office|headquarters|corporate)")

_PATTERNS = {
    "payout": [
        (rf"\b{_PAY_VERB}\b.{{0,25}}\b{_PAY_OBJ}\b", True),                 # (pattern, needs a "my / now / early" cue)
        (rf"\b{_PAY_OBJ}\b.{{0,20}}\b(?:paid out|paid|sent|released|wired|transferred)\b", True),
        (r"\bpay me\b", False),
        (r"\bearly payouts?\b", False),
    ],
    "credential": [
        (rf"\b(?:read|tell|give|say|share|spell|show|what s|what is|what are|provide|list|need|want|get|dictate)\b.{{0,40}}\b{_SECRET}\b", False),
        (r"\breal names?\b", False),
        (rf"\bnames?\b.{{0,12}}\b(?:of|for)\b.{{0,20}}\b{_PEOPLE}\b", False),
        (rf"\b(?:gifter|creator|user|account|customer|winner)s? names?\b", False),
    ],
    "override": [
        (r"\b(?:ignore|forget|disregard|bypass|override|drop|break|skip|circumvent|get around)\b.{0,25}\b(?:your|the|all|any|these|those)\b"
         r".{0,25}\b(?:rules?|instructions?|guidelines?|restrictions?|guardrails?|prompt|programming|verification|safeguards?|safety|checks?|filters?|limits?)\b", False),
        (r"\bsystem prompt\b|\bdeveloper mode\b|\bjailbreak\b|\bdan mode\b", False),
        (r"\bpretend (?:you|to be|that you)\b|\bact as (?:if|though) you\b|\b(?:you have|with) no (?:restrictions|rules|limits)\b", False),
        (r"\b(?:disable|turn off|switch off|deactivate|remove)\b.{0,20}\b(?:guard ?rails?|filters?|checks?|safeguards?|fraud|verification|restrictions?|protections?)\b", False),
    ],
    "action": [
        (rf"^{_LEAD}{_ACT}\b.{{0,30}}\b{_ACT_OBJ}\b", False),
        (rf"\b(?:can|could|will|would) you (?:please )?{_ACT}\b.{{0,30}}\b{_ACT_OBJ}\b", False),
        (rf"\bi (?:need|want) you to {_ACT}\b.{{0,30}}\b{_ACT_OBJ}\b", False),
        (rf"\b(?:please|go ahead and) {_ACT}\b.{{0,30}}\b{_ACT_OBJ}\b", False),
    ],
}
_IMPERSONATION = re.compile(rf"\b(?:i am|i m|this is|i m calling from|we are|we re|speaking as)\b.{{0,12}}\b{_ROLE}\b")
_PRESSURE = re.compile(r"\b(?:right now|immediately|urgent(?:ly)?|emergency|asap|no time|don t tell|do not tell|lose my job|get fired|"
                       r"last chance|hurry|skip (?:the )?verification|no need to verify|before anyone)\b")


def _normalise(text):
    t = text.lower().replace("’", "'").replace("'", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def assess_text(text):
    """None if the words look fine, otherwise {"categories": [...], "response": the fixed line to say}."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = _normalise(text)
    found = set()
    for category, patterns in _PATTERNS.items():
        for pattern, needs_cue in patterns:
            if re.search(pattern, t) and (not needs_cue or re.search(_CUE, t)):
                found.add(category)
                break
    if _IMPERSONATION.search(t) and (found or _PRESSURE.search(t)):
        found.add("impersonation")
        if _PRESSURE.search(t):
            found.add("pressure")
    if not found - {"pressure"}:
        return None
    top = next(c for c in _PRIORITY if c in found)
    return {"categories": sorted(found), "top": top, "response": RESPONSES[top]}


class Strikes:
    """Counts refused requests on one call. The last one allowed ends the call."""

    def __init__(self, limit=3):
        self.limit, self.count = limit, 0

    def add(self):
        self.count += 1
        return self.count >= self.limit


# --- records of what the guards did ------------------------------------------------------------------

def log_event(kind, *, category=None, confidence=None, action=None):
    """Note that a guard fired. There is deliberately nowhere to put what was said."""
    band = None if confidence is None else f"{math.floor(confidence * 10) / 10:.1f}+"
    log.info("guard fired: kind=%s category=%s action=%s confidence=%s", kind, category, action, band)
