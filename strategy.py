# ═══════════════════════════════════════════════════════
# STRATEGY — Volume Profile Scalper 1m
# BOT D'ORIGINE — 3 Setups LONG uniquement
#   1. VAL -> POC  (reversion depuis Value Area Low)
#   2. POC -> VAH  (rebond sur POC vers Value Area High)
#   3. HVN -> POC  (High Volume Node comme support)
# Filtres : CDV + Pin Bar + RR + Range
# VALIDATION : TP2 (niveau VP) doit toujours être
#              plus loin que TP1 (ATR-based) — runner cohérent
# ═══════════════════════════════════════════════════════

import numpy as np
from datetime import datetime, timezone
from config import *


def calc_atr(highs, lows, closes, period=14):
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    atr_vals = [None] * period
    atr_vals.append(np.mean(trs[:period]))
    for i in range(period, len(trs)):
        atr_vals.append((atr_vals[-1]*(period-1) + trs[i]) / period)
    return atr_vals + [None]


def calc_cdv(closes, opens, volumes, period=30):
    deltas = [v if c > o else -v if c < o else 0
              for c, o, v in zip(closes, opens, volumes)]
    cdv = [None] * (period - 1)
    for i in range(period - 1, len(closes)):
        cdv.append(sum(deltas[i-period+1:i+1]))
    return cdv


def calc_volume_profile(highs, lows, closes, volumes, lookback, bins, value_pct):
    if len(closes) < lookback:
        return None

    sl_h = highs[-lookback:]
    sl_l = lows[-lookback:]
    sl_c = closes[-lookback:]
    sl_v = volumes[-lookback:]

    lo = min(sl_l)
    hi = max(sl_h)
    rng = hi - lo
    if rng < 0.01:
        return None

    step = rng / bins
    prof = [0.0] * bins

    for j in range(len(sl_c)):
        b_lo = max(0, int((sl_l[j] - lo) / step))
        b_hi = min(bins-1, int((sl_h[j] - lo) / step))
        vol_per_bin = sl_v[j] / max(b_hi - b_lo + 1, 1)
        for b in range(b_lo, b_hi + 1):
            prof[b] += vol_per_bin

    poc_bin = prof.index(max(prof))
    poc = round(lo + (poc_bin + 0.5) * step, 2)

    total = sum(prof)
    target = total * value_pct
    cum = prof[poc_bin]
    lo_b, hi_b = poc_bin, poc_bin

    while cum < target and (lo_b > 0 or hi_b < bins-1):
        add_lo = prof[lo_b-1] if lo_b > 0 else 0
        add_hi = prof[hi_b+1] if hi_b < bins-1 else 0
        if add_lo >= add_hi and lo_b > 0:
            lo_b -= 1
            cum += prof[lo_b]
        elif hi_b < bins-1:
            hi_b += 1
            cum += prof[hi_b]
        else:
            break

    val = round(lo + lo_b * step, 2)
    vah = round(lo + (hi_b + 1) * step, 2)

    avg_vol = total / bins
    hvn = []
    for b in range(bins):
        if prof[b] > avg_vol * 1.5:
            price = round(lo + (b + 0.5) * step, 2)
            if price < poc:
                hvn.append(price)

    return {
        'poc': poc, 'vah': vah, 'val': val,
        'lo': round(lo, 2), 'hi': round(hi, 2),
        'step': round(step, 2), 'hvn': hvn
    }


def avg_volume(volumes, period=20):
    if len(volumes) < period:
        return None
    return sum(volumes[-period:]) / period


def calc_signal(candles):
    if len(candles) < CANDLES_NEEDED:
        return {"signal": None, "reason": "Pas assez de bougies"}

    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    opens   = [c["open"]   for c in candles]
    volumes = [c["volume"] for c in candles]

    i = len(candles) - 1
    d_close = closes[i]
    d_high  = highs[i]
    d_low   = lows[i]
    d_open  = opens[i]
    d_vol   = volumes[i]

    # ATR
    atr_arr = calc_atr(highs, lows, closes, ATR_PERIOD)
    at = atr_arr[i]
    if at is None:
        return {"signal": None, "reason": "ATR insuffisant"}

    recent_atrs = [x for x in atr_arr[max(0,i-50):i] if x is not None]
    avg_at = np.mean(recent_atrs) if recent_atrs else at

    # Filtre ATR minimum
    if at < MIN_ATR:
        return {"signal": None, "reason": f"ATR trop faible ({at:.2f}$) — session morte"}

    # CDV
    cdv_arr = calc_cdv(closes, opens, volumes, CDV_PERIOD)
    cdv  = cdv_arr[i]
    pcdv = cdv_arr[i-1] if i > 0 else None

    cdv_bull        = cdv is not None and cdv > 0 and pcdv is not None and cdv > pcdv
    cdv_absorb_long = cdv is not None and pcdv is not None and cdv > pcdv

    # Volume spike
    avg_vol = avg_volume(volumes, 20)
    vol_sp  = avg_vol is not None and d_vol > avg_vol * VOL_MULT

    # Volume Profile
    vp = calc_volume_profile(highs, lows, closes, volumes,
                              VP_LOOKBACK, VP_BINS, VALUE_PCT)
    if vp is None:
        return {"signal": None, "reason": "Volume Profile insuffisant"}

    poc = vp['poc']
    vah = vp['vah']
    val = vp['val']
    hvn = vp['hvn']

    tol = at * TOL_MULT

    # ══════════════════════════════════════════════════
    # SETUP 1 : VAL -> POC
    # ══════════════════════════════════════════════════
    if d_low <= val + tol * 2 and d_close > val - tol and poc > d_close:
        sl_price  = round(val - at * ATR_SL, 2)
        tp_price  = round(d_close + at * ATR_TP1, 2)
        tp_poc    = round(poc, 2)
        dist_sl   = max(d_close - sl_price, 0.01)
        dist_tp1  = tp_price - d_close
        est_rr    = dist_tp1 / dist_sl
        range_ok  = (poc - val) > at * MIN_RANGE

        # ── VALIDATION RUNNER : TP2 doit être plus loin que TP1 ──
        runner_valid = tp_poc > tp_price

        if est_rr >= MIN_RR and range_ok and runner_valid:
            score = 0
            tags  = []

            if d_low < val:                          score += 3.0; tags.append("wick<VAL")
            elif d_close < val + tol:                score += 2.0; tags.append("@VAL")
            else:                                    score += 1.0; tags.append("~VAL")

            pin_bar = d_low < val and d_close > val and d_close > d_open
            if pin_bar:                              score += 1.5; tags.append("PinBar")
            elif d_close > d_open:                   score += 0.5; tags.append("Bull")

            if cdv is not None and cdv > 0 and cdv_absorb_long:
                                                     score += 2.0; tags.append("CDV+")
            elif cdv_absorb_long:                    score += 1.0; tags.append("CDVup")
            elif cdv_bull:                           score += 0.5; tags.append("CDVbull")

            if vol_sp:                               score += 1.0; tags.append("Vol")
            if at / avg_at < 1.5:                    score += 0.5; tags.append("ATRok")
            if est_rr >= MIN_RR * 1.5:               score += 0.5; tags.append("RR+")

            if score >= MIN_SCORE:
                return {
                    "signal":   "long",
                    "setup":    "VAL->POC",
                    "score":    round(score, 1),
                    "price":    d_close,
                    "atr":      round(at, 2),
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "tp_poc":   tp_poc,
                    "rr":       round(est_rr, 1),
                    "reason":   "VAL->POC | " + "+".join(tags) + f" | RR {est_rr:.1f}",
                }

    # ══════════════════════════════════════════════════
    # SETUP 2 : POC -> VAH
    # ══════════════════════════════════════════════════
    if (abs(d_close - poc) < tol * 1.5 and d_close > d_open):
        prev_dip    = i > 0 and lows[i-1] < poc - tol * 0.5
        bounce_conf = d_close > poc + tol * 0.5 and d_close > d_open
        vah_access  = (vah - poc) <= at * 2.5 and (vah - poc) > at * MIN_RANGE
        cdv_clear   = cdv is not None and cdv > 0 and cdv_bull

        sl_price2 = round(poc - at * ATR_SL, 2)
        tp_price2 = round(d_close + at * ATR_TP1, 2)
        tp_poc2   = round(vah, 2)
        dist_sl2  = max(d_close - sl_price2, 0.01)
        est_rr2   = (tp_price2 - d_close) / dist_sl2

        # ── VALIDATION RUNNER : TP2 doit être plus loin que TP1 ──
        runner_valid2 = tp_poc2 > tp_price2

        if est_rr2 >= MIN_RR and vah_access and prev_dip and bounce_conf and runner_valid2:
            score2 = 0
            tags2  = []

            if abs(d_close - poc) < tol:             score2 += 2.5; tags2.append("@POC")
            else:                                    score2 += 1.5; tags2.append("~POC")
            if bounce_conf:                          score2 += 1.5; tags2.append("Bounce")
            if cdv_clear:                            score2 += 2.0; tags2.append("CDV+")
            elif cdv_absorb_long:                    score2 += 1.0; tags2.append("CDVup")
            if prev_dip:                             score2 += 1.0; tags2.append("DIP")
            if vol_sp:                               score2 += 1.0; tags2.append("Vol")
            if at / avg_at < 1.5:                    score2 += 0.5; tags2.append("ATRok")
            if vah_access:                           score2 += 0.5; tags2.append("VAHok")

            if score2 >= MIN_SCORE:
                return {
                    "signal":   "long",
                    "setup":    "POC->VAH",
                    "score":    round(score2, 1),
                    "price":    d_close,
                    "atr":      round(at, 2),
                    "sl_price": sl_price2,
                    "tp_price": tp_price2,
                    "tp_poc":   tp_poc2,
                    "rr":       round(est_rr2, 1),
                    "reason":   "POC->VAH | " + "+".join(tags2) + f" | RR {est_rr2:.1f}",
                }

    # ══════════════════════════════════════════════════
    # SETUP 3 : HVN -> POC
    # ══════════════════════════════════════════════════
    for hvn_lvl in hvn:
        if abs(d_close - hvn_lvl) > tol * 2:
            continue

        sl_hvn    = round(hvn_lvl - at * ATR_SL, 2)
        tp_hvn    = round(d_close + at * ATR_TP1, 2)
        tp_poc3   = round(poc, 2)
        dist_slh  = max(d_close - sl_hvn, 0.01)
        rr_hvn    = (tp_hvn - d_close) / dist_slh

        # ── VALIDATION RUNNER : TP2 doit être plus loin que TP1 ──
        if rr_hvn < MIN_RR or tp_poc3 <= tp_hvn:
            continue

        score_h = 0
        tags_h  = []

        if d_low < hvn_lvl and d_close > hvn_lvl: score_h += 2.5; tags_h.append("wick<HVN")
        elif d_close > d_open:                     score_h += 1.0; tags_h.append("Bull")
        if cdv_absorb_long:                        score_h += 2.0; tags_h.append("CDV+")
        if vol_sp:                                 score_h += 1.0; tags_h.append("Vol")
        if at / avg_at < 1.5:                      score_h += 0.5; tags_h.append("ATRok")

        if score_h >= MIN_SCORE:
            return {
                "signal":   "long",
                "setup":    "HVN->POC",
                "score":    round(score_h, 1),
                "price":    d_close,
                "atr":      round(at, 2),
                "sl_price": sl_hvn,
                "tp_price": tp_hvn,
                "tp_poc":   tp_poc3,
                "rr":       round(rr_hvn, 1),
                "reason":   f"HVN@{hvn_lvl} | " + "+".join(tags_h) + f" | RR {rr_hvn:.1f}",
            }

    return {
        "signal": None,
        "reason": f"Pas de setup | POC:{poc} VAH:{vah} VAL:{val} CDV:{round(cdv,1) if cdv else 'N/A'}"
    }
