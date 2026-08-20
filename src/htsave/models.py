"""Shared domain types. Adapters translate into these types at their boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeliveryMode(StrEnum):
    FULL = "full"
    REF = "ref"
    DELTA = "delta"
    BYPASS = "bypass"


class ReceiptState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class ContentObject:
    object_hash: str
    codec: str
    byte_size: int
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class Decision:
    mode: DeliveryMode
    target_hash: str
    payload: str
    original_tokens: int
    emitted_tokens: int
    base_hash: str | None = None
    reason: str | None = None
    delta_depth: int = 0
    cumulative_delta_tokens: int = 0

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.emitted_tokens)
