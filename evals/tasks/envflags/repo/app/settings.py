"""Read feature flags from the environment."""

import os


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return bool(value)
