"""Bitget public WebSocket client for XAU/USDT futures.

Infrastructure-only module. It does not contain trading logic.

Responsibilities:
- subscribe to ticker, candle1m and candle5m public channels;
- keep the latest live ticker and developing candles in memory;
- emit a closed-candle event only when a newer candle timestamp arrives;
- deduplicate/out-of-order protect candle updates by timestamp;
- maintain Bitget text heartbeat ("ping"/"pong");
- reconnect automatically with bounded exponential backoff;
- flag the caller when a REST resynchronisation is required after reconnect/gap.

REST history/bootstrap remains the responsibility of bitget.py / bot.py.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List, Optional

import websocket


DEFAULT_WS_URL = "wss://ws.bitget.com/v2/ws/public"
DEFAULT_INST_TYPE = "USDT-FUTURES"
SUPPORTED_INTERVALS = {"1m": 60, "5m": 300}


class BitgetPublicWS:
    """Threaded Bitget Classic v2 public WebSocket market-data client."""

    def __init__(
        self,
        symbol: str,
        *,
        url: str = DEFAULT_WS_URL,
        inst_type: str = DEFAULT_INST_TYPE,
        heartbeat_seconds: float = 30.0,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        closed_queue_maxlen: int = 256,
        logger: Optional[logging.Logger] = None,
        ws_app_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not symbol:
            raise ValueError("symbol requis")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds doit être > 0")
        if reconnect_min_seconds <= 0:
            raise ValueError("reconnect_min_seconds doit être > 0")
        if reconnect_max_seconds < reconnect_min_seconds:
            raise ValueError("reconnect_max_seconds doit être >= reconnect_min_seconds")
        if closed_queue_maxlen < 8:
            raise ValueError("closed_queue_maxlen doit être >= 8")

        self.symbol = symbol
        self.url = url
        self.inst_type = inst_type
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.reconnect_min_seconds = float(reconnect_min_seconds)
        self.reconnect_max_seconds = float(reconnect_max_seconds)
        self.log = logger or logging.getLogger("bitget_ws")
        self._ws_app_factory = ws_app_factory or websocket.WebSocketApp

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()

        self._supervisor_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._ws: Any = None

        self._connected = False
        self._subscriptions = set()
        self._expected_subscriptions = {"ticker", "candle1m", "candle5m"}
        self._connection_generation = 0
        self._needs_resync = False
        self._connection_had_data = False

        self._last_message_monotonic = 0.0
        self._last_data_monotonic = 0.0
        self._last_ping_monotonic = 0.0
        self._last_pong_monotonic = 0.0
        self._last_error: Optional[str] = None

        self._live_price: Optional[float] = None
        self._best_bid: Optional[float] = None
        self._best_ask: Optional[float] = None
        self._ticker_exchange_ts_ms: Optional[int] = None

        self._current_candles: Dict[str, Optional[dict]] = {
            "1m": None,
            "5m": None,
        }
        self._closed_candles = {
            "1m": deque(maxlen=closed_queue_maxlen),
            "5m": deque(maxlen=closed_queue_maxlen),
        }
        self._last_closed_ts = {"1m": None, "5m": None}

        self._stats = {
            "messages": 0,
            "ticker_updates": 0,
            "candle_1m_updates": 0,
            "candle_5m_updates": 0,
            "closed_1m": 0,
            "closed_5m": 0,
            "duplicates": 0,
            "out_of_order": 0,
            "gaps": 0,
            "reconnects": 0,
            "parse_errors": 0,
        }

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start supervisor + heartbeat threads. Idempotent."""
        with self._lock:
            if self._supervisor_thread and self._supervisor_thread.is_alive():
                return
            self._stop_event.clear()
            self._ready_event.clear()
            self._supervisor_thread = threading.Thread(
                target=self._supervisor_loop,
                name=f"bitget-ws-{self.symbol}",
                daemon=True,
            )
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"bitget-ws-heartbeat-{self.symbol}",
                daemon=True,
            )
            self._supervisor_thread.start()
            self._heartbeat_thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """Stop threads and close the active WebSocket connection."""
        self._stop_event.set()
        self._ready_event.clear()
        ws = None
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

        current = threading.current_thread()
        for thread in (self._supervisor_thread, self._heartbeat_thread):
            if thread and thread.is_alive() and thread is not current:
                thread.join(timeout=join_timeout)

    def request_reconnect(self, reason: str = "manual reconnect") -> bool:
        """Close the active socket so the supervisor performs a normal reconnect."""
        with self._lock:
            ws = self._ws
        if ws is None:
            self._record_error(f"{reason}: aucun socket actif")
            return False
        self.log.warning("WebSocket reconnexion demandée | %s", reason)
        try:
            ws.close()
            return True
        except Exception as exc:
            self._record_error(f"{reason}: close: {exc}")
            return False

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        """Wait until all three subscription acknowledgements are received."""
        return self._ready_event.wait(timeout)

    # ------------------------------------------------------------------
    # REST bootstrap / resync bridge
    # ------------------------------------------------------------------

    def seed_candles(self, interval: str, candles: Iterable[dict]) -> None:
        """Seed the developing candle from REST without generating close events.

        The current Bitget REST candle is expected to be the newest item. The
        method sorts by timestamp defensively and stores only that newest item.
        """
        self._validate_interval(interval)
        parsed = [self._normalise_rest_candle(c) for c in candles]
        parsed = [c for c in parsed if c is not None]
        if not parsed:
            return
        parsed.sort(key=lambda c: c["timestamp"])
        with self._lock:
            # Bootstrap/resync is authoritative: discard queued close events that
            # may belong to the pre-resync stream and realign the dedup marker.
            self._closed_candles[interval].clear()
            self._current_candles[interval] = parsed[-1]
            self._last_closed_ts[interval] = parsed[-2]["timestamp"] if len(parsed) >= 2 else None

    def mark_resynchronised(self) -> None:
        """Clear the reconnect/gap resync flag after the caller completed REST resync."""
        with self._lock:
            self._needs_resync = False

    def needs_resync(self) -> bool:
        with self._lock:
            return bool(self._needs_resync)

    # ------------------------------------------------------------------
    # Thread-safe data accessors
    # ------------------------------------------------------------------

    def get_live_price(self) -> Optional[float]:
        with self._lock:
            return self._live_price

    def get_best_bid_ask(self) -> tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self._best_bid, self._best_ask

    def get_current_candle(self, interval: str) -> Optional[dict]:
        self._validate_interval(interval)
        with self._lock:
            candle = self._current_candles[interval]
            return deepcopy(candle) if candle is not None else None

    def drain_closed_candles(self, interval: str) -> List[dict]:
        """Atomically return and clear closed-candle events for an interval."""
        self._validate_interval(interval)
        with self._lock:
            result = list(self._closed_candles[interval])
            self._closed_candles[interval].clear()
        return deepcopy(result)

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    def is_healthy(self) -> bool:
        """Health is connection + subscriptions + a pong newer than last ping.

        Bitget requires a text ping every 30s and recommends reconnecting if its
        corresponding pong is not received. Before the first ping, readiness is
        sufficient; after a ping, the latest pong must acknowledge it.
        """
        with self._lock:
            if not self._connected or not self._ready_event.is_set():
                return False
            if self._last_ping_monotonic <= 0:
                return True
            return self._last_pong_monotonic >= self._last_ping_monotonic

    def last_message_age(self) -> Optional[float]:
        with self._lock:
            ts = self._last_message_monotonic
        return None if ts <= 0 else max(0.0, time.monotonic() - ts)

    def last_data_age(self) -> Optional[float]:
        with self._lock:
            ts = self._last_data_monotonic
        return None if ts <= 0 else max(0.0, time.monotonic() - ts)

    def health_snapshot(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "ready": self._ready_event.is_set(),
                "healthy": self.is_healthy(),
                "subscriptions": sorted(self._subscriptions),
                "connection_generation": self._connection_generation,
                "needs_resync": self._needs_resync,
                "live_price": self._live_price,
                "best_bid": self._best_bid,
                "best_ask": self._best_ask,
                "ticker_exchange_ts_ms": self._ticker_exchange_ts_ms,
                "last_message_age": self.last_message_age(),
                "last_data_age": self.last_data_age(),
                "last_error": self._last_error,
                "stats": dict(self._stats),
            }

    # ------------------------------------------------------------------
    # WebSocket supervisor / heartbeat
    # ------------------------------------------------------------------

    def _supervisor_loop(self) -> None:
        delay = self.reconnect_min_seconds
        while not self._stop_event.is_set():
            self._connection_had_data = False
            app = self._ws_app_factory(
                self.url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            with self._lock:
                self._ws = app

            try:
                app.run_forever()
            except Exception as exc:
                self._record_error(f"run_forever: {exc}")

            with self._lock:
                self._connected = False
                self._subscriptions.clear()
                self._ready_event.clear()
                if self._ws is app:
                    self._ws = None
                had_data = self._connection_had_data

            if self._stop_event.is_set():
                break

            # A previously useful connection reconnects after the minimum delay.
            # Repeated failures before receiving data back off exponentially.
            if had_data:
                delay = self.reconnect_min_seconds
            self.log.warning("WebSocket déconnecté — reconnexion dans %.1fs", delay)
            if self._stop_event.wait(delay):
                break
            if not had_data:
                delay = min(self.reconnect_max_seconds, delay * 2)

    def _heartbeat_loop(self) -> None:
        # Bitget's heartbeat is a literal text message, not a WS protocol ping frame.
        while not self._stop_event.wait(0.25):
            ws = None
            now = time.monotonic()
            with self._lock:
                if not self._connected:
                    continue
                ws = self._ws
                last_ping = self._last_ping_monotonic
                last_pong = self._last_pong_monotonic

            if ws is None:
                continue

            # If one full heartbeat period elapsed without pong for the previous
            # ping, reconnect instead of continuing on an unverified connection.
            if last_ping > 0 and last_pong < last_ping:
                if now - last_ping >= self.heartbeat_seconds:
                    self._record_error("heartbeat pong manquant")
                    try:
                        ws.close()
                    except Exception:
                        pass
                    continue

            if last_ping <= 0 or now - last_ping >= self.heartbeat_seconds:
                self._send_text(ws, "ping")
                with self._lock:
                    self._last_ping_monotonic = time.monotonic()

    def _send_text(self, ws: Any, payload: str) -> None:
        try:
            with self._send_lock:
                ws.send(payload)
        except Exception as exc:
            self._record_error(f"send: {exc}")
            try:
                ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_open(self, ws: Any) -> None:
        with self._lock:
            self._ws = ws
            self._connected = True
            self._subscriptions.clear()
            self._ready_event.clear()
            self._connection_generation += 1
            generation = self._connection_generation
            if generation > 1:
                self._stats["reconnects"] += 1
                self._needs_resync = True
            now = time.monotonic()
            self._last_message_monotonic = now
            self._last_ping_monotonic = 0.0
            self._last_pong_monotonic = now
            self._last_error = None

        args = [
            {"instType": self.inst_type, "channel": "ticker", "instId": self.symbol},
            {"instType": self.inst_type, "channel": "candle1m", "instId": self.symbol},
            {"instType": self.inst_type, "channel": "candle5m", "instId": self.symbol},
        ]
        self._send_text(ws, json.dumps({"op": "subscribe", "args": args}, separators=(",", ":")))
        self.log.info("WebSocket connecté | génération=%s | %s", generation, self.symbol)

    def _on_message(self, ws: Any, message: Any) -> None:
        now = time.monotonic()
        with self._lock:
            self._last_message_monotonic = now
            self._stats["messages"] += 1

        if message == "pong" or message == b"pong":
            with self._lock:
                self._last_pong_monotonic = now
            return

        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except Exception:
                self._parse_error("message bytes non UTF-8")
                return

        try:
            payload = json.loads(message)
        except Exception as exc:
            self._parse_error(f"JSON invalide: {exc}")
            return

        if not isinstance(payload, dict):
            self._parse_error("payload non dictionnaire")
            return

        event = payload.get("event")
        if event == "subscribe":
            arg = payload.get("arg") or {}
            channel = arg.get("channel")
            if channel in self._expected_subscriptions:
                with self._lock:
                    self._subscriptions.add(channel)
                    if self._subscriptions == self._expected_subscriptions:
                        self._ready_event.set()
            return

        if event == "error" or (payload.get("code") and event is not None):
            code = payload.get("code", "?")
            msg = payload.get("msg", "WebSocket error")
            self._record_error(f"Bitget event error [{code}]: {msg}")
            try:
                ws.close()
            except Exception:
                pass
            return

        arg = payload.get("arg") or {}
        channel = arg.get("channel")
        data = payload.get("data")
        if not channel or data is None:
            return

        with self._lock:
            self._last_data_monotonic = now
            self._connection_had_data = True

        if channel == "ticker":
            self._handle_ticker(data, payload)
        elif channel == "candle1m":
            self._handle_candle_payload("1m", data, payload.get("action"))
        elif channel == "candle5m":
            self._handle_candle_payload("5m", data, payload.get("action"))

    def _on_error(self, ws: Any, error: Any) -> None:
        self._record_error(f"WebSocket: {error}")

    def _on_close(self, ws: Any, status_code: Any, close_msg: Any) -> None:
        with self._lock:
            self._connected = False
            self._subscriptions.clear()
            self._ready_event.clear()
        if not self._stop_event.is_set():
            self.log.warning("WebSocket fermé | code=%s | %s", status_code, close_msg)

    # ------------------------------------------------------------------
    # Payload handlers
    # ------------------------------------------------------------------

    def _handle_ticker(self, data: Any, payload: dict) -> None:
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            self._parse_error("ticker data invalide")
            return
        row = data[0]
        try:
            last_price = float(row["lastPr"])
            bid = self._optional_float(row.get("bidPr"))
            ask = self._optional_float(row.get("askPr"))
            exchange_ts = row.get("ts", payload.get("ts"))
            exchange_ts_ms = int(exchange_ts) if exchange_ts is not None else None
        except (KeyError, TypeError, ValueError) as exc:
            self._parse_error(f"ticker parse: {exc}")
            return

        with self._lock:
            self._live_price = last_price
            self._best_bid = bid
            self._best_ask = ask
            self._ticker_exchange_ts_ms = exchange_ts_ms
            self._stats["ticker_updates"] += 1

    def _handle_candle_payload(self, interval: str, data: Any, action: Optional[str]) -> None:
        self._validate_interval(interval)
        if not isinstance(data, list) or not data:
            self._parse_error(f"candle{interval} data invalide")
            return

        parsed = []
        for row in data:
            candle = self._parse_ws_candle(row)
            if candle is not None:
                parsed.append(candle)
        if not parsed:
            return
        parsed.sort(key=lambda c: c["timestamp"])

        with self._lock:
            no_seed = self._current_candles[interval] is None

        # A subscription snapshot can contain history. Without REST seed, adopt
        # only its newest candle so history is never replayed as fresh close events.
        if action == "snapshot" and no_seed:
            with self._lock:
                self._current_candles[interval] = parsed[-1]
                key = "candle_1m_updates" if interval == "1m" else "candle_5m_updates"
                self._stats[key] += len(parsed)
            return

        for candle in parsed:
            self._apply_candle_update(interval, candle)

    def _apply_candle_update(self, interval: str, candle: dict) -> None:
        step = SUPPORTED_INTERVALS[interval]
        ts = candle["timestamp"]
        with self._lock:
            current = self._current_candles[interval]
            key_updates = "candle_1m_updates" if interval == "1m" else "candle_5m_updates"
            self._stats[key_updates] += 1

            if current is None:
                self._current_candles[interval] = candle
                return

            current_ts = current["timestamp"]
            if ts < current_ts:
                self._stats["out_of_order"] += 1
                return

            if ts == current_ts:
                self._current_candles[interval] = candle
                self._stats["duplicates"] += 1
                return

            # New candle timestamp: the previous developing candle is now closed.
            if ts - current_ts > step:
                self._stats["gaps"] += 1
                self._needs_resync = True

            if self._last_closed_ts[interval] != current_ts:
                self._closed_candles[interval].append(deepcopy(current))
                self._last_closed_ts[interval] = current_ts
                closed_key = "closed_1m" if interval == "1m" else "closed_5m"
                self._stats[closed_key] += 1

            self._current_candles[interval] = candle

    # ------------------------------------------------------------------
    # Parsing / validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ws_candle(row: Any) -> Optional[dict]:
        # Bitget v2 candle payload:
        # [start_ms, open, high, low, close, base_volume, quote_volume, usdt_volume]
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            return None
        try:
            return {
                "timestamp": int(row[0]) // 1000,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalise_rest_candle(candle: Any) -> Optional[dict]:
        if not isinstance(candle, dict):
            return None
        try:
            return {
                "timestamp": int(candle["timestamp"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": float(candle["volume"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        return float(value)

    @staticmethod
    def _validate_interval(interval: str) -> None:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"interval non supporté: {interval}")

    def _parse_error(self, message: str) -> None:
        with self._lock:
            self._stats["parse_errors"] += 1
            self._last_error = message
        self.log.warning(message)

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        self.log.warning(message)
