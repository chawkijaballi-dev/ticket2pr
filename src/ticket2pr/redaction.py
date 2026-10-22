"""Keep known secret values out of everything ticket2pr publishes.

The sandbox is the primary control; this is the last line: before a commit is pushed or a
text (PR body, ticket comment, agent feedback) leaves the worker, any configured secret value
in it is either refused (commits) or masked (text).
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from pydantic import SecretStr

from ticket2pr.config import Settings

MASK = "[REDACTED]"
_MIN_SECRET_LEN = 8  # shorter values would mask ordinary words
_EXTRA_ENV_SECRETS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


class Redactor:
    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first, so a secret containing another one is masked whole.
        unique = {value for value in secrets if len(value) >= _MIN_SECRET_LEN}
        self._secrets = sorted(unique, key=len, reverse=True)

    @classmethod
    def from_settings(cls, settings: Settings) -> Redactor:
        values = [
            value.get_secret_value()
            for name in type(settings).model_fields
            if isinstance(value := getattr(settings, name), SecretStr)
        ]
        values += [os.environ.get(name, "") for name in _EXTRA_ENV_SECRETS]
        return cls(values)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text

    def contains_secret(self, data: str | bytes) -> bool:
        if isinstance(data, bytes):
            return any(secret.encode() in data for secret in self._secrets)
        return any(secret in data for secret in self._secrets)
