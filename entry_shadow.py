"""
BOT V4 — 13A Entry Shadow Lab
=============================

Observateur quantitatif NON BLOQUANT pour étudier une future exécution 1m.

Règles de sécurité :
- ce module ne passe jamais d'ordre ;
- il n'importe ni l'exchange ni open_position() ;
- il ne modifie ni capital, ni DD, ni position, ni SL/TP du bot ;
- toute erreur d'écriture shadow est absorbée et journalisée sans interrompre le bot.

Principe :
1. Un setup validé sur une bougie 5m clôturée est enregistré comme PENDING.
2. Seules les NOUVELLES bougies 1m clôturées après validation sont observées.
3. Le premier trigger 1m est mesuré simultanément pour TTL 2 / 3 / 5 bougies.
4. Les données sont écrites en JSONL pour analyse statistique ultérieure.

Le trigger de base est volontairement simple :
- LONG  : la 1m touche le niveau, clôture au-dessus et clôture haussière ;
- SHORT : la 1m touche le niveau, clôture en-dessous et clôture baissière.

Les ratios de mèche/corps, distance au niveau, spread et qualité d'exécution
sont MESURÉS mais ne sont pas des filtres durs dans cette phase shadow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SHADOW_VERSION = "13A.1"
DEFAULT_TTLS: Tuple[int, ...] = (2, 3, 5)
ONE_MINUTE_SECONDS = 60


def _finite_float(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _timestamp_seconds(value: Any) -> int:
    """Normalise un timestamp secondes ou millisecondes en secondes UTC."""
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return 0
    if ts > 10_000_000_000:  # millisecondes
        ts //= 1000
    return ts if ts > 0 else 0


def _candle_value(candle: Mapping[str, Any], long_key: str, short_key: str) -> Optional[float]:
    return _finite_float(candle.get(long_key, candle.get(short_key)))


def _utc_session(ts: int) -> str:
    if ts <= 0:
        return "UNKNOWN"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    total_min = dt.hour * 60 + dt.minute
    if total_min >= 13 * 60 + 30:
        return "NEW_YORK"
    if total_min >= 7 * 60:
        return "LONDON"
    return "ASIA"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


@dataclass
class PendingSetup:
    setup_id: str
    side: str
    setup: str
    level_price: float
    setup_entry: float
    sl: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    atr: Optional[float]
    score: Optional[float]
    rrd: Optional[float]
    validated_at: int
    signal_bar_ts: int
    session: str
    created_bid: Optional[float]
    created_ask: Optional[float]
    created_live_price: Optional[float]
    signal_context: Dict[str, Any] = field(default_factory=dict)
    bars_seen: int = 0
    last_1m_ts: int = 0


class EntryShadowLab:
    """
    Laboratoire shadow pour mesurer l'intérêt d'un timing d'entrée 1m.

    API prévue pour bot.py :
        shadow = EntryShadowLab(log)
        shadow.register_setup(signal, level_price=..., validated_at=...)
        shadow.on_closed_1m(candle, bid=..., ask=..., live_price=...)

    register_setup() doit recevoir le VRAI niveau du setup (VAL/POC/VAH/VWAP...).
    Aucun fallback silencieux sur le prix d'entrée n'est utilisé : mesurer le mauvais
    niveau serait plus dangereux statistiquement que manquer un échantillon.
    """

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        *,
        ttls: Sequence[int] = DEFAULT_TTLS,
        jsonl_path: Optional[str] = None,
        state_path: Optional[str] = None,
        max_pending: int = 50,
    ) -> None:
        clean_ttls = tuple(sorted({int(x) for x in ttls if int(x) > 0}))
        if not clean_ttls:
            raise ValueError("EntryShadowLab: au moins un TTL positif est requis")

        self.log = logger or logging.getLogger("entry_shadow")
        self.ttls = clean_ttls
        self.max_ttl = max(clean_ttls)
        self.max_pending = max(1, int(max_pending))

        default_dir = "/data" if os.path.isdir("/data") else "."
        self.jsonl_path = jsonl_path or os.getenv(
            "SHADOW_13A_LOG_PATH",
            os.path.join(default_dir, "entry_shadow_13A.jsonl"),
        )
        self.state_path = state_path or os.getenv(
            "SHADOW_13A_STATE_PATH",
            os.path.join(default_dir, "entry_shadow_pending_13A.json"),
        )

        self._lock = threading.RLock()
        self._pending: Dict[str, PendingSetup] = {}
        self._seen_setup_ids: set[str] = set()
        self._load_state_best_effort()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_setup(
        self,
        signal: Mapping[str, Any],
        *,
        level_price: Any,
        validated_at: Optional[Any] = None,
        signal_bar_ts: Optional[Any] = None,
        bid: Optional[Any] = None,
        ask: Optional[Any] = None,
        live_price: Optional[Any] = None,
    ) -> Optional[str]:
        """
        Enregistre un setup 5m comme PENDING sans aucune action de trading.

        Retourne setup_id, ou None si le setup est invalide/dupliqué.
        """
        try:
            side = str(signal.get("signal", "")).lower().strip()
            if side not in {"long", "short"}:
                return None

            setup = str(signal.get("setup", "UNKNOWN"))
            level = _finite_float(level_price)
            entry = _finite_float(signal.get("entry"))
            if level is None or entry is None:
                self.log.warning(
                    "SHADOW SKIP | setup=%s side=%s | niveau/entry invalide",
                    setup,
                    side,
                )
                return None

            bar_ts = _timestamp_seconds(
                signal_bar_ts
                if signal_bar_ts is not None
                else signal.get("setup_bar_ts", 0)
            )
            valid_ts = _timestamp_seconds(
                validated_at
                if validated_at is not None
                else signal.get("validated_at", 0)
            )

            # Si le signal ne fournit pas validated_at mais fournit le début de la 5m,
            # la validation devient disponible à la clôture de cette 5m.
            if valid_ts <= 0 and bar_ts > 0:
                valid_ts = bar_ts + 300
            if valid_ts <= 0:
                valid_ts = int(datetime.now(timezone.utc).timestamp())

            setup_id = self._make_setup_id(valid_ts, side, setup, level, entry)

            context_keys = (
                "tags",
                "adx",
                "struct_1h",
                "dxy_4h",
                "sweep",
                "vwap",
                "delta",
                "multi_vp_score",
                "multi_vp_bias",
                "vp_daily_score",
                "vp_4h_score",
                "vp_session_score",
                "vp_daily_maturity",
                "vp_session_maturity",
                "shadow_level_source",
                "shadow_dd_level",
                "shadow_position_open",
            )
            context = {k: _json_safe(signal.get(k)) for k in context_keys if k in signal}

            pending = PendingSetup(
                setup_id=setup_id,
                side=side,
                setup=setup,
                level_price=level,
                setup_entry=entry,
                sl=_finite_float(signal.get("sl")),
                tp1=_finite_float(signal.get("tp1")),
                tp2=_finite_float(signal.get("tp2")),
                atr=_finite_float(signal.get("atr")),
                score=_finite_float(signal.get("score")),
                rrd=_finite_float(signal.get("rrd")),
                validated_at=valid_ts,
                signal_bar_ts=bar_ts,
                session=_utc_session(valid_ts),
                created_bid=_finite_float(bid),
                created_ask=_finite_float(ask),
                created_live_price=_finite_float(live_price),
                signal_context=context,
            )

            with self._lock:
                if setup_id in self._seen_setup_ids or setup_id in self._pending:
                    return None

                if len(self._pending) >= self.max_pending:
                    # Fail-safe shadow uniquement : on retire le plus ancien pending.
                    oldest_id = min(
                        self._pending,
                        key=lambda sid: self._pending[sid].validated_at,
                    )
                    dropped = self._pending.pop(oldest_id)
                    self._write_event_best_effort(
                        "DROPPED_MAX_PENDING",
                        dropped,
                        extra={"reason": "max_pending"},
                    )

                self._pending[setup_id] = pending
                self._seen_setup_ids.add(setup_id)
                self._persist_state_best_effort()

            self._write_event_best_effort("PENDING", pending)
            self.log.info(
                "SHADOW 5M | PENDING %s %s | level=%.2f | score=%s | TTL=%s",
                side.upper(),
                setup,
                level,
                f"{pending.score:.2f}" if pending.score is not None else "NA",
                "/".join(str(x) for x in self.ttls),
            )
            return setup_id

        except Exception as exc:  # shadow ne doit jamais casser le bot
            self.log.warning("SHADOW register_setup ignoré: %s", exc, exc_info=True)
            return None

    def on_closed_1m(
        self,
        candle: Mapping[str, Any],
        *,
        bid: Optional[Any] = None,
        ask: Optional[Any] = None,
        live_price: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Traite UNE bougie 1m réellement clôturée.

        Retourne les résultats finalisés pendant cet appel. Le bot peut ignorer
        totalement cette valeur : elle n'a aucun effet sur le trading.
        """
        try:
            parsed = self._parse_closed_1m(candle)
            if parsed is None:
                return []

            candle_ts = parsed["timestamp"]
            candle_close_ts = candle_ts + ONE_MINUTE_SECONDS
            bid_f = _finite_float(bid)
            ask_f = _finite_float(ask)
            live_f = _finite_float(live_price)

            finalised: List[Dict[str, Any]] = []

            with self._lock:
                for setup_id in list(self._pending.keys()):
                    p = self._pending.get(setup_id)
                    if p is None:
                        continue

                    # Strictement postérieur à la validation du setup 5m.
                    if candle_close_ts <= p.validated_at:
                        continue

                    # Anti-doublon / anti-retour arrière par setup.
                    if candle_ts <= p.last_1m_ts:
                        continue

                    p.last_1m_ts = candle_ts

                    # Numéro de bougie calculé par TEMPS écoulé, pas par simple
                    # compteur de messages. Ainsi un restart/gap WS ne prolonge
                    # jamais artificiellement la durée de vie d'un setup.
                    elapsed_seconds = max(1, candle_close_ts - p.validated_at)
                    bar_number = max(
                        1,
                        int(math.ceil(elapsed_seconds / float(ONE_MINUTE_SECONDS))),
                    )
                    p.bars_seen = max(p.bars_seen, bar_number)

                    # Si la première bougie reçue après un trou est déjà hors TTL,
                    # le setup expire AVANT toute recherche de trigger.
                    if p.bars_seen > self.max_ttl:
                        result = self._finalise_expired(p, parsed)
                        finalised.append(result)
                        del self._pending[setup_id]
                        continue

                    metrics = self._bar_metrics(p, parsed)
                    trigger = self._is_trigger(p, parsed)

                    self._write_event_best_effort(
                        "BAR_1M",
                        p,
                        extra={
                            "bar_number": p.bars_seen,
                            "candle": parsed,
                            "trigger": trigger,
                            "metrics": metrics,
                        },
                    )

                    if trigger:
                        result = self._finalise_trigger(
                            p,
                            parsed,
                            bid=bid_f,
                            ask=ask_f,
                            live_price=live_f,
                            metrics=metrics,
                        )
                        finalised.append(result)
                        del self._pending[setup_id]
                        continue

                    if p.bars_seen >= self.max_ttl:
                        result = self._finalise_expired(p, parsed)
                        finalised.append(result)
                        del self._pending[setup_id]

                self._persist_state_best_effort()

            return finalised

        except Exception as exc:  # shadow ne doit jamais casser le bot
            self.log.warning("SHADOW on_closed_1m ignoré: %s", exc, exc_info=True)
            return []

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": SHADOW_VERSION,
                "ttls": list(self.ttls),
                "pending_count": len(self._pending),
                "pending": [_json_safe(asdict(p)) for p in self._pending.values()],
            }

    # ------------------------------------------------------------------
    # Trigger et métriques
    # ------------------------------------------------------------------

    def _parse_closed_1m(self, candle: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        ts = _timestamp_seconds(candle.get("timestamp", candle.get("ts", 0)))
        o = _candle_value(candle, "open", "o")
        h = _candle_value(candle, "high", "h")
        l = _candle_value(candle, "low", "l")
        c = _candle_value(candle, "close", "c")
        v = _candle_value(candle, "volume", "v")

        if ts <= 0 or None in (o, h, l, c):
            return None
        if h < l or h < max(o, c) or l > min(o, c):
            return None

        return {
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        }

    def _is_trigger(self, p: PendingSetup, c: Mapping[str, float]) -> bool:
        if p.side == "long":
            touched = c["low"] <= p.level_price
            reclaimed = c["close"] > p.level_price
            directional_close = c["close"] > c["open"]
            return bool(touched and reclaimed and directional_close)

        touched = c["high"] >= p.level_price
        reclaimed = c["close"] < p.level_price
        directional_close = c["close"] < c["open"]
        return bool(touched and reclaimed and directional_close)

    def _bar_metrics(self, p: PendingSetup, c: Mapping[str, float]) -> Dict[str, Any]:
        bar_range = max(c["high"] - c["low"], 0.0)
        body = abs(c["close"] - c["open"])
        wick_dn = max(0.0, min(c["close"], c["open"]) - c["low"])
        wick_up = max(0.0, c["high"] - max(c["close"], c["open"]))

        if bar_range > 0:
            body_ratio = body / bar_range
            wick_dn_ratio = wick_dn / bar_range
            wick_up_ratio = wick_up / bar_range
            close_location = (c["close"] - c["low"]) / bar_range
        else:
            body_ratio = wick_dn_ratio = wick_up_ratio = close_location = None

        atr = p.atr if p.atr and p.atr > 0 else None
        open_distance = c["open"] - p.level_price
        close_distance = c["close"] - p.level_price
        level_distance_atr_open = open_distance / atr if atr else None
        level_distance_atr_close = close_distance / atr if atr else None

        return {
            "bar_range": round(bar_range, 8),
            "body": round(body, 8),
            "body_ratio": body_ratio,
            "wick_dn_ratio": wick_dn_ratio,
            "wick_up_ratio": wick_up_ratio,
            "close_location": close_location,
            "open_minus_level": open_distance,
            "close_minus_level": close_distance,
            "open_distance_atr": level_distance_atr_open,
            "close_distance_atr": level_distance_atr_close,
            "touched_strict": (
                c["low"] <= p.level_price if p.side == "long"
                else c["high"] >= p.level_price
            ),
            "closed_correct_side": (
                c["close"] > p.level_price if p.side == "long"
                else c["close"] < p.level_price
            ),
            "directional_close": (
                c["close"] > c["open"] if p.side == "long"
                else c["close"] < c["open"]
            ),
        }

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def _finalise_trigger(
        self,
        p: PendingSetup,
        candle: Mapping[str, Any],
        *,
        bid: Optional[float],
        ask: Optional[float],
        live_price: Optional[float],
        metrics: Mapping[str, Any],
    ) -> Dict[str, Any]:
        bar_n = p.bars_seen
        execution_source = "candle_close"
        execution_price = float(candle["close"])

        # Exécution théorique réaliste : ASK pour acheter, BID pour vendre.
        if p.side == "long" and ask is not None:
            execution_price = ask
            execution_source = "ask"
        elif p.side == "short" and bid is not None:
            execution_price = bid
            execution_source = "bid"
        elif live_price is not None:
            execution_price = live_price
            execution_source = "live_price"

        spread = (ask - bid) if ask is not None and bid is not None else None

        if p.side == "long":
            improvement = p.setup_entry - execution_price
        else:
            improvement = execution_price - p.setup_entry

        sl_dist_shadow = (
            abs(execution_price - p.sl) if p.sl is not None else None
        )
        rr_tp1_shadow = None
        rr_tp2_shadow = None
        if sl_dist_shadow and sl_dist_shadow > 0:
            if p.tp1 is not None:
                rr_tp1_shadow = (
                    (p.tp1 - execution_price) / sl_dist_shadow
                    if p.side == "long"
                    else (execution_price - p.tp1) / sl_dist_shadow
                )
            if p.tp2 is not None:
                rr_tp2_shadow = (
                    (p.tp2 - execution_price) / sl_dist_shadow
                    if p.side == "long"
                    else (execution_price - p.tp2) / sl_dist_shadow
                )

        ttl_outcomes = {
            str(ttl): {
                "triggered": bar_n <= ttl,
                "trigger_bar": bar_n if bar_n <= ttl else None,
            }
            for ttl in self.ttls
        }

        result = {
            "event": "TRIGGER",
            "shadow_version": SHADOW_VERSION,
            "setup_id": p.setup_id,
            "side": p.side,
            "setup": p.setup,
            "session": p.session,
            "validated_at": p.validated_at,
            "signal_bar_ts": p.signal_bar_ts,
            "trigger_candle_ts": candle["timestamp"],
            "trigger_bar": bar_n,
            "delay_seconds": max(0, candle["timestamp"] + 60 - p.validated_at),
            "level_price": p.level_price,
            "setup_entry": p.setup_entry,
            "shadow_entry": execution_price,
            "execution_source": execution_source,
            "price_improvement": improvement,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "sl": p.sl,
            "tp1": p.tp1,
            "tp2": p.tp2,
            "sl_distance_shadow": sl_dist_shadow,
            "rr_to_original_tp1": rr_tp1_shadow,
            "rr_to_original_tp2": rr_tp2_shadow,
            "atr": p.atr,
            "score": p.score,
            "rrd": p.rrd,
            "ttl_outcomes": ttl_outcomes,
            "trigger_metrics": _json_safe(dict(metrics)),
            "signal_context": _json_safe(p.signal_context),
        }

        self._append_jsonl_best_effort(result)
        self.log.info(
            "SHADOW 1M | TRIGGER %s %s | bar=%s | entry=%.2f(%s) | improvement=%+.2f",
            p.side.upper(),
            p.setup,
            bar_n,
            execution_price,
            execution_source,
            improvement,
        )
        return result

    def _finalise_expired(self, p: PendingSetup, candle: Mapping[str, Any]) -> Dict[str, Any]:
        result = {
            "event": "EXPIRE",
            "shadow_version": SHADOW_VERSION,
            "setup_id": p.setup_id,
            "side": p.side,
            "setup": p.setup,
            "session": p.session,
            "validated_at": p.validated_at,
            "signal_bar_ts": p.signal_bar_ts,
            "last_candle_ts": candle["timestamp"],
            "bars_seen": p.bars_seen,
            "level_price": p.level_price,
            "setup_entry": p.setup_entry,
            "atr": p.atr,
            "score": p.score,
            "rrd": p.rrd,
            "ttl_outcomes": {
                str(ttl): {"triggered": False, "trigger_bar": None}
                for ttl in self.ttls
            },
            "signal_context": _json_safe(p.signal_context),
        }

        self._append_jsonl_best_effort(result)
        self.log.info(
            "SHADOW EXPIRE | %s %s | aucun trigger après %sm",
            p.side.upper(),
            p.setup,
            self.max_ttl,
        )
        return result

    # ------------------------------------------------------------------
    # Persistance shadow — best effort uniquement
    # ------------------------------------------------------------------

    def _write_event_best_effort(
        self,
        event: str,
        p: PendingSetup,
        *,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "event": event,
            "shadow_version": SHADOW_VERSION,
            "setup_id": p.setup_id,
            "side": p.side,
            "setup": p.setup,
            "session": p.session,
            "validated_at": p.validated_at,
            "signal_bar_ts": p.signal_bar_ts,
            "level_price": p.level_price,
            "setup_entry": p.setup_entry,
            "atr": p.atr,
            "score": p.score,
            "rrd": p.rrd,
            "bars_seen": p.bars_seen,
        }
        if extra:
            payload.update(_json_safe(dict(extra)))
        self._append_jsonl_best_effort(payload)

    def _append_jsonl_best_effort(self, payload: Mapping[str, Any]) -> None:
        try:
            directory = os.path.dirname(self.jsonl_path) or "."
            os.makedirs(directory, exist_ok=True)
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_json_safe(dict(payload)), ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
                fh.flush()
        except Exception as exc:
            self.log.warning("SHADOW écriture JSONL impossible: %s", exc)

    def _persist_state_best_effort(self) -> None:
        try:
            directory = os.path.dirname(self.state_path) or "."
            os.makedirs(directory, exist_ok=True)
            payload = {
                "shadow_version": SHADOW_VERSION,
                "ttls": list(self.ttls),
                "pending": [_json_safe(asdict(p)) for p in self._pending.values()],
                "seen_setup_ids": list(self._seen_setup_ids)[-5000:],
            }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        except Exception as exc:
            self.log.warning("SHADOW persistance pending impossible: %s", exc)

    def _load_state_best_effort(self) -> None:
        try:
            if not os.path.exists(self.state_path):
                return
            with open(self.state_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("shadow_version") != SHADOW_VERSION:
                return

            pending_rows = payload.get("pending", [])
            loaded: Dict[str, PendingSetup] = {}
            for row in pending_rows:
                try:
                    p = PendingSetup(**row)
                    loaded[p.setup_id] = p
                except Exception:
                    continue

            self._pending = loaded
            self._seen_setup_ids = set(payload.get("seen_setup_ids", [])) | set(loaded)
            if loaded:
                self.log.info("SHADOW 13A | pending restaurés=%s", len(loaded))
        except Exception as exc:
            self.log.warning("SHADOW état pending ignoré: %s", exc)

    @staticmethod
    def _make_setup_id(validated_at: int, side: str, setup: str, level: float, entry: float) -> str:
        raw = f"{validated_at}|{side}|{setup}|{level:.8f}|{entry:.8f}".encode("utf-8")
        digest = hashlib.sha1(raw).hexdigest()[:10]
        return f"S13A-{validated_at}-{digest}"
