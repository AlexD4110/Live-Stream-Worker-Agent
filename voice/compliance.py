"""Compliance guard, the rules: what the agent must never say, whatever the model writes.

Each rule is a wording pattern, checked on the model's own sentences before they are spoken. A breach is replaced with a fixed
line that says what the agent can do instead. The six rules:

  promise         guarantees and certainty about outcomes
  advice          buying, selling or holding securities or crypto
  pressure_spend  pushing big spenders to spend more
  verdict         calling a suspect a criminal as fact (findings are risk signals, not verdicts)
  allegation      stating pending allegations against a company as proven
  official_rate   stating a reported take rate as if it were official policy
"""
import re

RESPONSES = {
    "promise": "I can't promise results. I can only report what the data shows.",
    "advice": "I can't give financial or investment advice.",
    "pressure_spend": "I won't suggest pushing anyone to spend more. Recognition and outreach by a person are better.",
    "verdict": "That's a risk signal, not a verdict. A person on the trust and safety team has to review it.",
    "allegation": "Those are allegations, not findings, so I can't state them as fact.",
    "official_rate": "That figure is a reported benchmark, not an official rate.",
}

_SECURITIES = r"(?:stock|stocks|shares|equity|equities|crypto|cryptocurrency|bitcoin|securities|bonds|etfs?)"
_BIG = r"(?:whales?|big gifters?|heavy spenders?|top gifters?|biggest gifters?|high spenders?)"
_HEDGES = re.compile(r"\b(?:reported|reportedly|according to|benchmark|estimate|estimated|unofficial|not official)\b")

# checked in this order; the first that matches decides the replacement
_RULES = [
    ("verdict", [
        r"\b(?:definitely|certainly|clearly|obviously|undoubtedly|without a doubt|proven|confirmed)\b.{0,25}"
        r"\b(?:fraud|fraudsters?|fraudulent|money laundering|laundering|criminals?|scams?|ring)\b",
        r"\b(?:is|are|they re|he s|she s|that s) (?:a |an |the )?(?:fraudsters?|criminals?|scammers?|money launderers?|guilty|thieves)\b",
    ]),
    ("allegation", [
        r"\b(?:was|were|been|is|are) (?:found )?(?:guilty|convicted|fined|sanctioned|sued)\b",
        r"\b(?:tiktok|the company|the platform|they)\b.{0,25}\b(?:broke the law|violated|laundered|exploited)\b",
    ]),
    ("advice", [
        rf"\b(?:buy|buying|sell|selling|short|shorting|invest in|investing in|hold|holding)\b.{{0,25}}\b{_SECURITIES}\b",
    ]),
    ("promise", [
        r"\b(?:i|we) (?:promise|guarantee|assure|swear)\b",
        r"\b(?:is|are|be|will be|it s|was) guaranteed\b",
        r"\bwill (?:definitely|certainly|surely|for sure|absolutely)\b",
        r"\bno risk\b|\brisk[- ]free\b|\bsure thing\b|\bcannot fail\b",
    ]),
    ("pressure_spend", [
        rf"\b(?:push|pressure|encourage|nudge|persuade|convince|urge|get|incentivi[sz]e)\b.{{0,40}}\b{_BIG}\b.{{0,40}}"
        r"\b(?:spend|gift|buy|pay|give)\b.{0,15}\bmore\b",
    ]),
    ("official_rate", [
        r"\b(?:tiktok|the platform|the company|platforms?)\b.{0,15}\b(?:takes?|keeps?|charges?|retains?|pays?)\b.{0,30}"
        r"\b(?:\d+(?:\.\d+)? percent|half)\b",
    ]),
]


def _normalise(text):
    t = text.lower().replace("’", "'").replace("'", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9. ]", " ", t)).strip()


def assess_output(text):
    """None if the sentence is fine, otherwise {"category", "response"}."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = _normalise(text)
    for category, patterns in _RULES:
        if category == "official_rate" and _HEDGES.search(t):
            continue
        if any(re.search(p, t) for p in patterns):
            return {"category": category, "response": RESPONSES[category]}
    return None
