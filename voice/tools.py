"""The voice agent's fixed toolbox.

The AI model never calculates, remembers or guesses a number. It can only call the functions below, which read
the data through gifting/analysis.py (the same code the dashboard is checked against) and hand back exact
figures plus a ready-to-speak sentence. A question that fits no function gets "I can't answer that", not an
invented answer.
"""
import logging
import math
import re

from gifting import analysis
from voice import web as websearch

log = logging.getLogger(__name__)

REGIONS = ["US-West", "US-East", "US-Central", "US-South"]
PLATFORMS = ["ios", "android"]
PART_LABELS = {"viewers": "viewers", "sendRate": "gift-send rate", "giftsPerGifter": "gifts per gifter",
               "coinsPerGift": "coins per gift", "gifters": "gifters"}
LAPSED_DAYS = 21
# Closed while the caller's voice is under suspicion. Headline numbers and web search stay open.
SENSITIVE = {"get_findings", "get_lapsed_gifters", "get_creator_health", "get_briefing", "get_campaign_result", "diagnose_change"}
# Tools whose result already carries an exact, ready-to-speak line. That line is spoken as it is, with no second model call:
# half the tokens and delay, and nothing the model could get wrong between the number and the caller.
DIRECT = {"get_metric", "explain_change", "get_segment_changes", "diagnose_change", "get_campaign_result",
          "get_creator_health", "get_lapsed_gifters", "get_briefing"}


def speak_line(text):
    """Make a tool's sentence ready for the voice: no hyphens or symbols a voice would stumble on, and a capital to start."""
    from voice.briefing import speakable                  # imported here because briefing imports this module
    text = speakable(text)
    return text[:1].upper() + text[1:]


def direct_say(name, result):
    """The line to speak as-is for this tool result, or None if the model should handle it."""
    if name not in DIRECT or not isinstance(result, dict) or "error" in result or result.get("available") is False:
        return None
    text = result.get("script") if name == "get_briefing" else result.get("say")
    if not isinstance(text, str) or not text.strip():
        return None
    return text if name == "get_briefing" else speak_line(text)


# --- saying numbers so they sound right ----------------------------------------------------------

def _round_half_up(x):
    return math.floor(x + 0.5)


def spoken_count(n):
    n = abs(n)
    if n < 100:
        return str(_round_half_up(n))
    if n < 1000:
        return f"about {int(_round_half_up(n / 10) * 10)}"
    if n < 10_000:
        return f"about {_round_half_up(n / 100) / 10:g} thousand"
    if n < 1_000_000:
        return f"about {_round_half_up(n / 1000)} thousand"
    return f"about {_round_half_up(n / 100_000) / 10:g} million"


def spoken_pct(x):
    """A size in words. The direction (up or down) is said separately, never with a sign."""
    v = abs(x) * 100
    return f"{_round_half_up(v * 10) / 10:g} percent" if v < 1 else f"{_round_half_up(v)} percent"


def _direction(x):
    return "up" if x >= 0 else "down"


def _scope(region, platform):
    parts = [region, {"ios": "iOS", "android": "Android"}.get(platform)]
    return " ".join(p for p in parts if p) or "all traffic"


# --- the analyst ---------------------------------------------------------------------------------

class Analyst:
    METRICS = {
        "coins_gifted": "Total coins gifted, with the last 7 days compared with the 7 before",
        "gifters": "People who sent at least one gift",
        "gift_send_rate": "Gifters per viewer in the last 7 days",
        "coins_per_gifter": "Coins per gifter in the last 7 days",
        "new_gifter_return_7d": "Share of new gifters who gift again within 7 days",
        "new_gifter_return_30d": "Share of new gifters who gift again within 30 days",
        "top_1_percent_share": "Share of all coins sent by the top 1% of gifters",
        "chargeback_rate": "Share of successful coin purchases that were charged back",
        "failed_purchases": "Coin purchases that failed",
        "new_gifters": "Gifters whose first gift is inside the period",
    }

    def __init__(self, D, web=None):
        self.D = D
        self.web = web                                  # a voice.web.Tavily, or None if web search isn't set up
        self.restricted = False                         # set by the cloned-voice guard while a voice is suspect
        self._summaries = {}
        self._findings = None

    def _summary(self, region=None, platform=None):
        key = (region or "", platform or "")
        if key not in self._summaries:
            self._summaries[key] = analysis.summary(self.D, {"region": region or "", "platform": platform or ""})
        return self._summaries[key]

    def warm(self):
        """Do the slow calculations now, so the first question on a call is answered at once."""
        self._summary()
        self._all_findings()

    def _all_findings(self):
        if self._findings is None:
            self._findings = analysis.findings(self.D)
        return self._findings

    @staticmethod
    def _bad_filter(region, platform):
        if region and region not in REGIONS:
            return {"error": f"I don't know the region '{region}'.", "options": REGIONS}
        if platform and platform not in PLATFORMS:
            return {"error": f"I don't know the platform '{platform}'.", "options": PLATFORMS}
        return None

    # --- tools -----------------------------------------------------------------------------------

    def get_metric(self, metric, region=None, platform=None):
        if metric not in self.METRICS:
            return {"error": f"I don't have a metric called '{metric}'.", "options": list(self.METRICS)}
        bad = self._bad_filter(region, platform)
        if bad:
            return bad
        s, scope = self._summary(region, platform), _scope(region, platform)
        out = {"metric": metric, "scope": scope}
        if metric == "coins_gifted":
            out.update(value=s["coins"], last_7_days=s["last7"], prior_7_days=s["prev7"], change_vs_prior_week=s["wow"],
                       say=f"{scope}: {spoken_count(s['coins'])} coins gifted over the 14 weeks. The last 7 days were "
                           f"{spoken_count(s['last7'])}, {_direction(s['wow'])} {spoken_pct(s['wow'])} on the week before.")
        elif metric == "gifters":
            out.update(value=s["gifters"], say=f"{scope}: {spoken_count(s['gifters'])} people have sent a gift.")
        elif metric == "gift_send_rate":
            if s["sendRate"] is None:
                return dict(out, available=False,
                            reason="Viewer counts are not split by platform, so I can't work out a send rate for a platform.")
            out.update(value=s["sendRate"], say=f"{scope}: {spoken_pct(s['sendRate'])} of viewers sent a gift in the last 7 days.")
        elif metric == "coins_per_gifter":
            out.update(value=s["arppu"], say=f"{scope}: each gifter sent about {_round_half_up(s['arppu'])} coins in the last 7 days.")
        elif metric == "new_gifter_return_7d":
            out.update(value=s["ret7"], say=f"{scope}: {spoken_pct(s['ret7'])} of new gifters gift again within a week.")
        elif metric == "new_gifter_return_30d":
            out.update(value=s["ret30"], say=f"{scope}: {spoken_pct(s['ret30'])} of new gifters gift again within 30 days.")
        elif metric == "top_1_percent_share":
            out.update(value=s["top1"], say=f"{scope}: the top 1 percent of gifters send {spoken_pct(s['top1'])} of all coins.")
        elif metric == "chargeback_rate":
            out.update(value=s["chargebackRate"], say=f"{scope}: {spoken_pct(s['chargebackRate'])} of successful purchases were charged back.")
        elif metric == "failed_purchases":
            out.update(value=s["failedPurchases"], say=f"{scope}: {spoken_count(s['failedPurchases'])} coin purchases failed.")
        elif metric == "new_gifters":
            out.update(value=s["newGifters"], say=f"{scope}: {spoken_count(s['newGifters'])} new gifters, and {spoken_pct(1 - s['repeatShare'])} never sent a second gift.")
        return out

    def explain_change(self, region=None, platform=None):
        bad = self._bad_filter(region, platform)
        if bad:
            return bad
        s, scope = self._summary(region, platform), _scope(region, platform)
        if not s["decomp"]:
            return {"scope": scope, "available": False, "reason": "There isn't enough data in this view to split the change."}
        parts = [{"name": PART_LABELS[k], "change": v} for k, v in s["decomp"].items()]
        big = max(parts, key=lambda p: abs(p["change"]))
        return {"scope": scope, "total_change": s["wow"], "parts": parts, "biggest_mover": big["name"],
                "say": f"{scope}: coins are {_direction(s['wow'])} {spoken_pct(s['wow'])} on the week before. The biggest mover is "
                       f"{big['name']}, {_direction(big['change'])} {spoken_pct(big['change'])}."}

    def diagnose_change(self, region=None, platform=None):
        """One answer to "why did it change": what drove it, where it fell most, and the top issue."""
        from voice.briefing import speakable                  # imported here because briefing imports this module
        change = self.explain_change(region, platform)
        if "parts" not in change:
            return change
        w = analysis.segments(self.D)[0]
        found = self._all_findings()
        out = dict(change, worst_segment={"platform": w["platform"], "region": w["region"], "change": w["change"]})
        say = (f"{change['say']} The biggest fall is {_scope(w['region'], w['platform'])}, "
               f"{_direction(w['change'])} {spoken_pct(w['change'])}.")
        if found:
            top = found[0]
            out["top_finding"] = {"id": top["id"], "title": top["title"], "evidence": top["evidence"][0], "next_step": top["actions"][0]}
            say += f" The top issue is: {speakable(top['title'])}."
        return dict(out, say=say)

    def get_segment_changes(self):
        rows = [{"platform": r["platform"], "region": r["region"], "last_7_days": r["cur"], "prior_7_days": r["prev"],
                 "change": r["change"]} for r in analysis.segments(self.D)]
        w = rows[0]
        return {"segments": rows,
                "say": f"The biggest fall is {_scope(w['region'], w['platform'])}, {_direction(w['change'])} {spoken_pct(w['change'])} "
                       f"on the week before."}

    def get_findings(self, limit=3):
        limit = 3 if limit is None else limit
        found = self._all_findings()
        shown = [{"id": f["id"], "severity": f["severity"], "title": f["title"], "evidence": f["evidence"],
                  "next_steps": f["actions"], "guardrail": f["guardrail"]} for f in found[:limit]]
        return {"total_found": len(found), "findings": shown}

    def get_campaign_result(self):
        c = self._summary()["campaign"]
        if not c:
            return {"available": False, "reason": "There is no campaign with a holdout to compare against."}
        return {"campaign": c["name"], "headline_lift": c["rawLift"], "lift_vs_holdout": c["trueLift"],
                "week_after_vs_holdout": c["afterLift"],
                "caveat": "The headline compares treated regions with their own earlier weeks. Only the holdout comparison "
                          "shows what the campaign added. Suspected fraud accounts are left out.",
                "say": f"{c['name']}: the headline lift was {_direction(c['rawLift'])} {spoken_pct(c['rawLift'])}, but against the "
                       f"holdout regions it was {_direction(c['trueLift'])} {spoken_pct(c['trueLift'])}. The week after was "
                       f"{_direction(c['afterLift'])} {spoken_pct(c['afterLift'])} versus holdout."}

    def get_creator_health(self):
        cs = analysis.creator_stats(self.D)
        tiers = [{"tier": t["tier"], "creators": t["creators"], "active": t["active"],
                  "earning_share": t["earning"] / t["active"] if t["active"] else 0, "coins_received": t["coins"],
                  "stopped_last_4_weeks": t["churnedRecent"], "stopped_previous_4_weeks": t["churnedBefore"]} for t in cs["tiers"]]
        mid = next(t for t in tiers if t["tier"] == "mid")
        return {"tiers": tiers, "top_10_creators_share": cs["top10Share"],
                "say": f"{mid['stopped_last_4_weeks']} mid-tier creators stopped in the last 4 weeks, compared with "
                       f"{mid['stopped_previous_4_weeks']} in the 4 weeks before. The 10 biggest creators receive "
                       f"{spoken_pct(cs['top10Share'])} of all coins."}

    def get_lapsed_gifters(self):
        D, end = self.D, self.D["days"] - 1
        flagged, _ = analysis.device_clusters(D)
        n = len(D["gifters"]["id"])
        spend, last = [0] * n, [-1] * n
        G = D["gifts"]
        for i in range(len(G["day"])):
            g = G["g"][i]
            spend[g] += G["coins"][i]
            last[g] = max(last[g], G["day"][i])
        people = [i for i in range(n) if spend[i] > 0 and not flagged[i]]
        lapsed = [i for i in people if end - last[i] >= LAPSED_DAYS]
        by_spend = sorted(people, key=lambda i: -spend[i])
        big = by_spend[:max(1, math.floor(len(people) * 0.01))]
        lapsed_big = [i for i in big if end - last[i] >= LAPSED_DAYS]
        return {"lapsed_gifters": len(lapsed), "coins_they_gifted_before": sum(spend[i] for i in lapsed),
                "big_gifters": len(big), "lapsed_big_gifters": len(lapsed_big), "lapsed_means_days_without_a_gift": LAPSED_DAYS,
                "say": f"{spoken_count(len(lapsed))} gifters have gone quiet for {LAPSED_DAYS} days or more, and "
                       f"{len(lapsed_big)} of the {len(big)} biggest gifters are among them."}

    def search_web(self, query):
        """Background from outside our data. Always labeled external; never mixed with the computed numbers."""
        if self.web is None:
            return {"available": False, "reason": "Web search isn't set up."}
        try:
            results = self.web.search(query)
        except websearch.WebSearchError as e:
            return {"error": str(e)}
        base = {"external": True, "untrusted_text": True,
                "note": "These are web results, not our data. Say where each fact comes from and that it is reported. "
                        "Never mix them with our own figures."}
        if not results:
            return dict(base, results=[], say="I didn't find anything useful on the web for that.")
        top = results[0]
        first = re.split(r"(?<=[.!?])\s", top["snippet"])[0]
        return dict(base, results=results, say=f"According to {top['source']}: {first}")

    def get_briefing(self):
        from voice import briefing                      # imported here because briefing imports this module
        return {"script": briefing.build(self.D)["script"],
                "instruction": "Read the script word for word. Do not add, change or explain anything."}

    # --- calling a tool safely ---------------------------------------------------------------------

    def dispatch(self, name, args):
        spec = next((s for s in TOOL_SPECS if s["name"] == name), None)
        if spec is None:
            return {"error": f"There is no tool called '{name}'.", "options": [s["name"] for s in TOOL_SPECS]}
        args = args or {}
        problem = _check_args(spec, args)
        if problem:
            return {"error": problem}
        if self.restricted and name in SENSITIVE:
            return {"restricted": True,
                    "say": "I can't share that on this call right now because I couldn't confirm the caller's voice is genuine."}
        try:
            return getattr(self, name)(**args)
        except Exception:                       # never let a lookup crash the call
            log.exception("tool %s failed", name)
            return {"error": "That lookup failed, so I can't answer it."}


def _check_args(spec, args):
    for k in args:
        if k not in spec["properties"]:
            return f"'{k}' is not an option for {spec['name']}."
    for k in spec["required"]:
        if args.get(k) is None:
            return f"{spec['name']} needs '{k}'."
    for k, v in args.items():
        if v is None:
            continue                                  # null on an optional option means "not set"
        p = spec["properties"][k]
        types = p["type"] if isinstance(p["type"], list) else [p["type"]]
        if "integer" in types and (isinstance(v, bool) or not isinstance(v, int)):
            return f"'{k}' must be a whole number."
        if "string" in types and not isinstance(v, str):
            return f"'{k}' must be text."
        if "minimum" in p and v < p["minimum"] or "maximum" in p and v > p["maximum"]:
            return f"'{k}' must be between {p.get('minimum')} and {p.get('maximum')}."
        if "maxLength" in p and isinstance(v, str) and len(v) > p["maxLength"]:
            return f"'{k}' is too long (at most {p['maxLength']} characters)."
    return None


# Optional options accept null: models send null for "no filter", and a schema that forbids it gets the whole call rejected.
_FILTERS = {
    "region": {"type": ["string", "null"], "description": "Optional. Limit to one region, or null for all.", "enum": REGIONS + [None]},
    "platform": {"type": ["string", "null"], "description": "Optional. Limit to one phone platform, or null for all.",
                 "enum": PLATFORMS + [None]},
}

TOOL_SPECS = [
    {"name": "get_metric",
     "description": "Get one headline number, such as total coins gifted, how many new gifters come back, or the share of "
                    "coins from the top 1% of gifters. Use for any 'how many' or 'what is the rate' question.",
     "properties": {"metric": {"type": "string", "description": "Which number.", "enum": list(Analyst.METRICS)}, **_FILTERS},
     "required": ["metric"]},
    {"name": "diagnose_change",
     "description": "Quick diagnosis. Use for 'why did gifting change, fall or rise' and 'what happened' questions. Gives what "
                    "drove the change, where it fell most, and the top issue, all in one answer.",
     "properties": dict(_FILTERS), "required": []},
    {"name": "explain_change",
     "description": "Only the split of the weekly change into viewers, gift-send rate, gifts per gifter and coins per gift. Use "
                    "when asked specifically which of those parts moved.",
     "properties": dict(_FILTERS), "required": []},
    {"name": "get_segment_changes",
     "description": "Use for 'where' questions: which phone platform and region rose or fell most, last 7 days against the 7 before.",
     "properties": {}, "required": []},
    {"name": "get_findings",
     "description": "Get the problems and risks found by the rules, most urgent first, each with evidence, next steps and a "
                    "guardrail. Use for anything about fraud, gift rings, flagged minors, prizes, chargebacks, payment bugs, churn, "
                    "big gifters and other risks, and for 'what should we do' or 'recommend' questions.",
     "properties": {"limit": {"type": ["integer", "null"], "description": "How many to return, 1 to 20, or null for the default of 3.",
                              "minimum": 1, "maximum": 20}},
     "required": []},
    {"name": "get_briefing",
     "description": "Get the one-minute spoken briefing. Use when the analyst says 'brief me' or asks for a summary or overview.",
     "properties": {}, "required": []},
    {"name": "search_web",
     "description": "Look up general background on the web, outside our own data: how live-stream gifting works, published "
                    "industry reports, reported benchmarks. Never use it for our own numbers. Results are external and unverified.",
     "properties": {"query": {"type": "string", "description": "A short general question. No account IDs, personal "
                                                              "details or our own figures.",
                              "maxLength": websearch.MAX_QUERY_CHARS}},
     "required": ["query"]},
    {"name": "get_campaign_result",
     "description": "Get the result of the campaign: the headline lift, the lift against the holdout regions and the week after.",
     "properties": {}, "required": []},
    {"name": "get_creator_health",
     "description": "Get creator numbers: how many are active and earning by tier, and how many stopped recently.",
     "properties": {}, "required": []},
    {"name": "get_lapsed_gifters",
     "description": "Get how many past gifters have gone quiet, and how many of the biggest gifters are among them.",
     "properties": {}, "required": []},
]
