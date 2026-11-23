"""Project-wide exception types. Catch these (not bare Exception) at module boundaries."""


class MizanError(Exception):
    """Base class for all expected, handled errors."""


class ConfigError(MizanError):
    """Configuration file missing, unreadable or invalid."""


class UniverseError(MizanError):
    """Base class for stock-universe problems."""


class EmptyUniverseError(UniverseError):
    """The universe has no active tickers - modules must refuse to run rather than silently do nothing."""


class UniverseValidationError(UniverseError):
    """Raised only in strict mode when the universe file contains invalid lines."""


class DataError(MizanError):
    """Base class for data-acquisition problems."""


class DataUnavailableError(DataError):
    """Requested data could not be provided fresh and complete (never swallowed silently)."""

    def __init__(self, message: str, ticker: str | None = None, data_type: str | None = None, reason: str = "unavailable"):
        super().__init__(message)
        self.ticker, self.data_type, self.reason = ticker, data_type, reason


class ProviderPausedError(DataUnavailableError):
    """Circuit breaker is open (repeated rate limiting): no further requests this run."""


class ProviderError(DataError):
    """Raised by provider adapters. The three subclasses drive retry behaviour."""


class RateLimitError(ProviderError):
    """HTTP 429 / 'Too many requests' - back off for long, trip the breaker if it persists."""


class TransientProviderError(ProviderError):
    """Timeout, 5xx, connection reset - retry with exponential back-off."""


class PermanentProviderError(ProviderError):
    """Unknown symbol, no data, delisted - never retry."""
