from datetime import UTC, datetime

import pytest

from dreeve_zepp_connector import main as main_module
from dreeve_zepp_connector.config import Config
from dreeve_zepp_connector.ledger import Ledger
from dreeve_zepp_connector.main import fetch_workouts, sync

DAY = 86400


class StubClient:
    """Fakes ZeppDataClient.workouts_page() over a fixed set of pages,
    newest-first, cursor-linked like the real trackid-based pagination."""

    app_token = None
    user_id = None

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.calls: list[int | None] = []
        self.detail_calls: list[str] = []

    def workouts_page(self, limit: int, before_trackid=None):
        self.calls.append(before_trackid)
        index = 0 if before_trackid is None else before_trackid
        if index >= len(self.pages):
            return [], None
        items = self.pages[index][:limit]
        next_cursor = index + 1 if index + 1 < len(self.pages) else None
        return items, next_cursor

    def workout_detail(self, trackid: str, source=None):
        self.detail_calls.append(trackid)
        return {}


def _workout(trackid: int) -> dict:
    return {"trackid": str(trackid), "type": 1}


def test_stops_at_limit_without_fetching_extra_pages():
    now = int(datetime.now(tz=UTC).timestamp())
    pages = [[_workout(now - i * DAY) for i in range(3)], [_workout(now - i * DAY) for i in range(3, 6)]]
    client = StubClient(pages)

    result = fetch_workouts(client, cutoff=None, limit=3, page_size=3)

    assert len(result) == 3
    assert client.calls == [None]


def test_pages_back_until_cutoff_is_covered():
    now = int(datetime.now(tz=UTC).timestamp())
    # 6 workouts, one per day, spread across two pages of 3.
    pages = [[_workout(now - i * DAY) for i in range(3)], [_workout(now - i * DAY) for i in range(3, 6)]]
    client = StubClient(pages)
    cutoff = datetime.fromtimestamp(now - 4 * DAY, tz=UTC)

    result = fetch_workouts(client, cutoff=cutoff, limit=200, page_size=3)

    # Needs both pages: page 1's oldest item (day 2) is still newer than the
    # day-4 cutoff, so it must keep paging into page 2.
    assert len(result) == 6
    assert client.calls == [None, 1]


def test_stops_paging_once_a_page_is_entirely_before_cutoff():
    now = int(datetime.now(tz=UTC).timestamp())
    pages = [
        [_workout(now - i * DAY) for i in range(3)],
        [_workout(now - i * DAY) for i in range(3, 6)],
        [_workout(now - i * DAY) for i in range(6, 9)],
    ]
    client = StubClient(pages)
    cutoff = datetime.fromtimestamp(now - 4 * DAY, tz=UTC)

    fetch_workouts(client, cutoff=cutoff, limit=200, page_size=3)

    # Page 2 (days 3-5) already dips below the day-4 cutoff, so page 3 is
    # never fetched.
    assert client.calls == [None, 1]


def test_empty_page_stops_pagination():
    client = StubClient([])

    result = fetch_workouts(client, cutoff=None, limit=200, page_size=50)

    assert result == []
    assert client.calls == [None]


def _cfg(tmp_path, **overrides) -> Config:
    defaults = {
        "email": "user@example.com",
        "password": "secret",
        "watch_dir": tmp_path,
        "state_dir": tmp_path,
        "ledger_path": tmp_path / "ledger.json",
        "since": None,
        "limit": 200,
        "country": "US",
        "device_names": {},
        "max_retries": 5,
        "retry_base_delay": 2.0,
        "download_delay_seconds": 0.0,
        "max_downloads_per_cycle": None,
        "poll_interval_seconds": 3600,
        "health_port": 8080,
        "cycle_timeout_seconds": 1800,
    }
    defaults.update(overrides)
    return Config(**defaults)


def _stub_decoding(monkeypatch):
    """`sync()` calls into decoder/fit_writer for every export - stub both
    out so these tests exercise only the throttle/cap loop in `sync()`
    itself, not the real decoding/FIT-building pipeline (covered separately
    in test_decoder.py / test_fit_writer.py)."""
    monkeypatch.setattr(main_module.decoder, "parse_points", lambda trackid, detail: [])
    monkeypatch.setattr(main_module.decoder, "parse_kilometer_splits", lambda detail: [])
    monkeypatch.setattr(main_module.fit_writer, "write_fit", lambda *a, **k: None)


def test_sync_stops_at_max_downloads_per_cycle(tmp_path, monkeypatch, capsys):
    _stub_decoding(monkeypatch)
    now = int(datetime.now(tz=UTC).timestamp())
    client = StubClient([[_workout(now - i * DAY) for i in range(5)]])
    ledger = Ledger(tmp_path / "ledger.json")
    cfg = _cfg(tmp_path, max_downloads_per_cycle=2)

    result = sync(cfg, client, ledger, dry_run=False)

    assert result.exported == 2
    assert len(client.detail_calls) == 2
    assert "deferred to the next cycle" in capsys.readouterr().out


def test_sync_leaves_uncapped_workouts_for_next_cycle(tmp_path, monkeypatch):
    _stub_decoding(monkeypatch)
    now = int(datetime.now(tz=UTC).timestamp())
    workouts = [_workout(now - i * DAY) for i in range(5)]
    ledger = Ledger(tmp_path / "ledger.json")
    cfg = _cfg(tmp_path, max_downloads_per_cycle=2)

    sync(cfg, StubClient([workouts]), ledger, dry_run=False)
    # A second cycle should pick up where the first left off - already
    # exported ones are skipped via the ledger, remaining ones exported.
    result = sync(cfg, StubClient([workouts]), ledger, dry_run=False)

    assert result.exported == 2
    assert result.skipped == 2


def test_sync_sleeps_between_downloads(tmp_path, monkeypatch):
    _stub_decoding(monkeypatch)
    sleeps = []
    monkeypatch.setattr(main_module.time, "sleep", lambda s: sleeps.append(s))
    now = int(datetime.now(tz=UTC).timestamp())
    client = StubClient([[_workout(now - i * DAY) for i in range(3)]])
    ledger = Ledger(tmp_path / "ledger.json")
    cfg = _cfg(tmp_path, download_delay_seconds=0.5)

    sync(cfg, client, ledger, dry_run=False)

    assert sleeps == [0.5, 0.5, 0.5]


def test_sync_dry_run_does_not_sleep_or_call_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.time, "sleep", lambda s: pytest.fail("dry-run should not sleep"))
    now = int(datetime.now(tz=UTC).timestamp())
    client = StubClient([[_workout(now - i * DAY) for i in range(3)]])
    ledger = Ledger(tmp_path / "ledger.json")
    cfg = _cfg(tmp_path, download_delay_seconds=0.5)

    result = sync(cfg, client, ledger, dry_run=True)

    assert result.exported == 3
    assert client.detail_calls == []
