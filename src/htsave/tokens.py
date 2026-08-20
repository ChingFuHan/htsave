"""Model-aware token estimates used for transport decisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    count: int
    backend: str
    exact_encoding: bool


class TokenEstimator:
    """Use tiktoken when installed; expose fallback status instead of hiding it."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or ""
        self._encoding = None
        self.backend = "unavailable"
        self.exact_encoding = False
        try:
            import tiktoken

            try:
                self._encoding = tiktoken.encoding_for_model(self.model)
                self.backend = f"tiktoken:{self._encoding.name}"
                self.exact_encoding = True
            except KeyError:
                self._encoding = tiktoken.get_encoding("o200k_base")
                self.backend = "tiktoken:o200k_base-fallback"
        except ImportError:
            self._encoding = None

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
