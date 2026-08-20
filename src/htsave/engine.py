"""Domain orchestration for deterministic repeated-context delivery decisions."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from .cas import ContentAddressedStore
from .delta import create_unified_delta
from .errors import CorruptObjectError, TransportError
from .hashing import sha256_id
from .models import ContentObject, Decision, DeliveryMode
from .registry import DeliveryEvent, PendingReceipt, Registry
from .tokens import TokenEstimator
from .transport import parse_transport, render_delta, render_full_frame, render_ref


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    delta_min_saved_tokens: int = 128
    delta_min_saved_percent: int = 20
    delta_checkpoint_depth: int = 8
    delta_checkpoint_percent: int = 60

    def __post_init__(self) -> None:
        if (
            isinstance(self.delta_min_saved_tokens, bool)
            or not isinstance(self.delta_min_saved_tokens, int)
            or self.delta_min_saved_tokens < 0
        ):
            raise ValueError("delta_min_saved_tokens must be non-negative")
        if (
            isinstance(self.delta_min_saved_percent, bool)
            or not isinstance(self.delta_min_saved_percent, int)
            or not 0 <= self.delta_min_saved_percent <= 100
        ):
            raise ValueError("delta_min_saved_percent must be between 0 and 100")
        if (
            isinstance(self.delta_checkpoint_depth, bool)
            or not isinstance(self.delta_checkpoint_depth, int)
            or self.delta_checkpoint_depth < 1
        ):
            raise ValueError("delta_checkpoint_depth must be positive")
        if (
            isinstance(self.delta_checkpoint_percent, bool)
            or not isinstance(self.delta_checkpoint_percent, int)
            or not 0 <= self.delta_checkpoint_percent <= 100
        ):
            raise ValueError("delta_checkpoint_percent must be between 0 and 100")


class ContextEngine:
    """Own the CAS/receipt invariants independently from any Codex wire format."""

    def __init__(
        self,
        cas: ContentAddressedStore,
        registry: Registry,
        estimator: TokenEstimator,
        policy: DecisionPolicy | None = None,
    ) -> None:
        self.cas = cas
        self.registry = registry
        self.estimator = estimator
        self.policy = policy or DecisionPolicy()

    def start_generation(self, source: str) -> int:
        return self.registry.begin_generation(source)

    def confirm_pending(self, generation: int | None = None) -> int:
        return self.registry.confirm_pending(generation)

    def set_subagent_active(self, active: bool) -> int:
        return self.registry.set_subagent_active(active)

    def decide(
        self,
        *,
        text: str,
        source_fingerprint: str,
        safe_label: str,
        tool_use_id: str,
        allow_transform: bool = True,
        bypass_reason: str | None = None,
        force_full: bool = False,
    ) -> Decision:
        if not source_fingerprint:
            raise ValueError("source_fingerprint must not be empty")
        if not tool_use_id:
            raise ValueError("tool_use_id must not be empty")

        started = perf_counter()
        target_bytes = text.encode("utf-8")
        target_hash = self.cas.put(target_bytes)
        if target_hash != sha256_id(target_bytes):  # defensive CAS contract check
            raise CorruptObjectError("CAS returned an incorrect target hash")

        estimate = self.estimator.estimate(text)
        original_tokens = estimate.count
        self.registry.record_object(
            ContentObject(
                object_hash=target_hash,
                codec="identity-utf8",
                byte_size=len(target_bytes),
                estimated_tokens=original_tokens,
            )
        )
        generation = self.registry.ensure_generation()

        existing = self.registry.delivery_event(generation, tool_use_id)
        if existing is not None:
            return self._replay_existing(
                existing=existing,
                text=text,
                source_fingerprint=source_fingerprint,
                target_hash=target_hash,
                target_bytes=len(target_bytes),
            )

        if not self.estimator.available:
            allow_transform = False
            bypass_reason = bypass_reason or "tiktoken-unavailable"
        if self.registry.active_is_ambiguous():
            allow_transform = False
            bypass_reason = bypass_reason or "ambiguous-subagent-consumer"
        if self.registry.active_is_frozen():
            allow_transform = False
            bypass_reason = bypass_reason or "generation-frozen-for-compaction"
        if not allow_transform:
            return self._record(
                generation=generation,
                tool_use_id=tool_use_id,
                source_fingerprint=source_fingerprint,
                safe_label=safe_label,
                target_hash=target_hash,
                mode=DeliveryMode.BYPASS,
                payload=text,
                original_tokens=original_tokens,
                started=started,
                reason=bypass_reason or "adapter-bypass",
            )

        if force_full:
            return self._record_full(
                generation=generation,
                tool_use_id=tool_use_id,
                source_fingerprint=source_fingerprint,
                safe_label=safe_label,
                target_hash=target_hash,
                text=text,
                original_tokens=original_tokens,
                started=started,
                reason="forced-full",
            )

        pending_transformed = any(
            receipt.mode in {DeliveryMode.REF, DeliveryMode.DELTA}
            for receipt in self.registry.pending_receipts(generation)
        )
        if pending_transformed:
            return self._record_full(
                generation=generation,
                tool_use_id=tool_use_id,
                source_fingerprint=source_fingerprint,
                safe_label=safe_label,
                target_hash=target_hash,
                text=text,
                original_tokens=original_tokens,
                started=started,
                reason="unconfirmed-transformed-delivery",
            )

        confirmed_target = self.registry.confirmed_object_head(generation, target_hash)
        if confirmed_target is not None:
            ref_payload = render_ref(target_hash, len(target_bytes))
            ref_tokens = self.estimator.estimate(ref_payload).count
            if ref_tokens < original_tokens:
                return self._record(
                    generation=generation,
                    tool_use_id=tool_use_id,
                    source_fingerprint=source_fingerprint,
                    safe_label=safe_label,
                    target_hash=target_hash,
                    mode=DeliveryMode.REF,
                    payload=ref_payload,
                    original_tokens=original_tokens,
                    started=started,
                    delta_depth=confirmed_target.delta_depth,
                    cumulative_delta_tokens=confirmed_target.cumulative_delta_tokens,
                )

        head = self.registry.confirmed_head(generation, source_fingerprint)
        if head is not None and head.object_hash != target_hash:
            base_bytes = self.cas.get(head.object_hash)
            try:
                base = base_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CorruptObjectError("confirmed UTF-8 base is not UTF-8") from exc
            delta = create_unified_delta(base, text)
            delta_payload = render_delta(delta, len(target_bytes))
            delta_tokens = self.estimator.estimate(delta_payload).count
            saved_tokens = original_tokens - delta_tokens
            saves_enough = (
                saved_tokens >= self.policy.delta_min_saved_tokens
                and saved_tokens * 100 >= original_tokens * self.policy.delta_min_saved_percent
            )
            next_depth = head.delta_depth + 1
            cumulative = head.cumulative_delta_tokens + delta_tokens
            needs_depth_checkpoint = next_depth >= self.policy.delta_checkpoint_depth
            needs_size_checkpoint = (
                cumulative * 100 > original_tokens * self.policy.delta_checkpoint_percent
            )
            if saves_enough and not needs_depth_checkpoint and not needs_size_checkpoint:
                return self._record(
                    generation=generation,
                    tool_use_id=tool_use_id,
                    source_fingerprint=source_fingerprint,
                    safe_label=safe_label,
                    target_hash=target_hash,
                    base_hash=head.object_hash,
                    mode=DeliveryMode.DELTA,
                    payload=delta_payload,
                    original_tokens=original_tokens,
                    started=started,
                    delta_depth=next_depth,
                    cumulative_delta_tokens=cumulative,
                )
            if not saves_enough:
                reason = "delta-savings-threshold"
            elif needs_depth_checkpoint:
                reason = "delta-depth-checkpoint"
            elif needs_size_checkpoint:
                reason = "delta-size-checkpoint"
            else:  # pragma: no cover - guarded by the DELTA return above
                raise AssertionError("eligible delta unexpectedly fell through")
            return self._record_full(
                generation=generation,
                tool_use_id=tool_use_id,
                source_fingerprint=source_fingerprint,
                safe_label=safe_label,
                target_hash=target_hash,
                text=text,
                original_tokens=original_tokens,
                started=started,
                reason=reason,
            )

        return self._record_full(
            generation=generation,
            tool_use_id=tool_use_id,
            source_fingerprint=source_fingerprint,
            safe_label=safe_label,
            target_hash=target_hash,
            text=text,
            original_tokens=original_tokens,
            started=started,
            reason="no-confirmed-base",
        )

    def _record_full(
        self,
        *,
        generation: int,
        tool_use_id: str,
        source_fingerprint: str,
        safe_label: str,
        target_hash: str,
        text: str,
        original_tokens: int,
        started: float,
        reason: str,
    ) -> Decision:
        return self._record(
            generation=generation,
            tool_use_id=tool_use_id,
            source_fingerprint=source_fingerprint,
            safe_label=safe_label,
            target_hash=target_hash,
            mode=DeliveryMode.FULL,
            payload=text,
            original_tokens=original_tokens,
            started=started,
            reason=reason,
        )

    def _record(
        self,
        *,
        generation: int,
        tool_use_id: str,
        source_fingerprint: str,
        safe_label: str,
        target_hash: str,
        mode: DeliveryMode,
        payload: str,
        original_tokens: int,
        started: float,
        base_hash: str | None = None,
        reason: str | None = None,
        delta_depth: int = 0,
        cumulative_delta_tokens: int = 0,
    ) -> Decision:
        emitted_tokens = self.estimator.estimate(payload).count
        self.registry.record_delivery(
            generation=generation,
            tool_use_id=tool_use_id,
            source_fingerprint=source_fingerprint,
            safe_label=safe_label,
            target_hash=target_hash,
            base_hash=base_hash,
            mode=mode,
            original_tokens=original_tokens,
            emitted_tokens=emitted_tokens,
            latency_ms=max(0.0, (perf_counter() - started) * 1000),
            delta_depth=delta_depth,
            cumulative_delta_tokens=cumulative_delta_tokens,
            bypass_reason=reason,
        )
        return Decision(
            mode=mode,
            target_hash=target_hash,
            payload=payload,
            original_tokens=original_tokens,
            emitted_tokens=emitted_tokens,
            base_hash=base_hash,
            reason=reason,
            delta_depth=delta_depth,
            cumulative_delta_tokens=cumulative_delta_tokens,
        )

    def _replay_existing(
        self,
        *,
        existing: DeliveryEvent,
        text: str,
        source_fingerprint: str,
        target_hash: str,
        target_bytes: int,
    ) -> Decision:
        if existing.source_fingerprint != source_fingerprint or existing.target_hash != target_hash:
            raise CorruptObjectError("tool-use id was reused for different content")
        if existing.mode in {DeliveryMode.FULL, DeliveryMode.BYPASS}:
            payload = text
        elif existing.mode is DeliveryMode.REF:
            payload = render_ref(target_hash, target_bytes)
        elif existing.mode is DeliveryMode.DELTA:
            if existing.base_hash is None:
                raise CorruptObjectError("recorded delta has no base hash")
            try:
                base = self.cas.get(existing.base_hash).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CorruptObjectError("recorded delta base is not UTF-8") from exc
            payload = render_delta(create_unified_delta(base, text), target_bytes)
        else:  # pragma: no cover - exhaustive enum guard
            raise CorruptObjectError("recorded delivery has an unknown mode")
        if self.estimator.estimate(payload).count != existing.emitted_tokens:
            raise CorruptObjectError("replayed delivery token count changed")
        return Decision(
            mode=existing.mode,
            target_hash=existing.target_hash,
            payload=payload,
            original_tokens=existing.original_tokens,
            emitted_tokens=existing.emitted_tokens,
            base_hash=existing.base_hash,
            reason=existing.bypass_reason,
            delta_depth=existing.delta_depth,
            cumulative_delta_tokens=existing.cumulative_delta_tokens,
        )

    def hydrate_bytes(self, object_hash: str) -> bytes:
        metadata = self.registry.object_metadata(object_hash)
        if metadata is None:
            raise CorruptObjectError("object is not registered in this Codex session")
        content = self.cas.get(object_hash)
        if int(metadata["byte_size"]) != len(content):
            raise CorruptObjectError("registered object byte size does not match CAS")
        if str(metadata["codec"]) != "identity-utf8":
            raise TransportError("registered object codec is not supported")
        return content

    def hydrate_text(self, object_hash: str) -> str:
        try:
            return self.hydrate_bytes(object_hash).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptObjectError("registered UTF-8 object is not UTF-8") from exc

    def hydrate_transport(self, value: str | bytes) -> str:
        frame = parse_transport(value)
        target = self.hydrate_bytes(frame.target_hash)
        if len(target) != frame.target_bytes:
            raise CorruptObjectError("transport target byte count does not match CAS")
        if frame.mode is DeliveryMode.FULL:
            if frame.payload != target:
                raise CorruptObjectError("FULL transport disagrees with CAS")
        elif frame.mode is DeliveryMode.DELTA:
            if frame.base_hash is None:
                raise TransportError("DELTA transport has no base")
            base = self.hydrate_text(frame.base_hash)
            reconstructed = frame.delta().apply(base).encode("utf-8")
            if reconstructed != target:
                raise CorruptObjectError("DELTA reconstruction disagrees with CAS")
        try:
            return target.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptObjectError("transport target is not UTF-8") from exc

    def pending_transformed(self) -> tuple[PendingReceipt, ...]:
        return tuple(
            receipt
            for receipt in self.registry.pending_receipts()
            if receipt.mode in {DeliveryMode.REF, DeliveryMode.DELTA}
        )

    def pending_recovery_context(self) -> tuple[str, ...]:
        return tuple(
            render_full_frame(self.hydrate_bytes(receipt.object_hash))
            for receipt in self.pending_transformed()
        )
