from __future__ import annotations

from hypothesis import given, strategies as st
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState, CircuitOpenError


@given(st.lists(st.booleans(), min_size=1, max_size=100))
def test_circuit_breaker_properties(results: list[bool]) -> None:
    breaker = CircuitBreaker(
        name="test_fuzz",
        failure_threshold=3,
        reset_timeout_seconds=2.0,
        success_threshold=1,
    )
    
    for success in results:
        state_before = breaker.state
        
        if success:
            if breaker.state == CircuitState.OPEN:
                # Open circuit should reject unless timeout passed
                # In fuzzing, we aren't waiting for timeout, so it should deny
                assert breaker.allow_request() is False
            else:
                assert breaker.allow_request() is True
                breaker.record_success()
                if state_before == CircuitState.HALF_OPEN:
                    assert breaker.state == CircuitState.CLOSED
        else:
            if breaker.state == CircuitState.OPEN:
                assert breaker.allow_request() is False
            else:
                assert breaker.allow_request() is True
                breaker.record_failure()
                if state_before == CircuitState.HALF_OPEN:
                    assert breaker.state == CircuitState.OPEN
                
        # Invariants
        if breaker.state == CircuitState.CLOSED:
            assert breaker.failure_count < 3
        elif breaker.state == CircuitState.OPEN:
            assert breaker.opened_at is not None
        elif breaker.state == CircuitState.HALF_OPEN:
            # We don't transition to HALF_OPEN in this tight loop without time passing
            pass
