"""
Market Clock for PolyBMiCoB.

Generates precise market_tick events on the 5-minute grid (300s intervals).
Replaces REST polling of Gamma API for market discovery.

BTC 5-min markets start on a 300-second grid aligned to Unix epoch:
  - Slug: btc-updown-5m-{UNIX_TIMESTAMP}
  - Start: timestamp (multiple of 300)
  - End: timestamp + 300

The clock calculates the next slot boundary and wakes up precisely at
the right time to emit events, accounting for pre-market lead time.

Usage:
    clock = MarketClock(bus, pre_market_sec=30)
    clock.start()   # background thread
    clock.stop()

Events emitted:
    "market_tick" -> {
        "slot_ts": 1711018800,          # slot start timestamp
        "slug": "btc-updown-5m-1711018800",
        "phase": "pre_market",          # or "in_play", "ending"
        "seconds_to_start": 30,         # negative = already started
        "seconds_to_end": 330,
    }
"""

import logging
import threading
import time

log = logging.getLogger("polybmicob.clock")

SLOT_DURATION_SEC = 300  # 5 minutes


class MarketClock:
    """
    Background thread that emits market_tick events on the 5-min grid.

    Generates events at key moments:
      - pre_market: PRE_MARKET_SEC before slot starts (time to scan/prepare)
      - in_play: when slot starts (market is live)
      - mid_play: midpoint of the slot (for in-play analysis)
      - ending: ENDING_SEC before slot ends (time for hedging)
    """

    def __init__(
        self,
        bus,
        pre_market_sec: int = 30,
        mid_play_sec: int = 90,
        ending_sec: int = 60,
    ) -> None:
        """
        Args:
            bus: EventBus instance to emit events on.
            pre_market_sec: Seconds before slot start to emit pre_market tick.
            mid_play_sec: Seconds after slot start to emit mid_play tick.
            ending_sec: Seconds before slot end to emit ending tick.
        """
        self._bus = bus
        self._pre_market_sec = pre_market_sec
        self._mid_play_sec = mid_play_sec
        self._ending_sec = ending_sec
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the market clock in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            log.warning("MarketClock already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="market-clock",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "MarketClock started (pre=%ds, mid=%ds, ending=%ds)",
            self._pre_market_sec,
            self._mid_play_sec,
            self._ending_sec,
        )

    def stop(self) -> None:
        """Stop the market clock."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("MarketClock stopped")

    @staticmethod
    def current_slot_ts() -> int:
        """Get the timestamp of the currently active 5-min slot."""
        now = int(time.time())
        return now - (now % SLOT_DURATION_SEC)

    @staticmethod
    def next_slot_ts() -> int:
        """Get the timestamp of the next 5-min slot."""
        now = int(time.time())
        return now - (now % SLOT_DURATION_SEC) + SLOT_DURATION_SEC

    @staticmethod
    def slot_slug(slot_ts: int) -> str:
        """Generate the market slug for a given slot timestamp."""
        return f"btc-updown-5m-{slot_ts}"

    def _run(self) -> None:
        """Main clock loop: sleep until next event, emit, repeat."""
        while not self._stop_event.is_set():
            now = time.time()
            next_slot = self.next_slot_ts()
            current_slot = self.current_slot_ts()
            elapsed_in_slot = now - current_slot

            # Calculate all upcoming event times for current and next slot
            events = self._compute_upcoming_events(now, current_slot, next_slot)

            if not events:
                # Shouldn't happen, but sleep briefly and retry
                self._stop_event.wait(timeout=1.0)
                continue

            # Find the soonest event
            next_event_time, phase, slot_ts = events[0]
            wait_sec = max(0, next_event_time - now)

            if wait_sec > 0:
                # Sleep until the next event (interruptible)
                self._stop_event.wait(timeout=wait_sec)
                if self._stop_event.is_set():
                    break

            # Emit the event
            now = time.time()
            slot_end = slot_ts + SLOT_DURATION_SEC
            self._bus.emit("market_tick", {
                "slot_ts": slot_ts,
                "slug": self.slot_slug(slot_ts),
                "phase": phase,
                "seconds_to_start": int(slot_ts - now),
                "seconds_to_end": int(slot_end - now),
                "elapsed": int(now - slot_ts) if now >= slot_ts else 0,
            })

            log.debug(
                "Tick: %s phase=%s (slot %d, %+ds to start)",
                self.slot_slug(slot_ts),
                phase,
                slot_ts,
                int(slot_ts - now),
            )

            # Small delay to avoid double-firing at boundary
            self._stop_event.wait(timeout=0.5)

    def _compute_upcoming_events(
        self,
        now: float,
        current_slot: int,
        next_slot: int,
    ) -> list[tuple[float, str, int]]:
        """
        Compute upcoming (time, phase, slot_ts) tuples sorted by time.

        Returns events that haven't happened yet (time > now - 0.5).
        """
        events = []

        # Events for the NEXT slot
        # pre_market: fires PRE_MARKET_SEC before next slot starts
        pre_time = next_slot - self._pre_market_sec
        events.append((pre_time, "pre_market", next_slot))

        # in_play: fires when next slot starts
        events.append((float(next_slot), "in_play", next_slot))

        # mid_play: fires MID_PLAY_SEC after next slot starts
        mid_time = next_slot + self._mid_play_sec
        events.append((mid_time, "mid_play", next_slot))

        # ending: fires ENDING_SEC before next slot ends
        ending_time = next_slot + SLOT_DURATION_SEC - self._ending_sec
        events.append((ending_time, "ending", next_slot))

        # Events for the CURRENT slot (if still relevant)
        mid_current = current_slot + self._mid_play_sec
        events.append((mid_current, "mid_play", current_slot))

        ending_current = current_slot + SLOT_DURATION_SEC - self._ending_sec
        events.append((ending_current, "ending", current_slot))

        # Filter: only future events (with 0.5s tolerance for boundary)
        events = [(t, p, s) for t, p, s in events if t > now - 0.5]

        # Sort by time (soonest first)
        events.sort(key=lambda e: e[0])

        return events
