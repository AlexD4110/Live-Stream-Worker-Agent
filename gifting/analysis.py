"""Python version of dashboard/metrics.js, used by the voice agent.

It reads the same data file as the dashboard (dashboard/data.js) and follows the same definitions, the same
rules and the same wording. tests/test_analysis.py runs both versions on the same data and fails if a
number or a sentence differs, so the page and the phone agent cannot disagree.
"""
import json
import math
import os
from decimal import ROUND_HALF_UP, Decimal

RING_MIN_ACCOUNTS = 3      # accounts sharing one device before we call it a cluster
DATA_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "data.js")


# --- formatting that follows JavaScript's rules (a tie rounds up, not to the even digit) ---------

def _to_fixed(v, digits):
    q = Decimal(1).scaleb(-digits)
    return format(Decimal(v).quantize(q, rounding=ROUND_HALF_UP), "f")


def pct(x, digits=0):
    return _to_fixed(x * 100, digits) + "%"


def signed(x, digits=0):
    return ("+" if x >= 0 else "") + pct(x, digits)


def num(x):
    return f"{math.floor(x + 0.5):,}"


def usd(x):
    return "$" + num(x)


# --- data ------------------------------------------------------------------------------------------

def load(path=DATA_JS):
    with open(path) as f:
        text = f.read()
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def _mask(D, f):
    region, platform = f.get("region"), f.get("platform")
    G = D["gifters"]
    return [(not region or G["region"][i] == region) and (not platform or G["platform"][i] == platform)
            for i in range(len(G["id"]))]


def _sum_days(daily, lo, hi):
    return sum(daily[d] for d in range(lo, hi + 1) if 0 <= d < len(daily))


def device_clusters(D):
    """Accounts that share a device with at least RING_MIN_ACCOUNTS - 1 others."""
    by_dev = {}
    for i, dev in enumerate(D["gifters"]["device"]):
        by_dev.setdefault(dev, []).append(i)
    flagged = [False] * len(D["gifters"]["id"])
    devices = []
    for dev, members in by_dev.items():
        if len(members) >= RING_MIN_ACCOUNTS:
            devices.append(dev)
            for i in members:
                flagged[i] = True
    return flagged, devices


def campaign(D, ok, skip):
    c = D["campaign"]
    tr, ho = c["treatment_regions"].split("|"), c["holdout_regions"].split("|")
    s, e = c["start_day"], c["end_day"]
    Gf, region = D["gifts"], D["gifters"]["region"]
    t, h = [0, 0, 0], [0, 0, 0]                      # before (14 days), during, after (7 days)
    for i in range(len(Gf["day"])):
        g = Gf["g"][i]
        if not ok[g] or (skip and skip[g]):
            continue
        r = region[g]
        arm = t if r in tr else h if r in ho else None
        if arm is None:
            continue
        day = Gf["day"][i]
        if s - 14 <= day < s:
            arm[0] += Gf["coins"][i]
        elif s <= day <= e:
            arm[1] += Gf["coins"][i]
        elif e < day <= e + 7:
            arm[2] += Gf["coins"][i]
    tp, hp = t[0] / 2, h[0] / 2
    if not tp or not hp or not h[1] or not h[2]:
        return None
    return {"name": c["name"], "start": s, "end": e, "rawLift": t[1] / tp - 1,
            "trueLift": (t[1] / tp) / (h[1] / hp) - 1, "afterLift": (t[2] / tp) / (h[2] / hp) - 1,
            "treatedCoins": t[1], "holdoutCoins": h[1], "treatment": tr, "holdout": ho}


def summary(D, f=None):
    """Everything the KPI row and the diagnosis need, for one filter ({region, platform}, both optional)."""
    f = f or {}
    ok = _mask(D, f)
    Gf, S = D["gifts"], D["sessions"]
    days, end, n_g = D["days"], D["days"] - 1, len(D["gifters"]["id"])
    daily = [0] * days
    daily_gifters = [set() for _ in range(days)]
    per_gifter = [0] * n_g
    first = [10 ** 9] * n_g
    coins = rows = 0
    wk_g = {"cur": set(), "prev": set()}
    wk_rows = {"cur": 0, "prev": 0}
    for i in range(len(Gf["day"])):
        g = Gf["g"][i]
        if not ok[g]:
            continue
        day, c = Gf["day"][i], Gf["coins"][i]
        coins += c
        rows += 1
        daily[day] += c
        per_gifter[g] += c
        if day < first[g]:
            first[g] = day
        daily_gifters[day].add(g)
        if day > end - 7:
            wk_g["cur"].add(g)
            wk_rows["cur"] += 1
        elif day > end - 14:
            wk_g["prev"].add(g)
            wk_rows["prev"] += 1

    viewer_daily = [0] * days
    c_region = D["creators"]["region"]
    for j in range(len(S["day"])):
        if f.get("region") and c_region[S["c"][j]] != f["region"]:
            continue
        viewer_daily[S["day"][j]] += S["viewers"][j]

    signup = D["gifters"]["signup"]
    hit7, hit30, second = [0] * n_g, [0] * n_g, [0] * n_g
    for i in range(len(Gf["day"])):
        g = Gf["g"][i]
        if not ok[g] or signup[g] < 0:
            continue
        lag = Gf["day"][i] - first[g]
        if lag >= 1:
            second[g] = 1
        if 1 <= lag <= 7:
            hit7[g] = 1
        if 1 <= lag <= 30:
            hit30[g] = 1
    new_gifters = second_gift = cohort7 = cohort30 = ret7 = ret30 = 0
    for k in range(n_g):
        if not ok[k] or signup[k] < 0 or first[k] > 10 ** 8:
            continue
        new_gifters += 1
        second_gift += second[k]
        if first[k] <= end - 7:
            cohort7 += 1
            ret7 += hit7[k]
        if first[k] <= end - 30:
            cohort30 += 1
            ret30 += hit30[k]

    spend = sorted((v for v in per_gifter if v > 0), reverse=True)
    n_top = max(1, math.floor(len(spend) * 0.01))
    top_sum = sum(spend[:n_top])

    P = D["purchases"]
    buyers, failed, ok_n, cb, usd_sum = set(), 0, 0, 0, 0.0
    for p in range(len(P["day"])):
        if not ok[P["g"][p]]:
            continue
        if not P["ok"][p]:
            failed += 1
            continue
        ok_n += 1
        cb += P["cb"][p]
        usd_sum += P["usd"][p]
        buyers.add(P["g"][p])

    cur, prev = _sum_days(daily, end - 6, end), _sum_days(daily, end - 13, end - 7)
    v_cur, v_prev = _sum_days(viewer_daily, end - 6, end), _sum_days(viewer_daily, end - 13, end - 7)
    g_cur, g_prev = len(wk_g["cur"]), len(wk_g["prev"])
    decomp = None
    if prev > 0 and g_prev > 0 and wk_rows["prev"] > 0 and g_cur > 0 and wk_rows["cur"] > 0:
        coins_per_gift = (cur / wk_rows["cur"]) / (prev / wk_rows["prev"]) - 1
        rows_per_gifter = (wk_rows["cur"] / g_cur) / (wk_rows["prev"] / g_prev) - 1
        if f.get("platform"):
            decomp = {"gifters": g_cur / g_prev - 1, "giftsPerGifter": rows_per_gifter, "coinsPerGift": coins_per_gift}
        elif v_prev > 0 and v_cur > 0:
            decomp = {"viewers": v_cur / v_prev - 1, "sendRate": (g_cur / v_cur) / (g_prev / v_prev) - 1,
                      "giftsPerGifter": rows_per_gifter, "coinsPerGift": coins_per_gift}

    flagged, _ = device_clusters(D)
    return {
        "filter": f, "coins": coins, "giftRows": rows, "gifters": sum(1 for v in per_gifter if v > 0),
        "daily": daily, "viewerDaily": viewer_daily, "dailyGifters": [len(s) for s in daily_gifters],
        "wow": cur / prev - 1 if prev else 0, "last7": cur, "prev7": prev, "decomp": decomp,
        "sendRate": g_cur / v_cur if v_cur and not f.get("platform") else None,
        "arppu": cur / g_cur if g_cur else 0,
        "ret7": ret7 / cohort7 if cohort7 else 0, "ret30": ret30 / cohort30 if cohort30 else 0,
        "newGifters": new_gifters, "repeatShare": second_gift / new_gifters if new_gifters else 0,
        "top1": top_sum / sum(spend) if spend else 0,
        "buyers": len(buyers), "purchaseUsd": usd_sum, "chargebackRate": cb / ok_n if ok_n else 0,
        "failedPurchases": failed, "campaign": campaign(D, ok, flagged),
    }


def segments(D):
    """Coins by platform x region, last 7 days vs the 7 before, biggest drop first."""
    Gf, G, end = D["gifts"], D["gifters"], D["days"] - 1
    out = {}
    for i in range(len(Gf["day"])):
        day = Gf["day"][i]
        if day < end - 13:
            continue
        g = Gf["g"][i]
        row = out.setdefault(G["platform"][g] + "|" + G["region"][g],
                             {"platform": G["platform"][g], "region": G["region"][g], "cur": 0, "prev": 0})
        row["cur" if day > end - 7 else "prev"] += Gf["coins"][i]
    for r in out.values():
        r["delta"] = r["cur"] - r["prev"]
        r["change"] = r["cur"] / r["prev"] - 1 if r["prev"] else 0
    return sorted(out.values(), key=lambda r: r["delta"])


def creator_stats(D):
    C, end, n = D["creators"], D["days"] - 1, len(D["creators"]["id"])
    earned, recent = [0] * n, [0] * n
    for i in range(len(D["gifts"]["day"])):
        c = D["gifts"]["c"][i]
        earned[c] += D["gifts"]["coins"][i]
        if D["gifts"]["day"][i] > end - 28:
            recent[c] += D["gifts"]["coins"][i]
    tiers = {}
    for j in range(n):
        t = tiers.setdefault(C["tier"][j], {"tier": C["tier"][j], "creators": 0, "active": 0, "earning": 0,
                                           "coins": 0, "churnedRecent": 0, "churnedBefore": 0})
        t["creators"] += 1
        if C["join"][j] <= end and (C["churn"][j] < 0 or C["churn"][j] > end - 28):
            t["active"] += 1
            if recent[j] > 0:
                t["earning"] += 1
        t["coins"] += earned[j]
        if C["churn"][j] >= end - 27:
            t["churnedRecent"] += 1
        elif C["churn"][j] >= end - 55 and C["churn"][j] >= 0:
            t["churnedBefore"] += 1
    order = {"head": 0, "mid": 1, "emerging": 2}
    weekly = [0] * math.ceil(D["days"] / 7)
    for k in range(n):
        if C["tier"][k] == "mid" and C["churn"][k] >= 0:
            weekly[C["churn"][k] // 7] += 1
    ranked = sorted(earned, reverse=True)
    total = sum(ranked)
    return {"tiers": sorted(tiers.values(), key=lambda t: order[t["tier"]]), "midChurnWeekly": weekly,
            "top10Share": sum(ranked[:10]) / total if total else 0, "earned": earned}


def findings(D):
    """The rules engine. Most severe first. Every finding has evidence, next steps and a guardrail."""
    out, end = [], D["days"] - 1
    base, seg = summary(D, {}), segments(D)
    flagged, devices = device_clusters(D)
    P, G, Gf = D["purchases"], D["gifters"], D["gifts"]
    ring_accounts = [i for i, fl in enumerate(flagged) if fl]
    name_of = {"android": "Android", "ios": "iOS"}

    # 1. payments incident
    fail_last = fail_prev = bad_fails = 0
    fail_seg = {}
    for i in range(len(P["day"])):
        if P["ok"][i]:
            continue
        if P["day"][i] > end - 7:
            fail_last += 1
            if P["bad"][i]:
                bad_fails += 1
            gi = P["g"][i]
            key = G["platform"][gi] + "|" + G["region"][gi]
            fail_seg[key] = fail_seg.get(key, 0) + 1
        elif P["day"][i] > end - 14:
            fail_prev += 1
    if fail_last >= 20 and fail_last >= 3 * max(fail_prev, 1) and seg:
        worst = seg[0]
        keys = sorted(fail_seg, key=lambda k: -fail_seg[k])
        platform, region = keys[0].split("|")
        name = name_of[platform] + " " + region
        drop = base["prev7"] - base["last7"]
        share = -worst["delta"] / drop if drop > 0 else 0
        v = D["badVersion"]
        out.append({
            "id": "payments_incident", "severity": "high",
            "title": f"Coin purchases are failing: {name} on app {v}",
            "evidence": [
                f"Failed coin purchases: {fail_last} in the last 7 days vs {fail_prev} the week before.",
                f"{pct(bad_fails / fail_last)} of the failures are on app version {v}; the largest group is {name} ({fail_seg[keys[0]]} failures).",
                f"Gifting is {signed(base['wow'])} week over week; {name_of[worst['platform']]} {worst['region']} accounts for {pct(min(share, 1))} of the drop.",
            ],
            "actions": [f"Treat as an incident: ask engineering and payments to check the {v} purchase flow for {name}.",
                        "Offer a web-coin link or retry prompt to affected users while it is fixed.",
                        "After the fix, message gifters whose purchases failed."],
            "guardrail": "Check this is a bug and not a fraud block before reversing any payment rule.",
        })

    # 2. suspected gift ring
    if ring_accounts:
        ring = set(ring_accounts)
        ring_coins = 0
        ring_creators = {}
        for i in range(len(Gf["day"])):
            if Gf["g"][i] in ring:
                ring_coins += Gf["coins"][i]
                ring_creators[Gf["c"][i]] = ring_creators.get(Gf["c"][i], 0) + Gf["coins"][i]
        ring_cb = ring_ok = 0
        for i in range(len(P["day"])):
            if P["g"][i] in ring and P["ok"][i]:
                ring_ok += 1
                ring_cb += P["cb"][i]
        ring_creator_coins = sum(ring_creators.values())
        all_to_them = sum(Gf["coins"][i] for i in range(len(Gf["day"])) if Gf["c"][i] in ring_creators)
        n_t = len(ring_creators)
        out.append({
            "id": "gift_ring", "severity": "high", "accounts": [G["id"][a] for a in ring_accounts],
            "title": f"Possible gift ring: {len(ring_accounts)} accounts on {len(devices)} shared devices",
            "evidence": [
                f"{len(ring_accounts)} accounts share only {len(devices)} devices ({RING_MIN_ACCOUNTS}+ accounts per device).",
                f"Chargeback rate on their purchases is {pct(ring_cb / ring_ok if ring_ok else 0)} vs {pct(base['chargebackRate'], 1)} across all accounts.",
                f"They sent {num(ring_coins)} coins to {n_t} creators, who received {pct(ring_creator_coins / all_to_them if all_to_them else 0)} of their coins from this group.",
            ],
            "actions": [f"Hold payouts for the {n_t} receiving creators and send the cluster to trust and safety for review.",
                        "Leave these accounts out of campaign results and prize lists until reviewed.",
                        "Add a velocity alert: new accounts that buy web coins and gift one creator repeatedly."],
            "guardrail": "This is a risk signal, not a verdict. Human review before any ban or payout action.",
        })

    # 3. early churn
    s = base
    if s["ret7"] < 0.35:
        out.append({
            "id": "early_churn", "severity": "medium", "title": "Most new gifters do not come back",
            "evidence": [f"Only {pct(s['ret7'])} of new gifters gift again within 7 days, and {pct(s['ret30'])} within 30.",
                         f"{pct(1 - s['repeatShare'])} of new gifters never send a second gift."],
            "actions": ["Test a second-gift nudge in the first 48 hours (small status unlock or a creator thank-you).",
                        "Compare retention by first creator tier to see where first gifts go wrong."],
            "guardrail": "Measure against a holdout; watch refunds and complaints so the nudge does not push spending.",
        })

    # 4. whale concentration and lapsed big spenders
    per_g = [0] * len(G["id"])
    last_day = [-1] * len(G["id"])
    for i in range(len(Gf["day"])):
        gg = Gf["g"][i]
        per_g[gg] += Gf["coins"][i]
        if Gf["day"][i] > last_day[gg]:
            last_day[gg] = Gf["day"][i]
    ids = sorted((i for i, v in enumerate(per_g) if v > 0 and not flagged[i]), key=lambda i: -per_g[i])
    top_n = max(1, math.floor(len(ids) * 0.01))
    lapsed = lapsed_coins = 0
    for i in ids[:top_n]:
        if end - last_day[i] >= 21:
            lapsed += 1
            lapsed_coins += per_g[i]
    if s["top1"] > 0.30:
        out.append({
            "id": "whale_concentration", "severity": "medium" if lapsed / top_n > 0.15 else "info",
            "title": "Revenue leans on a few big gifters",
            "evidence": [f"The top 1% of gifters send {pct(s['top1'])} of all coins.",
                         f"{lapsed} of the top {top_n} have not gifted for 21+ days ({num(lapsed_coins)} coins of past spend)."],
            "actions": ["Have a person reach out to the lapsed big gifters before offering anything.",
                        "Track the share from the top 1% weekly and grow the middle (repeat gifters) to lower it."],
            "guardrail": "No pressure on heavy spenders. Offer recognition, not spending prompts, and honour spend limits.",
        })

    # 5. campaign: the headline lift overstates the real lift
    camp = campaign(D, [True] * len(G["id"]), flagged)
    if camp and camp["rawLift"] - camp["trueLift"] > 0.15:
        out.append({
            "id": "campaign_overstated", "severity": "medium",
            "title": f"{camp['name']}: the headline lift is bigger than the real lift",
            "evidence": [f"Treated regions grew {signed(camp['rawLift'])} vs the 2 weeks before.",
                         f"Against the holdout regions the lift is {signed(camp['trueLift'])} (the holdout also rose in the same week).",
                         f"In the week after, treated regions ran {signed(camp['afterLift'])} vs holdout, so some spend was pulled forward."],
            "actions": [f"Report {signed(camp['trueLift'])} as incremental, not {signed(camp['rawLift'])}.",
                        "Subtract prize cost and the after-event dip before calling the event profitable."],
            "guardrail": "Suspected fraud accounts are left out of these numbers.",
        })

    # 6. mid-tier creator churn
    mid = next((t for t in creator_stats(D)["tiers"] if t["tier"] == "mid"), None)
    if mid and mid["churnedRecent"] >= 2 * max(mid["churnedBefore"], 1):
        out.append({
            "id": "creator_churn", "severity": "medium", "title": "Mid-tier creators are leaving faster",
            "evidence": [f"{mid['churnedRecent']} mid-tier creators stopped in the last 4 weeks vs {mid['churnedBefore']} in the 4 weeks before.",
                         f"Only {mid['earning']} of {mid['active']} active mid-tier creators earned gifts in the last 28 days."],
            "actions": ["Interview a sample of the creators who left to find the reason.",
                        "Feature mid-tier creators in discovery and event slots before the next campaign."],
            "guardrail": "Do not read this as a payout-rate problem until the reasons are confirmed; no rate changes without a test.",
        })

    # 7. flagged minors still gifting
    minor_coins, minor_seen = 0, set()
    for i in range(len(Gf["day"])):
        if G["minor"][Gf["g"][i]]:
            minor_coins += Gf["coins"][i]
            minor_seen.add(Gf["g"][i])
    if minor_seen:
        out.append({
            "id": "minors_gifting", "severity": "high",
            "title": f"{len(minor_seen)} accounts flagged as possible minors are still gifting",
            "evidence": [f"{len(minor_seen)} flagged accounts have sent {num(minor_coins)} coins."],
            "actions": ["Send the list to trust and safety now; ask for age re-verification and a purchase hold while it is pending."],
            "guardrail": "Escalate to humans immediately; do not use these accounts in campaign targeting or results.",
        })

    # 8. prizes
    Z = D["prizes"]
    bad = bad_usd = suspects = 0
    stat = {}
    for i in range(len(Z["rank"])):
        stat[Z["status"][i]] = stat.get(Z["status"][i], 0) + 1
        if Z["status"][i] != "sent":
            bad += 1
            bad_usd += Z["usd"][i]
        if flagged[Z["g"][i]]:
            suspects += 1
    if bad or suspects:
        breakdown = ", ".join(f"{v} {k}" for k, v in stat.items() if k != "sent")
        out.append({
            "id": "prize_issues", "severity": "high" if suspects else "medium", "suspectWinners": suspects,
            "title": "Prize fulfilment needs a clean-up",
            "evidence": [f"{bad} of {len(Z['rank'])} prizes are not cleanly sent ({usd(bad_usd)}): {breakdown}.",
                         f"{suspects} winner{'' if suspects == 1 else 's'} on the suspected-ring list."],
            "actions": ["Reissue failed prizes and recover or void duplicates.",
                        "Hold prizes for suspected accounts until trust and safety clears them.",
                        "Add a reconciliation step: promised vs sent vs failed vs reissued, before and after each campaign."],
            "guardrail": "Do not remove a winner without review; keep an audit trail of every change.",
        })

    # 9. web coin share (information)
    web_lo, web_hi = [0.0, 0.0], [0.0, 0.0]
    for i in range(len(P["day"])):
        if not P["ok"][i]:
            continue
        if P["day"][i] <= 27:
            web_lo[0] += P["usd"][i]
            web_lo[1] += P["usd"][i] if P["web"][i] else 0
        elif P["day"][i] >= end - 27:
            web_hi[0] += P["usd"][i]
            web_hi[1] += P["usd"][i] if P["web"][i] else 0
    if web_lo[0] and web_hi[0] and web_hi[1] / web_hi[0] - web_lo[1] / web_lo[0] > 0.05:
        out.append({
            "id": "web_shift", "severity": "info", "title": "More coin purchases are moving to the web",
            "evidence": [f"Web share of coin purchases went from {pct(web_lo[1] / web_lo[0])} in the first 4 weeks to {pct(web_hi[1] / web_hi[0])} in the last 4."],
            "actions": ["Check whether web buyers gift more or just pay less for the same gifting before promoting web further."],
            "guardrail": "Judge it on margin and refunds, not web share alone.",
        })

    order = {"high": 0, "medium": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["severity"]])
