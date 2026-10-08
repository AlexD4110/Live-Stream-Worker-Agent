/* Dashboard maths and diagnosis rules. Pure functions over the arrays in data.js.
 * Same definitions as gifting/metrics.py; tests/test_dashboard.py checks they agree.
 * Works in the browser (window.Metrics) and in Node (require). */
(function (root) {
  'use strict';

  var RING_MIN_ACCOUNTS = 3;     // accounts sharing one device before we call it a cluster

  function pct(x, d) { return (x * 100).toFixed(d === undefined ? 0 : d) + '%'; }
  function signed(x, d) { return (x >= 0 ? '+' : '') + pct(x, d); }
  function num(x) { return Math.round(x).toLocaleString('en-US'); }
  function usd(x) { return '$' + Math.round(x).toLocaleString('en-US'); }

  /* Which gifters pass the filter. f = {region, platform}, both optional. */
  function gifterMask(D, f) {
    var G = D.gifters, n = G.id.length, ok = new Uint8Array(n);
    for (var i = 0; i < n; i++) {
      ok[i] = (!f.region || G.region[i] === f.region) && (!f.platform || G.platform[i] === f.platform) ? 1 : 0;
    }
    return ok;
  }

  function sumDays(daily, lo, hi) {
    var s = 0;
    for (var d = lo; d <= hi; d++) s += daily[d] || 0;
    return s;
  }

  /* Everything the KPI row, charts and decomposition need, for one filter. */
  function summary(D, f) {
    f = f || {};
    var ok = gifterMask(D, f), Gf = D.gifts, S = D.sessions, end = D.days - 1, nG = D.gifters.id.length;
    var daily = new Array(D.days).fill(0), dailyGifters = [], coins = 0, rows = 0;
    var perGifter = new Float64Array(nG), first = new Int32Array(nG).fill(1e9);
    var weekGifters = { cur: new Set(), prev: new Set() }, weekRows = { cur: 0, prev: 0 };
    for (var d = 0; d < D.days; d++) dailyGifters.push(new Set());

    for (var i = 0; i < Gf.day.length; i++) {
      var g = Gf.g[i];
      if (!ok[g]) continue;
      var day = Gf.day[i], c = Gf.coins[i];
      coins += c; rows++; daily[day] += c; perGifter[g] += c;
      if (day < first[g]) first[g] = day;
      dailyGifters[day].add(g);
      if (day > end - 7) { weekGifters.cur.add(g); weekRows.cur++; }
      else if (day > end - 14) { weekGifters.prev.add(g); weekRows.prev++; }
    }

    // viewers come from sessions, filtered by the creator's region (the platform filter cannot apply)
    var viewerDaily = new Array(D.days).fill(0), cReg = D.creators.region;
    for (var j = 0; j < S.day.length; j++) {
      if (f.region && cReg[S.c[j]] !== f.region) continue;
      viewerDaily[S.day[j]] += S.viewers[j];
    }

    // retention: new gifters only (signed up inside the window), first gift = first observed gift
    var dayHit = {}, cohort7 = 0, cohort30 = 0, ret7 = 0, ret30 = 0, i2;
    var hit7 = new Uint8Array(nG), hit30 = new Uint8Array(nG), second = new Uint8Array(nG);
    for (i2 = 0; i2 < Gf.day.length; i2++) {
      var g2 = Gf.g[i2];
      if (!ok[g2] || D.gifters.signup[g2] < 0) continue;
      var lag = Gf.day[i2] - first[g2];
      if (lag >= 1) second[g2] = 1;
      if (lag >= 1 && lag <= 7) hit7[g2] = 1;
      if (lag >= 1 && lag <= 30) hit30[g2] = 1;
    }
    var newGifters = 0, secondGift = 0;
    for (var k = 0; k < nG; k++) {
      if (!ok[k] || D.gifters.signup[k] < 0 || first[k] > 1e8) continue;
      newGifters++;
      if (second[k]) secondGift++;
      if (first[k] <= end - 7) { cohort7++; ret7 += hit7[k]; }
      if (first[k] <= end - 30) { cohort30++; ret30 += hit30[k]; }
    }

    // concentration: top 1% of gifters (at least one) by coins
    var spend = [];
    for (var m = 0; m < nG; m++) if (perGifter[m] > 0) spend.push(perGifter[m]);
    spend.sort(function (a, b) { return b - a; });
    var nTop = Math.max(1, Math.floor(spend.length * 0.01)), topSum = 0;
    for (var t = 0; t < nTop; t++) topSum += spend[t];

    // purchases
    var P = D.purchases, buyers = new Set(), failed = 0, okN = 0, cb = 0, usdSum = 0;
    for (var p = 0; p < P.day.length; p++) {
      if (!ok[P.g[p]]) continue;
      if (!P.ok[p]) { failed++; continue; }
      okN++; cb += P.cb[p]; usdSum += P.usd[p]; buyers.add(P.g[p]);
    }

    var cur = sumDays(daily, end - 6, end), prev = sumDays(daily, end - 13, end - 7);
    var vCur = sumDays(viewerDaily, end - 6, end), vPrev = sumDays(viewerDaily, end - 13, end - 7);
    var gCur = weekGifters.cur.size, gPrev = weekGifters.prev.size;
    // viewers cannot be split by platform, so with a platform filter we break down by gifters instead
    var decomp = null;
    if (prev > 0 && gPrev > 0 && weekRows.prev > 0 && gCur > 0 && weekRows.cur > 0) {
      var coinsPerGift = (cur / weekRows.cur) / (prev / weekRows.prev) - 1, rowsPerGifter = (weekRows.cur / gCur) / (weekRows.prev / gPrev) - 1;
      if (f.platform) {
        decomp = { gifters: gCur / gPrev - 1, giftsPerGifter: rowsPerGifter, coinsPerGift: coinsPerGift };
      } else if (vPrev > 0 && vCur > 0) {
        decomp = { viewers: vCur / vPrev - 1, sendRate: (gCur / vCur) / (gPrev / vPrev) - 1, giftsPerGifter: rowsPerGifter, coinsPerGift: coinsPerGift };
      }
    }
    var gifters = 0;
    for (var q = 0; q < nG; q++) if (perGifter[q] > 0) gifters++;

    return {
      filter: f, coins: coins, giftRows: rows, gifters: gifters, daily: daily, viewerDaily: viewerDaily,
      dailyGifters: dailyGifters.map(function (s) { return s.size; }),
      wow: prev ? cur / prev - 1 : 0, last7: cur, prev7: prev, decomp: decomp,
      sendRate: vCur && !f.platform ? gCur / vCur : null, arppu: gCur ? cur / gCur : 0,
      ret7: cohort7 ? ret7 / cohort7 : 0, ret30: cohort30 ? ret30 / cohort30 : 0,
      newGifters: newGifters, repeatShare: newGifters ? secondGift / newGifters : 0,
      top1: spend.length ? topSum / spend.reduce(function (a, b) { return a + b; }, 0) : 0,
      buyers: buyers.size, purchaseUsd: usdSum, chargebackRate: okN ? cb / okN : 0, failedPurchases: failed,
      campaign: campaign(D, f, ok, deviceClusters(D).flagged)
    };
  }

  /* Campaign: raw lift vs difference-in-differences against the holdout regions. `skip` = accounts to leave out. */
  function campaign(D, f, ok, skip) {
    var c = D.campaign, tr = c.treatment_regions.split('|'), ho = c.holdout_regions.split('|');
    var s = c.start_day, e = c.end_day, Gf = D.gifts, region = D.gifters.region;
    var sums = { t: [0, 0, 0], h: [0, 0, 0] };            // pre (14d), event, after (7d)
    for (var i = 0; i < Gf.day.length; i++) {
      var g = Gf.g[i];
      if (!ok[g] || (skip && skip[g])) continue;
      var day = Gf.day[i], r = region[g], arm = tr.indexOf(r) >= 0 ? sums.t : ho.indexOf(r) >= 0 ? sums.h : null;
      if (!arm) continue;
      if (day >= s - 14 && day < s) arm[0] += Gf.coins[i];
      else if (day >= s && day <= e) arm[1] += Gf.coins[i];
      else if (day > e && day <= e + 7) arm[2] += Gf.coins[i];
    }
    var tp = sums.t[0] / 2, hp = sums.h[0] / 2;
    if (!tp || !hp || !sums.h[1] || !sums.h[2]) return null;
    return {
      name: c.name, start: s, end: e, rawLift: sums.t[1] / tp - 1,
      trueLift: (sums.t[1] / tp) / (sums.h[1] / hp) - 1,
      afterLift: (sums.t[2] / tp) / (sums.h[2] / hp) - 1,
      treatedCoins: sums.t[1], holdoutCoins: sums.h[1], treatment: tr, holdout: ho
    };
  }

  /* Segment table: last 7 days vs the 7 before, for every platform x region. */
  function segments(D) {
    var Gf = D.gifts, end = D.days - 1, out = {}, G = D.gifters;
    for (var i = 0; i < Gf.day.length; i++) {
      var day = Gf.day[i];
      if (day < end - 13) continue;
      var g = Gf.g[i], key = G.platform[g] + '|' + G.region[g];
      var row = out[key] || (out[key] = { platform: G.platform[g], region: G.region[g], cur: 0, prev: 0 });
      if (day > end - 7) row.cur += Gf.coins[i]; else row.prev += Gf.coins[i];
    }
    var list = Object.keys(out).map(function (k) {
      var r = out[k]; r.delta = r.cur - r.prev; r.change = r.prev ? r.cur / r.prev - 1 : 0; return r;
    });
    return list.sort(function (a, b) { return a.delta - b.delta; });
  }

  /* Accounts that share a device with at least RING_MIN_ACCOUNTS-1 others. */
  function deviceClusters(D) {
    var byDev = {}, G = D.gifters;
    for (var i = 0; i < G.id.length; i++) (byDev[G.device[i]] = byDev[G.device[i]] || []).push(i);
    var flagged = new Uint8Array(G.id.length), devices = [];
    Object.keys(byDev).forEach(function (d) {
      if (byDev[d].length >= RING_MIN_ACCOUNTS) {
        devices.push(d);
        byDev[d].forEach(function (i) { flagged[i] = 1; });
      }
    });
    return { flagged: flagged, devices: devices };
  }

  function creatorStats(D) {
    var C = D.creators, end = D.days - 1, n = C.id.length, earned = new Float64Array(n), recent = new Float64Array(n);
    for (var i = 0; i < D.gifts.day.length; i++) {
      var c = D.gifts.c[i]; earned[c] += D.gifts.coins[i];
      if (D.gifts.day[i] > end - 28) recent[c] += D.gifts.coins[i];
    }
    var tiers = {};
    for (var j = 0; j < n; j++) {
      var t = tiers[C.tier[j]] || (tiers[C.tier[j]] = { tier: C.tier[j], creators: 0, active: 0, earning: 0, coins: 0, churnedRecent: 0, churnedBefore: 0 });
      t.creators++;
      var live = C.join[j] <= end && (C.churn[j] < 0 || C.churn[j] > end - 28);
      if (live) { t.active++; if (recent[j] > 0) t.earning++; }
      t.coins += earned[j];
      if (C.churn[j] >= end - 27) t.churnedRecent++;
      else if (C.churn[j] >= end - 55 && C.churn[j] >= 0) t.churnedBefore++;
    }
    var order = { head: 0, mid: 1, emerging: 2 };
    var list = Object.keys(tiers).map(function (k) { return tiers[k]; }).sort(function (a, b) { return order[a.tier] - order[b.tier]; });
    // weekly churn counts for mid-tier creators
    var weeks = Math.ceil(D.days / 7), weekly = [];
    for (var w = 0; w < weeks; w++) weekly.push(0);
    for (var k = 0; k < n; k++) if (C.tier[k] === 'mid' && C.churn[k] >= 0) weekly[Math.floor(C.churn[k] / 7)]++;
    var sorted = Array.prototype.slice.call(earned).sort(function (a, b) { return b - a; }), total = sorted.reduce(function (a, b) { return a + b; }, 0), top10 = 0;
    for (var q = 0; q < 10; q++) top10 += sorted[q];
    return { tiers: list, midChurnWeekly: weekly, top10Share: total ? top10 / total : 0, earned: earned };
  }

  /* The rules engine. Returns findings, most severe first. Each carries evidence, next steps and a guardrail. */
  function findings(D) {
    var out = [], end = D.days - 1, base = summary(D, {}), seg = segments(D), cl = deviceClusters(D);
    var P = D.purchases, G = D.gifters, ringAccounts = [], i;
    for (i = 0; i < G.id.length; i++) if (cl.flagged[i]) ringAccounts.push(i);

    // 1. payments incident: failed purchases jumped, concentrated on one version and segment
    var failLast = 0, failPrev = 0, badFails = 0, failSeg = {};
    for (i = 0; i < P.day.length; i++) {
      if (P.ok[i]) continue;
      if (P.day[i] > end - 7) {
        failLast++;
        if (P.bad[i]) badFails++;
        var gi = P.g[i], key = G.platform[gi] + '|' + G.region[gi];
        failSeg[key] = (failSeg[key] || 0) + 1;
      } else if (P.day[i] > end - 14) failPrev++;
    }
    if (failLast >= 20 && failLast >= 3 * Math.max(failPrev, 1) && seg.length) {
      var worst = seg[0], keys = Object.keys(failSeg).sort(function (a, b) { return failSeg[b] - failSeg[a]; });
      var parts = keys[0].split('|'), name = (parts[0] === 'android' ? 'Android' : 'iOS') + ' ' + parts[1];
      var drop = base.prev7 - base.last7, share = drop > 0 ? -worst.delta / drop : 0;
      out.push({
        id: 'payments_incident', severity: 'high',
        title: 'Coin purchases are failing: ' + name + ' on app ' + D.badVersion,
        evidence: [
          'Failed coin purchases: ' + failLast + ' in the last 7 days vs ' + failPrev + ' the week before.',
          pct(badFails / failLast) + ' of the failures are on app version ' + D.badVersion + '; the largest group is ' + name + ' (' + failSeg[keys[0]] + ' failures).',
          'Gifting is ' + signed(base.wow) + ' week over week; ' + (worst.platform === 'android' ? 'Android' : 'iOS') + ' ' + worst.region + ' accounts for ' + pct(Math.min(share, 1)) + ' of the drop.'
        ],
        actions: ['Treat as an incident: ask engineering and payments to check the ' + D.badVersion + ' purchase flow for ' + name + '.',
                  'Offer a web-coin link or retry prompt to affected users while it is fixed.',
                  'After the fix, message gifters whose purchases failed.'],
        guardrail: 'Check this is a bug and not a fraud block before reversing any payment rule.'
      });
    }

    // 2. suspected gift ring
    if (ringAccounts.length) {
      var ringSet = {}, ringCoins = 0, ringCreators = {}, ringCb = 0, ringOk = 0, ringUsd = 0;
      ringAccounts.forEach(function (a) { ringSet[a] = 1; });
      for (i = 0; i < D.gifts.day.length; i++) if (ringSet[D.gifts.g[i]]) {
        ringCoins += D.gifts.coins[i]; ringCreators[D.gifts.c[i]] = (ringCreators[D.gifts.c[i]] || 0) + D.gifts.coins[i];
      }
      for (i = 0; i < P.day.length; i++) if (ringSet[P.g[i]] && P.ok[i]) { ringOk++; ringCb += P.cb[i]; ringUsd += P.usd[i]; }
      var targets = Object.keys(ringCreators);
      var ringCreatorCoins = 0, allToThem = 0;
      targets.forEach(function (c) { ringCreatorCoins += ringCreators[c]; });
      for (i = 0; i < D.gifts.day.length; i++) if (ringCreators[D.gifts.c[i]]) allToThem += D.gifts.coins[i];
      out.push({
        id: 'gift_ring', severity: 'high', accounts: ringAccounts.map(function (a) { return G.id[a]; }),
        title: 'Possible gift ring: ' + ringAccounts.length + ' accounts on ' + cl.devices.length + ' shared devices',
        evidence: [
          ringAccounts.length + ' accounts share only ' + cl.devices.length + ' devices (' + RING_MIN_ACCOUNTS + '+ accounts per device).',
          'Chargeback rate on their purchases is ' + pct(ringOk ? ringCb / ringOk : 0) + ' vs ' + pct(base.chargebackRate, 1) + ' across all accounts.',
          'They sent ' + num(ringCoins) + ' coins to ' + targets.length + ' creators, who received ' + pct(allToThem ? ringCreatorCoins / allToThem : 0) + ' of their coins from this group.'
        ],
        actions: ['Hold payouts for the ' + targets.length + ' receiving creators and send the cluster to trust and safety for review.',
                  'Leave these accounts out of campaign results and prize lists until reviewed.',
                  'Add a velocity alert: new accounts that buy web coins and gift one creator repeatedly.'],
        guardrail: 'This is a risk signal, not a verdict. Human review before any ban or payout action.'
      });
    }

    // 3. early churn
    var s = base;
    if (s.ret7 < 0.35) {
      out.push({
        id: 'early_churn', severity: 'medium',
        title: 'Most new gifters do not come back',
        evidence: ['Only ' + pct(s.ret7) + ' of new gifters gift again within 7 days, and ' + pct(s.ret30) + ' within 30.',
                   pct(1 - s.repeatShare) + ' of new gifters never send a second gift.'],
        actions: ['Test a second-gift nudge in the first 48 hours (small status unlock or a creator thank-you).',
                  'Compare retention by first creator tier to see where first gifts go wrong.'],
        guardrail: 'Measure against a holdout; watch refunds and complaints so the nudge does not push spending.'
      });
    }

    // 4. whale concentration and lapsed big spenders
    var perG = new Float64Array(G.id.length), lastDay = new Int32Array(G.id.length).fill(-1);
    for (i = 0; i < D.gifts.day.length; i++) {
      var gg = D.gifts.g[i]; perG[gg] += D.gifts.coins[i];
      if (D.gifts.day[i] > lastDay[gg]) lastDay[gg] = D.gifts.day[i];
    }
    var ids = [];
    for (i = 0; i < perG.length; i++) if (perG[i] > 0 && !cl.flagged[i]) ids.push(i);
    ids.sort(function (a, b) { return perG[b] - perG[a]; });
    var topN = Math.max(1, Math.floor(ids.length * 0.01)), lapsed = 0, lapsedCoins = 0;
    for (i = 0; i < topN; i++) if (end - lastDay[ids[i]] >= 21) { lapsed++; lapsedCoins += perG[ids[i]]; }
    if (s.top1 > 0.30) {
      out.push({
        id: 'whale_concentration', severity: lapsed / topN > 0.15 ? 'medium' : 'info',
        title: 'Revenue leans on a few big gifters',
        evidence: ['The top 1% of gifters send ' + pct(s.top1) + ' of all coins.',
                   lapsed + ' of the top ' + topN + ' have not gifted for 21+ days (' + num(lapsedCoins) + ' coins of past spend).'],
        actions: ['Have a person reach out to the lapsed big gifters before offering anything.',
                  'Track the share from the top 1% weekly and grow the middle (repeat gifters) to lower it.'],
        guardrail: 'No pressure on heavy spenders. Offer recognition, not spending prompts, and honour spend limits.'
      });
    }

    // 5. campaign: raw lift overstates true lift
    var skip = cl.flagged, camp = campaign(D, {}, gifterMask(D, {}), skip);
    if (camp && camp.rawLift - camp.trueLift > 0.15) {
      out.push({
        id: 'campaign_overstated', severity: 'medium',
        title: camp.name + ': the headline lift is bigger than the real lift',
        evidence: ['Treated regions grew ' + signed(camp.rawLift) + ' vs the 2 weeks before.',
                   'Against the holdout regions the lift is ' + signed(camp.trueLift) + ' (the holdout also rose in the same week).',
                   'In the week after, treated regions ran ' + signed(camp.afterLift) + ' vs holdout, so some spend was pulled forward.'],
        actions: ['Report ' + signed(camp.trueLift) + ' as incremental, not ' + signed(camp.rawLift) + '.',
                  'Subtract prize cost and the after-event dip before calling the event profitable.'],
        guardrail: 'Suspected fraud accounts are left out of these numbers.'
      });
    }

    // 6. mid-tier creator churn
    var cs = creatorStats(D), mid = cs.tiers.filter(function (t) { return t.tier === 'mid'; })[0];
    if (mid && mid.churnedRecent >= 2 * Math.max(mid.churnedBefore, 1)) {
      out.push({
        id: 'creator_churn', severity: 'medium',
        title: 'Mid-tier creators are leaving faster',
        evidence: [mid.churnedRecent + ' mid-tier creators stopped in the last 4 weeks vs ' + mid.churnedBefore + ' in the 4 weeks before.',
                   'Only ' + mid.earning + ' of ' + mid.active + ' active mid-tier creators earned gifts in the last 28 days.'],
        actions: ['Interview a sample of the creators who left to find the reason.',
                  'Feature mid-tier creators in discovery and event slots before the next campaign.'],
        guardrail: 'Do not read this as a payout-rate problem until the reasons are confirmed; no rate changes without a test.'
      });
    }

    // 7. flagged minors still gifting
    var minors = 0, minorCoins = 0, minorSeen = {};
    for (i = 0; i < D.gifts.day.length; i++) if (G.minor[D.gifts.g[i]]) { minorCoins += D.gifts.coins[i]; minorSeen[D.gifts.g[i]] = 1; }
    minors = Object.keys(minorSeen).length;
    if (minors) {
      out.push({
        id: 'minors_gifting', severity: 'high',
        title: minors + ' accounts flagged as possible minors are still gifting',
        evidence: [minors + ' flagged accounts have sent ' + num(minorCoins) + ' coins.'],
        actions: ['Send the list to trust and safety now; ask for age re-verification and a purchase hold while it is pending.'],
        guardrail: 'Escalate to humans immediately; do not use these accounts in campaign targeting or results.'
      });
    }

    // 8. prizes
    var Z = D.prizes, bad = 0, badUsd = 0, suspects = 0, stat = {};
    for (i = 0; i < Z.rank.length; i++) {
      stat[Z.status[i]] = (stat[Z.status[i]] || 0) + 1;
      if (Z.status[i] !== 'sent') { bad++; badUsd += Z.usd[i]; }
      if (cl.flagged[Z.g[i]]) suspects++;
    }
    if (bad || suspects) {
      out.push({
        id: 'prize_issues', severity: suspects ? 'high' : 'medium', suspectWinners: suspects,
        title: 'Prize fulfilment needs a clean-up',
        evidence: [bad + ' of ' + Z.rank.length + ' prizes are not cleanly sent (' + usd(badUsd) + '): ' +
                   Object.keys(stat).filter(function (k) { return k !== 'sent'; }).map(function (k) { return stat[k] + ' ' + k; }).join(', ') + '.',
                   suspects + ' winner' + (suspects === 1 ? '' : 's') + ' on the suspected-ring list.'],
        actions: ['Reissue failed prizes and recover or void duplicates.',
                  'Hold prizes for suspected accounts until trust and safety clears them.',
                  'Add a reconciliation step: promised vs sent vs failed vs reissued, before and after each campaign.'],
        guardrail: 'Do not remove a winner without review; keep an audit trail of every change.'
      });
    }

    // 9. web coin share (information)
    var webLo = [0, 0], webHi = [0, 0];
    for (i = 0; i < P.day.length; i++) {
      if (!P.ok[i]) continue;
      if (P.day[i] <= 27) { webLo[0] += P.usd[i]; webLo[1] += P.web[i] ? P.usd[i] : 0; }
      else if (P.day[i] >= end - 27) { webHi[0] += P.usd[i]; webHi[1] += P.web[i] ? P.usd[i] : 0; }
    }
    if (webLo[0] && webHi[0] && webHi[1] / webHi[0] - webLo[1] / webLo[0] > 0.05) {
      out.push({
        id: 'web_shift', severity: 'info',
        title: 'More coin purchases are moving to the web',
        evidence: ['Web share of coin purchases went from ' + pct(webLo[1] / webLo[0]) + ' in the first 4 weeks to ' + pct(webHi[1] / webHi[0]) + ' in the last 4.'],
        actions: ['Check whether web buyers gift more or just pay less for the same gifting before promoting web further.'],
        guardrail: 'Judge it on margin and refunds, not web share alone.'
      });
    }

    var order = { high: 0, medium: 1, info: 2 };
    return out.sort(function (a, b) { return order[a.severity] - order[b.severity]; });
  }

  root.Metrics = { summary: summary, findings: findings, segments: segments, creatorStats: creatorStats,
                   deviceClusters: deviceClusters, campaign: campaign, gifterMask: gifterMask,
                   fmt: { pct: pct, signed: signed, num: num, usd: usd } };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.Metrics;
})(typeof window !== 'undefined' ? window : globalThis);
