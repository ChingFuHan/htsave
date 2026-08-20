from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htsave.delta import UnifiedDelta, create_unified_delta
from htsave.errors import CorruptObjectError, DeltaError

SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=400,
)


@given(base=SAFE_TEXT, target=SAFE_TEXT)
def test_unified_delta_round_trips_exact_utf8(base: str, target: str) -> None:
    delta = create_unified_delta(base, target)
    parsed = UnifiedDelta.parse(delta.serialize())

    assert parsed.apply(base) == target


def test_unified_delta_preserves_line_endings_and_missing_final_newline() -> None:
    base = "alpha\r\nbeta\nlast"
    target = "alpha\r\nchanged\r\nlast"

    delta = create_unified_delta(base, target)

    assert UnifiedDelta.parse(delta.serialize()).apply(base) == target


def test_unified_delta_rejects_wrong_base() -> None:
    delta = create_unified_delta("base\n", "target\n")

    try:
        delta.apply("other\n")
    except (CorruptObjectError, DeltaError):
        pass
    else:  # pragma: no cover - assertion helper
        raise AssertionError("wrong base was accepted")


def test_unified_delta_parser_rejects_noncanonical_hashes_and_counts() -> None:
    with pytest.raises(DeltaError, match="hashes"):
        UnifiedDelta.parse(
            '{"format":"unified-diff-v1","base":"base","target":"target","hunks":[]}'
        )

    valid_base = "sha256:" + "0" * 64
    with pytest.raises(DeltaError, match="non-negative integer"):
        UnifiedDelta.parse(
            json.dumps(
                {
                    "format": "unified-diff-v1",
                    "base": valid_base,
                    "target": valid_base,
                    "hunks": [
                        {
                            "old_start": True,
                            "old_count": 0,
                            "new_start": 0,
                            "new_count": 0,
                            "lines": [],
                        }
                    ],
                }
            )
        )
