---
name: htsave
description: Lossless repeated-context caching for Claude Code. Saves tokens by deduplicating repeated tool outputs.
htsave-managed: true
---

htsave is active and transparently deduplicating repeated tool outputs through
lifecycle hooks. When you see `HTSAVE/1` frames (FULL, REF, or DELTA), treat
them as exact transport records — never summaries.

- **FULL**: The complete original text, stored for future reference.
- **REF**: A pointer to previously confirmed content (sha256:<hex>). The bytes
  are identical to the original.
- **DELTA**: A verified unified-diff-v1 patch against a confirmed base. Apply
  the patch to recover exact bytes.

If you need the full text from a REF or DELTA, call `htsave_hydrate` with the
reference. If htsave reports an unsupported or unsafe state, use the original
tool path and preserve its output unchanged.
