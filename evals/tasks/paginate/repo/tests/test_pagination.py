from shop.pagination import paginate


def test_first_page():
    assert paginate(list(range(25)), 1, 10) == list(range(10))
