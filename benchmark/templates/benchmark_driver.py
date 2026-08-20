#!/usr/bin/env python3
"""Deterministic fixture output driver used inside isolated benchmark repos."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8"))


def emit() -> None:
    paths = CONFIG["emit_paths"]
    include_headers = CONFIG.get("include_headers", False)
    chunks: list[bytes] = []
    for relative in paths:
        if include_headers:
            chunks.append(f"--- {relative} ---\n".encode())
        chunks.append((ROOT / relative).read_bytes())
    sys.stdout.buffer.write(b"".join(chunks))


def mutate() -> None:
    changed = 0
    for mutation in CONFIG.get("mutations", []):
        path = ROOT / mutation["path"]
        content = path.read_text(encoding="utf-8")
        before = mutation["before"]
        after = mutation["after"]
        before_count = content.count(before)
        after_count = content.count(after)
        if before_count == 1 and after_count == 0:
            path.write_text(content.replace(before, after), encoding="utf-8", newline="\n")
            changed += 1
        elif before_count == 0 and after_count == 1:
            continue
        else:
            raise RuntimeError(f"mutation precondition failed for {path}")
    print(f"mutated_lines={changed}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"emit", "mutate"}:
        print("usage: benchmark_driver.py emit|mutate", file=sys.stderr)
        return 2
    if sys.argv[1] == "emit":
        emit()
    else:
        mutate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
