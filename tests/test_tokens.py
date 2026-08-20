from __future__ import annotations

from htsave.tokens import TokenEstimator


def test_tiktoken_is_available_in_supported_install() -> None:
    estimator = TokenEstimator("gpt-5")

    assert estimator.available
    assert estimator.estimate("hello world").count > 0
    assert estimator.estimate("hello world").backend.startswith("tiktoken:")
