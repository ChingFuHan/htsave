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

## Hydration and context changes

OpenCode exposes these MCP tools as `htsave_htsave_read` and
`htsave_htsave_hydrate`. Use the actual exposed names when calling them.
A FULL delivery can be plain original text without a transport header.

A REF is a byte-exact identity guarantee. Reuse the earlier FULL when its exact
text remains available; apply a DELTA only when its verified base is available.
Call `htsave_htsave_hydrate` whenever a referenced target or base is unavailable,
including after compaction or pruning. A previous hydration does not mean the
bytes are still in context. Never guess missing content from a reference.

If htsave reports an unsupported or unsafe state, use the original tool path.
Treat frames as exact, reversible transport records, never summaries. This
integration is experimental; local correctness checks do not prove savings.
