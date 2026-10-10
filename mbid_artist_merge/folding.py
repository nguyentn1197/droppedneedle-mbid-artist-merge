"""Name folding that mirrors DroppedNeedle's ``library_indexer._fold_value``.

A scanner-created local artist's ``name`` is exactly the tag credit it came from,
so folding both sides the same way is how a tag credit is matched to an artist.
"""

from __future__ import annotations

import unicodedata


def fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.strip())
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return " ".join(without_marks.split())
