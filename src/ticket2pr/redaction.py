"""Keep known secret values out of everything ticket2pr publishes.

The sandbox is the primary control; this is the last line: before a commit is pushed or a
text (PR body, ticket comment, agent feedback) leaves the worker, any configured secret value
in it is either refused (commits) or masked (text).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from contextlib import suppress
from urllib.parse import urlsplit

from pydantic import SecretStr

from ticket2pr.config import (
    CLAUDE_CREDENTIAL_ENV_VARS,
    OTHER_CREDENTIAL_ENV_VARS,
    PROVIDER_CREDENTIAL_ENV_VARS,
    PROXY_ENV_VARS,
    Settings,
)

MASK = "REDACTED"  # no brackets: it lands after _inert and must not form a [link](url)
_MIN_SECRET_LEN = 8  # shorter values would mask ordinary words


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
        extra = (*CLAUDE_CREDENTIAL_ENV_VARS, *OTHER_CREDENTIAL_ENV_VARS)
        values += [os.environ.get(name, "") for name in extra]
        for name in PROVIDER_CREDENTIAL_ENV_VARS:
            value = os.environ.get(name, "")
            values += [value, *(pair.partition("=")[2].strip() for pair in value.split(","))]
        for name in PROXY_ENV_VARS:
            proxy = os.environ.get(name, "")
            with suppress(ValueError):
                url = urlsplit(proxy)
                if url.password or url.username:  # a token can be the user name alone
                    values += [proxy, url.password or "", url.username or ""]
        return cls(values)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
            # A tail cut (test output) can start inside a secret: mask that end of it too.
            for start in range(1, len(secret) - _MIN_SECRET_LEN + 1):
                if text.startswith(secret[start:]):
                    text = MASK + text[len(secret) - start :]
                    break
        return text

    def contains_secret(self, data: str | bytes) -> bool:
        if isinstance(data, bytes):
            return any(secret.encode() in data for secret in self._secrets)
        return any(secret in data for secret in self._secrets)
