"""Deterministic metric math. Every number the dashboard or agent states comes from here.

Gift rows are dicts with at least: day (int), gifter_id, creator_id, coins.
"""
from collections import defaultdict


def daily_sum(rows, field):
    """{day: sum of field} over rows."""
    out = defaultdict(int)
    for r in rows:
        out[r["day"]] += r[field]
    return dict(out)


def retention(gifts, window, end_day):
    """Share of gifters who gift again within days 1..window after their first gift.

    Only gifters whose first gift is at least `window` days before end_day are counted,
    so every cohort member had the full window to come back.
    """
    days = defaultdict(set)
    for g in gifts:
        days[g["gifter_id"]].add(g["day"])
    cohort = retained = 0
    for ds in days.values():
        first = min(ds)
        if first > end_day - window:
            continue
        cohort += 1
        if any(first < d <= first + window for d in ds):
            retained += 1
    return retained / cohort if cohort else 0.0


def top_share(gifts, pct):
    """Share of all coins sent by the top `pct` of gifters (at least one gifter)."""
    totals = defaultdict(int)
    for g in gifts:
        totals[g["gifter_id"]] += g["coins"]
    ranked = sorted(totals.values(), reverse=True)
    n = max(1, int(len(ranked) * pct))
    total = sum(ranked)
    return sum(ranked[:n]) / total if total else 0.0


def did_lift(treat_pre, treat_post, hold_pre, hold_post):
    """Difference-in-differences lift: treatment growth relative to holdout growth."""
    return (treat_post / treat_pre) / (hold_post / hold_pre) - 1


def wow_change(daily, end_day):
    """Last 7 days ending end_day vs the 7 days before."""
    cur = sum(daily.get(d, 0) for d in range(end_day - 6, end_day + 1))
    prev = sum(daily.get(d, 0) for d in range(end_day - 13, end_day - 6))
    return cur / prev - 1 if prev else 0.0
