"""Retries with backoff. Lesson 91.

Three decisions, each of which is easy to get wrong:
  what to retry   only failures that might not happen again: a timeout, a 429, a 5xx.
                  Never a 401, a 404 or a bad argument. Retrying those is just slower failing.
  what NOT to     writes. A write that timed out may have happened. Dayflow's writes carry
                  idempotency keys, so a retry would be safe, but that decision belongs to
                  whoever approved the write, not to a loop in dispatch().
  how long        exponential backoff with full jitter, capped, and a hard attempt limit.
                  Without jitter every client that failed together retries together.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Backoff:
    attempts: int = 3          # total tries, including the first
    base: float = 0.5          # seconds; the ceiling for the first wait
    cap: float = 8.0           # no single wait is longer than this

    def wait(self, retry_number: int, rng: Callable[[], float] = random.random) -> float:
        """Full jitter: a uniform draw from 0 to min(cap, base * 2^n). retry_number starts at 0."""
        return rng() * min(self.cap, self.base * (2 ** retry_number))

    def worst_case(self) -> float:
        """The longest this policy can make a caller wait in sleeps alone."""
        return sum(min(self.cap, self.base * (2 ** i)) for i in range(self.attempts - 1))


def call(fn: Callable[[], Any], *, policy: Backoff, retry_if: Callable[[BaseException], bool],
         sleep: Callable[[float], None] = time.sleep, rng: Callable[[], float] = random.random) -> tuple[Any, int]:
    """Run fn until it returns, the error is not retryable, or attempts run out.
    Returns (result, attempts). The last error is re-raised with `.attempts` set."""
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn(), attempt
        except BaseException as e:      # noqa: BLE001 - retry_if decides
            if attempt == policy.attempts or not retry_if(e):
                e.attempts = attempt    # type: ignore[attr-defined]
                raise
            sleep(policy.wait(attempt - 1, rng))
    raise AssertionError("unreachable")
