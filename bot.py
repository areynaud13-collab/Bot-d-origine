# ═══════════════════════════════════════════════════════
# BOT D'ORIGINE — Volume Profile Scalper · BITGET
# 3 Setups LONG : VAL->POC · POC->VAH · HVN->POC
# Runner : Lot 1 ferme au TP1 / Lot 2 continue vers POC/VAH
# ═══════════════════════════════════════════════════════

import time
import logging
import threading
import requests as req
from datetime import datetime, date, timezone
from config import *
from strategy import calc_signal
import bitget as exchange
import dashboard


# ── N8N Journal ────────────────────────────────────────
def notify_n8n(pos, event_type, pnl_lot1, pnl_lot2, total_pnl, phase_atteinte, resultat):
    if not N8N_WEBHOOK_URL:
        return
    try:
        now        = datetime.now(timezone.utc)
        entry_time = pos.get("entry_time", now)
        duree      = int((now - entry_time).total_seconds() / 60)
        data = {
            "Date":           now.strftime("%Y-%m-%d"),
            "Heure_UTC":      entry_time.strftime("%H:%M"),
            "Type":           f"{pos.get('trade_id', '?')} · {event_type}",
            "Setup":          pos.get("setup", "VP"),
            "Score":          pos.get("score", 0),
            "Entree":         pos["entry"],
            "SL":             pos["sl"],
            "TP1":            pos["tp"],
            "TP2_POC":        pos.get("tp_poc", pos["tp"]),
            "ATR":            pos.get("atr", 0),
            "RR_Cible":       pos.get("rr", 0),
            "Phase_Atteinte": phase_atteinte,
            "Resultat":       resultat,
            "PnL_Lot1":       round(pnl_lot1, 2),
            "PnL_Lot2":       round(pnl_lot2, 2),
            "PnL_Total":      round(total_pnl, 2),
            "Capital_Avant":  round(pos.get("capital_at_entry", 0), 2),
            "Capital_Apres":  round(state.paper_balance, 2),
            "Duree_min":      duree,
        }
        req.post(N8N_WEBHOOK_URL, json=data, timeout=5)
    except Exception as e:
        log.warning(f"N8N webhook erreur: {e}")


# ── Telegram ────────────────────────────────────────────
def tg(msg):
    try:
        r = req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
        if not r.ok:
            log.warning(f"Telegram erreur HTTP {r.status_code}: {r.text}")
    except Exception as e:
        log.warning(f"Telegram erreur: {e}")


# ── Logging ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("bot")


# ── État partagé ────────────────────────────────────────
class State:
    def __init__(self):
        self.positions      = []
        self.last_price     = 0.0
        self.paper_balance  = float(CAPITAL)
        self.paper_pnl      = 0.0
        self.paper_mode     = PAPER_MODE
        self.total_trades   = 0
        self.trade_counter  = 0
        self.wins           = 0
        self.losses         = 0
        self.breakevens     = 0
        self.daily_pnl      = 0.0
        self.daily_trades   = 0
        self.start_date     = date.today()
        self.contract_size  = 0.01
        self.last_sl_time   = 0
        self.peak_capital   = float(CAPITAL)
        self.dd_level       = 0
        self.dd_pause_until = 0

    def reset_daily(self):
        if date.today() != self.start_date:
            log.info(f"Nouveau jour · P&L hier: {self.daily_pnl:+.2f}$")
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.start_date   = date.today()

    @property
    def wr(self):
        t = self.wins + self.losses
        return self.wins / t * 100 if t > 0 else 0.0

    @property
    def capital(self):
        if PAPER_MODE:
            return self.paper_balance
        try:
            return exchange.get_balance()
        except:
            return self.paper_balance


state  = State()
trades = []


def calc_qty_risk(price, sl_price, risk_pct):
    cap      = state.capital
    risk_usd = cap * risk_pct
    sl_dist  = abs(price - sl_price)
    if sl_dist <= 0:
        return 1
    contracts_by_risk   = risk_usd / (sl_dist * state.contract_size * LEVERAGE)
    margin_per_contract = (price * state.contract_size) / LEVERAGE
    max_margin          = cap * MAX_MARGIN_PCT
    contracts_by_margin = max_margin / margin_per_contract if margin_per_contract > 0 else contracts_by_risk
    return max(1, int(min(contracts_by_risk, contracts_by_margin)))


def open_position(sig, dd_level=0):
    if len(state.positions) >= MAX_POSITIONS:
        return

    price  = sig["price"]
    atr    = sig["atr"]
    sl     = sig["sl_price"]
    tp     = sig["tp_price"]
    tp_poc = sig.get("tp_poc", tp)
    score  = sig["score"]
    reason = sig["reason"]
    rr     = sig.get("rr", round(abs(tp - price) / max(abs(price - sl), 0.01), 1))
    setup  = sig.get("setup", "VP")

    utc_hour = datetime.now(timezone.utc).hour

    if utc_hour in REDUCED_RISK_HOURS:
        effective_risk = REDUCED_RISK_PCT
    elif dd_level >= 2 and setup == "VAL->POC":
        effective_risk = RISK_PER_TRADE / 2
    else:
        effective_risk = RISK_PER_TRADE

    total_contracts = calc_qty_risk(price, sl, effective_risk)
    if total_contracts >= 2:
        runner_contracts = max(1, int(total_contracts * RUNNER_PCT))
        lot1_contracts   = total_contracts - runner_contracts
    else:
        runner_contracts = 0
        lot1_contracts   = total_contracts

    cap_now   = state.capital
    risk_real = abs(price - sl) * total_contracts * state.contract_size * LEVERAGE
    risk_pct  = risk_real / cap_now * 100

    if risk_pct / 100 < MIN_RISK_PCT:
        log.warning(f"[Guard] Risque {risk_pct:.2f}% < minimum — position annulée")
        return

    log.info(f"SIGNAL LONG {setup} · Score {score} · ${price:.2f} · SL ${sl:.2f} · TP1 ${tp:.2f} · TP2 ${tp_poc:.2f}")

    tg(
        f"🟢 <b>BOT ORIGINE — SIGNAL LONG XAU/USDT — {setup}</b>\n"
        f"\n"
        f"📍 Entrée    : <b>${price:.2f}</b>\n"
        f"🛑 SL        : ${sl:.2f}\n"
        f"🎯 TP1       : ${tp:.2f}  ← fermeture Lot 1\n"
        f"🏃 TP2 (POC) : ${tp_poc:.2f}  ← runner s'active ici\n"
        f"\n"
        f"📊 RR 1:{rr}  · Score {score}/8\n"
        f"💼 Lot 1 : {lot1_contracts} contrats → ferme au TP1\n"
        f"🏃 Lot 2 : {runner_contracts} contrats → runner s'active AU TP2\n"
        f"⚠️  Risque : {risk_pct:.1f}% = ${risk_real:.2f}\n"
        f"💰 Capital : ${cap_now:.2f}\n"
        f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE BITGET'}"
    )

    position_id = None
    if not PAPER_MODE:
        try:
            order = exchange.place_order(1, total_contracts)
            log.info(f"Ordre Bitget: {order}")
            time.sleep(1)
            open_pos = exchange.get_open_positions(SYMBOL)
            if open_pos:
                position_id = open_pos[-1].get("positionId")
                if position_id:
                    exchange.set_stop_loss_take_profit(position_id, sl, tp)
        except Exception as e:
            log.error(f"Erreur ordre: {e}")
            return

    state.trade_counter += 1
    trade_id = f"OG{state.trade_counter:04d}"

    state.positions.append({
        "trade_id":         trade_id,
        "side":             "long",
        "entry":            price,
        "sl":               sl,
        "tp":               tp,
        "tp_poc":           tp_poc,
        "atr":              atr,
        "contracts":        total_contracts,
        "lot1_contracts":   lot1_contracts,
        "runner_contracts": runner_contracts,
        "phase":            1,
        "runner_active":    False,
        "runner_sl":        None,
        "highest_close":    0.0,
        "runner_stall":     0,
        "tp1_pnl":          0.0,
        "capital_at_entry": cap_now,
        "risk_usd":         risk_real,
        "position_id":      position_id,
        "setup":            setup,
        "score":            score,
        "entry_time":       datetime.now(timezone.utc),
        "rr":               rr,
    })
    state.daily_trades += 1
    state.total_trades += 1


def check_exits(current_price, last_candle=None):
    if not state.positions:
        return

    candle_high  = last_candle["high"]  if last_candle else current_price
    candle_low   = last_candle["low"]   if last_candle else current_price
    candle_close = last_candle["close"] if last_candle else current_price

    still_open = []
    for pos in state.positions:
        ep        = pos["entry"]
        atr       = pos["atr"]
        setup     = pos["setup"]
        cap_entry = pos.get("capital_at_entry", state.paper_balance)
        phase     = pos.get("phase", 1)

        # ── PHASE 3 : Runner Chandelier ──────────────────
        if phase == 3:
            runner_sl        = pos["runner_sl"]
            runner_contracts = pos["runner_contracts"]

            if candle_close > pos["highest_close"]:
                pos["highest_close"] = candle_close
                pos["runner_stall"]  = 0
            else:
                pos["runner_stall"] += 1

            new_trail        = pos["highest_close"] - RUNNER_TRAIL_ATR * atr
            pos["runner_sl"] = max(runner_sl, new_trail)
            runner_sl        = pos["runner_sl"]
            hit_sl        = candle_low <= runner_sl
            hit_time_exit = pos["runner_stall"] >= RUNNER_MAX_STALL

            if not hit_sl and not hit_time_exit:
                still_open.append(pos)
                continue

            exit_price = runner_sl if hit_sl else current_price
            exit_label = "SL Chandelier" if hit_sl else f"Time Exit ({RUNNER_MAX_STALL} bougies)"
            pnl_runner = ((exit_price - ep) / ep) * runner_contracts * state.contract_size * ep * LEVERAGE
            tp1_pnl    = pos.get("tp1_pnl", 0.0)
            total_pnl  = tp1_pnl + pnl_runner
            acct_pct   = total_pnl / cap_entry * 100 if cap_entry else 0

            if not PAPER_MODE:
                try:
                    exchange.place_order(2, runner_contracts)
                except Exception as e:
                    log.error(f"Fermeture runner error: {e}")
                    still_open.append(pos)
                    continue

            state.paper_balance += pnl_runner
            state.paper_pnl     += pnl_runner
            state.daily_pnl     += pnl_runner

            if total_pnl > 0.01:   state.wins += 1
            elif abs(total_pnl) < 0.01: state.breakevens += 1
            else:
                state.losses += 1
                state.last_sl_time = time.time()

            trades.append({"e": ep, "x": exit_price, "side": "long",
                           "pnl": round(total_pnl, 2), "res": f"RUNNER — {exit_label}",
                           "setup": setup, "date": datetime.now().strftime("%m/%d %H:%M")})
            notify_n8n(pos, "CLOSE_RUNNER", tp1_pnl, pnl_runner, total_pnl, 3,
                       "WIN" if total_pnl > 0.01 else "LOSS")

            icon = "⏱️" if not hit_sl else ("📉" if pnl_runner < 0 else "💹")
            tg(
                f"{icon} <b>BOT ORIGINE — RUNNER FERMÉ — {setup}</b>\n"
                f"\n"
                f"📍 Entrée      : ${ep:.2f}\n"
                f"✅ TP1 (Lot 1) : ${pos['tp']:.2f}   P&L: +${tp1_pnl:.2f}$\n"
                f"🏁 Runner exit : ${exit_price:.2f}  P&L: {pnl_runner:+.2f}$\n"
                f"\n"
                f"💰 P&L total   : <b>{total_pnl:+.2f}$</b>  ({acct_pct:+.2f}%)\n"
                f"📈 Capital     : ${state.paper_balance:.2f}\n"
                f"🎯 WR          : {state.wr:.0f}%  ({state.wins}W / {state.losses}L)\n"
                f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE BITGET'}"
            )
            continue

        # ── PHASE 2 : Lot 2 vers TP2, SL = TP1 ──────────
        elif phase == 2:
            sl_lot2          = pos["tp"]
            tp2_price        = pos["tp_poc"]
            runner_contracts = pos["runner_contracts"]
            tp1_pnl          = pos.get("tp1_pnl", 0.0)

            hit_sl  = candle_low  <= sl_lot2
            hit_tp2 = candle_high >= tp2_price

            if hit_sl and hit_tp2:
                hit_tp2 = True

            if hit_tp2:
                pos["phase"]         = 3
                pos["runner_active"] = True
                pos["runner_sl"]     = tp2_price
                pos["highest_close"] = candle_close
                pos["runner_stall"]  = 0

                tg(
                    f"🚀 <b>BOT ORIGINE — TP2 ATTEINT — Runner activé — {setup}</b>\n"
                    f"\n"
                    f"📍 Entrée    : ${ep:.2f}\n"
                    f"✅ TP1       : ${pos['tp']:.2f}   P&L: +${tp1_pnl:.2f}$\n"
                    f"🎯 TP2/POC   : ${tp2_price:.2f}  ← SL plancher garanti\n"
                    f"\n"
                    f"🏃 Runner actif : {runner_contracts} contrats\n"
                    f"🛡️ SL garanti  : ${tp2_price:.2f}\n"
                    f"📈 Capital     : ${state.paper_balance:.2f}\n"
                    f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE BITGET'}"
                )
                still_open.append(pos)
                continue

            if hit_sl:
                exit_price = sl_lot2
                pnl_lot2   = ((exit_price - ep) / ep) * runner_contracts * state.contract_size * ep * LEVERAGE
                total_pnl  = tp1_pnl + pnl_lot2
                acct_pct   = total_pnl / cap_entry * 100 if cap_entry else 0

                if not PAPER_MODE:
                    try:
                        exchange.place_order(2, runner_contracts)
                    except Exception as e:
                        log.error(f"Fermeture Lot 2 error: {e}")
                        still_open.append(pos)
                        continue

                state.paper_balance += pnl_lot2
                state.paper_pnl     += pnl_lot2
                state.daily_pnl     += pnl_lot2
                state.wins          += 1

                trades.append({"e": ep, "x": exit_price, "side": "long",
                               "pnl": round(total_pnl, 2), "res": "TP1×2 (retour SL Lot 2)",
                               "setup": setup, "date": datetime.now().strftime("%m/%d %H:%M")})
                notify_n8n(pos, "CLOSE_LOT2_TP1", tp1_pnl, pnl_lot2, total_pnl, 2, "WIN")

                tg(
                    f"✅ <b>BOT ORIGINE — LOT 2 FERMÉ au retour TP1 — {setup}</b>\n"
                    f"\n"
                    f"📍 Entrée    : ${ep:.2f}\n"
                    f"✅ TP1       : ${pos['tp']:.2f}   P&L: +${tp1_pnl:.2f}$\n"
                    f"🛑 SL Lot 2  : ${exit_price:.2f}  P&L: +${pnl_lot2:.2f}$\n"
                    f"\n"
                    f"💰 P&L total : <b>+{total_pnl:.2f}$</b>  ({acct_pct:+.2f}%)\n"
                    f"📈 Capital   : ${state.paper_balance:.2f}\n"
                    f"🎯 WR        : {state.wr:.0f}%  ({state.wins}W / {state.losses}L)\n"
                    f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE BITGET'}"
                )
                continue

            still_open.append(pos)
            continue

        # ── PHASE 1 : Les 2 lots, SL initial ─────────────
        else:
            sl = pos["sl"]
            tp = pos["tp"]

            hit_tp = candle_high >= tp
            hit_sl = candle_low  <= sl

            if hit_sl and hit_tp:
                hit_sl = True

            if not hit_sl and not hit_tp:
                still_open.append(pos)
                continue

            lot1_contracts   = pos["lot1_contracts"]
            runner_contracts = pos["runner_contracts"]
            total_contracts  = pos["contracts"]

            # TP1 touché → fermer Lot 1, passer Phase 2
            if hit_tp and runner_contracts > 0:
                pnl_lot1  = ((tp - ep) / ep) * lot1_contracts * state.contract_size * ep * LEVERAGE
                acct_lot1 = pnl_lot1 / cap_entry * 100 if cap_entry else 0

                if not PAPER_MODE:
                    try:
                        exchange.place_order(2, lot1_contracts)
                    except Exception as e:
                        log.error(f"Fermeture Lot 1 error: {e}")
                        still_open.append(pos)
                        continue

                state.paper_balance += pnl_lot1
                state.paper_pnl     += pnl_lot1
                state.daily_pnl     += pnl_lot1

                pos["phase"]   = 2
                pos["tp1_pnl"] = pnl_lot1

                tg(
                    f"✅ <b>BOT ORIGINE — TP1 ATTEINT — Lot 1 fermé — {setup}</b>\n"
                    f"\n"
                    f"📍 Entrée    : ${ep:.2f}\n"
                    f"🎯 TP1       : ${tp:.2f}\n"
                    f"💰 Lot 1 P&L : <b>+${pnl_lot1:.2f}$</b>  ({acct_lot1:+.2f}%)\n"
                    f"\n"
                    f"📊 <b>Phase 2 — Lot 2 vers POC</b> : {runner_contracts} contrats\n"
                    f"🛑 SL Lot 2  : ${tp:.2f}  (= TP1 — profit garanti)\n"
                    f"🏁 TP2 (POC) : ${pos['tp_poc']:.2f}\n"
                    f"\n"
                    f"📈 Capital   : ${state.paper_balance:.2f}\n"
                    f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE BITGET'}"
                )
                still_open.append(pos)
                continue

            # TP1 touché, 1 seul contrat → fermer tout
            if hit_tp and runner_contracts == 0:
                pnl_usd  = ((tp - ep) / ep) * total_contracts * state.contract_size * ep * LEVERAGE
                acct_pct = pnl_usd / cap_entry * 100 if cap_entry else 0

                if not PAPER_MODE:
                    try:
                        exchange.place_order(2, total_contracts)
                    except Exception as e:
                        log.error(f"Fermeture TP1 error: {e}")
                        still_open.append(pos)
                        continue

                state.paper_balance += pnl_usd
                state.paper_pnl     += pnl_usd
                state.daily_pnl     += pnl_usd
                state.wins          += 1

                trades.append({"e": ep, "x": tp, "side": "long",
                               "pnl": round(pnl_usd, 2), "res": "TP1",
                               "setup": setup, "date": datetime.now().strftime("%m/%d %H:%M")})
                notify_n8n(pos, "CLOSE_TP1", pnl_usd, 0, pnl_usd, 1, "WIN")

                tg(
                    f"🎯 <b>BOT ORIGINE — TP1 ATTEINT — {setup}</b>\n"
                    f"💰 P&L : <b>+{pnl_usd:.2f}$</b>  ({acct_pct:+.2f}%)\n"
                    f"📈 Capital : ${state.paper_balance:.2f}\n"
                    f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE BITGET'}"
                )
                continue

            # SL touché → fermer les 2 lots
            pnl_usd  = ((sl - ep) / ep) * total_contracts * state.contract_size * ep * LEVERAGE
            acct_pct = pnl_usd / cap_entry * 100 if cap_entry else 0

            if not PAPER_MODE:
                try:
                    exchange.place_order(2, total_contracts)
                except Exception as e:
                    log.error(f"Fermeture SL error: {e}")
                    still_open.append(pos)
                    continue

            state.paper_balance += pnl_usd
            state.paper_pnl     += pnl_usd
            state.daily_pnl     += pnl_usd
            state.losses        += 1
            state.last_sl_time   = time.time()

            trades.append({"e": ep, "x": sl, "side": "long",
                           "pnl": round(pnl_usd, 2), "res": "SL",
                           "setup": setup, "date": datetime.now().strftime("%m/%d %H:%M")})
            notify_n8n(pos, "CLOSE_SL", pnl_usd, 0, pnl_usd, 1, "LOSS")

            tg(
                f"❌ <b>BOT ORIGINE — SL TOUCHÉ — {setup}</b>\n"
                f"\n"
                f"📍 Entrée  : ${ep:.2f}\n"
                f"🛑 SL      : ${sl:.2f}\n"
                f"📏 Distance: {abs(sl - ep):.2f}$\n"
                f"\n"
                f"💰 P&L     : <b>{pnl_usd:+.2f}$</b>  ({acct_pct:+.2f}%)\n"
                f"📈 Capital : ${state.paper_balance:.2f}\n"
                f"🎯 WR      : {state.wr:.0f}%  ({state.wins}W / {state.losses}L)\n"
                f"📊 P&L jour: {state.daily_pnl:+.2f}$\n"
                f"⏸️ Cooldown: {COOLDOWN_AFTER_SL//60} min\n"
                f"{'📄 PAPER MODE' if PAPER_MODE else '💰 LIVE BITGET'}"
            )

    state.positions = still_open


def check_drawdown():
    cap = state.capital
    if cap > state.peak_capital:
        state.peak_capital = cap
        if state.dd_level > 0:
            state.dd_level = 0
            tg(f"✅ <b>Nouveau pic : ${cap:.2f}</b>\nDrawdown remis à zéro")

    dd = (state.peak_capital - cap) / state.peak_capital if state.peak_capital > 0 else 0

    if state.dd_pause_until > time.time():
        remaining = int((state.dd_pause_until - time.time()) / 60)
        log.info(f"DD pause niveau 3 — {remaining}min restantes")
        return False

    old_level = state.dd_level
    if dd >= DD_LEVEL3:   state.dd_level = 3
    elif dd >= DD_LEVEL2: state.dd_level = 2
    elif dd >= DD_LEVEL1: state.dd_level = 1
    else:                 state.dd_level = 0

    if state.dd_level != old_level:
        dd_pct = round(dd * 100, 1)
        if state.dd_level == 3:
            state.dd_pause_until = time.time() + DD_PAUSE
            tg(f"🔴 <b>DRAWDOWN Niveau 3 — {dd_pct}%</b>\nPAUSE 1 heure")
            return False
        elif state.dd_level == 2:
            tg(f"🟠 <b>DRAWDOWN Niveau 2 — {dd_pct}%</b>")
        elif state.dd_level == 1:
            tg(f"⚠️ <b>DRAWDOWN Niveau 1 — {dd_pct}%</b>")

    return state.dd_level < 3


def main():
    log.info("=" * 58)
    log.info("🤖  BOT D'ORIGINE — VP SCALPER BITGET")
    log.info(f"    {SYMBOL} · Capital: ${CAPITAL} · Levier: {LEVERAGE}×")
    log.info(f"    Setups LONG: VAL->POC · POC->VAH · HVN->POC")
    log.info(f"    Runner: Lot 1 → TP1 / Lot 2 → POC/VAH")
    log.info(f"    Mode: {'📄 PAPER' if PAPER_MODE else '💰 LIVE'}")
    log.info("=" * 58)

    tg(
        f"🤖 <b>BOT D'ORIGINE démarré sur Bitget</b>\n"
        f"📊 {SYMBOL} · Levier {LEVERAGE}x · 1 contrat = {state.contract_size} oz\n"
        f"🎯 Stratégie : VP 1m · Lot 1 → TP1 · Lot 2 → Runner POC\n"
        f"💰 Capital : ${CAPITAL:.2f}\n"
        f"{'📄 PAPER MODE — aucun ordre réel' if PAPER_MODE else '💰 LIVE MODE'}"
    )

    try:
        info = exchange.get_contract_info(SYMBOL)
        state.contract_size = info["contractSize"]
        log.info(f"1 lot = {state.contract_size} oz XAU")
    except Exception as e:
        log.warning(f"contract_size=0.01 par défaut ({e})")

    if not PAPER_MODE:
        try:
            exchange.set_leverage(SYMBOL, LEVERAGE)
        except Exception as e:
            log.warning(f"Levier: {e}")

    while True:
        try:
            state.reset_daily()

            if not check_drawdown():
                time.sleep(LOOP_SECONDS)
                continue

            candles = exchange.get_candles(SYMBOL, INTERVAL, CANDLES_NEEDED + 10)
            if not candles:
                time.sleep(30)
                continue

            current_price    = candles[-1]["close"]
            state.last_price = current_price

            if state.positions:
                last_closed = candles[-2] if len(candles) >= 2 else candles[-1]
                check_exits(current_price, last_closed)

            signal = {"signal": None}
            if len(state.positions) < MAX_POSITIONS:
                cooldown_remaining = COOLDOWN_AFTER_SL - (time.time() - state.last_sl_time)
                if cooldown_remaining > 0:
                    signal = {"signal": None, "reason": f"Cooldown SL: {int(cooldown_remaining//60)}min"}
                else:
                    signal = calc_signal(candles)
                    if signal.get("signal"):
                        open_position(signal, state.dd_level)

            dd_pct = (state.peak_capital - state.capital) / state.peak_capital * 100 if state.peak_capital > 0 else 0
            pos_desc = " | ".join(f"LONG[{p['setup']}]@${p['entry']:.1f}" for p in state.positions) or "FLAT"
            log.info(
                f"${current_price:.2f} │ {pos_desc} │ "
                f"Capital: ${state.paper_balance:.2f} │ "
                f"P&L: {state.paper_pnl:+.2f}$ │ "
                f"WR: {state.wr:.0f}% │ DD: {dd_pct:.1f}% │ "
                f"→ {signal.get('reason', '–')}"
            )

        except KeyboardInterrupt:
            log.info("Bot arrêté")
            break
        except Exception as e:
            log.error(f"Erreur: {e}")
            time.sleep(30)
            continue

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    dashboard.init(state, trades)
    t = threading.Thread(target=dashboard.run, daemon=True)
    t.start()
    main()
