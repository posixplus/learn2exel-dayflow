"""The agent's notion of "today". Found by lesson 84, owed since lesson 81.

The brief asks for "today" but the prompt never said what day it was. Under
the raw loop the model quietly assumed the fixtures' date; the SDK run
assumed the wall clock and declared every fixture stale. Both were guesses.
The fix is to state the date, and to make it injectable so tests and
fixtures are deterministic.

Precedence: DAYFLOW_TODAY env var > fixture Monday under MOCK=1 > wall clock.
"""
from __future__ import annotations

import os
from datetime import date

FIXTURE_TODAY = "2026-09-07"   # the Monday the fixture week starts on


def today() -> str:
    """ISO date (YYYY-MM-DD) the agent should treat as today."""
    forced = os.environ.get("DAYFLOW_TODAY")
    if forced:
        return forced
    if os.environ.get("MOCK", "1") == "1":
        return FIXTURE_TODAY
    return date.today().isoformat()
