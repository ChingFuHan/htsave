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

Call `htsave_hydrate` whenever you need full bytes from a REF or DELTA.

If htsave reports an unsupported or unsafe state, fall back to the original
tool path and preserve its output unchanged. Never treat HTSAVE/1 frames as
summaries — they are exact, reversible transport records.
