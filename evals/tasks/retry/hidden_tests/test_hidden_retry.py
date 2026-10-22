import pytest

from net import retry as module
from net.retry import retry


def test_succeeds_after_failures(monkeypatch):
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    calls = []

    @retry(attempts=3, exceptions=(ConnectionError,), delay=0.5)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3
    assert sleeps == [0.5, 0.5]


def test_reraises_last_exception():
    @retry(attempts=2)
    def always():
        raise TimeoutError("late")

    with pytest.raises(TimeoutError, match="late"):
        always()


def test_unlisted_exception_propagates_immediately():
    calls = []

    @retry(attempts=5, exceptions=(ConnectionError,))
    def boom():
        calls.append(1)
        raise KeyError("x")

    with pytest.raises(KeyError):
        boom()
    assert calls == [1]


def test_invalid_attempts():
    with pytest.raises(ValueError):
        retry(attempts=0)


def test_preserves_metadata():
    @retry()
    def documented():
        """Doc."""

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "Doc."
