# ═══════════════════════════════════════════════════════════════════
# BOT V4 — VP+VWAP+Delta+MTF Scalper · XAU/USDT · BITGET
# Architecture quant institutionnelle · Sans EMA · DXY 4h
# ───────────────────────────────────────────────────────────────────
# V4 vs V2 :
#   ✅ EMA gate supprimée → Structure HH/HL 1h
#   ✅ MTF 4h : Liquidité Equal H/L + Order Blocks
#   ✅ MTF 1h : Sweep detection + Structure HH/HL
#   ✅ DXY 4h : Corrélation négative or/dollar (bonus/malus score)
#   ✅ 1 seule position (pas 2) — fermeture partielle TP1/TP2
#   ✅ Runner supprimé — TP2 fixe par RR Dynamique
#   ✅ DD institutionnel 4 niveaux calibrés (20% max)
#   ✅ Sizing dynamique capital $2000 · marge 30% max · lot max 0.08
#   ✅ Fermeture weekend vendredi 20h UTC
#   ✅ Cartes MTF recalculées seulement sur nouvelle bougie HTF
# ═══════════════════════════════════════════════════════════════════

import time
import json
import logging
import threading
import requests as req
from datetime import datetime, date, timezone
from config import *
from strategy import (
    calc_signal,
    build_liquidity_map_4h, build_order_blocks_4h,
    build_sweep_map_1h,     build_structure_1h,
    build_dxy_structure_4h,
    calc_lot_size,
)
import bitget as exchange
import dashboard

STATE_FILE = "bot_state.json"


# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("bot_v4")


# ── Telegram ────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        log.warning(f"Telegram: {e}")


# ── N8N Journal enrichi V4 ──────────────────────────────────────
def notify_n8n(pos, event_type, pnl_amount, phase, resultat):
    if not N8N_WEBHOOK_URL:
        return
    try:
        now        = datetime.now(timezone.utc)
        entry_time = pos.get("entry_time", now)
        duree      = int((now - entry_time).total_seconds() / 60)
        data = {
            "Date":         now.strftime("%Y-%m-%d"),
            "Heure_UTC":    entry_time.strftime("%H:%M"),
            "Type":         f"{pos.get('trade_id','?')} · {event_type}",
            "Setup":        pos.get("setup", "V4"),
            "Score":        pos.get("score", 0),
            "Entree":       pos["entry"],
            "SL":           pos["sl"],
            "TP1":          pos["tp1"],
            "TP2":          pos["tp2"],
            "Lot_Total":    pos.get("lot_total", 0),
            "Lot_TP1":      pos.get("lot_tp1", 0),
            "Lot_TP2":      pos.get("lot_tp2", 0),
            "ATR":          pos.get("atr", 0),
            "ADX":          pos.get("adx", 0),
            "RRD":          pos.get("rrd", 0),
            "Struct_1h":    pos.get("struct_1h", "N/A"),
            "DXY_4h":       pos.get("dxy_4h", "N/A"),
            "Sweep":        pos.get("sweep", "none"),
            "VWAP":         pos.get("vwap", 0),
            "Delta_Bias":   pos.get("delta", 0),
            "VP_Score":             pos.get("multi_vp_score", None),
            "VP_Bias":              pos.get("multi_vp_bias", "N/A"),
            "VP_Daily":             pos.get("vp_daily_score", None),
            "VP_4H":                pos.get("vp_4h_score", None),
            "VP_Session":           pos.get("vp_session_score", None),
            "VP_Daily_Maturity":    pos.get("vp_daily_maturity", None),
            "VP_Session_Maturity":  pos.get("vp_session_maturity", None),
            "Phase":        phase,
            "Resultat":     resultat,
            "PnL":          round(pnl_amount, 2),
            "Capital_Apres":round(state.paper_balance, 2),
            "Duree_min":    duree,
            "DD_Level":     state.dd_level,
            "WR_Daily":     round(state.daily_wr, 1),
            "WR_Total":     round(state.wr, 1),
        }
        req.post(N8N_WEBHOOK_URL, json=data, timeout=5)
    except Exception as e:
        log.warning(f"N8N: {e}")


# ════════════════════════════════════════════════════════
# ÉTAT GLOBAL
# ════════════════════════════════════════════════════════

class State:
    def __init__(self):
        self.position        = None       # 1 seule position (dict ou None)
        self.last_price      = 0.0
        self.paper_balance   = float(CAPITAL)
        self.paper_pnl       = 0.0
        self.paper_mode      = PAPER_MODE
        self.total_trades    = 0
        self.trade_counter   = 0
        self.wins            = 0
        self.losses          = 0
        self.daily_pnl       = 0.0
        self.daily_trades    = 0
        self.daily_wins      = 0
        self.daily_losses    = 0
        self.start_date      = date.today()
        self.contract_size   = 0.01
        self.peak_capital    = float(CAPITAL)
        self.dd_level        = 0          # 0=normal 1=jaune 2=orange 3=rouge 4=stop
        self.dd_pause_until  = 0
        self.consec_losses   = 0          # Compteur pertes consécutives
        # Cooldown directionnel
        self.last_sl_long    = 0.0
        self.last_sl_short   = 0.0
        # Cartes MTF (recalculées sur nouvelle bougie HTF)
        self.liq_map         = {}
        self.ob_map          = {}
        self.sweep_map       = {}
        self.struct_map      = {}
        self.dxy_map         = {}
        self.last_4h_ts      = 0
        self.last_1h_ts      = 0

    def reset_daily(self):
        if date.today() != self.start_date:
            log.info(f"Nouveau jour | P&L hier: {self.daily_pnl:+.2f}$")
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.daily_wins   = 0
            self.daily_losses = 0
            self.start_date   = date.today()

    @property
    def wr(self):
        t = self.wins + self.losses
        return self.wins / t * 100 if t > 0 else 0.0

    @property
    def daily_wr(self):
        t = self.daily_wins + self.daily_losses
        return self.daily_wins / t * 100 if t > 0 else 0.0

    @property
    def capital(self):
        if PAPER_MODE:
            return self.paper_balance
        try:
            return exchange.get_balance()
        except:
            return self.paper_balance

    def cooldown_remaining(self, side):
        cd = DD_COOLDOWN_N
        if self.dd_level == 1: cd = DD_COOLDOWN_Y
        elif self.dd_level == 2: cd = DD_COOLDOWN_O
        elif self.dd_level >= 3: cd = DD_COOLDOWN_R
        base = COOLDOWN_AFTER_SL_LONG if side == "long" else COOLDOWN_AFTER_SL_SHORT
        elapsed = time.time() - (self.last_sl_long if side=="long" else self.last_sl_short)
        return max(0, max(base, cd) - elapsed)


state  = State()
trades = []


# ── Persistance état ────────────────────────────────────────────
def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "paper_balance":  state.paper_balance,
                "paper_pnl":      state.paper_pnl,
                "peak_capital":   state.peak_capital,
                "dd_level":       state.dd_level,
                "dd_pause_until": state.dd_pause_until,
                "consec_losses":  state.consec_losses,
                "wins":           state.wins,
                "losses":         state.losses,
                "total_trades":   state.total_trades,
                "trade_counter":  state.trade_counter,
                "daily_pnl":      state.daily_pnl,
                "daily_trades":   state.daily_trades,
                "last_sl_long":   state.last_sl_long,
                "last_sl_short":  state.last_sl_short,
                "last_4h_ts":     state.last_4h_ts,
                "last_1h_ts":     state.last_1h_ts,
            }, f)
    except Exception as e:
        log.warning(f"save_state: {e}")


def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        state.paper_balance  = d.get("paper_balance",  float(CAPITAL))
        state.paper_pnl      = d.get("paper_pnl",      0.0)
        state.peak_capital   = d.get("peak_capital",   float(CAPITAL))
        state.dd_level       = d.get("dd_level",       0)
        state.dd_pause_until = d.get("dd_pause_until", 0)
        state.consec_losses  = d.get("consec_losses",  0)
        state.wins           = d.get("wins",           0)
        state.losses         = d.get("losses",         0)
        state.total_trades   = d.get("total_trades",   0)
        state.trade_counter  = d.get("trade_counter",  0)
        state.daily_pnl      = d.get("daily_pnl",      0.0)
        state.daily_trades   = d.get("daily_trades",   0)
        state.last_sl_long   = d.get("last_sl_long",   0.0)
        state.last_sl_short  = d.get("last_sl_short",  0.0)
        state.last_4h_ts     = d.get("last_4h_ts",     0)
        state.last_1h_ts     = d.get("last_1h_ts",     0)
        log.info(f"État chargé | Capital: ${state.paper_balance:.2f} | "
                 f"Trades: {state.total_trades} | WR: {state.wr:.0f}%")
    except FileNotFoundError:
        log.info("Nouvel état initialisé")
    except Exception as e:
        log.warning(f"load_state: {e}")


# ════════════════════════════════════════════════════════
# FERMETURE WEEKEND
# ════════════════════════════════════════════════════════

def is_weekend_close_time():
    """Vendredi 20h UTC → fermer toutes les positions."""
    now = datetime.now(timezone.utc)
    return now.weekday() == WEEKEND_CLOSE_DAY and now.hour >= WEEKEND_CLOSE_HOUR


def close_weekend():
    if state.position is None:
        return
    log.warning("⏰ FERMETURE WEEKEND — Clôture position ouverte")
    pos = state.position
    current_price = state.last_price
    pnl = _calc_pnl(pos["side"], pos["entry"], current_price, pos["lot_total"])
    state.paper_balance += pnl
    if pnl > 0: state.wins += 1
    else:        state.losses += 1
    state.total_trades += 1
    state.paper_pnl    += pnl
    state.daily_pnl    += pnl
    state.position      = None
    tg(f"⏰ <b>Weekend Close</b>\n"
       f"${pos['entry']:.2f}→${current_price:.2f} | {pnl:+.2f}$\n"
       f"Capital: ${state.paper_balance:.2f}")
    notify_n8n(pos, "WEEKEND_CLOSE", pnl, "WEEKEND", "WEEKEND")


# ════════════════════════════════════════════════════════
# DRAWDOWN PROTECTION INSTITUTIONNEL
# ════════════════════════════════════════════════════════

def get_dd_params():
    """
    Retourne (risk_pct, score_malus, cooldown) selon niveau DD actuel.

    Niveau 1 (< 6%)   → Rien ne change — variance naturelle
    Niveau 2 (6-10%)  → Score +0.5 | Cooldown 1200s | Risque inchangé
    Niveau 3 (10-15%) → Risque 1.0% | Score +1.0 | Cooldown 1800s
    Niveau 4 (> 15%)  → Arrêt complet (géré par check_drawdown)
    """
    if state.dd_level == 3:
        return DD_RISK_ORANGE, DD_SCORE_MALUS_O, DD_COOLDOWN_O
    elif state.dd_level == 2:
        return DD_RISK_NORMAL, DD_SCORE_MALUS_Y, DD_COOLDOWN_Y
    else:  # Niveau 1 ou 0 — rien ne change
        return DD_RISK_NORMAL, DD_SCORE_MALUS_N, DD_COOLDOWN_N


def check_drawdown():
    """
    DD Protection institutionnel 4 niveaux — 20% max.
    Retourne True si le bot peut trader, False sinon.
    """
    cap = state.capital
    if cap > state.peak_capital:
        state.peak_capital = cap
        if state.dd_level > 0:
            old = state.dd_level
            state.dd_level = 0
            log.info(f"Nouveau pic ${cap:.2f} — DD remis à zéro (était N{old})")
            tg(f"✅ <b>Nouveau pic: ${cap:.2f}</b> | DD remis à zéro")

    dd = (state.peak_capital - cap) / state.peak_capital if state.peak_capital > 0 else 0

    # Stop total > 20%
    if dd >= DD_STOP_TOTAL:
        if state.dd_level != 4:
            state.dd_level = 4
            log.error(f"🛑 DD STOP TOTAL {dd*100:.1f}% — Bot arrêté")
            tg(f"🛑 <b>DD STOP TOTAL {dd*100:.1f}%</b>\n"
               f"Capital: ${cap:.2f} | Reprise manuelle requise")
        return False

    # Pause active
    if state.dd_pause_until > time.time():
        remaining = int((state.dd_pause_until - time.time()) / 60)
        if remaining % 5 == 0:
            log.info(f"DD pause — {remaining}min restantes")
        return False

    # Fin de pause
    if state.dd_pause_until > 0 and state.dd_pause_until <= time.time():
        log.info(f"Pause DD terminée — reprise (DD: {dd*100:.1f}%)")
        tg(f"🟡 <b>Pause DD terminée</b> | DD: {dd*100:.1f}% | Capital: ${cap:.2f}")
        state.dd_pause_until = 0

    # ── Mise à jour niveau DD selon seuils validés ───────────────
    old_level = state.dd_level
    if   dd >= DD_STOP_TOTAL:    state.dd_level = 4   # > 15% → arrêt
    elif dd >= DD_ALERT_ORANGE:  state.dd_level = 3   # 10-15% → risque réduit
    elif dd >= DD_ALERT_YELLOW:  state.dd_level = 2   # 6-10% → plus sélectif
    else:                        state.dd_level = 1   # < 6% → variance naturelle

    if state.dd_level != old_level:
        dd_pct = round(dd * 100, 1)
        msgs = {
            1: None,  # Niveau 1 : silence — variance naturelle
            2: f"⚠️ <b>DD Niveau 2 — {dd_pct}%</b>\nScore +{DD_SCORE_MALUS_Y} | Cooldown 20min\nBot continue — plus sélectif",
            3: f"🟠 <b>DD Niveau 3 — {dd_pct}%</b>\nRisque → {DD_RISK_ORANGE*100:.0f}% | Score +{DD_SCORE_MALUS_O} | Cooldown 30min\nBot continue — très sélectif",
            4: f"🛑 <b>DD STOP — {dd_pct}%</b>\nBot arrêté — reprise manuelle requise",
        }
        msg = msgs.get(state.dd_level)
        if msg:
            log.warning(msg.replace("<b>","").replace("</b>",""))
            tg(msg + f"\nCapital: ${cap:.2f}")

    # ── Pause séries perdantes — indépendante du DD% ─────────────
    if state.consec_losses >= DD_CONSEC_L2:
        state.dd_pause_until = time.time() + DD_PAUSE_CONSEC2
        state.consec_losses  = 0
        log.warning(f"Série {DD_CONSEC_L2}+ pertes — pause {DD_PAUSE_CONSEC2//60}min")
        tg(f"⚠️ <b>Série {DD_CONSEC_L2} pertes</b> | Pause {DD_PAUSE_CONSEC2//60}min")
        return False

    elif state.consec_losses >= DD_CONSEC_L1:
        state.dd_pause_until = time.time() + DD_PAUSE_CONSEC1
        state.consec_losses  = 0
        log.warning(f"Série {DD_CONSEC_L1} pertes — pause {DD_PAUSE_CONSEC1//60}min")
        return False

    return True


# ════════════════════════════════════════════════════════
# CALCUL P&L
# ════════════════════════════════════════════════════════

def _calc_pnl(side, entry, exit_p, lot):
    """P&L en dollars sur une fermeture partielle ou totale."""
    raw = (exit_p - entry) / entry if side == "long" else (entry - exit_p) / entry
    return round(raw * (lot / 0.01) * 0.01 * entry * LEVERAGE, 2)


# ════════════════════════════════════════════════════════
# OUVERTURE DE POSITION
# ════════════════════════════════════════════════════════

def open_position(signal, risk_pct):
    """
    Ouvre 1 seule position avec sizing dynamique.
    Fermeture partielle : 2/3 au TP1, 1/3 au TP2.
    """
    if state.position is not None:
        return

    side   = signal["signal"]
    entry  = signal["entry"]
    sl     = signal["sl"]
    tp1    = signal["tp1"]
    tp2    = signal["tp2"]
    sl_dist = abs(entry - sl)

    # Sizing avec risque adapté au niveau DD
    risk_usd  = CAPITAL * risk_pct
    lot_total = calc_lot_size(risk_usd, sl_dist, entry)
    lot_tp1   = round(lot_total * LOT_RATIO_TP1, 2)  # 2/3 → min 0.02
    lot_tp2   = round(lot_total * LOT_RATIO_TP2, 2)  # 1/3 → min 0.01
    # LOT_MIN = 0.03 garantit lot_tp1 ≥ 0.02 et lot_tp2 ≥ 0.01
    if lot_tp1 < 0.01: lot_tp1 = 0.01
    if lot_tp2 < 0.01: lot_tp2 = 0.01

    state.trade_counter += 1
    trade_id = f"V4-{state.trade_counter:04d}"

    pos = {
        "trade_id":     trade_id,
        "side":         side,
        "entry":        entry,
        "sl":           sl,
        "tp1":          tp1,
        "tp2":          tp2,
        "lot_total":    lot_total,
        "lot_tp1":      lot_tp1,
        "lot_tp2":      lot_tp2,
        "tp1_hit":      False,    # TP1 atteint → 2/3 fermés
        "sl_lot2":      sl,       # SL du 1/3 restant (remonté au TP1)
        "setup":        signal.get("setup", "V4"),
        "score":        signal.get("score", 0),
        "atr":          signal.get("atr", 0),
        "adx":          signal.get("adx", 0),
        "rrd":          signal.get("rrd", 0),
        "struct_1h":    signal.get("struct_1h", "N/A"),
        "dxy_4h":       signal.get("dxy_4h", "N/A"),
        "sweep":        signal.get("sweep", "none"),
        "vwap":         signal.get("vwap", 0),
        "delta":        signal.get("delta", 0),
        "multi_vp_score":       signal.get("multi_vp_score", None),
        "multi_vp_bias":        signal.get("multi_vp_bias", "N/A"),
        "vp_daily_score":       signal.get("vp_daily_score", None),
        "vp_4h_score":          signal.get("vp_4h_score", None),
        "vp_session_score":     signal.get("vp_session_score", None),
        "vp_daily_maturity":    signal.get("vp_daily_maturity", None),
        "vp_session_maturity":  signal.get("vp_session_maturity", None),
        "entry_time":   datetime.now(timezone.utc),
        "capital_at_entry": state.paper_balance,
    }
    state.position = pos

    rr_cible = round(abs(tp1 - entry) / sl_dist, 2) if sl_dist > 0 else 0
    log.info(
        f"🟢 OPEN {side.upper()} [{signal['setup']}] ${entry:.2f} | "
        f"SL:${sl:.2f} TP1:${tp1:.2f} TP2:${tp2:.2f} | "
        f"Lot:{lot_total} (TP1:{lot_tp1}/TP2:{lot_tp2}) | "
        f"Score:{signal.get('score',0):.1f} | RRd:{signal.get('rrd',0)}"
    )
    tg(
        f"🟢 <b>OPEN {side.upper()} — {signal['setup']}</b>\n"
        f"${entry:.2f} | SL:${sl:.2f} | TP1:${tp1:.2f} | TP2:${tp2:.2f}\n"
        f"Lot:{lot_total} | RRd:{signal.get('rrd',0)} | Score:{signal.get('score',0):.1f}\n"
        f"Struct:{signal.get('struct_1h','?')} | DXY:{signal.get('dxy_4h','?')}"
    )
    notify_n8n(pos, "OPEN", 0, "OPEN", "OPEN")


# ════════════════════════════════════════════════════════
# GESTION DES SORTIES
# ════════════════════════════════════════════════════════

def check_exits(current_price, last_1m_candle):
    """
    Gestion complète des sorties pour 1 position avec fermeture partielle.

    Phase 1 : lot_total ouvert → SL ou TP1
    Phase 2 : TP1 atteint → lot_tp1 fermé, lot_tp2 continue vers TP2
              SL du lot_tp2 remonté au niveau TP1 (breakeven garanti)
    """
    if state.position is None:
        return

    pos  = state.position
    side = pos["side"]
    ep   = pos["entry"]
    h    = last_1m_candle.get("high",  current_price) if last_1m_candle else current_price
    l    = last_1m_candle.get("low",   current_price) if last_1m_candle else current_price
    acct = state.paper_balance

    # ── Phase 1 — TP1 non encore atteint ─────────────────────────
    if not pos["tp1_hit"]:
        # SL total touché
        sl_hit = (side == "long"  and l  <= pos["sl"]) or \
                 (side == "short" and h  >= pos["sl"])
        tp1_hit = (side == "long"  and h  >= pos["tp1"]) or \
                  (side == "short" and l  <= pos["tp1"])

        if sl_hit:
            exit_p = pos["sl"]
            pnl    = _calc_pnl(side, ep, exit_p, pos["lot_total"])
            state.paper_balance += pnl
            state.paper_pnl     += pnl
            state.daily_pnl     += pnl
            state.losses        += 1
            state.daily_losses  += 1
            state.total_trades  += 1
            state.daily_trades  += 1
            state.consec_losses += 1
            if side == "long":  state.last_sl_long  = time.time()
            else:               state.last_sl_short = time.time()
            state.position = None
            acct_pct = pnl / CAPITAL * 100
            log.info(f"❌ SL [{pos['setup']}] ${ep:.2f}→${exit_p:.2f} | "
                     f"{pnl:+.2f}$ ({acct_pct:+.1f}%) | Capital:${state.paper_balance:.2f} | "
                     f"Série:{state.consec_losses}")
            tg(f"❌ <b>SL — {pos['setup']}</b>\n"
               f"${ep:.2f}→${exit_p:.2f} | {pnl:+.2f}$ ({acct_pct:+.2f}%)\n"
               f"Capital: ${state.paper_balance:.2f} | WR: {state.wr:.0f}%")
            notify_n8n(pos, "CLOSE_SL", pnl, "SL", "LOSS")
            trades.append({"e": ep, "x": exit_p, "side": side,
                           "pnl": pnl, "res": "SL", "setup": pos["setup"],
                           "date": datetime.now().strftime("%m/%d %H:%M")})
            return

        if tp1_hit:
            # Fermeture partielle lot_tp1 (2/3)
            pnl_lot1 = _calc_pnl(side, ep, pos["tp1"], pos["lot_tp1"])
            state.paper_balance += pnl_lot1
            state.paper_pnl     += pnl_lot1
            state.daily_pnl     += pnl_lot1
            state.consec_losses  = 0  # TP1 = reset série perdante
            # SL du lot restant remonté au TP1 (breakeven garanti)
            pos["tp1_hit"] = True
            pos["sl_lot2"] = pos["tp1"]
            log.info(f"🟡 TP1 [{pos['setup']}] ${pos['tp1']:.2f} | "
                     f"Lot1 {pos['lot_tp1']} fermé → +{pnl_lot1:.2f}$ | "
                     f"SL Lot2 → ${pos['tp1']:.2f} (breakeven) | "
                     f"Capital:${state.paper_balance:.2f}")
            tg(f"🟡 <b>TP1 — {pos['setup']}</b>\n"
               f"${ep:.2f}→${pos['tp1']:.2f} | +{pnl_lot1:.2f}$ (lot {pos['lot_tp1']})\n"
               f"SL Lot2 → ${pos['tp1']:.2f} | Capital: ${state.paper_balance:.2f}")
            notify_n8n(pos, "TP1_PARTIAL", pnl_lot1, "TP1", "TP1_WIN")
            return

    # ── Phase 2 — TP1 atteint, lot_tp2 en cours ──────────────────
    else:
        sl2_hit = (side == "long"  and l  <= pos["sl_lot2"]) or \
                  (side == "short" and h  >= pos["sl_lot2"])
        tp2_hit = (side == "long"  and h  >= pos["tp2"]) or \
                  (side == "short" and l  <= pos["tp2"])

        if sl2_hit:
            # SL lot2 touché au breakeven → gain nul sur ce lot
            exit_p   = pos["sl_lot2"]
            pnl_lot2 = _calc_pnl(side, ep, exit_p, pos["lot_tp2"])
            state.paper_balance += pnl_lot2
            state.paper_pnl     += pnl_lot2
            state.daily_pnl     += pnl_lot2
            state.wins          += 1  # TP1 atteint = WIN global
            state.daily_wins    += 1
            state.total_trades  += 1
            state.daily_trades  += 1
            state.position       = None
            log.info(f"✅ TP1+BE [{pos['setup']}] | Lot2 BE ${exit_p:.2f} | "
                     f"{pnl_lot2:+.2f}$ | Capital:${state.paper_balance:.2f}")
            tg(f"✅ <b>TP1+BE — {pos['setup']}</b>\n"
               f"Lot2 clôturé au breakeven ${exit_p:.2f}\n"
               f"Capital: ${state.paper_balance:.2f} | WR: {state.wr:.0f}%")
            notify_n8n(pos, "CLOSE_TP1_BE", pnl_lot2, "TP2_BE", "WIN")
            trades.append({"e": ep, "x": exit_p, "side": side,
                           "pnl": pnl_lot2, "res": "TP1_BE", "setup": pos["setup"],
                           "date": datetime.now().strftime("%m/%d %H:%M")})
            return

        if tp2_hit:
            # TP2 atteint — fermeture lot_tp2
            pnl_lot2 = _calc_pnl(side, ep, pos["tp2"], pos["lot_tp2"])
            state.paper_balance += pnl_lot2
            state.paper_pnl     += pnl_lot2
            state.daily_pnl     += pnl_lot2
            state.wins          += 1
            state.daily_wins    += 1
            state.total_trades  += 1
            state.daily_trades  += 1
            state.position       = None
            acct_pct = pnl_lot2 / CAPITAL * 100
            log.info(f"🎯 TP2 [{pos['setup']}] ${pos['tp2']:.2f} | "
                     f"+{pnl_lot2:.2f}$ ({acct_pct:+.1f}%) | "
                     f"Capital:${state.paper_balance:.2f}")
            tg(f"🎯 <b>TP2 — {pos['setup']}</b>\n"
               f"${ep:.2f}→${pos['tp2']:.2f} | +{pnl_lot2:.2f}$ ({acct_pct:+.2f}%)\n"
               f"Capital: ${state.paper_balance:.2f} | WR: {state.wr:.0f}%")
            notify_n8n(pos, "CLOSE_TP2", pnl_lot2, "TP2", "WIN_BOTH")
            trades.append({"e": ep, "x": pos["tp2"], "side": side,
                           "pnl": pnl_lot2, "res": "TP2", "setup": pos["setup"],
                           "date": datetime.now().strftime("%m/%d %H:%M")})
            return


# ════════════════════════════════════════════════════════
# MISE À JOUR CARTES MTF
# ════════════════════════════════════════════════════════

def refresh_htf_maps(candles_4h, candles_1h, candles_dxy_4h):
    """
    Recalcule les cartes MTF uniquement quand une nouvelle bougie HTF se ferme.
    Économise les ressources Railway.
    """
    # Timestamp de la dernière bougie 4h
    if candles_4h:
        ts_4h = candles_4h[-1].get("timestamp", 0)
        if ts_4h != state.last_4h_ts:
            state.liq_map    = build_liquidity_map_4h(candles_4h[:-1])
            state.ob_map     = build_order_blocks_4h(candles_4h[:-1])
            if candles_dxy_4h and DXY_ENABLED:
                state.dxy_map = build_dxy_structure_4h(candles_dxy_4h)
            state.last_4h_ts = ts_4h
            log.info(f"Cartes 4h recalculées | Liq:{len(state.liq_map)} OB:{len(state.ob_map)}")

    # Timestamp de la dernière bougie 1h
    if candles_1h:
        ts_1h = candles_1h[-1].get("timestamp", 0)
        if ts_1h != state.last_1h_ts:
            state.sweep_map  = build_sweep_map_1h(candles_1h[:-1])
            state.struct_map = build_structure_1h(candles_1h[:-1])
            state.last_1h_ts = ts_1h
            log.info(f"Cartes 1h recalculées | Sweep:{len(state.sweep_map)} Struct:{len(state.struct_map)}")


# ════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ════════════════════════════════════════════════════════

def main():
    log.info("=" * 65)
    log.info("  VP+VWAP+DELTA+MTF SCALPER V4 [BITGET] — Sans EMA · DXY 4h")
    log.info(f"  {SYMBOL} · Capital: ${CAPITAL} · Levier: {LEVERAGE}x")
    log.info(f"  MTF: 4h(Liq+OB+DXY) / 1h(Struct+Sweep) / 5m(Signal) / 1m(Confirm)")
    log.info(f"  1 position · Fermeture partielle TP1(2/3)+TP2(1/3)")
    log.info(f"  DD 4 niveaux (20% max) · Lot max:{LOT_MAX} · Marge max:{MARGIN_CAP*100:.0f}%")
    log.info(f"  Mode: {'📄 PAPER' if PAPER_MODE else '💰 LIVE FUTURES'}")
    log.info("=" * 65)

    load_state()

    try:
        info = exchange.get_contract_info(SYMBOL)
        state.contract_size = info["contractSize"]
        log.info(f"1 contrat = {state.contract_size} oz XAU")
        tg(
            f"🤖 <b>VP+VWAP+Delta+MTF Scalper V4</b>\n"
            f"{SYMBOL} · {LEVERAGE}x · Capital: ${CAPITAL}\n"
            f"MTF 4h/1h/5m/1m · Sans EMA · DXY: {'✅' if DXY_ENABLED else '⏸️'}\n"
            f"DD max 20% · Lot max {LOT_MAX} · Marge {MARGIN_CAP*100:.0f}%\n"
            f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE'}"
        )
    except Exception as e:
        log.warning(f"contract_size=0.01 par défaut ({e})")

    if not PAPER_MODE:
        try:
            exchange.set_leverage(SYMBOL, LEVERAGE)
            log.info(f"Levier {LEVERAGE}x configuré")
        except Exception as e:
            log.warning(f"Levier: {e}")

    while True:
        try:
            state.reset_daily()

            # ── Fermeture weekend ─────────────────────────────────
            if is_weekend_close_time():
                close_weekend()
                time.sleep(LOOP_SECONDS)
                continue

            # ── DD Protection ─────────────────────────────────────
            if not check_drawdown():
                time.sleep(LOOP_SECONDS)
                continue

            # ── Fetch données ─────────────────────────────────────
            candles_5m = exchange.get_candles(SYMBOL, INTERVAL_SIGNAL,  CANDLES_5M + 10)
            candles_1m = exchange.get_candles(SYMBOL, INTERVAL_CONFIRM, CANDLES_1M + 5)
            candles_1h = exchange.get_candles(SYMBOL, INTERVAL_HTF_1H,  CANDLES_1H + 10)
            candles_4h = exchange.get_candles(SYMBOL, INTERVAL_HTF_4H,  CANDLES_4H + 10)

            # DXY — désactivé si symbole non disponible sur Bitget
            candles_dxy = []
            if DXY_ENABLED:
                try:
                    candles_dxy = exchange.get_candles(DXY_SYMBOL, INTERVAL_HTF_4H, CANDLES_4H + 10)
                except Exception as e:
                    log.debug(f"DXY non disponible: {e}")

            if not candles_5m or len(candles_5m) < 50:
                log.warning("Bougies 5m insuffisantes")
                time.sleep(30)
                continue

            current_price    = candles_5m[-1]["close"]
            state.last_price = current_price

            # ── Mise à jour cartes MTF ────────────────────────────
            refresh_htf_maps(candles_4h, candles_1h, candles_dxy)

            # ── Sorties ───────────────────────────────────────────
            if state.position:
                last_1m = candles_1m[-1] if candles_1m else None
                check_exits(current_price, last_1m)

            # ── Signal ───────────────────────────────────────────
            signal = {"signal": None, "reason": "–"}

            if state.position is None:
                risk_pct, score_malus, _ = get_dd_params()

                signal = calc_signal(
                    candles_5m, candles_1m,
                    candles_1h, candles_4h, candles_dxy,
                    state.liq_map, state.ob_map,
                    state.sweep_map, state.struct_map, state.dxy_map
                )

                if signal.get("signal"):
                    side_sig = signal["signal"]

                    # Filtre score DD
                    min_score_eff = MIN_SCORE + score_malus
                    if signal.get("score", 0) < min_score_eff:
                        signal = {"signal": None,
                                  "reason": f"DD N{state.dd_level} score {signal['score']:.1f}<{min_score_eff:.1f}"}

                    # Filtre cooldown
                    elif state.cooldown_remaining(side_sig) > 0:
                        cd = state.cooldown_remaining(side_sig)
                        signal = {"signal": None,
                                  "reason": f"Cooldown {side_sig.upper()}: {int(cd/60)}m{int(cd%60):02d}s"}

                    else:
                        open_position(signal, risk_pct)

            # ── Log boucle ────────────────────────────────────────
            if state.position:
                pos = state.position
                ph  = "Ph2" if pos["tp1_hit"] else "Ph1"
                pos_desc = (f"{pos['side'].upper()}[{pos['setup']}]"
                            f"@${pos['entry']:.1f} {ph}")
            else:
                pos_desc = "FLAT"

            dd_pct   = (state.peak_capital - state.capital) / state.peak_capital * 100
            dd_label = ["", "DD⚠️", "DD🟠", "DD🔴", "DD🛑"][min(state.dd_level, 4)]

            log.info(
                f"${current_price:.2f} | {pos_desc} | "
                f"Cap:${state.paper_balance:.2f} | P&L:{state.paper_pnl:+.2f}$ | "
                f"WR:{state.wr:.0f}% | DD:{dd_pct:.1f}%{dd_label} | "
                f"{signal.get('reason', '–')}"
            )

        except KeyboardInterrupt:
            log.info("Bot arrêté manuellement")
            break
        except Exception as e:
            log.error(f"Erreur boucle: {e}", exc_info=True)
            time.sleep(30)
            continue

        save_state()
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    dashboard.init(state, trades)
    t = threading.Thread(target=dashboard.run, daemon=True)
    t.start()
    log.info("Dashboard démarré")
    main()
