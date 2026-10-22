"""ticket2pr: turn tickets into review-ready pull requests with a budget-capped Claude agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ticket2pr")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
