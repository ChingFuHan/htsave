"""Model-aware token estimates used for transport decisions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    count: int
    backend: str
    exact_encoding: bool


def tiktoken_cache_dir() -> str:
    """Where tiktoken keeps its encoding files, defaulting to a persistent user cache.

    tiktoken downloads each encoding on first use and otherwise caches it in the
    system temp directory, which a reboot clears, so the next hook would go back
    to the network.  An operator's own ``TIKTOKEN_CACHE_DIR`` or
    ``DATA_GYM_CACHE_DIR`` always wins.
    """

    for name in ("TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR"):
        if name in os.environ:
            return os.environ[name]
    return str(Path(user_cache_path("htsave", appauthor=False)) / "tiktoken")


class TokenEstimator:
    """Use tiktoken when it can load; expose fallback status instead of hiding it."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or ""
        self._encoding = None
        self.backend = "unavailable"
        self.exact_encoding = False
        try:
            import tiktoken
        except ImportError:
            return
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", tiktoken_cache_dir())
        try:
            try:
                self._encoding = tiktoken.encoding_for_model(self.model)
                self.backend = f"tiktoken:{self._encoding.name}"
                self.exact_encoding = True
            except KeyError:
                self._encoding = tiktoken.get_encoding("o200k_base")
                self.backend = "tiktoken:o200k_base-fallback"
        except Exception:
            # An uncached encoding needs the network; offline it is unavailable,
            # and the decision engine bypasses transformations.
            self._encoding = None
            self.backend = "unavailable"
            self.exact_encoding = False

    @property
    def available(self) -> bool:
        return self._encoding is not None

    def estimate(self, text: str) -> TokenEstimate:
        if self._encoding is not None:
            return TokenEstimate(
                count=len(self._encoding.encode(text)),
                backend=self.backend,
                exact_encoding=self.exact_encoding,
            )
        # Reporting only. The decision engine must bypass transformations when
        # tiktoken is unavailable.
        count = max(1, (len(text.encode("utf-8")) + 3) // 4)
        return TokenEstimate(count=count, backend="utf8-bytes/4", exact_encoding=False)
