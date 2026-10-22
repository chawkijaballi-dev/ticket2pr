"""Pagination helpers for product listings."""


def paginate(items, page, size):
    """Return the items on ``page`` (1-based) when showing ``size`` items per page."""
    start = (page - 1) * size - 1
    return items[max(start, 0) : start + size]
