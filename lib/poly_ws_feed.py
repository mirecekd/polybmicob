"""
Polymarket WebSocket Feed for PolyBMiCoB.

Provides real-time orderbook updates via Polymarket CLOB WebSocket.
No authentication required for market channel (read-only).

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribes to token IDs and receives:
  - "book": full orderbook snapshot
  - "price_change": individual price level update
  - "best_bid_ask": top-of-book update
  - "last_trade_price": trade executed
  - "market_resolved": market resolved

Emits EventBus events:
  - "orderbook_update": full book snapshot or price level change
  - "best_bid_ask": top-of-book bid/ask update
  - "market_resolved": market has been resolved

Usage:
    feed = PolyWsFeed(bus=event_bus)
    feed.start()
    feed.subscribe(["token_id_1", "token_id_2"])
    feed.unsubscribe(["token_id_1"])
    feed.stop()
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field

import websocket  # websocket-client library

log = logging.getLogger("polybmicob.polywsfeed")

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class PolyWsFeed:
    """
    Real-time Polymarket orderbook feed via WebSocket.

    Maintains a set of subscribed token IDs and emits events on the bus
    when orderbook data arrives. Supports dynamic subscribe/unsubscribe
    for token IDs as markets come and go.

    Attributes:
        connected: Whether WebSocket is connected.
        subscribed_tokens: Set of currently subscribed token IDs.
    """

    bus: object = field(repr=False)  # EventBus (required)
    connected: bool = False
    subscribed_tokens: set[str] = field(default_factory=set)
    _ws: websocket.WebSocketApp | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _pending_subscribes: list[list[str]] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Stats
    messages_received: int = 0
    books_received: int = 0

    def start(self) -> None:
        """Start the WebSocket connection in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            log.warning("Polymarket WS feed already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name="poly-ws-feed",
            daemon=True,
        )
        self._thread.start()
        log.info("Polymarket WebSocket feed started")

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
        self.subscribed_tokens.clear()
        log.info("Polymarket WebSocket feed stopped")

    def subscribe(self, token_ids: list[str]) -> None:
        """
        Subscribe to orderbook updates for the given token IDs.

        Can be called before or after connection is established.
        If not yet connected, subscriptions are queued and sent on connect.
        """
        if not token_ids:
            return

        new_tokens = [t for t in token_ids if t not in self.subscribed_tokens]
        if not new_tokens:
            return

        with self._lock:
            self.subscribed_tokens.update(new_tokens)

        if self.connected and self._ws:
            self._send_subscribe(new_tokens)
        else:
            # Queue for when connection is established
            with self._lock:
                self._pending_subscribes.append(new_tokens)

        log.info(
            "Poly WS: subscribing to %d token(s) (%d total)",
            len(new_tokens),
            len(self.subscribed_tokens),
        )

    def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from orderbook updates for the given token IDs."""
        if not token_ids:
            return

        tokens_to_remove = [t for t in token_ids if t in self.subscribed_tokens]
        if not tokens_to_remove:
            return

        with self._lock:
            self.subscribed_tokens -= set(tokens_to_remove)

        if self.connected and self._ws:
            self._send_unsubscribe(tokens_to_remove)

        log.info(
            "Poly WS: unsubscribed %d token(s) (%d remaining)",
            len(tokens_to_remove),
            len(self.subscribed_tokens),
        )

    def _send_subscribe(self, token_ids: list[str]) -> None:
        """Send subscribe message over WebSocket."""
        try:
            msg = json.dumps({
                "assets_ids": token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            })
            self._ws.send(msg)
        except Exception as exc:
            log.warning("Poly WS subscribe failed: %s", exc)

    def _send_unsubscribe(self, token_ids: list[str]) -> None:
        """Send unsubscribe message over WebSocket."""
        try:
            msg = json.dumps({
                "assets_ids": token_ids,
                "operation": "unsubscribe",
            })
            self._ws.send(msg)
        except Exception as exc:
            log.warning("Poly WS unsubscribe failed: %s", exc)

    def _run_forever(self) -> None:
        """Run WebSocket with auto-reconnect."""
        while not self._stop_event.is_set():
            try:
                self._connect()
            except Exception as exc:
                log.warning("Poly WS error: %s, reconnecting in 5s...", exc)
            if not self._stop_event.is_set():
                self.connected = False
                time.sleep(5)  # reconnect delay

    def _connect(self) -> None:
        """Establish WebSocket connection."""
        self._ws = websocket.WebSocketApp(
            POLY_WS_URL,
            on_message=self._on_message,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws) -> None:
        self.connected = True
        log.info("Polymarket WebSocket connected")

        # Send any pending subscriptions
        with self._lock:
            pending = list(self._pending_subscribes)
            self._pending_subscribes.clear()

        for token_batch in pending:
            self._send_subscribe(token_batch)

        # Re-subscribe all existing tokens on reconnect
        with self._lock:
            all_tokens = list(self.subscribed_tokens)

        if all_tokens:
            self._send_subscribe(all_tokens)
            log.info("Poly WS: re-subscribed to %d token(s) on reconnect", len(all_tokens))

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self.connected = False
        log.info("Polymarket WebSocket disconnected (code=%s)", close_status_code)

    def _on_error(self, ws, error) -> None:
        log.debug("Polymarket WebSocket error: %s", error)

    def _on_message(self, ws, message) -> None:
        """Process incoming market data message and emit bus events."""
        self.messages_received += 1

        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return

        # Handle list of messages (Polymarket sometimes batches)
        messages = data if isinstance(data, list) else [data]

        for msg in messages:
            event_type = msg.get("event_type", "")

            if event_type == "book":
                self._handle_book(msg)
            elif event_type == "price_change":
                self._handle_price_change(msg)
            elif event_type == "best_bid_ask":
                self._handle_best_bid_ask(msg)
            elif event_type == "last_trade_price":
                self._handle_last_trade(msg)
            elif event_type == "market_resolved":
                self._handle_market_resolved(msg)

    def _handle_book(self, msg: dict) -> None:
        """Handle full orderbook snapshot."""
        self.books_received += 1
        asset_id = msg.get("asset_id", "")
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        # Parse bids/asks into (price, size) tuples
        parsed_bids = [
            (float(b["price"]), float(b["size"]))
            for b in bids if "price" in b and "size" in b
        ]
        parsed_asks = [
            (float(a["price"]), float(a["size"]))
            for a in asks if "price" in a and "size" in a
        ]

        self.bus.emit("orderbook_update", {
            "token_id": asset_id,
            "market": msg.get("market", ""),
            "bids": parsed_bids,
            "asks": parsed_asks,
            "snapshot": True,
            "timestamp": msg.get("timestamp", ""),
        })

    def _handle_price_change(self, msg: dict) -> None:
        """Handle individual price level update (delta)."""
        asset_id = msg.get("asset_id", "")

        self.bus.emit("orderbook_update", {
            "token_id": asset_id,
            "market": msg.get("market", ""),
            "changes": msg.get("changes", []),
            "snapshot": False,
            "side": msg.get("side", ""),
            "price": msg.get("price", ""),
            "size": msg.get("size", ""),
            "timestamp": msg.get("timestamp", ""),
        })

    def _handle_best_bid_ask(self, msg: dict) -> None:
        """Handle top-of-book update."""
        asset_id = msg.get("asset_id", "")

        self.bus.emit("best_bid_ask", {
            "token_id": asset_id,
            "market": msg.get("market", ""),
            "best_bid": msg.get("best_bid", ""),
            "best_ask": msg.get("best_ask", ""),
            "timestamp": msg.get("timestamp", ""),
        })

    def _handle_last_trade(self, msg: dict) -> None:
        """Handle trade execution notification."""
        asset_id = msg.get("asset_id", "")

        self.bus.emit("last_trade", {
            "token_id": asset_id,
            "market": msg.get("market", ""),
            "price": msg.get("price", ""),
            "size": msg.get("size", ""),
            "side": msg.get("side", ""),
            "fee_rate_bps": msg.get("fee_rate_bps", ""),
            "timestamp": msg.get("timestamp", ""),
        })

    def _handle_market_resolved(self, msg: dict) -> None:
        """Handle market resolution notification."""
        asset_id = msg.get("asset_id", "")

        self.bus.emit("market_resolved", {
            "token_id": asset_id,
            "market": msg.get("market", ""),
            "timestamp": msg.get("timestamp", ""),
        })

        log.info("Market resolved via WS: %s", msg.get("market", "unknown"))
