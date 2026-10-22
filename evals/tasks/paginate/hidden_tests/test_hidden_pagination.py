import pytest

from shop.pagination import paginate

ITEMS = list(range(25))


def test_pages_do_not_overlap():
    assert paginate(ITEMS, 2, 10) == list(range(10, 20))
    assert paginate(ITEMS, 3, 10) == list(range(20, 25))


def test_page_past_the_end_is_empty():
    assert paginate(ITEMS, 4, 10) == []


@pytest.mark.parametrize(("page", "size"), [(0, 10), (-1, 10), (1, 0)])
def test_invalid_arguments(page, size):
    with pytest.raises(ValueError):
        paginate(ITEMS, page, size)
