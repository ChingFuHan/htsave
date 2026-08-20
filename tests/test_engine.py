from __future__ import annotations

from pathlib import Path

from htsave.cas import ContentAddressedStore
from htsave.engine import ContextEngine, DecisionPolicy
from htsave.models import DeliveryMode
from htsave.paths import build_state_paths
from htsave.registry import Registry
from htsave.tokens import TokenEstimator


def _large_text(marker: str = "base") -> str:
    return "".join(
        f"line {number:04d}: deterministic context payload {marker} value {number}\n"
        for number in range(1200)
    )


def _engine(tmp_path: Path, *, policy: DecisionPolicy | None = None) -> ContextEngine:
    paths = build_state_paths("session-one", tmp_path / "state")
    return ContextEngine(
        ContentAddressedStore(paths.objects),
        Registry(paths.database, paths.session_key),
        TokenEstimator("gpt-5"),
        policy,
    )


def test_exact_ref_requires_confirmed_receipt_in_same_generation(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    text = _large_text()
    try:
        first = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-1",
        )
        unconfirmed_repeat = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-2",
        )

        assert first.mode is DeliveryMode.FULL
        assert unconfirmed_repeat.mode is DeliveryMode.FULL

        engine.confirm_pending()
        repeat = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-3",
        )

        assert repeat.mode is DeliveryMode.REF
        assert repeat.emitted_tokens < repeat.original_tokens
        assert engine.hydrate_transport(repeat.payload) == text

        engine.start_generation("compact")
        after_compaction = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-4",
        )
        assert after_compaction.mode is DeliveryMode.FULL
    finally:
        engine.registry.close()


def test_verified_delta_and_following_ref_hydrate_byte_exact(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    base = _large_text()
    target = base.replace("value 17\n", "value changed-a\n").replace(
        "value 611\n", "value changed-b\n"
    )
    try:
        engine.decide(
            text=base,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-1",
        )
        engine.confirm_pending()

        changed = engine.decide(
            text=target,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-2",
        )
        assert changed.mode is DeliveryMode.DELTA
        assert changed.saved_tokens >= 128
        assert changed.saved_tokens * 100 >= changed.original_tokens * 20
        assert engine.hydrate_transport(changed.payload) == target

        engine.confirm_pending()
        repeat = engine.decide(
            text=target,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-3",
        )
        assert repeat.mode is DeliveryMode.REF
        assert engine.hydrate_transport(repeat.payload).encode() == target.encode()
    finally:
        engine.registry.close()


def test_delta_below_required_savings_sends_full(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    base = "one\ntwo\nthree\n"
    try:
        engine.decide(
            text=base,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-1",
        )
        engine.confirm_pending()
        changed = engine.decide(
            text="one\nchanged\nthree\n",
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-2",
        )

        assert changed.mode is DeliveryMode.FULL
        assert changed.reason == "delta-savings-threshold"
    finally:
        engine.registry.close()


def test_delta_depth_forces_full_checkpoint(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        policy=DecisionPolicy(delta_checkpoint_depth=3),
    )
    base = _large_text()
    texts = [
        base,
        base.replace("value 17\n", "value revision-1\n"),
        base.replace("value 17\n", "value revision-2\n"),
        base.replace("value 17\n", "value revision-3\n"),
    ]
    try:
        decisions = []
        for index, text in enumerate(texts):
            decision = engine.decide(
                text=text,
                source_fingerprint="source-a",
                safe_label="fixture",
                tool_use_id=f"tool-{index}",
            )
            decisions.append(decision)
            engine.confirm_pending()

        assert [decision.mode for decision in decisions] == [
            DeliveryMode.FULL,
            DeliveryMode.DELTA,
            DeliveryMode.DELTA,
            DeliveryMode.FULL,
        ]
        assert decisions[-1].reason == "delta-depth-checkpoint"
        assert decisions[-1].delta_depth == 0
    finally:
        engine.registry.close()


def test_pending_transformed_result_forces_concurrent_full(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    text = _large_text()
    try:
        engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-1",
        )
        engine.confirm_pending()
        transformed = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-2",
        )
        concurrent = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-3",
        )

        assert transformed.mode is DeliveryMode.REF
        assert concurrent.mode is DeliveryMode.FULL
        assert concurrent.reason == "unconfirmed-transformed-delivery"
        recovery = engine.pending_recovery_context()
        assert len(recovery) == 1
        assert engine.hydrate_transport(recovery[0]) == text
    finally:
        engine.registry.close()


def test_ambiguous_subagent_state_is_fail_open_bypass(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        engine.set_subagent_active(True)
        decision = engine.decide(
            text=_large_text(),
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="tool-1",
        )

        assert decision.mode is DeliveryMode.BYPASS
        assert decision.payload == _large_text()
        assert decision.reason == "ambiguous-subagent-consumer"
    finally:
        engine.registry.close()


def test_tool_retry_replays_original_decision(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    text = _large_text()
    try:
        first = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="same-tool",
        )
        engine.confirm_pending()
        replay = engine.decide(
            text=text,
            source_fingerprint="source-a",
            safe_label="fixture",
            tool_use_id="same-tool",
        )

        assert replay == first
        assert engine.registry.stats().events == 1
    finally:
        engine.registry.close()
