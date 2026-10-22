"""Text helpers for blog posts."""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(title):
    return _NON_ALNUM.sub("-", title.lower()).strip("-")
