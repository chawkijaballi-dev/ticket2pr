import pytest

from blog.text import slugify


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Crème brûlée à Paris", "creme-brulee-a-paris"),
        ("Ça va? Très bien!", "ca-va-tres-bien"),
        ("  --Hello--  ", "hello"),
        ("Python 3.12 Released", "python-3-12-released"),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug
