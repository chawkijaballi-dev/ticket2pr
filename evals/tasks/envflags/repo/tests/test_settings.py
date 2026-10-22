from app.settings import env_flag


def test_unset_uses_default(monkeypatch):
    monkeypatch.delenv("FLAG", raising=False)
    assert env_flag("FLAG") is False
    assert env_flag("FLAG", default=True) is True
