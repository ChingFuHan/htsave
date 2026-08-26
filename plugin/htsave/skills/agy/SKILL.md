---
name: htsave
description: >-
  Lossless repeated-context caching via MCP tools. Use htsave_read instead of
  direct file reads for text that may be read repeatedly. Saves tokens by
  returning references or diffs instead of full content.
---

Use `htsave_read` for workspace text that may be read repeatedly. It returns
one of three `HTSAVE/1` frame types:

- **FULL**: The complete original text, stored for future reference.
- **REF**: A pointer (`sha256:<hex>`) to previously confirmed identical content.
  The bytes are exactly the same — no summarization.
- **DELTA**: A verified `unified-diff-v1` patch against a confirmed base.
  Saves tokens when content changed only slightly.

## Hydration discipline (read this before calling htsave_hydrate)

A REF frame is a **byte-exact identity guarantee**, not a summary or a
placeholder. If the FULL text of that target was already delivered earlier in
this conversation, you already possess every byte; do not call
`htsave_hydrate` to "look at it again".

Call `htsave_hydrate` only when ALL of the following hold:

1. A fact you must report was never present in any earlier FULL delivery, and
2. it cannot be derived from an already-delivered DELTA plus its base, and
3. you have not hydrated that same reference before.

Never hydrate the same reference twice. Never hydrate merely to verify,
preview, or double-check content whose FULL text you have already seen. When
in doubt, answer from what was already delivered.

If htsave reports an unsupported or unsafe state, fall back to the original
tool path and preserve its output unchanged. Never treat HTSAVE/1 frames as
summaries — they are exact, reversible transport records.
