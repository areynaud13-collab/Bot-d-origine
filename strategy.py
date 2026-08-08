# ═══════════════════════════════════════════════════════════════════
# STRATEGY V4 — VP+VWAP+Delta+MTF Scalper · XAU/USDT
# Architecture quant institutionnelle · Sans EMA · DXY 4h
# ───────────────────────────────────────────────────────────────────
# Couche 1 — Volume Profile 5m (POC, VAH, VAL)
# Couche 2 — VWAP + bandes ±2SD
# Couche 3 — Delta Order Flow
# Couche 4 — ADX régime de marché
# Couche 5 — RR Dynamique (adaptatif)
# Couche 6 — Liquidité 4h (Equal H/L + Order Blocks + adj TP)
# Couche 7 — Sweep detection 1h
# Couche 8 — Structure HH/HL 1h (remplace EMA)
# Couche 9 — Corrélation DXY 4h (bonus/malus score)
# Confirmation 1m — wick de rejet ≥ 30%
# ═══════════════════════════════════════════════════════════════════

import numpy as np
from datetime import datetime, timezone
from config import *


# ════════════════════════════════════════════════════════
# INDICATEURS DE BASE
# ════════════════════════════════════════════════════════

def calc_atr(highs, lows, closes, period=14):
    """ATR Wilder — longueur exacte len(closes)."""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1, n)]
    atr_vals = [None] * period
    atr_vals.append(np.mean(trs[:period]))
    for i in range(period, len(trs)):
        atr_vals.append((atr_vals[-1] * (period - 1) + trs[i]) / period)
    return atr_vals


def calc_avg_atr(atr_arr, i, period=50):
    """ATR moyen sur period bougies — pour ratio volatilité."""
    vals = [x for x in atr_arr[max(0, i - period):i] if x is not None]
    return np.mean(vals) if vals else atr_arr[i]


def calc_adx(highs, lows, closes, period=14):
    """ADX — détecteur de régime RANGE/TREND."""
    n = len(closes)
    if n < period * 2 + 5:
        return [None] * n
    pd_ = []; md_ = []; tr = []
    for i in range(1, n):
        u = highs[i] - highs[i-1]; d = lows[i-1] - lows[i]
        pd_.append(u if u > d and u > 0 else 0)
        md_.append(d if d > u and d > 0 else 0)
        tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                      abs(lows[i]-closes[i-1])))
    def smooth(arr, p):
        if len(arr) < p: return [0] * len(arr)
        r = [0] * (p - 1); s = sum(arr[:p]); r.append(s)
        for i in range(p, len(arr)): s = s - s/p + arr[i]; r.append(s)
        return r
    at_ = smooth(tr, period); pt = smooth(pd_, period); mt = smooth(md_, period)
    dx = []
    for i in range(len(at_)):
        if at_[i] > 0:
            pi = 100*pt[i]/at_[i]; mi = 100*mt[i]/at_[i]
            dx.append(100*abs(pi-mi)/(pi+mi) if (pi+mi) > 0 else 0)
        else:
            dx.append(0)
    av = [None] * (n - len(dx) + period - 1)
    av_ = np.mean(dx[:period]); av.append(av_)
    for i in range(period, len(dx)):
        av_ = (av_ * (period - 1) + dx[i]) / period; av.append(av_)
    while len(av) < n: av.append(av[-1])
    return av[:n]


def calc_delta_volume(closes, opens, volumes, period):
    """Delta Order Flow — déséquilibre acheteurs/vendeurs."""
    n = len(closes)
    if n < 2:
        return [None] * n, [None] * n
    deltas = []
    for i in range(n):
        bar_range = max(abs(closes[i] - opens[i]), 0.0001)
        buy_frac  = max(0.0, min(1.0, (closes[i] - opens[i] + bar_range) / (2 * bar_range)))
        deltas.append(volumes[i] * (2 * buy_frac - 1))
    bias = [None] * (period - 1)
    prev_bias = [None] * (period - 1)
    for i in range(period - 1, n):
        window    = deltas[i - period + 1:i + 1]
        total_vol = sum(abs(d) for d in window)
        b         = sum(window) / total_vol if total_vol > 0 else 0
        bias.append(b)
        prev_bias.append(bias[i - 1] if i > period - 1 else None)
    return bias, prev_bias


def calc_vwap_session(highs, lows, closes, volumes, timestamps=None):
    """
    VWAP institutionnel avec reset par session de trading.

    3 sessions par jour (méthode desks or institutionnels) :
    ┌─────────────────────────────────────────────────────┐
    │ Session 1 Asie    : reset 00h00 UTC                 │
    │ Session 2 Londres : reset 07h00 UTC                 │
    │ Session 3 New York: reset 13h30 UTC                 │
    └─────────────────────────────────────────────────────┘

    Chaque session a son propre VWAP et ses propres bandes ±2SD.
    Les bandes sont fiables et exploitables dès 30-45min après
    l'ouverture de chaque session.

    Si timestamps non fournis → cumulatif (compatibilité backtest).
    """
    def get_session_id(dt_utc):
        h = dt_utc.hour; m = dt_utc.minute
        total_min = h * 60 + m
        if total_min >= 13 * 60 + 30:
            session = 2
        elif total_min >= 7 * 60:
            session = 1
        else:
            session = 0
        return (dt_utc.date(), session)

    n = len(closes)
    result = [None] * n
    cum_vol = 0.0; cum_tpv = 0.0; cum_tp2v = 0.0
    prev_session = None

    for i in range(n):
        if timestamps is not None:
            dt = datetime.fromtimestamp(timestamps[i], tz=timezone.utc)
            current_session = get_session_id(dt)
            if prev_session is not None and current_session != prev_session:
                cum_vol = 0.0; cum_tpv = 0.0; cum_tp2v = 0.0
            prev_session = current_session

        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        cum_vol  += volumes[i]
        cum_tpv  += tp * volumes[i]
        cum_tp2v += tp * tp * volumes[i]
        if cum_vol < 1e-9: continue
        vwap = cum_tpv / cum_vol
        sd   = max(0.0, cum_tp2v / cum_vol - vwap ** 2) ** 0.5
        result[i] = {
            "vwap":   round(vwap, 2),
            "sd2_up": round(vwap + 2 * sd, 2),
            "sd2_dn": round(vwap - 2 * sd, 2),
            "sd":     round(sd, 2),
        }
    return result


def detect_regime(vwap_arr, adx, i, warmup=9, slope_period=10,
                  slope_bull=0.3, slope_bear=-0.3, adx_min=22):
    """
    Détecte le régime de session via pente VWAP + ADX.
    Retourne TREND_BULL, TREND_BEAR ou RANGE.
    Utilisé uniquement pour les setups de continuation TF1/TF2.
    """
    valid = [(j, vwap_arr[j]) for j in range(max(0, i - slope_period*2), i+1)
             if vwap_arr[j] is not None]
    if len(valid) < warmup or adx is None or adx < adx_min:
        return "RANGE"
    recent = valid[-slope_period:]
    if len(recent) < 2:
        return "RANGE"
    vals = [v["vwap"] for _, v in recent]
    slope = (vals[-1] - vals[0]) / len(vals)
    if slope >= slope_bull:  return "TREND_BULL"
    if slope <= slope_bear:  return "TREND_BEAR"
    return "RANGE"


def calc_volume_profile(highs, lows, closes, volumes, lookback, bins, value_pct):
    """Volume Profile institutionnel — POC, VAH, VAL."""
    if len(closes) < lookback:
        return None
    sl_h = highs[-lookback:]; sl_l = lows[-lookback:]
    sl_v = volumes[-lookback:]
    lo = min(sl_l); hi = max(sl_h)
    if hi - lo < 0.01: return None
    step = (hi - lo) / bins
    prof = [0.0] * bins
    for j in range(len(sl_h)):
        b_lo = max(0,      int((sl_l[j] - lo) / step))
        b_hi = min(bins-1, int((sl_h[j] - lo) / step))
        vpb  = sl_v[j] / max(b_hi - b_lo + 1, 1)
        for b in range(b_lo, b_hi + 1): prof[b] += vpb
    poc_bin = prof.index(max(prof))
    poc     = round(lo + (poc_bin + 0.5) * step, 2)
    total = sum(prof); target = total * value_pct
    cum = prof[poc_bin]; lo_b = poc_bin; hi_b = poc_bin
    while cum < target and (lo_b > 0 or hi_b < bins - 1):
        add_lo = prof[lo_b - 1] if lo_b > 0      else 0
        add_hi = prof[hi_b + 1] if hi_b < bins-1 else 0
        if add_lo >= add_hi and lo_b > 0: lo_b -= 1; cum += prof[lo_b]
        elif hi_b < bins - 1:             hi_b += 1; cum += prof[hi_b]
        else: break
    return {
        "poc": poc,
        "vah": round(lo + (hi_b + 1) * step, 2),
        "val": round(lo + lo_b * step, 2),
    }


def classify_vp_direction(current_vp, previous_vp):
    """
    Classe la migration de valeur d'un Volume Profile.

    BULLISH  : POC, VAH et VAL montent ensemble.
    BEARISH  : POC, VAH et VAL baissent ensemble.
    BALANCED : configuration mixte ou données insuffisantes.
    """
    if current_vp is None or previous_vp is None:
        return "BALANCED"

    poc_up = current_vp["poc"] > previous_vp["poc"]
    vah_up = current_vp["vah"] > previous_vp["vah"]
    val_up = current_vp["val"] > previous_vp["val"]

    poc_dn = current_vp["poc"] < previous_vp["poc"]
    vah_dn = current_vp["vah"] < previous_vp["vah"]
    val_dn = current_vp["val"] < previous_vp["val"]

    if poc_up and vah_up and val_up:
        return "BULLISH"

    if poc_dn and vah_dn and val_dn:
        return "BEARISH"

    return "BALANCED"


# ════════════════════════════════════════════════════════
# RR DYNAMIQUE
# ════════════════════════════════════════════════════════

def calc_rr_dynamique(atr, avg_atr, adx, hour, setup):
    """
    RR dynamique adaptatif selon volatilité, session, ADX et setup.
    Calculé depuis l'entrée initiale — pas depuis le SL remonté.
    """
    ratio = atr / avg_atr if avg_atr and avg_atr > 0 else 1.0
    if ratio < 0.7:    rr = RRD_CALM
    elif ratio < 1.0:  rr = RRD_NORMAL
    elif ratio < 1.5:  rr = RRD_VOLATILE
    else:              rr = RRD_EXPLOSIVE
    # Ajustement session
    if 7 <= hour <= 11:    rr += 0.2   # Londres — peak liquidité
    elif 13 <= hour <= 17: rr += 0.1   # NY
    elif 2 <= hour <= 6:   rr -= 0.1   # Asie — range
    # Ajustement ADX
    if adx and adx > 35:   rr += 0.3   # Trend fort → laisser courir
    elif adx and adx < 25: rr += 0.1   # Range → mean reversion fiable
    # Ajustement setup
    if setup and 'VWAP' in setup: rr += 0.3  # Retournement extrême
    return round(max(RRD_MIN, min(RRD_MAX, rr)), 2)


# ════════════════════════════════════════════════════════
# COUCHES MTF 4H — LIQUIDITÉ
# ════════════════════════════════════════════════════════

def build_liquidity_map_4h(candles_4h):
    """
    Carte Equal Highs/Lows sur 4h.
    Identifie les pools de stops institutionnels.
    Recalculer seulement quand une nouvelle bougie 4h se ferme.
    """
    liq_map = {}
    highs = [c["high"]  for c in candles_4h]
    lows  = [c["low"]   for c in candles_4h]
    lk    = LIQ_LOOKBACK
    tol   = LIQ_TOLERANCE
    for i in range(lk, len(candles_4h)):
        wh = highs[i-lk:i+1]; wl = lows[i-lk:i+1]
        eq_h = []; eq_l = []
        for j in range(len(wh)-1):
            for k in range(j+1, len(wh)):
                if abs(wh[j]-wh[k])/wh[j] < tol:
                    lvl = round((wh[j]+wh[k])/2, 2)
                    if not any(abs(lvl-x)/lvl < tol for x in eq_h):
                        eq_h.append(lvl)
        for j in range(len(wl)-1):
            for k in range(j+1, len(wl)):
                if abs(wl[j]-wl[k])/wl[j] < tol:
                    lvl = round((wl[j]+wl[k])/2, 2)
                    if not any(abs(lvl-x)/lvl < tol for x in eq_l):
                        eq_l.append(lvl)
        ts = candles_4h[i].get("timestamp", i)
        liq_map[ts] = {"eq_highs": sorted(eq_h), "eq_lows": sorted(eq_l)}
    return liq_map


def build_order_blocks_4h(candles_4h):
    """
    Order Blocks institutionnels sur 4h.
    OB Bull : bougie baissière fort volume → haussier → cassure haute
    OB Bear : bougie haussière fort volume → baissier → cassure basse
    """
    ob_map = {}
    highs = [c["high"]   for c in candles_4h]
    lows  = [c["low"]    for c in candles_4h]
    vols  = [c["volume"] for c in candles_4h]
    opens = [c["open"]   for c in candles_4h]
    closes= [c["close"]  for c in candles_4h]
    lk    = OB_LOOKBACK
    for i in range(lk + 2, len(candles_4h)):
        ob_bull = []; ob_bear = []
        for j in range(max(0, i-lk), i-1):
            avg_v = np.mean(vols[max(0,j-20):j]) if j > 20 else vols[j]
            if avg_v <= 0: continue
            # OB Bullish : bougie baissière fort volume + suivante haussière + cassure
            if (closes[j] < opens[j] and
                vols[j] > avg_v * OB_VOL_MULT and
                closes[j+1] > opens[j+1] and
                closes[j+1] > highs[j]):
                if all(lows[k] >= lows[j] * 0.998 for k in range(j+1, i)):
                    ob_bull.append({"h": highs[j], "l": lows[j]})
            # OB Bearish : bougie haussière fort volume + suivante baissière + cassure
            if (closes[j] > opens[j] and
                vols[j] > avg_v * OB_VOL_MULT and
                closes[j+1] < opens[j+1] and
                closes[j+1] < lows[j]):
                if all(highs[k] <= highs[j] * 1.002 for k in range(j+1, i)):
                    ob_bear.append({"h": highs[j], "l": lows[j]})
        ts = candles_4h[i].get("timestamp", i)
        ob_map[ts] = {"ob_bull": ob_bull[-3:], "ob_bear": ob_bear[-3:]}
    return ob_map


def adjust_tp_for_liquidity(side, price, tp, atr, liq_ctx):
    """
    Ajuste le TP pour prendre profit AVANT un obstacle de liquidité 4h.
    Ne bloque pas le trade — optimise uniquement l'objectif de prix.
    """
    margin = atr * LIQ_TP_MARGIN
    if liq_ctx is None:
        return tp
    if side == "long":
        for lvl in liq_ctx.get("eq_highs", []):
            if price < lvl < tp:
                adj = round(lvl - margin, 2)
                if adj > price + atr * 0.5:
                    return adj
    else:
        for lvl in sorted(liq_ctx.get("eq_lows", []), reverse=True):
            if tp < lvl < price:
                adj = round(lvl + margin, 2)
                if adj < price - atr * 0.5:
                    return adj
    return tp


def ob_in_zone(price, atr, ob_ctx, side):
    """Vérifie si le prix est dans une zone d'Order Block institutionnel."""
    if ob_ctx is None:
        return False
    tol = atr * 1.0
    if side == "long":
        for ob in ob_ctx.get("ob_bull", []):
            if ob["l"] - tol <= price <= ob["h"] + tol:
                return True
    else:
        for ob in ob_ctx.get("ob_bear", []):
            if ob["l"] - tol <= price <= ob["h"] + tol:
                return True
    return False


# ════════════════════════════════════════════════════════
# COUCHES MTF 1H — CONTEXTE DE MARCHÉ
# ════════════════════════════════════════════════════════

def build_sweep_map_1h(candles_1h):
    """
    Détecte les sweeps de liquidité sur 1h.
    Sweep bull : low perce sous min récent ET close revient au-dessus
    Sweep bear : high perce au-dessus max récent ET close revient en dessous
    Signal post-sweep = meilleur timing d'entrée institutionnel.
    """
    sweep_map = {}
    highs  = [c["high"]      for c in candles_1h]
    lows   = [c["low"]       for c in candles_1h]
    closes = [c["close"]     for c in candles_1h]
    lk     = SWEEP_LOOKBACK
    tol    = SWEEP_TOL
    for i in range(lk, len(candles_1h)):
        key_high = max(highs[i-lk:i])
        key_low  = min(lows[i-lk:i])
        sweep_bull = lows[i] < key_low * (1 - tol) and closes[i] > key_low
        sweep_bear = highs[i] > key_high * (1 + tol) and closes[i] < key_high
        if sweep_bull or sweep_bear:
            ts = candles_1h[i].get("timestamp", i)
            sweep_map[ts] = {"sweep_bull": sweep_bull, "sweep_bear": sweep_bear}
    return sweep_map


def build_structure_1h(candles_1h):
    """
    Structure HH/HL ou LH/LL sur 1h — remplace l'EMA gate.
    BULLISH : derniers pivots hauts ET bas croissants (HH + HL)
    BEARISH : derniers pivots hauts ET bas décroissants (LH + LL)
    NEUTRAL : sinon
    """
    struct_map = {}
    highs  = [c["high"] for c in candles_1h]
    lows   = [c["low"]  for c in candles_1h]
    lk     = STRUCT_LOOKBACK
    for i in range(lk, len(candles_1h)):
        wh = highs[i-lk:i+1]; wl = lows[i-lk:i+1]
        ph = []; pl = []
        for j in range(1, len(wh)-1):
            if wh[j] > wh[j-1] and wh[j] > wh[j+1]: ph.append(wh[j])
            if wl[j] < wl[j-1] and wl[j] < wl[j+1]: pl.append(wl[j])
        struct = "NEUTRAL"
        if len(ph) >= 2 and len(pl) >= 2:
            if ph[-1] > ph[-2] and pl[-1] > pl[-2]: struct = "BULLISH"
            elif ph[-1] < ph[-2] and pl[-1] < pl[-2]: struct = "BEARISH"
        ts = candles_1h[i].get("timestamp", i)
        struct_map[ts] = struct
    return struct_map


# ════════════════════════════════════════════════════════
# COUCHE DXY 4H — CORRÉLATION NÉGATIVE OR/DOLLAR
# ════════════════════════════════════════════════════════

def build_dxy_structure_4h(candles_dxy_4h):
    """
    Structure HH/HL sur DXY 4h — même logique que structure or 1h.
    BULLISH DXY = dollar fort → défavorable LONG or
    BEARISH DXY = dollar faible → favorable LONG or
    NEUTRAL = pas d'ajustement score
    PAS DE FILTRE DUR — uniquement bonus/malus score.
    """
    if not candles_dxy_4h:
        return {}
    dxy_map = {}
    highs = [c["high"] for c in candles_dxy_4h]
    lows  = [c["low"]  for c in candles_dxy_4h]
    lk    = DXY_LOOKBACK
    for i in range(lk, len(candles_dxy_4h)):
        wh = highs[i-lk:i+1]; wl = lows[i-lk:i+1]
        ph = []; pl = []
        for j in range(1, len(wh)-1):
            if wh[j] > wh[j-1] and wh[j] > wh[j+1]: ph.append(wh[j])
            if wl[j] < wl[j-1] and wl[j] < wl[j+1]: pl.append(wl[j])
        struct = "NEUTRAL"
        if len(ph) >= 2 and len(pl) >= 2:
            if ph[-1] > ph[-2] and pl[-1] > pl[-2]: struct = "BULLISH"
            elif ph[-1] < ph[-2] and pl[-1] < pl[-2]: struct = "BEARISH"
        ts = candles_dxy_4h[i].get("timestamp", i)
        dxy_map[ts] = struct
    return dxy_map


# ════════════════════════════════════════════════════════
# HELPERS CONTEXTE MTF
# ════════════════════════════════════════════════════════

def get_context_at(ctx_map, ts_5m):
    """
    Récupère le contexte HTF actif au moment d'une bougie 5m.
    Lookup O(log n) via timestamps triés.
    """
    if not ctx_map:
        return None
    best_ts = None
    for ts_htf in sorted(ctx_map.keys()):
        if ts_htf <= ts_5m:
            best_ts = ts_htf
        else:
            break
    return ctx_map.get(best_ts) if best_ts is not None else None


# ════════════════════════════════════════════════════════
# CONFIRMATION 1M
# ════════════════════════════════════════════════════════

def confirm_entry_1m(candles_1m, side, level_price, atr):
    """
    Confirmation micro sur 1m avant entrée.
    Wick de rejet ≥ 30% sur le niveau identifié en 5m.
    Retourne (confirmed, entry_price) ou (False, None).
    """
    if not candles_1m or len(candles_1m) < 2:
        return False, None
    for c in reversed(candles_1m[-5:]):
        o  = c.get("open",  c.get("o", 0))
        h  = c.get("high",  c.get("h", 0))
        l  = c.get("low",   c.get("l", 0))
        cl = c.get("close", c.get("c", 0))
        body    = abs(cl - o)
        wick_dn = min(cl, o) - l
        wick_up = h - max(cl, o)
        bar     = h - l
        if bar < 1e-6: continue
        if side == "long":
            touched      = l <= level_price + bar * 0.10
            closed_above = cl > level_price
            if touched and closed_above and cl > o:
                if wick_dn / bar >= 0.30 or body / bar >= 0.50:
                    return True, cl
        else:
            touched      = h >= level_price - bar * 0.10
            closed_below = cl < level_price
            if touched and closed_below and cl < o:
                if wick_up / bar >= 0.30 or body / bar >= 0.50:
                    return True, cl
    return False, None


# ════════════════════════════════════════════════════════
# SIZING DYNAMIQUE
# ════════════════════════════════════════════════════════

def calc_lot_size(risk_usd, sl_dist, price):
    """
    Sizing dynamique avec double plafond :
    1. Lot selon risque 2% du capital
    2. Lot selon marge max 30% du capital
    Prend le minimum des deux + plafond LOT_MAX.

    LOT_MIN = 0.03 — minimum viable pour fermeture partielle cohérente :
        lot_tp1 = 0.03 × 2/3 = 0.02 lot → fermé au TP1
        lot_tp2 = 0.03 × 1/3 = 0.01 lot → fermé au TP2
    En dessous de 0.03, la division 2/3 + 1/3 génère des lots
    inférieurs à 0.01 et un ratio frais/gain intenable.
    """
    if sl_dist <= 0:
        return LOT_MIN
    lot_risk  = risk_usd / (sl_dist * LEVERAGE)
    lot_marge = (CAPITAL * MARGIN_CAP * LEVERAGE) / price
    lot       = min(lot_risk, lot_marge, LOT_MAX)
    return round(max(LOT_MIN, lot), 2)


# ════════════════════════════════════════════════════════
# SIGNAL PRINCIPAL
# ════════════════════════════════════════════════════════

def calc_signal(candles_5m, candles_1m,
                candles_1h=None, candles_4h=None, candles_dxy_4h=None,
                liq_map=None, ob_map=None,
                sweep_map=None, struct_map=None, dxy_map=None):
    """
    Calcule le signal de trading V4.

    Entrées :
        candles_5m      : bougies 5m (signal principal)
        candles_1m      : bougies 1m (confirmation timing)
        candles_1h      : bougies 1h (structure + sweep)
        candles_4h      : bougies 4h or (liquidité + OB)
        candles_dxy_4h  : bougies 4h DXY (corrélation)
        liq_map         : carte Equal H/L 4h (pré-calculée)
        ob_map          : carte Order Blocks 4h (pré-calculée)
        sweep_map       : carte Sweeps 1h (pré-calculée)
        struct_map      : carte Structure 1h (pré-calculée)
        dxy_map         : carte Structure DXY 4h (pré-calculée)

    Retourne dict avec signal, sl, tp1, tp2, lot_total, lot_tp1, lot_tp2, etc.
    """
    # ── Validation données minimales ─────────────────────────────
    if not candles_5m or len(candles_5m) < VP_LOOKBACK + 60:
        return {"signal": None, "reason": "Données 5m insuffisantes"}

    # ── Extraction arrays 5m ─────────────────────────────────────
    highs   = [c["high"]   for c in candles_5m]
    lows    = [c["low"]    for c in candles_5m]
    closes  = [c["close"]  for c in candles_5m]
    opens   = [c["open"]   for c in candles_5m]
    volumes = [c["volume"] for c in candles_5m]

    i = len(candles_5m) - 1  # bougie courante
    ts_now = candles_5m[i].get("timestamp", 0)
    hour   = datetime.fromtimestamp(ts_now, tz=timezone.utc).hour if ts_now else 0

    # ── Indicateurs 5m ───────────────────────────────────────────
    atr_arr = calc_atr(highs, lows, closes, ATR_PERIOD)
    adx_arr = calc_adx(highs, lows, closes, ATR_PERIOD)
    at      = atr_arr[i]
    ax      = adx_arr[i]

    if at is None or at < MIN_ATR:
        return {"signal": None, "reason": f"ATR insuffisant ({at})"}
    if ax is None:
        return {"signal": None, "reason": "ADX non disponible"}

    avg_at = calc_avg_atr(atr_arr, i, 50)

    bias_arr, prev_bias_arr = calc_delta_volume(closes, opens, volumes, DELTA_PERIOD)
    bias      = bias_arr[i]
    prev_bias = prev_bias_arr[i]

    timestamps = [c.get("timestamp", 0) for c in candles_5m]
    vwap_arr = calc_vwap_session(highs, lows, closes, volumes, timestamps)
    vw       = vwap_arr[i]

    vp = calc_volume_profile(highs, lows, closes, volumes,
                              VP_LOOKBACK, VP_BINS, VALUE_PCT)
    if vp is None:
        return {"signal": None, "reason": "VP non calculable"}

    poc = vp["poc"]; vah = vp["vah"]; val = vp["val"]
    price = closes[i]
    tol   = at * TOL_MULT

    # ── ADX — score de base selon régime ─────────────────────────
    if ax < 25:   ms_base = 3.0   # Range
    elif ax > 35: ms_base = 4.0   # Trend fort
    else:         ms_base = 3.5   # Transition

    # ── Delta — signaux d'order flow ─────────────────────────────
    delta_reversal_long  = (bias is not None and prev_bias is not None and
                            prev_bias < -DELTA_EXHAUSTION and
                            bias > prev_bias + 0.20)
    delta_reversal_short = (bias is not None and prev_bias is not None and
                            prev_bias > DELTA_EXHAUSTION and
                            bias < prev_bias - 0.20)
    delta_bull = bias is not None and bias > DELTA_IMBALANCE
    delta_bear = bias is not None and bias < -DELTA_IMBALANCE

    # ── VWAP — extremes ──────────────────────────────────────────
    sd2dn = (vw is not None and price < vw["sd2_dn"] and
             prev_bias is not None and prev_bias < -DELTA_EXHAUSTION and
             bias is not None and bias > prev_bias + 0.15)
    sd2up = (vw is not None and price > vw["sd2_up"] and
             prev_bias is not None and prev_bias > DELTA_EXHAUSTION and
             bias is not None and bias < prev_bias - 0.15)

    # ── Contextes MTF (depuis cartes pré-calculées) ───────────────
    liq_ctx    = get_context_at(liq_map,    ts_now) if liq_map    else None
    ob_ctx     = get_context_at(ob_map,     ts_now) if ob_map     else None
    sweep_ctx  = get_context_at(sweep_map,  ts_now) if sweep_map  else None
    struct_ctx = get_context_at(struct_map, ts_now) if struct_map else "NEUTRAL"
    dxy_ctx    = get_context_at(dxy_map,    ts_now) if dxy_map    else "NEUTRAL"

    # Sweep actif (expiry 4h)
    sweep_bull = False; sweep_bear = False
    if sweep_ctx and isinstance(sweep_ctx, dict):
        sweep_bull = sweep_ctx.get("sweep_bull", False)
        sweep_bear = sweep_ctx.get("sweep_bear", False)

    struct = struct_ctx if isinstance(struct_ctx, str) else "NEUTRAL"
    dxy_struct = dxy_ctx if isinstance(dxy_ctx, str) else "NEUTRAL"

    # ── Gate directionnelle — Structure 1h (remplace EMA) ────────
    long_ok  = (struct == "BULLISH" or struct == "NEUTRAL" or sd2dn)
    short_ok = (struct == "BEARISH" or struct == "NEUTRAL" or sd2up)

    # ── Helper calcul score commun ────────────────────────────────
    def base_score_long(near_level):
        sc = near_level
        if delta_reversal_long:  sc += 2.0
        elif delta_bull:         sc += 1.5
        elif bias and bias > 0:  sc += 0.5
        if ax < 25: sc += 0.5
        # Bonus MTF
        if sweep_bull:                    sc += SWEEP_BONUS
        if struct == "BULLISH":           sc += STRUCT_BONUS
        if ob_ctx and ob_in_zone(price, at, ob_ctx, "long"): sc += OB_BONUS
        # DXY corrélation
        if DXY_ENABLED:
            if dxy_struct == "BEARISH":   sc += DXY_BONUS   # dollar faible = bon LONG or
            elif dxy_struct == "BULLISH": sc -= DXY_MALUS   # dollar fort = mauvais LONG or
        return sc

    def base_score_short(near_level):
        sc = near_level
        if delta_reversal_short: sc += 2.0
        elif delta_bear:         sc += 1.5
        elif bias and bias < 0:  sc += 0.5
        if ax < 25: sc += 0.5
        # Bonus MTF
        if sweep_bear:                     sc += SWEEP_BONUS
        if struct == "BEARISH":            sc += STRUCT_BONUS
        if ob_ctx and ob_in_zone(price, at, ob_ctx, "short"): sc += OB_BONUS
        # DXY corrélation
        if DXY_ENABLED:
            if dxy_struct == "BULLISH":    sc += DXY_BONUS   # dollar fort = bon SHORT or
            elif dxy_struct == "BEARISH":  sc -= DXY_MALUS   # dollar faible = mauvais SHORT or
        return sc

    def build_signal(side, setup, sl, tp1, tp2, score, tags):
        """Construit le dict signal avec sizing."""
        sl_dist = abs(price - sl)
        if sl_dist < MIN_SL_DIST:
            return None
        # Vérif RR minimum
        rr_check = (tp1 - price) / sl_dist if side == "long" else (price - tp1) / sl_dist
        if rr_check < MIN_RR:
            return None
        # Ajustement TP via liquidité 4h
        tp1_adj = adjust_tp_for_liquidity(side, price, tp1, at, liq_ctx)
        tp2_adj = adjust_tp_for_liquidity(side, price, tp2, at, liq_ctx)
        # Vérif RR minimum après ajustement
        rr_adj = (tp1_adj-price)/sl_dist if side=="long" else (price-tp1_adj)/sl_dist
        if rr_adj < MIN_RR:
            return None
        # Sizing dynamique
        risk_usd  = CAPITAL * DD_RISK_NORMAL  # sera ajusté par bot.py selon DD
        lot_total = calc_lot_size(risk_usd, sl_dist, price)
        lot_tp1   = round(lot_total * LOT_RATIO_TP1, 2)
        lot_tp2   = round(lot_total * LOT_RATIO_TP2, 2)
        if lot_tp1 < 0.01: lot_tp1 = 0.01   # minimum absolu par lot partiel
        if lot_tp2 < 0.01: lot_tp2 = 0.01   # LOT_MIN=0.03 garantit déjà ≥0.01
        return {
            "signal":    side,
            "setup":     setup,
            "entry":     price,
            "sl":        sl,
            "tp1":       tp1_adj,
            "tp2":       tp2_adj,
            "lot_total": lot_total,
            "lot_tp1":   lot_tp1,
            "lot_tp2":   lot_tp2,
            "score":     round(score, 2),
            "tags":      tags,
            "atr":       round(at, 2),
            "adx":       round(ax, 1),
            "rrd":       calc_rr_dynamique(at, avg_at, ax, hour, setup),
            "struct_1h": struct,
            "dxy_4h":    dxy_struct,
            "sweep":     "bull" if sweep_bull else ("bear" if sweep_bear else "none"),
            "vwap":      vw["vwap"] if vw else None,
            "delta":     round(bias, 3) if bias else None,
            "reason":    f"{setup} | score={score:.1f} | {' '.join(tags)}",
        }

    # ════════════════════════════════════════════════════
    # SETUPS LONG
    # ════════════════════════════════════════════════════
    if long_ok:
        ms = ms_base - 0.5 if sd2dn else ms_base

        # L1 : VAL → POC
        if (abs(price - val) < tol * 1.5 and poc > price and
                (poc - val) > at * MIN_RANGE):
            sl_p = round(val - at * ATR_SL, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "VAL->POC")
            tp1  = round(price + abs(price - sl_p) * ATR_TP1, 2)
            tp2  = round(price + abs(price - sl_p) * rrd, 2)
            near = 3.0 if price < val else (2.0 if abs(price-val) < tol else 1.0)
            sc   = base_score_long(near)
            if sc >= ms:
                sig = build_signal("long", "VAL->POC", sl_p, tp1, tp2, sc,
                                   ["@VAL", f"ADX{ax:.0f}"])
                if sig: return sig

        # L2 : POC → VAH
        if (abs(price - poc) < tol * 1.5 and price > poc and
                (vah - poc) > at * MIN_RANGE):
            sl_p = round(poc - at * ATR_SL, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "POC->VAH")
            tp1  = round(price + abs(price - sl_p) * ATR_TP1, 2)
            tp2  = round(price + abs(price - sl_p) * rrd, 2)
            sc   = base_score_long(2.0)
            if sc >= ms:
                sig = build_signal("long", "POC->VAH", sl_p, tp1, tp2, sc,
                                   ["@POC", f"ADX{ax:.0f}"])
                if sig: return sig

        # L3 : VWAP -2SD → retournement extrême
        if sd2dn and vw:
            sl_p = round(vw["sd2_dn"] - at * ATR_SL * 0.8, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "VWAP-2SD")
            tp1  = round(price + abs(price - sl_p) * ATR_TP1, 2)
            tp2  = round(price + abs(price - sl_p) * rrd, 2)
            sc   = base_score_long(3.5)
            if sc >= max(ms, 4.0):
                sig = build_signal("long", "VWAP-2SD", sl_p, tp1, tp2, sc,
                                   ["@2SD", "ΔExhaust"])
                if sig: return sig

    # ════════════════════════════════════════════════════
    # SETUPS SHORT
    # ════════════════════════════════════════════════════
    if short_ok:
        ms = ms_base - 0.5 if sd2up else ms_base

        # S1 : VAH → POC
        if (abs(price - vah) < tol * 1.5 and poc < price and
                (vah - poc) > at * MIN_RANGE):
            sl_p = round(vah + at * ATR_SL, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "VAH->POC")
            tp1  = round(price - abs(sl_p - price) * ATR_TP1, 2)
            tp2  = round(price - abs(sl_p - price) * rrd, 2)
            near = 3.0 if price > vah else (2.0 if abs(price-vah) < tol else 1.0)
            sc   = base_score_short(near)
            if sc >= ms:
                sig = build_signal("short", "VAH->POC", sl_p, tp1, tp2, sc,
                                   ["@VAH", f"ADX{ax:.0f}"])
                if sig: return sig

        # S2 : POC → VAL
        if (abs(price - poc) < tol * 1.5 and price < poc and
                (poc - val) > at * MIN_RANGE):
            sl_p = round(poc + at * ATR_SL, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "POC->VAL")
            tp1  = round(price - abs(sl_p - price) * ATR_TP1, 2)
            tp2  = round(price - abs(sl_p - price) * rrd, 2)
            sc   = base_score_short(2.0)
            if sc >= ms:
                sig = build_signal("short", "POC->VAL", sl_p, tp1, tp2, sc,
                                   ["@POC", f"ADX{ax:.0f}"])
                if sig: return sig

        # S3 : VWAP +2SD → retournement extrême
        if sd2up and vw:
            sl_p = round(vw["sd2_up"] + at * ATR_SL * 0.8, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "VWAP+2SD")
            tp1  = round(price - abs(sl_p - price) * ATR_TP1, 2)
            tp2  = round(price - abs(sl_p - price) * rrd, 2)
            sc   = base_score_short(3.5)
            if sc >= max(ms, 4.0):
                sig = build_signal("short", "VWAP+2SD", sl_p, tp1, tp2, sc,
                                   ["@2SD", "ΔExhaust"])
                if sig: return sig

    # ════════════════════════════════════════════════════
    # SETUPS CONTINUATION DE TENDANCE (additifs)
    # TF1 : Pullback VWAP central | TF2 : Retest POC
    # Déclenchés uniquement si aucun setup VP/VWAP±2SD validé
    # ════════════════════════════════════════════════════
    regime = detect_regime(vwap_arr, ax, i,
                           adx_min=TF_ADX_MIN_TREND)

    if regime != "RANGE" and vw:
        vwap_c = vw["vwap"]
        tol_vwap = at * TF_VWAP_TOL

        # ── TF1 : Pullback VWAP central LONG ─────────────────
        # Régime TREND_BULL + prix revient sur VWAP + delta épuisé baissier
        if (regime == "TREND_BULL" and
                abs(price - vwap_c) < tol_vwap and
                price > vwap_c - tol_vwap and
                bias is not None and prev_bias is not None and
                prev_bias < -TF_DELTA_EXHAUST and bias > prev_bias + 0.10):
            sl_p = round(vwap_c - at * ATR_SL * 0.8, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "TF1-VWAP-LONG")
            tp1  = round(price + abs(price - sl_p) * ATR_TP1, 2)
            tp2  = round(price + abs(price - sl_p) * rrd, 2)
            sc   = base_score_long(2.5)
            if sc >= ms_base:
                sig = build_signal("long", "TF1-VWAP", sl_p, tp1, tp2, sc,
                                   ["@VWAP", "TREND_BULL", f"ADX{ax:.0f}"])
                if sig: return sig

        # ── TF1 : Pullback VWAP central SHORT ────────────────
        # Régime TREND_BEAR + prix revient sur VWAP + delta épuisé haussier
        if (regime == "TREND_BEAR" and
                abs(price - vwap_c) < tol_vwap and
                price < vwap_c + tol_vwap and
                bias is not None and prev_bias is not None and
                prev_bias > TF_DELTA_EXHAUST and bias < prev_bias - 0.10):
            sl_p = round(vwap_c + at * ATR_SL * 0.8, 2)
            rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "TF1-VWAP-SHORT")
            tp1  = round(price - abs(sl_p - price) * ATR_TP1, 2)
            tp2  = round(price - abs(sl_p - price) * rrd, 2)
            sc   = base_score_short(2.5)
            if sc >= ms_base:
                sig = build_signal("short", "TF1-VWAP", sl_p, tp1, tp2, sc,
                                   ["@VWAP", "TREND_BEAR", f"ADX{ax:.0f}"])
                if sig: return sig

    # ── TF2 : Retest POC en support LONG ─────────────────────
    # Structure 1h BULLISH + ADX > 25 + prix reteste POC par le dessus + delta positif
    if (struct == "BULLISH" and ax >= TF_ADX_MIN_RETEST and
            price > poc and abs(price - poc) < tol * 1.2 and
            delta_bull):
        sl_p = round(poc - at * ATR_SL, 2)
        rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "TF2-POC-LONG")
        tp1  = round(price + abs(price - sl_p) * ATR_TP1, 2)
        tp2  = round(price + abs(price - sl_p) * rrd, 2)
        sc   = base_score_long(2.5)
        if sc >= ms_base:
            sig = build_signal("long", "TF2-POC", sl_p, tp1, tp2, sc,
                               ["@POC-RETEST", "BULL", f"ADX{ax:.0f}"])
            if sig: return sig

    # ── TF2 : Retest POC en résistance SHORT ─────────────────
    # Structure 1h BEARISH + ADX > 25 + prix reteste POC par le dessous + delta négatif
    if (struct == "BEARISH" and ax >= TF_ADX_MIN_RETEST and
            price < poc and abs(price - poc) < tol * 1.2 and
            delta_bear):
        sl_p = round(poc + at * ATR_SL, 2)
        rrd  = calc_rr_dynamique(at, avg_at, ax, hour, "TF2-POC-SHORT")
        tp1  = round(price - abs(sl_p - price) * ATR_TP1, 2)
        tp2  = round(price - abs(sl_p - price) * rrd, 2)
        sc   = base_score_short(2.5)
        if sc >= ms_base:
            sig = build_signal("short", "TF2-POC", sl_p, tp1, tp2, sc,
                               ["@POC-RETEST", "BEAR", f"ADX{ax:.0f}"])
            if sig: return sig

    # ── Pas de setup ─────────────────────────────────────────────
    vw_str  = f"VWAP:{vw['vwap']:.1f}" if vw else "VWAP:N/A"
    bias_str = f"Δ:{bias:.2f}" if bias is not None else "Δ:N/A"
    return {
        "signal": None,
        "reason": (f"No setup | POC:{poc} VAH:{vah} VAL:{val} | "
                   f"Struct:{struct} DXY:{dxy_struct} | "
                   f"{vw_str} | {bias_str} | ATR:{at:.2f} ADX:{ax:.1f}")
    }
