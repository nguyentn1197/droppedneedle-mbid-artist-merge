from __future__ import annotations

import pytest

from mbid_artist_merge.folding import fold


def test_full_width_nfkd_and_casefold() -> None:
    assert fold("Ｏｔｏｎｏｓｅ　Ｋａｎａｄｅ") == "otonose kanade"


def test_combining_marks_are_dropped() -> None:
    assert fold("Beyoncé") == "beyonce"
    assert fold("Beyoncé") == "beyonce"


@pytest.mark.parametrize(
    "raw",
    ["  Some Band  ", "Some\tBand", "Some \n  Band", "SOME BAND", "some　band"],
)
def test_whitespace_collapses(raw: str) -> None:
    assert fold(raw) == "some band"


def test_japanese_unchanged_except_casefold() -> None:
    assert fold("音乃瀬奏") == "音乃瀬奏"
    assert fold(" 音乃瀬奏 ") == "音乃瀬奏"


def test_kana_voicing_marks_fold_like_upstream() -> None:
    # NFKD splits ガ into カ + U+3099 (a combining mark), which upstream drops too.
    assert fold("ガ") == "カ"


def test_tag_credit_and_artist_name_fold_equal() -> None:
    assert fold("Otonose Kanade") == fold("otonose  kanade")
    assert fold("") == ""
