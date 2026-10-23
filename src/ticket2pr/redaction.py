"""Keep known secret values out of everything ticket2pr publishes.

The sandbox is the primary control; this is the last line: before a commit is pushed or a
text (PR body, ticket comment, agent feedback) leaves the worker, any configured secret value
in it is either refused (commits) or masked (text).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from contextlib import suppress
from urllib.parse import unquote, urlsplit

from pydantic import SecretStr

from ticket2pr.config import PROXY_ENV_VARS, Settings

MASK = "REDACTED"  # no brackets: it lands after _inert and must not form a [link](url)
_MIN_SECRET_LEN = 8  # shorter values would mask ordinary words
# Environment variables that hold credentials (ANTHROPIC_API_KEY, GH_TOKEN, AWS_SECRET_ACCESS_KEY,
# ANTHROPIC_CUSTOM_HEADERS, OTEL_EXPORTER_OTLP_HEADERS...): the agent may see some of them.
_CREDENTIAL_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE|HEADERS", re.IGNORECASE)
# Header lists: "k=v,k2=v2" (OTLP) or "Name: value" lines (ANTHROPIC_CUSTOM_HEADERS).
_HEADER_VALUE = re.compile(r"[^\n,=:]+[=:]\s*([^\n,]+)")


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
        for name, value in os.environ.items():
            if _CREDENTIAL_NAME.search(name):
                for part in (value, *_HEADER_VALUE.findall(value)):
                    decoded = unquote(part.strip())  # OTLP headers are percent-encoded
                    values += [part, decoded, decoded.rpartition(" ")[2]]  # "Bearer <token>"
        for name in PROXY_ENV_VARS:
            proxy = os.environ.get(name, "")
            with suppress(ValueError):
                # curl and pip read "user:pass@host:port" without a scheme as http.
                url = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
                if url.password or url.username:  # a token can be the user name alone
                    values += [proxy, unquote(url.password or ""), unquote(url.username or "")]
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
