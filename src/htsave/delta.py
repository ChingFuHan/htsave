"""A structured, byte-exact unified diff transport."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from .errors import DeltaError
from .hashing import sha256_id, verify_sha256

DeltaOp = Literal[" ", "-", "+"]
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DeltaLine:
    op: DeltaOp
    text: str


@dataclass(frozen=True, slots=True)
class DeltaHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[DeltaLine, ...]


@dataclass(frozen=True, slots=True)
class UnifiedDelta:
    base_hash: str
    target_hash: str
    hunks: tuple[DeltaHunk, ...]
    format: str = "unified-diff-v1"

    def serialize(self) -> str:
        document = {
            "format": self.format,
            "base": self.base_hash,
            "target": self.target_hash,
            "hunks": [
                {
                    "old_start": hunk.old_start,
                    "old_count": hunk.old_count,
                    "new_start": hunk.new_start,
                    "new_count": hunk.new_count,
                    "lines": [[line.op, line.text] for line in hunk.lines],
                }
                for hunk in self.hunks
            ],
        }
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def parse(cls, payload: str) -> UnifiedDelta:
        try:
            document = json.loads(payload)
            if not isinstance(document, dict):
                raise DeltaError("delta document must be an object")
            if document["format"] != "unified-diff-v1":
                raise DeltaError("unsupported delta format")
            base_hash = document["base"]
            target_hash = document["target"]
            if (
                not isinstance(base_hash, str)
                or not _HASH.fullmatch(base_hash)
                or not isinstance(target_hash, str)
                or not _HASH.fullmatch(target_hash)
            ):
                raise DeltaError("delta hashes must be SHA-256 object ids")
            raw_hunks = document["hunks"]
            if not isinstance(raw_hunks, list):
                raise DeltaError("delta hunks must be an array")
            hunks = []
            for raw_hunk in raw_hunks:
                if not isinstance(raw_hunk, dict):
                    raise DeltaError("delta hunk must be an object")
                raw_lines = raw_hunk["lines"]
                if not isinstance(raw_lines, list):
                    raise DeltaError("delta hunk lines must be an array")
                parsed_lines: list[DeltaLine] = []
                for raw_line in raw_lines:
                    if (
                        not isinstance(raw_line, list)
                        or len(raw_line) != 2
                        or not isinstance(raw_line[0], str)
                        or not isinstance(raw_line[1], str)
                    ):
                        raise DeltaError("delta hunk line must be [operation, text]")
                    parsed_lines.append(DeltaLine(op=raw_line[0], text=raw_line[1]))
                lines = tuple(parsed_lines)
                if any(line.op not in {" ", "-", "+"} for line in lines):
                    raise DeltaError("invalid delta operation")
                integer_fields = ("old_start", "old_count", "new_start", "new_count")
                counts: dict[str, int] = {}
                for field in integer_fields:
                    value = raw_hunk[field]
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise DeltaError(f"delta {field} must be a non-negative integer")
                    counts[field] = value
                hunks.append(
                    DeltaHunk(
                        old_start=counts["old_start"],
                        old_count=counts["old_count"],
                        new_start=counts["new_start"],
                        new_count=counts["new_count"],
                        lines=lines,
                    )
                )
            return cls(
                base_hash=base_hash,
                target_hash=target_hash,
                hunks=tuple(hunks),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, DeltaError):
                raise
            raise DeltaError("malformed unified-diff-v1 payload") from exc

    def apply(self, base: str) -> str:
        verify_sha256(base.encode("utf-8"), self.base_hash)
        base_lines = base.splitlines(keepends=True)
        output: list[str] = []
        cursor = 0

        for hunk in self.hunks:
            if hunk.old_start < cursor or hunk.old_start > len(base_lines):
                raise DeltaError("overlapping or out-of-range delta hunk")
            output.extend(base_lines[cursor : hunk.old_start])
            if hunk.new_start != len(output):
                raise DeltaError("delta hunk new_start does not match reconstructed output")
            old_cursor = hunk.old_start
            produced = 0
            consumed = 0

            for line in hunk.lines:
                if line.op in {" ", "-"}:
                    if old_cursor >= len(base_lines) or base_lines[old_cursor] != line.text:
                        raise DeltaError("delta context does not match base")
                    old_cursor += 1
                    consumed += 1
                if line.op in {" ", "+"}:
                    output.append(line.text)
                    produced += 1

            if consumed != hunk.old_count or produced != hunk.new_count:
                raise DeltaError("delta hunk counts do not match payload")
            cursor = old_cursor

        output.extend(base_lines[cursor:])
        target = "".join(output)
        verify_sha256(target.encode("utf-8"), self.target_hash)
        return target


def create_unified_delta(base: str, target: str, context_lines: int = 3) -> UnifiedDelta:
    base_lines = base.splitlines(keepends=True)
    target_lines = target.splitlines(keepends=True)
    matcher = SequenceMatcher(None, base_lines, target_lines, autojunk=False)
    hunks: list[DeltaHunk] = []

    for group in matcher.get_grouped_opcodes(context_lines):
        old_start = group[0][1]
        old_end = group[-1][2]
        new_start = group[0][3]
        new_end = group[-1][4]
        lines: list[DeltaLine] = []

        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend(DeltaLine(" ", line) for line in base_lines[i1:i2])
            elif tag == "delete":
                lines.extend(DeltaLine("-", line) for line in base_lines[i1:i2])
            elif tag == "insert":
                lines.extend(DeltaLine("+", line) for line in target_lines[j1:j2])
            elif tag == "replace":
                lines.extend(DeltaLine("-", line) for line in base_lines[i1:i2])
                lines.extend(DeltaLine("+", line) for line in target_lines[j1:j2])
            else:  # pragma: no cover - SequenceMatcher contract
                raise DeltaError(f"unsupported SequenceMatcher opcode: {tag}")

        hunks.append(
            DeltaHunk(
                old_start=old_start,
                old_count=old_end - old_start,
                new_start=new_start,
                new_count=new_end - new_start,
                lines=tuple(lines),
            )
        )

    delta = UnifiedDelta(
        base_hash=sha256_id(base.encode("utf-8")),
        target_hash=sha256_id(target.encode("utf-8")),
        hunks=tuple(hunks),
    )
    if delta.apply(base) != target:  # Defensive proof at the creation boundary.
        raise DeltaError("generated delta did not recreate target")
    return delta
