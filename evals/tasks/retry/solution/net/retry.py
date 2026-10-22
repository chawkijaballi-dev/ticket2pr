"""Retry helpers."""

import functools
import time


def retry(attempts=3, exceptions=(Exception,), delay=0.0):
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    if attempt == attempts:
                        raise
                    time.sleep(delay)
            return None

        return wrapper

    return decorator
