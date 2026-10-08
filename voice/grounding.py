"""Hallucination guard, the logic: can every number the model says be traced to a tool result from this call?

The model is never trusted with a figure. Each tool result is recorded in a FactBook (every number in it, in the forms a voice
would say it). A sentence the model wrote is checked against that book: a number with no match is "ungrounded", and the sentence
is replaced instead of spoken. Sentences without numbers pass. Direction (up or down) is not checked, only size.

What counts as a claim: digits ("558", "7,583,854", "$650"), digits with a scale or percent ("21 thousand", "7.6 million",
"13 percent"), and number words big enough to be a figure ("twenty one thousand", "fifteen percent"). Everyday small words
("one", "two sentences"), clock times ("one minute"), years and ID-like strings are not claims.
"""
import math
import re
from collections import namedtuple

Claim = namedtuple("Claim", "value kind decimal")          # kind: "pct" for a percentage, "num" for anything else
Check = namedtuple("Check", "ok ungrounded")

UNGROUNDED_LINE = "I can't confirm that figure from the data, so I won't say it."

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
          "eighty": 80, "ninety": 90}
_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_TIME_UNITS = {"minute", "minutes", "second", "seconds", "hour", "hours", "sentence", "sentences"}
_PCT_WORDS = {"percent", "%"}

_DIGITS_STRICT = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")
_DIGITS_LOOSE = re.compile(r"\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")
_WORD = re.compile(r"[a-z]+|\d[\d,]*(?:\.\d+)?|%")


def _after(text, end):
    """The next word (or %) after a number, with how far it reached."""
    m = re.match(r"\s*(thousand|million|billion)?\s*(%|percent|per cent)?", text[end:], re.IGNORECASE)
    scale, pct = (m.group(1) or "").lower(), (m.group(2) or "").lower()
    return scale, bool(pct), end + m.end()


def _next_word(text, pos):
    m = re.match(r"\s*([a-z]+)", text[pos:], re.IGNORECASE)
    return m.group(1).lower() if m else ""


_RANGE = re.compile(r"\s*(?:to|or|and|through|-|\u2013|\u2014)\s*", re.IGNORECASE)


def extract_claims(text, strict=True):
    """Every figure in the text, in order. strict=True is for sentences to check; False is for reading facts out of tool text,
    where small numbers and ID-like digits are kept too.

    A unit after a range applies to both ends: "fifty to seventy percent" is two percentages, "21 to 25 thousand" is 21,000 and 25,000."""
    if not isinstance(text, str) or not text.strip():
        return []
    entries = []                    # each: [start, end, value, scale, is_pct, decimal, skippable, is_time]
    digit_re = _DIGITS_STRICT if strict else _DIGITS_LOOSE
    for m in digit_re.finditer(text):
        base = float((m.group(1).replace(",", "")) + (m.group(2) or ""))
        scale, is_pct, end = _after(text, m.end())
        mult = _SCALES[scale] if scale else 1
        is_time = _next_word(text, m.end()) in _TIME_UNITS and not scale and not is_pct
        skippable = bool(not scale and not is_pct and not m.group(2) and (1900 <= base <= 2100 or base <= 1))
        entries.append([m.start(), end, base, mult, is_pct, bool(m.group(2)), skippable, is_time])
    lower = text.lower()
    words = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(lower)]
    i = 0
    while i < len(words):
        if words[i][0] not in _UNITS and words[i][0] != "hundred":
            i += 1
            continue
        j, total, current, run_end, last_scale = i, 0, 0, words[i][2], False
        while j < len(words):
            w = words[j][0]
            if w in _UNITS:
                current += _UNITS[w]
            elif w == "hundred":
                current = (current or 1) * 100
            elif w in _SCALES:
                total += (current or 1) * _SCALES[w]
                current, last_scale = 0, True
            elif w == "and" and j + 1 < len(words) and words[j + 1][0] in _UNITS:
                j += 1
                continue
            else:
                break
            run_end = words[j][2]
            j += 1
        value = total + current
        is_pct = j < len(words) and words[j][0] in _PCT_WORDS
        nxt = words[j][0] if j < len(words) else ""
        end = words[j][2] if is_pct else run_end
        entries.append([words[i][1], end, float(value), 1, is_pct, False, value <= 10 and not is_pct and not last_scale,
                        nxt in _TIME_UNITS and not is_pct])
        i = max(j, i + 1)
    entries.sort(key=lambda e: e[0])
    for a, b in zip(entries, entries[1:]):                       # a unit at the end of a range reaches back to the start
        if _RANGE.fullmatch(text[a[1]:b[0]]) and a[3] == 1 and not a[4] and (b[4] or b[3] > 1):
            a[4], a[3], a[6] = b[4], b[3], False
        elif _RANGE.fullmatch(text[a[1]:b[0]]) and b[7] and not a[4] and a[3] == 1:
            a[7] = True
    out = []
    for start, end, base, mult, is_pct, decimal, skippable, is_time in entries:
        if strict and ((skippable and not is_pct and mult == 1) or (is_time and not is_pct and mult == 1)):
            continue
        out.append(Claim(base * mult, "pct" if is_pct else "num", decimal))
    return out


def _close(claim, fact):
    """Does a fact support a spoken figure? Spoken rounding is allowed, invention is not."""
    c, f = abs(claim.value), abs(fact)
    if claim.kind == "pct":
        return abs(c - f) <= (0.06 if claim.decimal else 0.55)
    if claim.decimal and c < 100:
        return abs(c - f) <= 0.06
    if c < 100:
        return abs(c - f) <= 0.5
    if c < 1000:
        return abs(c - f) <= max(0.5, 0.015 * c)
    return abs(c - f) <= 0.05 * c


class FactBook:
    """Every number from this call's tool results, kept in the forms they are said."""

    def __init__(self):
        self.nums, self.pcts = [], []

    def add(self, name, result):
        self._walk(result)

    def _walk(self, v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            if math.isfinite(v):
                self.nums.append(abs(float(v)))
                if not float(v).is_integer() and abs(v) <= 1.5:
                    self.pcts.append(abs(float(v)) * 100)       # a fraction like 0.18 is said as 18 percent
        elif isinstance(v, str):
            for c in extract_claims(v, strict=False):
                (self.pcts if c.kind == "pct" else self.nums).append(c.value)
        elif isinstance(v, dict):
            for item in v.values():
                self._walk(item)
        elif isinstance(v, (list, tuple)):
            for item in v:
                self._walk(item)

    def check(self, text):
        bad = []
        for claim in extract_claims(text):
            pool = self.pcts if claim.kind == "pct" else self.nums
            if not any(_close(claim, f) for f in pool):
                bad.append(claim)
        return Check(not bad, bad)
