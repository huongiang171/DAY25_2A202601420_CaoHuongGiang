from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Circuit breaker skeleton.

    Implements a production-safe state machine:
    - CLOSED: calls pass through; count failures.
    - OPEN: fail fast until reset timeout elapses.
    - HALF_OPEN: allow a probe; close on success or re-open on failure.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)
    redis_url: str | None = None
    _redis: Any | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.redis_url:
            import redis as redis_lib
            self._redis = redis_lib.Redis.from_url(self.redis_url, decode_responses=True)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted.

        - CLOSED → always allow
        - HALF_OPEN → allow (probe request)
        - OPEN → check if reset_timeout_seconds has elapsed since opened_at
          - If elapsed: transition to HALF_OPEN and allow
          - If not elapsed: deny (return False)
        """
        # If using Redis, check global state first
        if self._redis:
            try:
                global_state = self._redis.get(f"cb:{self.name}:state")
                if global_state == "open":
                    return False
            except Exception:
                pass # Fallback to local state

        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.HALF_OPEN:
            return True
        # OPEN: check if timeout has elapsed
        if self.opened_at is not None:
            elapsed = time.monotonic() - self.opened_at
            if elapsed >= self.reset_timeout_seconds:
                self._transition(CircuitState.HALF_OPEN, "timeout_elapsed")
                return True
        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function through the circuit breaker.

        1. Check allow_request() — if denied, raise CircuitOpenError
        2. Try calling fn(*args, **kwargs)
        3. On success: call record_success() and return the result
        4. On exception: call record_failure() and re-raise
        """
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit '{self.name}' is OPEN — request denied")
        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def record_success(self) -> None:
        """Record a successful call.

        1. Reset failure_count to 0
        2. Increment success_count
        3. If in HALF_OPEN and success_count >= success_threshold:
           - Transition to CLOSED with reason "probe_success"
           - Reset success_count to 0
        """
        self.failure_count = 0
        self.success_count += 1
        
        if self._redis:
            try:
                self._redis.delete(f"cb:{self.name}:failures")
                self._redis.delete(f"cb:{self.name}:state")
            except Exception:
                pass

        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0

    def record_failure(self) -> None:
        """Record a failed call.

        1. Increment failure_count, reset success_count to 0
        2. If in HALF_OPEN state:
           - Immediately transition to OPEN with reason "probe_failure"
           - Set opened_at = time.monotonic()
        3. Else if failure_count >= failure_threshold:
           - Transition to OPEN with reason "failure_threshold_reached"
           - Set opened_at = time.monotonic()

        IMPORTANT: HALF_OPEN and threshold cases need DIFFERENT reasons
        and must be handled separately (if/elif, not combined with or).
        """
        self.failure_count += 1
        self.success_count = 0
        
        # Redis distributed state sharing
        if self._redis:
            try:
                fail_key = f"cb:{self.name}:failures"
                state_key = f"cb:{self.name}:state"
                count = self._redis.incr(fail_key)
                self._redis.expire(fail_key, int(self.reset_timeout_seconds) or 1)
                
                if self.state == CircuitState.HALF_OPEN:
                    self._redis.set(state_key, "open", ex=int(self.reset_timeout_seconds) or 1)
                elif count >= self.failure_threshold:
                    self._redis.set(state_key, "open", ex=int(self.reset_timeout_seconds) or 1)
            except Exception:
                pass
                
        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN, "probe_failure")
            self.opened_at = time.monotonic()
        elif self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold_reached")
            self.opened_at = time.monotonic()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state
