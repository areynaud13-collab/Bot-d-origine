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
import os
import shutil
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
from bitget_ws import BitgetPublicWS
from entry_shadow import EntryShadowLab

STATE_FILE = "/data/bot_state.json"
STATE_BACKUP_FILE = "/data/bot_state.backup.json"
STATE_VERSION = 3
PRE_WS_CUTOVER_STATE_FILE = "/data/bot_state.pre_ws12d.json"
PRE_WS_CUTOVER_BACKUP_FILE = "/data/bot_state.backup.pre_ws12d.json"
WS_STARTUP_VALIDATION_MARKER_FILE = f"/data/ws_startup_validation_{WS_STARTUP_VALIDATION_ID}.json"


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
        # Curseur temporel persistant du dernier événement marché effectivement traité.
        self.last_processed_market_ts_ms = 0
        # Version réellement chargée depuis disque (non persistée séparément).
        self.loaded_state_version = STATE_VERSION

    def reset_daily(self):
        if date.today() != self.start_date:
            log.info(f"Nouveau jour | P&L hier: {self.daily_pnl:+.2f}$")
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.daily_wins   = 0
            self.daily_losses = 0
            self.start_date   = date.today()
            save_state()

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


# ════════════════════════════════════════════════════════
# WEBSOCKET CUTOVER — bootstrap / resynchronisation 1m+5m
# ════════════════════════════════════════════════════════

def build_ws_client():
    """Construit le client WS public sans démarrer de décision de trading."""
    return BitgetPublicWS(
        SYMBOL,
        url=WS_PUBLIC_URL,
        inst_type=WS_INST_TYPE,
        heartbeat_seconds=WS_HEARTBEAT_SECONDS,
        reconnect_min_seconds=WS_RECONNECT_MIN_SECONDS,
        reconnect_max_seconds=WS_RECONNECT_MAX_SECONDS,
        logger=log,
    )


def wait_for_fresh_ws_market(ws_client, baseline_stats=None, timeout=None):
    """Attend au moins un ticker + update 1m + update 5m postérieurs au baseline."""
    timeout = WS_READY_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if baseline_stats is None:
        baseline_stats = ws_client.health_snapshot().get("stats", {})
    baseline = {
        "ticker_updates": int(baseline_stats.get("ticker_updates", 0)),
        "candle_1m_updates": int(baseline_stats.get("candle_1m_updates", 0)),
        "candle_5m_updates": int(baseline_stats.get("candle_5m_updates", 0)),
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = ws_client.health_snapshot()
        stats = snap.get("stats", {})
        fresh = all(int(stats.get(key, 0)) > value for key, value in baseline.items())
        if snap.get("ready") and snap.get("healthy") and fresh:
            return True
        time.sleep(0.05)
    return False


def bootstrap_ws_market_data(ws_client):
    """Bootstrap REST autoritaire puis démarrage WS, selon le contrat d'interface."""
    candles_5m = exchange.get_candles(SYMBOL, INTERVAL_SIGNAL, CANDLES_5M + 10)
    candles_1m = exchange.get_candles(SYMBOL, INTERVAL_CONFIRM, CANDLES_1M + 5)

    if not candles_5m or len(candles_5m) < 50:
        raise RuntimeError("Bootstrap WS refusé: historique 5m insuffisant")
    if not candles_1m or len(candles_1m) < 2:
        raise RuntimeError("Bootstrap WS refusé: historique 1m insuffisant")

    ws_client.seed_candles("5m", candles_5m)
    ws_client.seed_candles("1m", candles_1m)
    ws_client.start()

    if not ws_client.wait_until_ready(WS_READY_TIMEOUT_SECONDS):
        snap = ws_client.health_snapshot()
        ws_client.stop()
        raise RuntimeError(f"WebSocket non prêt après {WS_READY_TIMEOUT_SECONDS}s: {snap}")
    if not wait_for_fresh_ws_market(ws_client, baseline_stats={}):
        snap = ws_client.health_snapshot()
        ws_client.stop()
        raise RuntimeError(f"WebSocket prêt mais données fraîches absentes: {snap}")

    log.info(
        "WebSocket cutover prêt | subscriptions=%s | live_price=%s",
        ws_client.health_snapshot().get("subscriptions"),
        ws_client.get_live_price(),
    )
    return candles_5m, candles_1m


def resync_ws_market_data(ws_client):
    """Resynchronisation REST 1m/5m après reconnexion/gap, avec recovery PAPER."""
    # Une position ouverte impose d'abord le replay sûr des minutes manquées.
    if state.position is not None:
        recover_open_position_closed_rest()

    candles_5m = exchange.get_candles(SYMBOL, INTERVAL_SIGNAL, CANDLES_5M + 10)
    candles_1m = exchange.get_candles(SYMBOL, INTERVAL_CONFIRM, CANDLES_1M + 5)

    if not candles_5m or len(candles_5m) < 50:
        raise RuntimeError("Resync WS refusé: historique 5m insuffisant")
    if not candles_1m or len(candles_1m) < 2:
        raise RuntimeError("Resync WS refusé: historique 1m insuffisant")

    ws_client.seed_candles("5m", candles_5m)
    ws_client.seed_candles("1m", candles_1m)
    baseline_stats = dict(ws_client.health_snapshot().get("stats", {}))

    if not ws_client.is_ready() or not ws_client.is_healthy():
        raise RuntimeError(f"Resync WS refusé: transport non sain {ws_client.health_snapshot()}")
    if not wait_for_fresh_ws_market(ws_client, baseline_stats=baseline_stats):
        raise RuntimeError(f"Resync WS refusé: aucune donnée fraîche post-seed {ws_client.health_snapshot()}")

    if state.position is not None:
        recover_open_position_ws_handoff(ws_client)

    ws_client.mark_resynchronised()
    log.warning("WebSocket resynchronisé par REST 1m/5m + recovery temporel")
    return candles_5m, candles_1m


def run_ws_startup_validation(ws_client, candles_5m_hist, candles_1m_hist):
    """Gate live one-shot avant le premier remplacement Railway de la V4."""
    if not WS_STARTUP_VALIDATION:
        log.warning("12D STARTUP VALIDATION désactivée par configuration")
        return candles_5m_hist, candles_1m_hist

    if os.path.exists(WS_STARTUP_VALIDATION_MARKER_FILE):
        log.info(f"12D STARTUP VALIDATION déjà validée: {WS_STARTUP_VALIDATION_MARKER_FILE}")
        return candles_5m_hist, candles_1m_hist

    if state.position is not None:
        raise RuntimeError("12D startup validation exige un état FLAT au premier cutover")

    start_snap = ws_client.health_snapshot()
    start_stats = dict(start_snap.get("stats", {}))
    base_closed_1m = int(start_stats.get("closed_1m", 0))
    base_closed_5m = int(start_stats.get("closed_5m", 0))
    deadline = time.monotonic() + float(WS_STARTUP_VALIDATION_TIMEOUT_SECONDS)

    log.warning("12D STARTUP VALIDATION | attente clôture réelle 1m + 5m avant trading")
    while time.monotonic() < deadline:
        snap = ws_client.health_snapshot()
        stats = snap.get("stats", {})
        got_1m = int(stats.get("closed_1m", 0)) > base_closed_1m
        got_5m = int(stats.get("closed_5m", 0)) > base_closed_5m
        if snap.get("ready") and snap.get("healthy") and got_1m and got_5m:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError(f"12D validation live: clôtures 1m/5m non observées: {ws_client.health_snapshot()}")

    generation_before = int(ws_client.health_snapshot().get("connection_generation", 0))
    if not ws_client.request_reconnect("12D startup validation"):
        raise RuntimeError("12D validation live: reconnexion forcée impossible")

    reconnect_deadline = time.monotonic() + float(WS_STARTUP_RECONNECT_TIMEOUT_SECONDS)
    while time.monotonic() < reconnect_deadline:
        snap = ws_client.health_snapshot()
        generation = int(snap.get("connection_generation", 0))
        if generation > generation_before and snap.get("ready") and snap.get("needs_resync"):
            break
        time.sleep(0.10)
    else:
        raise RuntimeError(f"12D validation live: reconnexion/resync flag non observé: {ws_client.health_snapshot()}")

    candles_5m_hist, candles_1m_hist = resync_ws_market_data(ws_client)
    final_snap = ws_client.health_snapshot()
    if final_snap.get("needs_resync") or not final_snap.get("ready") or not final_snap.get("healthy"):
        raise RuntimeError(f"12D validation live: transport final non sain: {final_snap}")

    _atomic_write_json(WS_STARTUP_VALIDATION_MARKER_FILE, {
        "validation_id": WS_STARTUP_VALIDATION_ID,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "connection_generation": final_snap.get("connection_generation"),
        "stats": final_snap.get("stats", {}),
    })
    log.warning("12D STARTUP VALIDATION | PASS | clôtures 1m+5m + reconnexion + resync REST validées")
    tg("✅ <b>12D WS CUTOVER</b> — validation Railway live PASS, trading PAPER autorisé.")
    return candles_5m_hist, candles_1m_hist


# ── Historique rapide WS ─────────────────────────────────────────
def _apply_closed_candles(history, closed_candles, max_len):
    """Intègre des clôtures WS sans doublon ni retour arrière."""
    for candle in closed_candles:
        if not candle:
            continue
        ts = int(candle.get("timestamp", 0))
        if ts <= 0:
            continue
        if history:
            last_ts = int(history[-1].get("timestamp", 0))
            if ts < last_ts:
                continue
            if ts == last_ts:
                history[-1] = candle
                continue
        history.append(candle)
    if len(history) > max_len:
        del history[:-max_len]


def _market_view(history, current_candle, max_len):
    """Retourne historique + bougie en formation sans muter l'historique fermé."""
    view = list(history)
    if current_candle:
        ts = int(current_candle.get("timestamp", 0))
        if ts > 0:
            if view and int(view[-1].get("timestamp", 0)) == ts:
                view[-1] = current_candle
            elif not view or ts > int(view[-1].get("timestamp", 0)):
                view.append(current_candle)
    return view[-max_len:]


def _fetch_htf_market_data():
    """1h/4h restent REST pendant le chantier WebSocket."""
    candles_1h = exchange.get_candles(SYMBOL, INTERVAL_HTF_1H, CANDLES_1H + 10)
    candles_4h = exchange.get_candles(SYMBOL, INTERVAL_HTF_4H, CANDLES_4H + 10)
    candles_dxy = []
    if DXY_ENABLED:
        try:
            candles_dxy = exchange.get_candles(DXY_SYMBOL, INTERVAL_HTF_4H, CANDLES_4H + 10)
        except Exception as e:
            log.debug(f"DXY non disponible: {e}")
    return candles_1h, candles_4h, candles_dxy


# ════════════════════════════════════════════════════════
# RECOVERY TEMPOREL PAPER — fenêtre marché manquée
# ════════════════════════════════════════════════════════

class MarketRecoveryError(RuntimeError):
    """Le chemin marché manqué ne peut pas être reconstruit sans ambiguïté."""


def _sorted_unique_1m(candles):
    """Normalise l'ordre temporel et déduplique les bougies REST 1m."""
    by_ts = {}
    for candle in candles or []:
        if not isinstance(candle, dict):
            continue
        try:
            ts = int(candle.get("timestamp", 0))
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        by_ts[ts] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


def _classify_recovery_candle(pos, candle):
    """
    Classe une bougie 1m manquée pour une position PAPER.

    `TP1` pendant une coupure est volontairement déclaré ambigu : une OHLC 1m
    ne permet pas de savoir si, après le passage de TP1, le prix est revenu au
    nouveau stop `sl_lot2` dans la même minute. On refuse donc d'inventer un
    ordre intrabar.
    """
    if not pos or not candle:
        return "NONE"

    side = pos["side"]
    high = float(candle.get("high", candle.get("close", 0.0)))
    low = float(candle.get("low", candle.get("close", 0.0)))

    if not pos.get("tp1_hit", False):
        sl_hit = (side == "long" and low <= pos["sl"]) or (side == "short" and high >= pos["sl"])
        tp1_hit = (side == "long" and high >= pos["tp1"]) or (side == "short" and low <= pos["tp1"])
        if sl_hit and tp1_hit:
            return "AMBIGUOUS_PHASE1_BOTH"
        if tp1_hit:
            return "AMBIGUOUS_TP1_INTRABAR"
        if sl_hit:
            return "SL"
        return "NONE"

    sl2_hit = (side == "long" and low <= pos["sl_lot2"]) or (side == "short" and high >= pos["sl_lot2"])
    tp2_hit = (side == "long" and high >= pos["tp2"]) or (side == "short" and low <= pos["tp2"])
    if sl2_hit and tp2_hit:
        return "AMBIGUOUS_PHASE2_BOTH"
    if sl2_hit:
        return "SL2"
    if tp2_hit:
        return "TP2"
    return "NONE"


def _fetch_recovery_1m():
    candles = exchange.get_candles(
        SYMBOL, INTERVAL_CONFIRM, WS_RECOVERY_MAX_1M_CANDLES
    )
    candles = _sorted_unique_1m(candles)
    if not candles:
        raise MarketRecoveryError("recovery impossible: historique REST 1m vide")
    return candles


def recover_open_position_closed_rest(now_ms=None):
    """
    Rejoue uniquement les minutes REST déjà clôturées depuis le curseur persistant.
    Retourne True si le recovery est sûr. Toute ambiguïté déclenche un fail-safe.
    """
    if state.position is None:
        return True
    if state.loaded_state_version != STATE_VERSION:
        raise MarketRecoveryError(
            f"position ouverte issue du schéma v{state.loaded_state_version}: recovery v3 impossible"
        )
    if state.last_processed_market_ts_ms <= 0:
        raise MarketRecoveryError("position ouverte sans last_processed_market_ts_ms valide")

    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    candles = _fetch_recovery_1m()
    first_start_ms = int(candles[0]["timestamp"]) * 1000
    cursor = int(state.last_processed_market_ts_ms)
    if cursor < first_start_ms:
        raise MarketRecoveryError(
            f"fenêtre recovery non couverte: cursor={cursor} earliest_1m={first_start_ms}"
        )

    processed = 0
    for candle in candles:
        start_ms = int(candle["timestamp"]) * 1000
        end_ms = start_ms + 60_000
        if end_ms <= cursor:
            continue
        if end_ms > now_ms:
            break  # minute en formation : traitée au handoff WS, pas ici

        action = _classify_recovery_candle(state.position, candle)
        if action.startswith("AMBIGUOUS_"):
            raise MarketRecoveryError(
                f"recovery intrabar ambigu {action} @ {start_ms} pour {state.position['trade_id']}"
            )

        # Le curseur est avancé AVANT toute transition : si check_exits persiste
        # une clôture, le snapshot position+curseur reste atomiquement cohérent.
        state.last_processed_market_ts_ms = end_ms
        state.last_price = float(candle.get("close", state.last_price))

        if action in {"SL", "SL2", "TP2"}:
            check_exits(state.last_price, candle, phase2_use_extremes=True)

        processed += 1
        cursor = end_ms
        if state.position is None:
            break

    if processed:
        if not save_state():
            raise MarketRecoveryError("recovery calculé mais persistance impossible")
        log.warning(
            "Recovery REST 1m appliqué | minutes=%s | cursor=%s | position=%s",
            processed,
            state.last_processed_market_ts_ms,
            state.position["trade_id"] if state.position else "FLAT",
        )
    return True


def recover_open_position_ws_handoff(ws_client):
    """
    Ferme la fenêtre entre la dernière minute REST clôturée et le premier ticker WS.
    Aucun tick normal n'est accepté tant que ce handoff n'est pas validé.
    """
    if state.position is None:
        return True

    snap = ws_client.health_snapshot()
    market_ts_ms = snap.get("ticker_exchange_ts_ms")
    current_1m = ws_client.get_current_candle("1m")
    if not market_ts_ms or current_1m is None:
        raise MarketRecoveryError("handoff WS sans ticker horodaté ou bougie 1m courante")

    market_ts_ms = int(market_ts_ms)
    start_ms = int(current_1m.get("timestamp", 0)) * 1000
    if start_ms <= 0:
        raise MarketRecoveryError("handoff WS: timestamp 1m invalide")
    if state.last_processed_market_ts_ms < start_ms:
        raise MarketRecoveryError(
            f"handoff WS avec trou temporel: cursor={state.last_processed_market_ts_ms} current_1m={start_ms}"
        )
    if market_ts_ms <= state.last_processed_market_ts_ms:
        return True

    action = _classify_recovery_candle(state.position, current_1m)
    if action.startswith("AMBIGUOUS_"):
        raise MarketRecoveryError(
            f"handoff intrabar ambigu {action} @ {start_ms} pour {state.position['trade_id']}"
        )

    state.last_processed_market_ts_ms = market_ts_ms
    live_price = ws_client.get_live_price()
    if live_price is not None:
        state.last_price = float(live_price)

    if action in {"SL", "SL2", "TP2"}:
        check_exits(state.last_price, current_1m, phase2_use_extremes=True)

    if not save_state():
        raise MarketRecoveryError("handoff WS validé mais persistance impossible")
    log.warning(
        "Recovery handoff WS validé | cursor=%s | position=%s",
        state.last_processed_market_ts_ms,
        state.position["trade_id"] if state.position else "FLAT",
    )
    return True


# ── Snapshot pré-cutover ───────────────────────────────────────
def snapshot_pre_ws_cutover_state():
    """Conserve une copie one-shot de l'état présent avant la migration WS v3."""
    for source, target in (
        (STATE_FILE, PRE_WS_CUTOVER_STATE_FILE),
        (STATE_BACKUP_FILE, PRE_WS_CUTOVER_BACKUP_FILE),
    ):
        if os.path.exists(source) and not os.path.exists(target):
            try:
                shutil.copy2(source, target)
                with open(target, "rb+") as f:
                    os.fsync(f.fileno())
                _fsync_directory(target)
                log.warning(f"Snapshot pré-WS conservé: {target}")
            except Exception as e:
                raise RuntimeError(f"snapshot pré-cutover impossible ({source}): {e}") from e


# ── Persistance état ────────────────────────────────────────────
def _json_safe(value):
    """Convertit récursivement les scalaires non natifs (ex. NumPy) en types JSON."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return _json_safe(value.item())
    raise TypeError(f"Type non sérialisable dans l'état: {type(value).__name__}")


def _state_payload():
    position = _json_safe(state.position) if state.position is not None else None
    return {
        "state_version":   STATE_VERSION,
        "position":        position,
        "last_price":      state.last_price,
        "last_processed_market_ts_ms": state.last_processed_market_ts_ms,
        "paper_balance":   state.paper_balance,
        "paper_pnl":       state.paper_pnl,
        "peak_capital":    state.peak_capital,
        "dd_level":        state.dd_level,
        "dd_pause_until":  state.dd_pause_until,
        "consec_losses":   state.consec_losses,
        "wins":            state.wins,
        "losses":          state.losses,
        "total_trades":    state.total_trades,
        "trade_counter":   state.trade_counter,
        "daily_pnl":       state.daily_pnl,
        "daily_trades":    state.daily_trades,
        "daily_wins":      state.daily_wins,
        "daily_losses":    state.daily_losses,
        "start_date":      state.start_date.isoformat(),
        "last_sl_long":    state.last_sl_long,
        "last_sl_short":   state.last_sl_short,
    }


def _fsync_directory(path):
    directory = os.path.dirname(path) or "."
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path, payload):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_json_safe(payload), f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path)


def _validate_state_dict(d):
    if not isinstance(d, dict):
        raise ValueError("état racine non dictionnaire")

    numeric_fields = (
        "paper_balance", "paper_pnl", "peak_capital", "dd_pause_until",
        "last_sl_long", "last_sl_short",
    )
    for key in numeric_fields:
        if key in d and (isinstance(d[key], bool) or not isinstance(d[key], (int, float))):
            raise ValueError(f"champ {key} invalide")

    if "state_version" in d and (
        isinstance(d["state_version"], bool)
        or not isinstance(d["state_version"], int)
        or d["state_version"] <= 0
    ):
        raise ValueError("state_version invalide")

    int_fields = (
        "dd_level", "consec_losses", "wins", "losses", "total_trades",
        "trade_counter", "daily_trades", "daily_wins", "daily_losses",
        "last_processed_market_ts_ms",
    )
    for key in int_fields:
        if key in d and (isinstance(d[key], bool) or not isinstance(d[key], int) or d[key] < 0):
            raise ValueError(f"champ {key} invalide")

    if "paper_balance" in d and d["paper_balance"] <= 0:
        raise ValueError("paper_balance invalide")
    if "peak_capital" in d and d["peak_capital"] <= 0:
        raise ValueError("peak_capital invalide")
    if "dd_level" in d and not 0 <= d["dd_level"] <= 4:
        raise ValueError("dd_level invalide")

    if d.get("start_date") is not None:
        date.fromisoformat(d["start_date"])

    position = d.get("position")
    if position is not None:
        if not isinstance(position, dict):
            raise ValueError("position invalide")
        required = (
            "trade_id", "side", "entry", "sl", "tp1", "tp2",
            "lot_total", "lot_tp1", "lot_tp2", "tp1_hit",
            "sl_lot2", "entry_time",
        )
        missing = [key for key in required if key not in position]
        if missing:
            raise ValueError(f"position incomplète: {','.join(missing)}")
        if position["side"] not in ("long", "short"):
            raise ValueError("side position invalide")
        if not isinstance(position["trade_id"], str):
            raise ValueError("trade_id invalide")
        entry_time = datetime.fromisoformat(position["entry_time"])
        if entry_time.tzinfo is None:
            raise ValueError("entry_time sans timezone")
        for key in ("entry", "sl", "tp1", "tp2", "sl_lot2"):
            if isinstance(position[key], bool) or not isinstance(position[key], (int, float)):
                raise ValueError(f"position {key} invalide")
        for key in ("lot_total", "lot_tp1", "lot_tp2"):
            if isinstance(position[key], bool) or not isinstance(position[key], int) or position[key] <= 0:
                raise ValueError(f"position {key} invalide")
        if not isinstance(position["tp1_hit"], bool):
            raise ValueError("tp1_hit invalide")

    version = d.get("state_version", 1)
    if version >= 3 and "last_processed_market_ts_ms" not in d:
        raise ValueError("curseur marché v3 absent")
    if position is not None and version >= 3 and d.get("last_processed_market_ts_ms", 0) <= 0:
        raise ValueError("position v3 ouverte sans curseur marché")

    return d


def _read_valid_state(path):
    with open(path, "r", encoding="utf-8") as f:
        return _validate_state_dict(json.load(f))


def save_state():
    try:
        payload = _json_safe(_state_payload())
        _validate_state_dict(payload)
        _atomic_write_json(STATE_FILE, payload)
        _atomic_write_json(STATE_BACKUP_FILE, payload)
        state.loaded_state_version = STATE_VERSION
        return True
    except Exception as e:
        log.error(f"save_state: {e}", exc_info=True)
        return False


def _apply_state(d):
    state.loaded_state_version = d.get("state_version", 1)
    state.last_processed_market_ts_ms = d.get("last_processed_market_ts_ms", 0)
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
    state.daily_wins     = d.get("daily_wins",     0)
    state.daily_losses   = d.get("daily_losses",   0)
    state.last_sl_long   = d.get("last_sl_long",   0.0)
    state.last_sl_short  = d.get("last_sl_short",  0.0)
    state.last_price     = d.get("last_price",      0.0)
    state.start_date     = date.fromisoformat(d["start_date"]) if d.get("start_date") else date.today()

    position = d.get("position")
    if position is not None:
        position = dict(position)
        position["entry_time"] = datetime.fromisoformat(position["entry_time"])
    state.position = position

    # Les cartes MTF ne sont pas persistées : forcer leur recalcul au premier cycle.
    state.liq_map = {}
    state.ob_map = {}
    state.sweep_map = {}
    state.struct_map = {}
    state.dxy_map = {}
    state.last_4h_ts = 0
    state.last_1h_ts = 0


def load_state():
    errors = []
    for path, label in ((STATE_FILE, "principal"), (STATE_BACKUP_FILE, "backup")):
        try:
            d = _read_valid_state(path)
            _apply_state(d)
            loaded_version = d.get("state_version", 1)
            if loaded_version != STATE_VERSION and state.position is not None:
                raise ValueError(
                    f"migration v{loaded_version}->v{STATE_VERSION} refusée avec position ouverte"
                )
            if label == "backup":
                log.warning("État principal indisponible/invalide — backup restauré")
                if not save_state():
                    raise ValueError("restauration backup non persistable")
            elif loaded_version != STATE_VERSION or not os.path.exists(STATE_BACKUP_FILE):
                log.info("Migration/initialisation du schéma de persistance")
                state.loaded_state_version = STATE_VERSION
                if not save_state():
                    raise ValueError("migration persistance impossible")
            log.info(
                f"État chargé ({label}) | Capital: ${state.paper_balance:.2f} | "
                f"Trades: {state.total_trades} | WR: {state.wr:.0f}% | "
                f"Position: {state.position['trade_id'] if state.position else 'FLAT'}"
            )
            return True
        except FileNotFoundError:
            errors.append(f"{label}: absent")
        except Exception as e:
            errors.append(f"{label}: {e}")

    log.critical("Aucun état persistant valide — " + " | ".join(errors))
    return False


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
    save_state()
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
            save_state()
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
        save_state()

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
        save_state()
        return False

    elif state.consec_losses >= DD_CONSEC_L1:
        state.dd_pause_until = time.time() + DD_PAUSE_CONSEC1
        state.consec_losses  = 0
        log.warning(f"Série {DD_CONSEC_L1} pertes — pause {DD_PAUSE_CONSEC1//60}min")
        save_state()
        return False

    return True


# ════════════════════════════════════════════════════════
# CALCUL P&L
# ════════════════════════════════════════════════════════

def _calc_pnl(side, entry, exit_p, contracts):
    """P&L Bitget en USDT selon le nombre de contrats."""
    price_move = (exit_p - entry) if side == "long" else (entry - exit_p)
    return round(price_move * contracts * state.contract_size, 2)


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

    # Sizing Bitget en contrats — risque + plafond de marge
    risk_usd = CAPITAL * risk_pct
    contracts_risk = risk_usd / (sl_dist * state.contract_size)
    contracts_margin = (CAPITAL * MARGIN_CAP * LEVERAGE) / (entry * state.contract_size)
    lot_total = int(min(contracts_risk, contracts_margin))
    lot_total -= lot_total % 3
    if lot_total < 3:
        return
    lot_tp1 = lot_total * 2 // 3
    lot_tp2 = lot_total - lot_tp1

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
    save_state()

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

def check_exits(current_price, last_1m_candle, *, phase2_use_extremes=False):
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
            save_state()
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
            save_state()
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
        # En temps réel, ne jamais réutiliser le high/low de toute la minute après
        # le passage de TP1 : ces extrêmes peuvent précéder l'activation du SL2.
        # Le recovery REST demande explicitement les extrêmes via phase2_use_extremes.
        phase2_high = h if phase2_use_extremes else current_price
        phase2_low  = l if phase2_use_extremes else current_price
        sl2_hit = (side == "long"  and phase2_low  <= pos["sl_lot2"]) or \
                  (side == "short" and phase2_high >= pos["sl_lot2"])
        tp2_hit = (side == "long"  and phase2_high >= pos["tp2"]) or \
                  (side == "short" and phase2_low  <= pos["tp2"])

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
            save_state()
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
            save_state()
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
# 13A — LABORATOIRE SHADOW ENTRÉE 1M (NON BLOQUANT)
# ════════════════════════════════════════════════════════

def _shadow_level_from_signal(signal):
    """
    Reconstruit le niveau technique du setup SANS modifier strategy.py.

    La stratégie actuelle calcule déjà ce niveau pour ses SL :
    - VP / POC / VAH : SL à ATR_SL du niveau ;
    - VWAP ±2SD      : SL à 0.8 * ATR_SL du niveau ;
    - TF1-VWAP       : le VWAP central est déjà retourné dans le signal.

    Cette reconstruction sert uniquement au laboratoire shadow.
    Elle n'est jamais utilisée par open_position() ni par la stratégie live/PAPER.
    """
    try:
        setup = str(signal.get("setup", ""))
        side = str(signal.get("signal", "")).lower()
        if side not in {"long", "short"}:
            return None, "invalid_side"

        if setup == "TF1-VWAP":
            level = signal.get("vwap")
            if level is None:
                return None, "missing_vwap"
            return round(float(level), 2), "signal_vwap"

        sl = signal.get("sl")
        atr = signal.get("atr")
        if sl is None or atr is None:
            return None, "missing_sl_atr"

        sl = float(sl)
        atr = float(atr)
        if atr <= 0:
            return None, "invalid_atr"

        standard_level_setups = {
            "VAL->POC", "POC->VAH", "VAH->POC", "POC->VAL", "TF2-POC",
        }
        sd_level_setups = {"VWAP-2SD", "VWAP+2SD"}

        if setup in standard_level_setups:
            multiplier = 1.0
            source = "sl_atr_reconstructed"
        elif setup in sd_level_setups:
            multiplier = 0.8
            source = "sl_atr08_reconstructed"
        else:
            return None, f"unsupported_setup:{setup}"

        offset = atr * ATR_SL * multiplier
        level = sl + offset if side == "long" else sl - offset
        return round(level, 2), source

    except Exception as exc:
        log.warning("SHADOW niveau non résolu: %s", exc)
        return None, "resolver_error"


def _shadow_history_through(history, max_start_ts):
    """Vue historique sans look-ahead, bornée au timestamp de début indiqué."""
    try:
        cutoff = int(max_start_ts)
    except (TypeError, ValueError):
        return []
    return [
        candle for candle in history
        if int(candle.get("timestamp", 0) or 0) <= cutoff
    ]


def _shadow_register_closed_5m(
    shadow,
    closed_5m_events,
    candles_5m_hist,
    candles_1m_hist,
    candles_1h,
    candles_4h,
    candles_dxy,
    *,
    bid=None,
    ask=None,
    live_price=None,
):
    """
    Évalue chaque NOUVELLE 5m clôturée pour le laboratoire 13A.

    Important : cette fonction ne touche jamais state.position et n'appelle jamais
    open_position(). Toute exception shadow est absorbée ici pour que le trading
    PAPER actuel reste strictement indépendant du laboratoire.
    """
    if not closed_5m_events:
        return

    try:
        events = sorted(
            closed_5m_events,
            key=lambda c: int(c.get("timestamp", 0) or 0),
        )

        for closed_5m in events:
            bar_ts = int(closed_5m.get("timestamp", 0) or 0)
            if bar_ts <= 0:
                continue

            # La vue 5m se termine exactement sur la bougie qui vient de clôturer.
            shadow_5m = _shadow_history_through(candles_5m_hist, bar_ts)

            # À la validation de la 5m (bar_ts + 300), seules les 1m dont la
            # clôture est <= cette heure sont autorisées. Pour une bougie 1m,
            # timestamp = début, donc dernier début admissible = bar_ts + 240.
            shadow_1m = _shadow_history_through(candles_1m_hist, bar_ts + 240)

            shadow_signal = calc_signal(
                shadow_5m, shadow_1m,
                candles_1h, candles_4h, candles_dxy,
                state.liq_map, state.ob_map,
                state.sweep_map, state.struct_map, state.dxy_map,
            )

            if not shadow_signal.get("signal"):
                continue

            level_price, level_source = _shadow_level_from_signal(shadow_signal)
            if level_price is None:
                log.warning(
                    "SHADOW SKIP | %s %s | niveau non résolu (%s)",
                    str(shadow_signal.get("signal", "?")).upper(),
                    shadow_signal.get("setup", "UNKNOWN"),
                    level_source,
                )
                continue

            # Contexte analytique uniquement : n'influence aucun filtre du bot.
            shadow_payload = dict(shadow_signal)
            shadow_payload["shadow_level_source"] = level_source
            shadow_payload["shadow_dd_level"] = state.dd_level
            shadow_payload["shadow_position_open"] = state.position is not None

            shadow.register_setup(
                shadow_payload,
                level_price=level_price,
                validated_at=bar_ts + 300,
                signal_bar_ts=bar_ts,
                bid=bid,
                ask=ask,
                live_price=live_price,
            )

    except Exception as exc:
        log.warning("SHADOW 13A | calcul 5m ignoré: %s", exc, exc_info=True)


# ════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ════════════════════════════════════════════════════════

def main():
    log.info("=" * 65)
    log.info("  VP+VWAP+DELTA+MTF SCALPER V4 [BITGET] — WS CUTOVER 12D")
    log.info(f"  {SYMBOL} · Capital config: ${CAPITAL} · Levier: {LEVERAGE}x")
    log.info(f"  WS: ticker + 1m + 5m | REST: 1h + 4h | STATE v{STATE_VERSION}")
    log.info(f"  Mode: {'📄 PAPER' if PAPER_MODE else '💰 LIVE FUTURES'}")
    log.info("=" * 65)

    try:
        snapshot_pre_ws_cutover_state()
    except Exception as e:
        log.critical(f"Démarrage refusé — snapshot pré-cutover: {e}", exc_info=True)
        tg(f"🛑 <b>CUTOVER WS</b> — snapshot état impossible: {e}")
        return

    if not load_state():
        log.critical("Démarrage trading refusé : état persistant non restaurable")
        tg("🛑 <b>PERSISTANCE</b> — état principal et backup invalides/absents. Trading non démarré.")
        return

    if not WS_ENABLED:
        log.critical("WS staging désactivé : aucun fallback silencieux vers polling 1m/5m")
        tg("🛑 <b>WEBSOCKET CUTOVER</b> — WS_ENABLED=false, trading non démarré.")
        return

    # Avant même d'ouvrir le transport WS, reconstruire les minutes clôturées
    # manquées si une position PAPER est restaurée.
    if state.position is not None:
        try:
            recover_open_position_closed_rest()
        except MarketRecoveryError as e:
            log.critical(f"Démarrage refusé — recovery temporel: {e}")
            tg(f"🛑 <b>RECOVERY PAPER</b> — démarrage refusé: {e}")
            return

    ws_client = build_ws_client()
    try:
        candles_5m_hist, candles_1m_hist = bootstrap_ws_market_data(ws_client)
        candles_5m_hist, candles_1m_hist = run_ws_startup_validation(
            ws_client, candles_5m_hist, candles_1m_hist
        )
    except Exception as e:
        ws_client.stop()
        log.critical(f"Démarrage WS cutover refusé: {e}", exc_info=True)
        tg(f"🛑 <b>WEBSOCKET CUTOVER</b> — démarrage refusé: {e}")
        return

    # Handoff final REST -> WS : aucune décision/tick normal avant validation.
    if state.position is not None:
        try:
            recover_open_position_ws_handoff(ws_client)
        except MarketRecoveryError as e:
            ws_client.stop()
            log.critical(f"Démarrage refusé — handoff recovery WS: {e}")
            tg(f"🛑 <b>RECOVERY PAPER</b> — handoff WS refusé: {e}")
            return

    try:
        info = exchange.get_contract_info(SYMBOL)
        state.contract_size = info["contractSize"]
        log.info(f"1 contrat = {state.contract_size} oz XAU")
    except Exception as e:
        log.warning(f"contract_size=0.01 par défaut ({e})")

    if not PAPER_MODE:
        try:
            exchange.set_leverage(SYMBOL, LEVERAGE)
            log.info(f"Levier {LEVERAGE}x configuré")
        except Exception as e:
            log.warning(f"Levier: {e}")

    candles_1h, candles_4h, candles_dxy = _fetch_htf_market_data()
    refresh_htf_maps(candles_4h, candles_1h, candles_dxy)

    # 13A : laboratoire d'entrée 1m strictement observationnel.
    shadow = EntryShadowLab(log)
    log.info(
        "SHADOW 13A | actif NON BLOQUANT | TTL=2/3/5 | log=%s",
        shadow.jsonl_path,
    )

    next_signal_eval = 0.0
    next_htf_refresh = 0.0

    try:
        while True:
            try:
                state.reset_daily()

                # Le transport doit être prêt, sain et alimenté avant toute décision.
                if not ws_client.is_ready() or not ws_client.is_healthy():
                    time.sleep(WS_CONSUMER_SLEEP_SECONDS)
                    continue

                # Reconnexion/gap : REST autoritaire puis reprise, sans replay.
                if ws_client.needs_resync():
                    try:
                        candles_5m_hist, candles_1m_hist = resync_ws_market_data(ws_client)
                    except MarketRecoveryError as e:
                        log.critical(f"Trading arrêté — recovery après resync impossible: {e}")
                        tg(f"🛑 <b>RECOVERY PAPER</b> — resync impossible: {e}")
                        break

                data_age = ws_client.last_data_age()
                if data_age is None or data_age > WS_MAX_DATA_AGE_SECONDS:
                    log.warning(f"WS data stale: age={data_age}")
                    time.sleep(WS_CONSUMER_SLEEP_SECONDS)
                    continue

                # Les seules clôtures rapides viennent désormais du WebSocket.
                # On conserve les événements AVANT de les injecter dans l'historique
                # afin que le laboratoire 13A puisse les observer exactement une fois.
                closed_1m_events = ws_client.drain_closed_candles("1m")
                closed_5m_events = ws_client.drain_closed_candles("5m")

                _apply_closed_candles(
                    candles_1m_hist,
                    closed_1m_events,
                    CANDLES_1M + 5,
                )
                _apply_closed_candles(
                    candles_5m_hist,
                    closed_5m_events,
                    CANDLES_5M + 10,
                )

                current_1m = ws_client.get_current_candle("1m")
                current_5m = ws_client.get_current_candle("5m")
                candles_1m = _market_view(candles_1m_hist, current_1m, CANDLES_1M + 5)
                candles_5m = _market_view(candles_5m_hist, current_5m, CANDLES_5M + 10)

                current_price = ws_client.get_live_price()
                best_bid, best_ask = ws_client.get_best_bid_ask()
                market_ts_ms = ws_client.health_snapshot().get("ticker_exchange_ts_ms")

                # 13A — ORDRE TEMPOREL IMPORTANT :
                # 1) les 1m qui viennent de clôturer ne peuvent agir que sur les
                #    setups déjà pending AVANT cette clôture ;
                # 2) ensuite seulement la nouvelle 5m clôturée peut créer un pending.
                # Ainsi la dernière 1m contenue dans la 5m de signal ne peut jamais
                # devenir artificiellement son propre trigger.
                for closed_1m in closed_1m_events:
                    shadow.on_closed_1m(
                        closed_1m,
                        bid=best_bid,
                        ask=best_ask,
                        live_price=current_price,
                    )

                _shadow_register_closed_5m(
                    shadow,
                    closed_5m_events,
                    candles_5m_hist, candles_1m_hist,
                    candles_1h, candles_4h, candles_dxy,
                    bid=best_bid, ask=best_ask, live_price=current_price,
                )

                # Le shadow est traité avant ce gate pour ne jamais perdre un événement
                # de clôture déjà drainé si le ticker n'a pas encore avancé son curseur.
                if current_price is None or not market_ts_ms:
                    time.sleep(WS_CONSUMER_SLEEP_SECONDS)
                    continue
                market_ts_ms = int(market_ts_ms)
                if market_ts_ms <= state.last_processed_market_ts_ms:
                    time.sleep(WS_CONSUMER_SLEEP_SECONDS)
                    continue

                state.last_price = current_price
                # Avancer le curseur AVANT une éventuelle sortie qui appelle save_state().
                state.last_processed_market_ts_ms = market_ts_ms

                # Phase 1 peut utiliser les extrêmes de la 1m courante. Phase 2 utilise
                # uniquement le prix live afin de ne pas recycler un low/high antérieur à TP1.
                if state.position:
                    check_exits(current_price, current_1m, phase2_use_extremes=False)

                # Fermeture weekend reste prioritaire pour toute position restante.
                if is_weekend_close_time():
                    close_weekend()
                    time.sleep(WS_CONSUMER_SLEEP_SECONDS)
                    continue

                now_mono = time.monotonic()
                if now_mono < next_signal_eval:
                    time.sleep(WS_CONSUMER_SLEEP_SECONDS)
                    continue
                next_signal_eval = now_mono + LOOP_SECONDS

                # Les couches 1h/4h restent REST, à la cadence historique de la V4.
                if now_mono >= next_htf_refresh:
                    candles_1h, candles_4h, candles_dxy = _fetch_htf_market_data()
                    refresh_htf_maps(candles_4h, candles_1h, candles_dxy)
                    next_htf_refresh = now_mono + LOOP_SECONDS

                signal = {"signal": None, "reason": "–"}

                # DD bloque les nouvelles entrées, pas la gestion d'une position existante.
                trading_allowed = check_drawdown()
                if trading_allowed and state.position is None:
                    risk_pct, score_malus, _ = get_dd_params()

                    signal = calc_signal(
                        candles_5m, candles_1m,
                        candles_1h, candles_4h, candles_dxy,
                        state.liq_map, state.ob_map,
                        state.sweep_map, state.struct_map, state.dxy_map
                    )

                    if signal.get("signal"):
                        side_sig = signal["signal"]
                        min_score_eff = MIN_SCORE + score_malus
                        if signal.get("score", 0) < min_score_eff:
                            signal = {
                                "signal": None,
                                "reason": f"DD N{state.dd_level} score {signal['score']:.1f}<{min_score_eff:.1f}",
                            }
                        elif state.cooldown_remaining(side_sig) > 0:
                            cd = state.cooldown_remaining(side_sig)
                            signal = {
                                "signal": None,
                                "reason": f"Cooldown {side_sig.upper()}: {int(cd/60)}m{int(cd%60):02d}s",
                            }
                        else:
                            open_position(signal, risk_pct)

                if state.position:
                    pos = state.position
                    ph = "Ph2" if pos["tp1_hit"] else "Ph1"
                    pos_desc = f"{pos['side'].upper()}[{pos['setup']}]@${pos['entry']:.1f} {ph}"
                else:
                    pos_desc = "FLAT"

                dd_pct = (state.peak_capital - state.capital) / state.peak_capital * 100
                dd_label = ["", "DD⚠️", "DD🟠", "DD🔴", "DD🛑"][min(state.dd_level, 4)]
                log.info(
                    f"${current_price:.2f} | {pos_desc} | "
                    f"Cap:${state.paper_balance:.2f} | P&L:{state.paper_pnl:+.2f}$ | "
                    f"WR:{state.wr:.0f}% | DD:{dd_pct:.1f}%{dd_label} | "
                    f"WS age:{data_age:.2f}s | {signal.get('reason', '–')}"
                )
                save_state()

            except KeyboardInterrupt:
                log.info("Bot arrêté manuellement")
                break
            except Exception as e:
                log.error(f"Erreur boucle WS: {e}", exc_info=True)
                time.sleep(1.0)

            time.sleep(WS_CONSUMER_SLEEP_SECONDS)
    finally:
        ws_client.stop()
        save_state()
        log.info("WebSocket cutover arrêté proprement")


if __name__ == "__main__":
    dashboard.init(state, trades)
    t = threading.Thread(target=dashboard.run, daemon=True)
    t.start()
    log.info("Dashboard démarré")
    main()
