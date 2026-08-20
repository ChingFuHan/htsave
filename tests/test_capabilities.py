from __future__ import annotations

from pathlib import Path

import pytest

from htsave.capabilities import (
    canonical_arguments_hash,
    consume_session_capability,
    issue_session_capability,
    session_key_from_token,
    token_from_context,
)
from htsave.errors import SecurityBoundaryError
from htsave.paths import build_state_paths
from htsave.registry import Registry

TOOL_NAME = "mcp__htsave__htsave_read"


def _issue(tmp_path: Path, *, now: float = 100.0, ttl: float = 60.0) -> tuple[str, Path]:
    state_root = tmp_path / "state"
    paths = build_state_paths("session-one", state_root)
    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("startup")
        token = issue_session_capability(
            registry,
            turn_id="turn-1",
            tool_use_id="tool-1",
            tool_name=TOOL_NAME,
            arguments={"path": "README.md", "start_line": 1},
            model="gpt-5",
            cwd=str(tmp_path),
            now=now,
            ttl_seconds=ttl,
        )
    return token, state_root


def test_argument_digest_is_canonical_and_ignores_internal_metadata() -> None:
    first = {"path": "雪.md", "range": {"end": 3, "start": 1}}
    second = {
        "range": {"start": 1, "end": 3},
        "_htsave_context": {"token": "ignored"},
        "path": "雪.md",
    }

    assert canonical_arguments_hash(first) == canonical_arguments_hash(second)


def test_capability_is_scoped_and_consumed_exactly_once(tmp_path: Path) -> None:
    token, state_root = _issue(tmp_path)
    arguments = {
        "start_line": 1,
        "path": "README.md",
        "_htsave_context": {"token": token},
    }

    with consume_session_capability(
        {"token": token},
        tool_name=TOOL_NAME,
        arguments=arguments,
        state_root=state_root,
        now=120.0,
    ) as session:
        assert session.record.tool_use_id == "tool-1"
        assert session.record.cwd == str(tmp_path)
        assert session.paths.session_key == session_key_from_token(token)

    with (
        pytest.raises(SecurityBoundaryError, match="already used"),
        consume_session_capability(
            {"token": token},
            tool_name=TOOL_NAME,
            arguments=arguments,
            state_root=state_root,
            now=121.0,
        ),
    ):
        pass


def test_scope_mismatch_does_not_consume_capability(tmp_path: Path) -> None:
    token, state_root = _issue(tmp_path)
    with (
        pytest.raises(SecurityBoundaryError, match="argument mismatch"),
        consume_session_capability(
            {"token": token},
            tool_name=TOOL_NAME,
            arguments={"path": "different.md", "start_line": 1},
            state_root=state_root,
            now=110.0,
        ),
    ):
        pass

    with consume_session_capability(
        {"token": token},
        tool_name=TOOL_NAME,
        arguments={"path": "README.md", "start_line": 1},
        state_root=state_root,
        now=111.0,
    ):
        pass


def test_expired_or_old_generation_capability_is_rejected(tmp_path: Path) -> None:
    expired, state_root = _issue(tmp_path, ttl=1.0)
    with (
        pytest.raises(SecurityBoundaryError, match="expired"),
        consume_session_capability(
            {"token": expired},
            tool_name=TOOL_NAME,
            arguments={"path": "README.md", "start_line": 1},
            state_root=state_root,
            now=102.0,
        ),
    ):
        pass

    paths = build_state_paths("session-one", state_root)
    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("resume")
    with (
        pytest.raises(SecurityBoundaryError, match="generation"),
        consume_session_capability(
            {"token": expired},
            tool_name=TOOL_NAME,
            arguments={"path": "README.md", "start_line": 1},
            state_root=state_root,
            now=100.5,
        ),
    ):
        pass


def test_capability_issue_retry_returns_same_token(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    paths = build_state_paths("session-one", state_root)
    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("startup")
        kwargs = {
            "turn_id": "turn-1",
            "tool_use_id": "tool-1",
            "tool_name": TOOL_NAME,
            "arguments": {"path": "README.md"},
            "model": "gpt-5",
            "cwd": str(tmp_path),
            "now": 100.0,
        }
        first = issue_session_capability(registry, **kwargs)
        second = issue_session_capability(registry, **kwargs)

    assert second == first


@pytest.mark.parametrize(
    "context",
    [None, "token", {}, {"token": 1}, {"token": "v1.invalid.secret"}, {"token": "x", "extra": 1}],
)
def test_malformed_context_is_rejected(context: object) -> None:
    with pytest.raises(SecurityBoundaryError):
        token_from_context(context)
