import pytest
import requests

from dreeve_zepp_connector import zepp_client
from dreeve_zepp_connector.zepp_client import ZeppClientError, ZeppDataClient


class FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = str(self._json_body)

    def json(self):
        return self._json_body


class FakeHttp:
    """Fakes `requests.Session.get()` over a fixed queue of responses/errors,
    one per call - mirrors `StubClient` in `tests/test_main.py`."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls = 0
        self.params: list[dict | None] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls += 1
        self.params.append(params)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(responses: list, max_retries: int = 5, retry_base_delay: float = 2.0) -> ZeppDataClient:
    client = ZeppDataClient(
        email="user@example.com",
        password="secret",
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        _http=FakeHttp(responses),  # type: ignore[arg-type]  # duck-types Session.get(), doesn't subclass it
    )
    client.use_cached_auth("cached-token", "123")
    return client


def test_retries_on_429_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: sleeps.append(s))
    client = _client([FakeResponse(429), FakeResponse(429), FakeResponse(200, {"data": "ok"})])

    result = client._get("/v1/sport/run/history.json", {})

    assert result == {"data": "ok"}
    assert client._http.calls == 3
    assert len(sleeps) == 2


def test_429_honors_retry_after_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: sleeps.append(s))
    client = _client([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {"data": "ok"})])

    client._get("/v1/sport/run/history.json", {})

    assert sleeps == [7.0]


def test_gives_up_after_max_retries_on_429(monkeypatch):
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: None)
    client = _client([FakeResponse(429)] * 3, max_retries=2)

    with pytest.raises(ZeppClientError):
        client._get("/v1/sport/run/history.json", {})

    assert client._http.calls == 3  # initial attempt + 2 retries


def test_retries_on_connection_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: None)
    client = _client([requests.exceptions.ConnectionError("boom"), FakeResponse(200, {"data": "ok"})])

    result = client._get("/v1/sport/run/history.json", {})

    assert result == {"data": "ok"}


def test_gives_up_after_max_retries_on_connection_error(monkeypatch):
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: None)
    client = _client([requests.exceptions.ConnectionError("boom")] * 3, max_retries=2)

    with pytest.raises(ZeppClientError):
        client._get("/v1/sport/run/history.json", {})


def test_401_still_drops_token_and_retries_once_without_backoff(monkeypatch):
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: pytest.fail("should not back off on 401"))
    client = _client([FakeResponse(401), FakeResponse(200, {"data": "ok"})])
    # Re-login is a separate, orthogonal concern (unchanged by backoff) -
    # stub it out so this test stays focused on the retry-loop interaction.
    monkeypatch.setattr(client, "login", lambda: setattr(client, "_app_token", "fresh-token"))

    result = client._get("/v1/sport/run/history.json", {})

    assert result == {"data": "ok"}
    assert client.app_token == "fresh-token"


def test_workouts_page_sends_page_size_as_count_and_returns_cursor():
    # `count`, not `limit` - Zepp ignores `limit` and returns the whole
    # history with next=-1 (see workouts_page()).
    body = {"data": {"next": 1789990471, "summary": [{"trackid": "1790440058", "source": "run.mi.com"}]}}
    client = _client([FakeResponse(200, body)])

    items, cursor = client.workouts_page(limit=3, before_trackid=1790500000)

    params = client._http.params[0]
    assert params["count"] == "3"
    assert "limit" not in params
    assert params["trackid"] == "1790500000"
    assert [w["trackid"] for w in items] == ["1790440058"]
    assert cursor == 1789990471
