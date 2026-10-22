"""Text helpers for blog posts."""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(title):
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return _NON_ALNUM.sub("-", ascii_title.lower()).strip("-")
