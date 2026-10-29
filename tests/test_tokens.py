from __future__ import annotations

from htsave.tokens import TokenEstimator, tiktoken_cache_dir


def test_tiktoken_is_available_in_supported_install() -> None:
    estimator = TokenEstimator("gpt-5")

    assert estimator.available
    assert estimator.estimate("hello world").count > 0
    assert estimator.estimate("hello world").backend.startswith("tiktoken:")


def test_an_encoding_that_cannot_load_is_reported_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import tiktoken

    def offline(*_: object) -> None:
        raise OSError("network unreachable")

    monkeypatch.setattr(tiktoken, "encoding_for_model", offline)
    monkeypatch.setattr(tiktoken, "get_encoding", offline)
    estimator = TokenEstimator("gpt-5")

    assert not estimator.available
    assert estimator.backend == "unavailable"
    assert estimator.estimate("hello world").backend == "utf8-bytes/4"


def test_cache_dir_is_persistent_unless_the_operator_chose_one(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import tempfile

    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)
    assert not tiktoken_cache_dir().startswith(tempfile.gettempdir())

    monkeypatch.setenv("DATA_GYM_CACHE_DIR", "/gym")
    assert tiktoken_cache_dir() == "/gym"
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
    assert tiktoken_cache_dir() == "", "an empty value deliberately disables tiktoken's cache"
