import pytest

from app.settings import env_flag


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
def test_truthy(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert env_flag("FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", " OFF", ""])
def test_falsy(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert env_flag("FLAG", default=True) is False


def test_garbage_raises_with_name(monkeypatch):
    monkeypatch.setenv("FEATURE_X", "maybe")
    with pytest.raises(ValueError, match="FEATURE_X"):
        env_flag("FEATURE_X")
