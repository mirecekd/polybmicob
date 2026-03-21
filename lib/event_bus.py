"""
Event Bus for PolyBMiCoB.

Thread-safe in-process event bus for decoupling producers and consumers.
Replaces the poll-sleep loop with event-driven architecture.

Event types:
  "btc_price"        - BTC price updated (from Binance WS)
  "market_tick"      - New 5-min epoch started (from MarketClock)
  "orderbook_update" - Polymarket orderbook changed (from Poly WS)
  "best_bid_ask"     - Top-of-book update (from Poly WS)
  "trade_filled"     - Order was filled
  "market_resolved"  - Market resolved (from Poly WS)

Usage:
    bus = EventBus()

    # Register handlers
    bus.on("btc_price", handle_btc_price)
    bus.on("market_tick", handle_market_tick)

    # Emit events (from any thread)
    bus.emit("btc_price", {"price": 84500.0, "timestamp": 1711018800})

    # Schedule periodic tasks
    bus.schedule(interval_sec=30, handler=check_resolutions)

    # Run (blocks until stop)
    bus.run()

    # Stop from signal handler or another thread
    bus.stop()
"""

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Callable

log = logging.getLogger("polybmicob.eventbus")

# Type alias for event handler: fn(event_type, data) -> None
EventHandler = Callable[[str, dict[str, Any]], None]


@dataclass
class ScheduledTask:
    """A periodic task that runs on a fixed interval."""

    interval_sec: float
    handler: Callable[[], None]
    name: str
    next_run: float = 0.0


@dataclass
class Event:
    """An event to be processed by the bus."""

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EventBus:
    """
    Thread-safe event bus with handler registration and periodic scheduling.

    All handlers run in the main processing thread (single-threaded dispatch).
    Events can be emitted from any thread (thread-safe queue).
    """

    def __init__(self, max_queue_size: int = 10000) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._queue: Queue[Event | None] = Queue(maxsize=max_queue_size)
        self._scheduled: list[ScheduledTask] = []
        self._running = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Stats
        self.events_processed: int = 0
        self.events_dropped: int = 0
        self._drop_log_count: int = 0
        self._drop_log_interval: int = 100  # log every Nth drop

    def on(self, event_type: str, handler: EventHandler) -> None:
        """
        Register a handler for an event type.

        Handler signature: fn(event_type: str, data: dict) -> None
        Multiple handlers can be registered for the same event type.
        """
        with self._lock:
            self._handlers[event_type].append(handler)
            log.debug("Registered handler %s for '%s'", handler.__name__, event_type)

    def off(self, event_type: str, handler: EventHandler) -> None:
        """Unregister a handler for an event type."""
        with self._lock:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """
        Emit an event (thread-safe).

        Can be called from any thread. Event is queued for processing
        in the main dispatch loop.
        """
        event = Event(event_type=event_type, data=data or {})
        try:
            self._queue.put_nowait(event)
        except Exception:
            self.events_dropped += 1
            self._drop_log_count += 1
            if self._drop_log_count <= 1 or self._drop_log_count % self._drop_log_interval == 0:
                log.warning(
                    "Event queue full, dropped '%s' event (total dropped: %d)",
                    event_type, self.events_dropped,
                )

    def schedule(
        self,
        interval_sec: float,
        handler: Callable[[], None],
        name: str = "",
    ) -> None:
        """
        Schedule a periodic task.

        Task runs every interval_sec seconds in the main dispatch loop.
        Unlike event handlers, scheduled tasks take no arguments.
        """
        task_name = name or handler.__name__
        task = ScheduledTask(
            interval_sec=interval_sec,
            handler=handler,
            name=task_name,
            next_run=time.time() + interval_sec,
        )
        self._scheduled.append(task)
        log.debug("Scheduled '%s' every %.0fs", task_name, interval_sec)

    def run(self) -> None:
        """
        Run the event dispatch loop (blocks until stop() is called).

        Processes events from the queue and runs scheduled tasks.
        """
        self._running = True
        self._stop_event.clear()
        log.info(
            "EventBus started (%d event types, %d scheduled tasks)",
            len(self._handlers),
            len(self._scheduled),
        )

        while not self._stop_event.is_set():
            # Process all pending events (non-blocking drain)
            self._drain_events()

            # Run scheduled tasks that are due
            self._run_scheduled()

            # Brief sleep to avoid busy-wait (100ms = responsive enough)
            self._stop_event.wait(timeout=0.1)

        # Drain remaining events before exit
        self._drain_events()
        self._running = False
        log.info(
            "EventBus stopped (processed %d events, dropped %d)",
            self.events_processed,
            self.events_dropped,
        )

    def stop(self) -> None:
        """Signal the dispatch loop to stop."""
        log.info("EventBus stop requested")
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        """Whether the dispatch loop is running."""
        return self._running

    def _drain_events(self) -> None:
        """Process all events currently in the queue."""
        while True:
            try:
                event = self._queue.get_nowait()
            except Empty:
                break

            if event is None:
                continue

            self._dispatch(event)

    def _dispatch(self, event: Event) -> None:
        """Dispatch a single event to all registered handlers."""
        with self._lock:
            handlers = list(self._handlers.get(event.event_type, []))

        for handler in handlers:
            try:
                handler(event.event_type, event.data)
            except Exception as exc:
                log.error(
                    "Handler %s failed for '%s': %s",
                    handler.__name__,
                    event.event_type,
                    exc,
                    exc_info=True,
                )

        self.events_processed += 1

    def _run_scheduled(self) -> None:
        """Run any scheduled tasks that are due."""
        now = time.time()
        for task in self._scheduled:
            if now >= task.next_run:
                try:
                    task.handler()
                except Exception as exc:
                    log.error(
                        "Scheduled task '%s' failed: %s",
                        task.name,
                        exc,
                        exc_info=True,
                    )
                task.next_run = now + task.interval_sec
