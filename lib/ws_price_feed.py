"""
WebSocket BTC Price Feed for PolyBMiCoB.

Provides real-time BTC price via Binance WebSocket stream.
Runs in a background thread, updates shared state that the bot reads instantly.

Replaces REST polling (10s latency) with event-driven updates (<1s latency).

Usage:
    feed = BtcPriceFeed()
    feed.start()
    # ... later ...
    price = feed.price          # latest BTC price (float)
    change = feed.change_since(start_price)  # % change since a reference price
    feed.stop()
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field

import websocket  # websocket-client library

log = logging.getLogger("polybmicob.wsfeed")

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"


@dataclass
class BtcPriceFeed:
    """
    Real-time BTC price feed via Binance WebSocket.

    Attributes:
        price: Latest BTC/USDT price (updated every ~1s).
        updated_at: Timestamp of last price update.
        connected: Whether WebSocket is connected.
    """

    price: float = 0.0
    updated_at: float = 0.0
    connected: bool = False
    _ws: websocket.WebSocketApp | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def start(self) -> None:
        """Start the WebSocket connection in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            log.warning("BTC price feed already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name="btc-ws-feed",
            daemon=True,
        )
        self._thread.start()
        log.info("BTC WebSocket price feed started")

    def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self.connected = False
        log.info("BTC WebSocket price feed stopped")

    def change_since(self, start_price: float) -> float:
        """Calculate % change from start_price to current price."""
        if start_price <= 0 or self.price <= 0:
            return 0.0
        return ((self.price - start_price) / start_price) * 100

    def is_fresh(self, max_age_sec: float = 5.0) -> bool:
        """Check if price data is fresh (updated within max_age_sec)."""
        return (time.time() - self.updated_at) < max_age_sec

    def _run_forever(self) -> None:
        """Run WebSocket with auto-reconnect."""
        while not self._stop_event.is_set():
            try:
                self._connect()
            except Exception as exc:
                log.warning("WS feed error: %s, reconnecting in 5s...", exc)
            if not self._stop_event.is_set():
                self.connected = False
                time.sleep(5)  # reconnect delay

    def _connect(self) -> None:
        """Establish WebSocket connection."""
        self._ws = websocket.WebSocketApp(
            BINANCE_WS_URL,
            on_message=self._on_message,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        # run_forever blocks until connection closes
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws) -> None:
        self.connected = True
        log.info("BTC WebSocket connected to Binance")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self.connected = False
        log.info("BTC WebSocket disconnected (code=%s)", close_status_code)

    def _on_error(self, ws, error) -> None:
        log.debug("BTC WebSocket error: %s", error)

    def _on_message(self, ws, message) -> None:
        """Process incoming kline message and update price."""
        try:
            data = json.loads(message)
            kline = data.get("k", {})
            # Use close price of current candle as latest price
            close_price = float(kline.get("c", 0))
            if close_price > 0:
                self.price = close_price
                self.updated_at = time.time()
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
