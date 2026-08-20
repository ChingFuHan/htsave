---
name: htsave
description: Read and restore repeated workspace context losslessly with htsave.
---

Use `htsave_read` for text that may be read repeatedly. Treat `HTSAVE/1` FULL,
REF, and DELTA frames as exact transport records, never summaries. Call
`htsave_hydrate` whenever a referenced base is unavailable or full bytes are
needed. If htsave reports an unsupported or unsafe state, use the original tool
path and preserve its output unchanged.
