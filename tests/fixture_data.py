"""Read-only access to the captured v2.15.0 fixtures, plus the ids they contain."""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "api-v2.15.0"

KANADE_MBID = "11111111-1111-4111-8111-111111111111"
OTHER_MBID = "22222222-2222-4222-8222-222222222222"
CONFLICT_MBID_1 = "33333333-3333-4333-8333-333333333333"
CONFLICT_MBID_2 = "44444444-4444-4444-8444-444444444444"
LONELY_MBID = "55555555-5555-4555-8555-555555555555"

OTONOSE_ID = "a8a4098f-d8fd-5921-82d5-87baa6aefb57"
KANADE_ID = "eae66d5e-0631-5537-9af9-2e92c28e90bc"
OTHER_ID = "fec642d4-ffa6-588f-ba31-5af997e2680a"
LONELY_ID = "acc3eea1-0dda-5287-8e77-a9043b76ffaf"
SOME_BAND_ID = "7b29edce-65d4-5f8a-bf7e-0c9df11f39a6"
CONFLICT_ID = "fde1f8f7-391d-5eea-8576-88eafeb24475"


@cache
def _read(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def record(name: str) -> dict[str, Any]:
    """One captured request/response file (deep copy, safe to mutate)."""
    return copy.deepcopy(_read(name))


def body(name: str) -> Any:
    return record(name)["body"]


def snapshot() -> dict[str, dict[str, Any]]:
    """Every GET of the split library, keyed by path + query relative to /api/v1."""
    return record("library_snapshot")["responses"]
