from chalkcompute._client_cache import ClientCache


class Client:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def test_retired_client_closes_once_after_all_borrowers_finish():
    cache = ClientCache(Client)
    with cache.borrow() as first:
        with cache.borrow() as second:
            assert second is first
            cache.retire(expected=first)
            cache.retire(expected=first)
            assert first.close_calls == 0
        assert first.close_calls == 0
    assert first.close_calls == 1
    cache.retire(expected=first)
    assert first.close_calls == 1


def test_credential_change_replaces_client_without_interrupting_borrower():
    cache = ClientCache(Client)
    with cache.borrow(key="old-token") as first:
        replacement = cache.get(key="new-token")
        assert replacement is not first
        assert first.close_calls == 0
        cache.retire(expected=first)
        assert cache.client is replacement
    assert first.close_calls == 1
    assert replacement.close_calls == 0


def test_exception_releases_a_retired_client():
    cache = ClientCache(Client)
    try:
        with cache.borrow() as first:
            cache.retire()
            raise ValueError("request failed")
    except ValueError:
        pass
    assert first.close_calls == 1
