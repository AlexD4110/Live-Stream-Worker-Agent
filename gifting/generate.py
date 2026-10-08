"""Synthetic LIVE gifting data with realistic, planted patterns.

Everything is invented. The patterns are planted on purpose so the dashboard (and later the
voice agent) has something real to find:

  - early-churn cliff: most new gifters never send a second gift
  - whale concentration: the top 1% of gifters send roughly 30-50% of coins
  - lapsed whales: some big spenders went quiet weeks ago
  - weekend peaks and a rising share of cheaper web coin purchases
  - a campaign whose raw lift overstates its incremental (holdout) lift, then a pull-forward dip
  - an incident: a bad Android release breaks app coin purchases in US-East for the last week
  - a fraud ring: new accounts on shared devices, web-funded, chargebacks, gifting three new creators
  - flagged minors still gifting
  - mid-tier creator churn speeding up late in the window
  - prize fulfillment with failures, and fraud accounts among the winners

Usage:  python -m gifting.generate          (writes data/*.csv and dashboard/data.js)
"""
import csv
import json
import math
import os
import random
from bisect import bisect
from datetime import date, timedelta
from itertools import accumulate

START = date(2026, 7, 1)
DAYS = 98                      # 2026-07-01 .. 2026-10-06
WARMUP = 150                   # simulated before day 0 so the population is steady on day 0
REGIONS = ["US-West", "US-East", "US-Central", "US-South"]
REGION_WEIGHTS = [0.28, 0.32, 0.17, 0.23]
TIERS = {"head": (15, 40.0, 0.70, 900), "mid": (90, 6.0, 0.50, 120), "emerging": (192, 1.0, 0.30, 20)}
#            count, pick weight, stream prob, base viewers

APP_PRICE = 19.99 / 1321       # USD per coin, in-app
WEB_DISCOUNT = 0.28            # web coins are cheaper (no app-store fee)
BUNDLES = [30, 70, 350, 700, 1400, 3500, 7000, 17500, 35000, 70000]
UNIVERSE = 44999               # the biggest single gift
SPEND_SIGMA = 1.10             # spread of gifter spending intensity
POPULARITY_SIGMA = 1.6         # spread of creator popularity within a tier
WHALE = 12                     # intensity above this is a whale

CAMPAIGN = {"campaign_id": "C1", "name": "End-of-Summer Battle Week", "start_day": 56, "end_day": 62,
            "treatment_regions": "US-West|US-East", "holdout_regions": "US-Central|US-South"}
EFFECT_NEW, EFFECT_FREQ, EFFECT_COIN = 1.2, 1.09, 1.06   # true campaign effect on treated regions
SEASONAL = 1.22                # everyone spends more that week, campaign or not
GOOD_VERSION, BAD_VERSION = "34.1", "34.2"
BAD_ROLLOUT_DAY = 88           # Android 34.2 ships
INCIDENT_DAY = DAYS - 7        # 34.2 app purchases start failing in US-East
RING_SIGNUP, RING_LAST = 50, 66


def day_date(d):
    return START + timedelta(days=d)


def is_weekend(d):
    return day_date(d).weekday() >= 5


def in_campaign(d):
    return CAMPAIGN["start_day"] <= d <= CAMPAIGN["end_day"]


def treated(region):
    return region in CAMPAIGN["treatment_regions"].split("|")


def lognormal(rng, sigma):
    return math.exp(rng.gauss(0, sigma))


# --- creators -----------------------------------------------------------------

def _creators(rng):
    rows = []
    n = 0
    for tier, (count, _, _, _) in TIERS.items():
        for _ in range(count):
            n += 1
            join = rng.randint(0, 90) if tier == "emerging" and rng.random() < 0.3 else -rng.randint(30, 700)
            churn = ""
            for d in range(max(join, 0), DAYS):
                hazard = {"head": 0.0002, "emerging": 0.003}.get(tier) or (0.0006 if d < 63 else 0.004)
                if rng.random() < hazard:
                    churn = d
                    break
            rows.append({"creator_id": f"c{n:03d}", "tier": tier, "region": rng.choices(REGIONS, REGION_WEIGHTS)[0],
                         "agency": int(rng.random() < {"head": 0.8, "mid": 0.5, "emerging": 0.15}[tier]),
                         "join_day": join, "churn_day": churn})
    ring = []
    for _ in range(3):
        n += 1
        ring.append(f"c{n:03d}")
        rows.append({"creator_id": ring[-1], "tier": "emerging", "region": "US-South", "agency": 0,
                     "join_day": RING_SIGNUP - 2, "churn_day": ""})
    return rows, ring


def _active(c, d):
    return c["join_day"] <= d and (c["churn_day"] == "" or d < c["churn_day"])


def _sessions(rng, creators):
    rows = []
    for c in creators:
        _, _, p_stream, base = TIERS[c["tier"]]
        for d in range(DAYS):
            if not _active(c, d) or rng.random() > p_stream * (1.15 if is_weekend(d) else 1):
                continue
            boost = (1.25 if is_weekend(d) else 1) * (1.2 if in_campaign(d) and treated(c["region"]) else 1)
            rows.append({"day": d, "creator_id": c["creator_id"], "hours": rng.choice([1, 1.5, 2, 2.5, 3, 4]),
                         "viewers": max(1, round(base * lognormal(rng, 0.4) * boost))})
    return rows


class _Picker:
    """Chooses favourite creators, weighted by tier and a home-region bonus, among those active that day."""

    def __init__(self, creators, exclude, seed):
        self.creators = [c for c in creators if c["creator_id"] not in exclude]
        # Each creator has a fixed popularity, so many small creators earn little or nothing.
        # Its own random stream keeps the rest of the data unchanged.
        pop_rng = random.Random(seed + 1000)
        self.pop = {c["creator_id"]: lognormal(pop_rng, POPULARITY_SIGMA) for c in self.creators}
        self.cache = {}

    def pick(self, rng, d, region, k):
        key = (max(d, 0), region)
        if key not in self.cache:
            pool = [c for c in self.creators if _active(c, key[0])]
            w = [TIERS[c["tier"]][1] * self.pop[c["creator_id"]] * (2 if c["region"] == region else 1) for c in pool]
            self.cache[key] = (pool, list(accumulate(w)))
        pool, cum = self.cache[key]
        return [pool[bisect(cum, rng.random() * cum[-1])]["creator_id"] for _ in range(k)]


# --- gifters ------------------------------------------------------------------

def _new_gifters_today(rng, d, region):
    rate = 175 * REGION_WEIGHTS[REGIONS.index(region)] * (1.3 if is_weekend(d) else 1)
    if in_campaign(d):
        rate *= SEASONAL * (EFFECT_NEW if treated(region) else 1)
    whole = int(rate)
    return whole + (rng.random() < rate - whole)


def _spend_factors(d, region):
    """(frequency, coin) multipliers for a day and region."""
    freq = coin = 1.0
    if is_weekend(d):
        freq *= 1.35
    if in_campaign(d):
        freq *= SEASONAL
        if treated(region):
            freq *= EFFECT_FREQ
            coin *= EFFECT_COIN
    elif CAMPAIGN["end_day"] < d <= CAMPAIGN["end_day"] + 7 and treated(region):
        freq *= 0.85               # spend was pulled forward into the event
    return freq, coin


def _bundle(need):
    for b in BUNDLES:
        if b >= need:
            return b
    return math.ceil(need / BUNDLES[-1]) * BUNDLES[-1]


def build(seed=7):
    """Returns (tables, truth). tables: dict of lists of dicts. truth: answers the tables must not leak."""
    rng = random.Random(seed)
    creators, ring_creators = _creators(rng)
    sessions = _sessions(rng, creators)
    picker = _Picker(creators, set(ring_creators), seed)
    by_id = {c["creator_id"]: c for c in creators}

    gifters, gifts, purchases = [], [], []
    seq = {"g": 0, "p": 0}

    def add_purchase(d, g, coins, channel, version, status, chargeback_p):
        seq["p"] += 1
        price = APP_PRICE * (1 - WEB_DISCOUNT if channel == "web" else 1)
        purchases.append({"purchase_id": f"p{seq['p']:06d}", "day": d, "gifter_id": g["gifter_id"],
                          "coins": coins, "usd": round(coins * price, 2), "channel": channel,
                          "platform": g["platform"], "app_version": version, "status": status,
                          "chargeback": int(status == "ok" and rng.random() < chargeback_p)})

    def new_gifter(d, region, platform=None, device=None):
        seq["g"] += 1
        g = {"gifter_id": f"g{seq['g']:05d}", "region": region,
             "platform": platform or ("android" if rng.random() < (0.62 if region == "US-East" else 0.5) else "ios"), "signup_day": d,
             "device_id": device or f"d{seq['g']:05d}",
             "age_signal": "flagged_minor" if rng.random() < 0.004 else "ok"}
        gifters.append(g)
        return g

    # Organic gifters, simulated from before day 0.
    for d0 in range(-WARMUP, DAYS):
        for region in REGIONS:
            for _ in range(_new_gifters_today(rng, d0, region)):
                g = new_gifter(d0, region)
                intensity = lognormal(rng, SPEND_SIGMA)
                returner = rng.random() < 0.20 + 0.25 * min(1, intensity / 5)
                life = rng.expovariate(1 / (35 * (1 + math.log1p(intensity) / 2))) if returner else 0
                stop = d0 + life
                if intensity > WHALE and rng.random() < 0.35:
                    stop = min(stop, rng.randint(55, 74))     # a whale who went quiet
                q = min(0.6, 0.08 + 0.05 * intensity ** 0.7)
                favs = picker.pick(rng, d0, region, 2)
                for d in range(d0, DAYS):
                    if d > stop:
                        break
                    freq, coin_mult = _spend_factors(d, region)
                    if d != d0 and rng.random() > q * freq:
                        continue
                    need = max(1, round(30 * intensity ** 0.9 * lognormal(rng, 0.4) * coin_mult))
                    if intensity > WHALE and rng.random() < 0.002:
                        need += UNIVERSE
                    if d < 0:
                        continue
                    version = BAD_VERSION if (g["platform"] == "android" and d >= BAD_ROLLOUT_DAY
                                              and rng.random() < 0.9) else GOOD_VERSION
                    channel = "web" if rng.random() < 0.15 + 0.17 * d / (DAYS - 1) else "app"
                    broken = (channel == "app" and version == BAD_VERSION and g["region"] == "US-East"
                              and d >= INCIDENT_DAY)
                    if broken or rng.random() < 0.0005:
                        add_purchase(d, g, _bundle(need), channel, version, "failed", 0)
                        if rng.random() > 0.05:         # almost none can gift without coins
                            continue
                    else:
                        add_purchase(d, g, _bundle(need), channel, version, "ok", 0.004)
                    alive = [c for c in favs if _active(by_id[c], d)] \
                        if d % 7 == 0 else favs
                    if len(alive) < len(favs):
                        favs = alive + picker.pick(rng, d, region, len(favs) - len(alive))
                    targets = favs[:2] if rng.random() < 0.25 else favs[:1]
                    for i, c in enumerate(targets):
                        share = need if len(targets) == 1 else (need // 2 + (need % 2 if i == 0 else 0))
                        if share <= 0:
                            continue
                        camp = in_campaign(d) and treated(region)
                        gifts.append({"day": d, "gifter_id": g["gifter_id"], "creator_id": c, "coins": share,
                                      "gifts": max(1, round(share / rng.choice([1, 5, 30, 99, 299]))),
                                      "campaign_id": "C1" if camp else "", "battle": int(camp and rng.random() < 0.6)})

    # The fraud ring: 14 accounts on 3 devices, all web-funded, gifting only the 3 ring creators.
    ring_gifters = []
    devices = [f"dx{i}" for i in range(3)]
    for i in range(14):
        g = new_gifter(RING_SIGNUP + i % 3, "US-South", "android", devices[i % 3])
        g["age_signal"] = "ok"
        ring_gifters.append(g["gifter_id"])
        for d in range(g["signup_day"], RING_LAST + 1):
            if rng.random() > 0.7:
                continue
            need = rng.randint(800, 3000)
            add_purchase(d, g, _bundle(need), "web", GOOD_VERSION, "ok", 0.4)
            camp = in_campaign(d)
            gifts.append({"day": d, "gifter_id": g["gifter_id"], "creator_id": rng.choice(ring_creators),
                          "coins": need, "gifts": max(1, need // 299), "campaign_id": "C1" if camp else "",
                          "battle": int(camp)})

    # Keep only gifters seen in the window.
    seen = {x["gifter_id"] for x in gifts} | {p["gifter_id"] for p in purchases}
    gifters = [g for g in gifters if g["gifter_id"] in seen]

    # Prizes: top 40 campaign gifters by campaign coins.
    camp_coins = {}
    for x in gifts:
        if x["campaign_id"] == "C1":
            camp_coins[x["gifter_id"]] = camp_coins.get(x["gifter_id"], 0) + x["coins"]
    winners = sorted(camp_coins, key=camp_coins.get, reverse=True)[:40]
    prizes = []
    for rank, gid in enumerate(winners, 1):
        prizes.append({"campaign_id": "C1", "rank": rank, "gifter_id": gid,
                       "prize_usd": 500 if rank <= 3 else 200 if rank <= 10 else 50,
                       "status": rng.choices(["sent", "failed", "reissued", "duplicate"], [0.86, 0.06, 0.05, 0.03])[0]})
    campaigns = [dict(CAMPAIGN, prize_budget_usd=sum(p["prize_usd"] for p in prizes))]

    tables = {"creators": creators, "gifters": gifters, "sessions": sessions, "purchases": purchases,
              "gifts": gifts, "campaigns": campaigns, "prizes": prizes}
    truth = {"ring_gifters": ring_gifters, "ring_creators": ring_creators}
    return tables, truth


# --- output -------------------------------------------------------------------

def write_csvs(tables, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, rows in tables.items():
        with open(os.path.join(out_dir, f"{name}.csv"), "w", newline="") as f:
            fields = list(rows[0])
            if "day" in fields:
                fields.insert(fields.index("day") + 1, "date")
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(dict(r, date=day_date(r["day"]).isoformat()) if "day" in r else r)


def write_dashboard_data(tables, path):
    """Compact column arrays (ids replaced by indexes) so the dashboard loads as a plain <script>."""
    gi = {g["gifter_id"]: i for i, g in enumerate(tables["gifters"])}
    ci = {c["creator_id"]: i for i, c in enumerate(tables["creators"])}

    def cols(rows, spec):
        return {k: [f(r) for r in rows] for k, f in spec.items()}

    data = {
        "start": START.isoformat(), "days": DAYS, "regions": REGIONS, "badVersion": BAD_VERSION,
        "campaign": tables["campaigns"][0],
        "creators": cols(tables["creators"], {"id": lambda r: r["creator_id"], "tier": lambda r: r["tier"],
                                              "region": lambda r: r["region"], "agency": lambda r: r["agency"],
                                              "join": lambda r: r["join_day"],
                                              "churn": lambda r: -1 if r["churn_day"] == "" else r["churn_day"]}),
        "gifters": cols(tables["gifters"], {"id": lambda r: r["gifter_id"], "region": lambda r: r["region"],
                                            "platform": lambda r: r["platform"], "signup": lambda r: r["signup_day"],
                                            "device": lambda r: r["device_id"],
                                            "minor": lambda r: int(r["age_signal"] == "flagged_minor")}),
        "gifts": cols(tables["gifts"], {"day": lambda r: r["day"], "g": lambda r: gi[r["gifter_id"]],
                                        "c": lambda r: ci[r["creator_id"]], "coins": lambda r: r["coins"],
                                        "camp": lambda r: int(r["campaign_id"] != "")}),
        "purchases": cols(tables["purchases"], {"day": lambda r: r["day"], "g": lambda r: gi[r["gifter_id"]],
                                                "usd": lambda r: r["usd"], "web": lambda r: int(r["channel"] == "web"),
                                                "bad": lambda r: int(r["app_version"] == BAD_VERSION),
                                                "ok": lambda r: int(r["status"] == "ok"),
                                                "cb": lambda r: r["chargeback"]}),
        "sessions": cols(tables["sessions"], {"day": lambda r: r["day"], "c": lambda r: ci[r["creator_id"]],
                                              "viewers": lambda r: r["viewers"], "hours": lambda r: r["hours"]}),
        "prizes": cols(tables["prizes"], {"rank": lambda r: r["rank"], "g": lambda r: gi[r["gifter_id"]],
                                          "usd": lambda r: r["prize_usd"], "status": lambda r: r["status"]}),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("// Generated by python -m gifting.generate. Synthetic data, do not edit.\n")
        f.write("window.GIFTING_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    t, _ = build()
    write_csvs(t, os.path.join(root, "data"))
    write_dashboard_data(t, os.path.join(root, "dashboard", "data.js"))
    print(f"Wrote {sum(len(v) for v in t.values()):,} rows to data/ and dashboard/data.js "
          f"({len(t['gifters']):,} gifters, {len(t['creators'])} creators, {len(t['gifts']):,} gift rows)")
