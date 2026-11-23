"""Request throttling, retry/back-off and the circuit breaker (requirement 5: throttling, retry, timeout mgmt)."""
from __future__ import annotations

import random
import threading
import time
from typing import Callable

from ..core.errors import PermanentProviderError, RateLimitError, TransientProviderError


class RateLimiter:
    """Spaces requests so that at most `per_minute` are started per minute (thread-safe)."""

    def __init__(self, per_minute: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self._clock, self._sleep, self._lock, self._next = clock, sleep, threading.Lock(), 0.0

    def acquire(self, weight: int = 1) -> None:
        """Reserve `weight` request slots (a call that makes several HTTP requests must pay for all of them)."""
        if self.interval <= 0:
            return
        with self._lock:
            now = self._clock()
            wait = self._next - now
            self._next = max(now, self._next) + self.interval * max(1, int(weight))
        if wait > 0:
            self._sleep(wait)


class CircuitBreaker:
    """Opens after `threshold` consecutive rate-limit failures; stays open for the rest of the run so we stop
    hammering the provider and simply resume on the next scheduled run."""

    def __init__(self, threshold: int = 3):
        self.threshold, self.count, self._lock = threshold, 0, threading.Lock()

    def record_rate_limit(self) -> None:
        with self._lock:
            self.count += 1

    def record_success(self) -> None:
        with self._lock:
            self.count = 0

    @property
    def tripped(self) -> bool:
        return self.threshold > 0 and self.count >= self.threshold


def retry_call(fn: Callable, *, retries: int, base_delay: float, rate_limit_delay: float,
               sleep: Callable[[float], None] = time.sleep, rng: Callable[[], float] = random.random):
    """Call fn(); returns (value, attempts).
    Permanent errors -> raised at once. Transient -> exponential back-off. RateLimit -> longer exponential back-off.
    After `retries` extra attempts the last error is raised."""
    attempt = 0
    while True:
        try:
            return fn(), attempt + 1
        except PermanentProviderError:
            raise
        except (RateLimitError, TransientProviderError) as exc:
            if attempt >= retries:
                raise
            base = rate_limit_delay if isinstance(exc, RateLimitError) else base_delay
            delay = base * (2 ** attempt)
            sleep(delay + delay * 0.25 * rng())
            attempt += 1
