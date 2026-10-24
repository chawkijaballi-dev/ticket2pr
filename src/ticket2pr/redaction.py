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

from ticket2pr.config import (
    CLAUDE_CREDENTIAL_ENV_VARS,
    OTHER_CREDENTIAL_ENV_VARS,
    PROXY_ENV_VARS,
    Settings,
    agent_env_kept,
)

MASK = "REDACTED"  # no brackets: it lands after _inert and must not form a [link](url)
_MIN_SECRET_LEN = 8  # shorter values would mask ordinary words
# Variables the agent sees that hold credentials (AWS_SECRET_ACCESS_KEY, ANTHROPIC_CUSTOM_HEADERS,
# OTEL_EXPORTER_OTLP_HEADERS...): whole words only, so UV_KEYRING_PROVIDER is not one.
_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE|HEADERS)(?:_|$)", re.IGNORECASE
)
# Header lists: "k=v,k2=v2" (OTLP) or "Name: value" lines (ANTHROPIC_CUSTOM_HEADERS). Only
# credential headers count (not x-honeycomb-dataset or anthropic-beta).
_HEADER = re.compile(r"([^\n,=:]+)[=:]\s*([^\n,]+)")
_CREDENTIAL_HEADER = re.compile(r"auth|key|token|secret|password|team|dsn", re.IGNORECASE)


def _url_secret(url: str) -> str:
    """The credential in a URL: its password, or a user name standing alone (a token)."""
    with suppress(ValueError):
        parts = urlsplit(url)
        return unquote(parts.password or parts.username or "")
    return ""


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
        known = (*CLAUDE_CREDENTIAL_ENV_VARS, *OTHER_CREDENTIAL_ENV_VARS)
        for name, value in os.environ.items():
            # A variable the agent never sees cannot reach the diff: no false positives from it.
            if name in known or (agent_env_kept(name) and _CREDENTIAL_NAME.search(name)):
                if value.isdigit():  # CLAUDE_CODE_API_KEY_HELPER_TTL_MS=86400000
                    continue
                values.append(value)
                for header, part in _HEADER.findall(value):
                    decoded = unquote(part.strip())  # OTLP headers are percent-encoded
                    in_url = _url_secret(decoded)  # a DSN: https://<token>@host/1
                    if not (_CREDENTIAL_HEADER.search(header) or in_url):
                        continue
                    values += [part, decoded, in_url]
                    if "authorization" in header.lower():  # "Bearer <token>", "Api-Token <t>"
                        values.append(decoded.rpartition(" ")[2])
        for name in PROXY_ENV_VARS:
            proxy = os.environ.get(name, "")
            # curl and pip read "user:pass@host:port" without a scheme as http.
            if secret := _url_secret(proxy if "://" in proxy else f"http://{proxy}"):
                values += [proxy, secret]
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
