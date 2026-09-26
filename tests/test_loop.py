import os
import time

import pytest

from dreeve_zepp_connector.loop import CycleError, _run_cycle
from dreeve_zepp_connector.main import SyncResult

from .test_main import _cfg

# Worker stand-ins for loop._cycle_worker. They have to be module-level so the
# "spawn" child process can import them by name.


def _ok_worker(cfg, conn):
    conn.send(("ok", (2, 5, 1)))
    conn.close()


def _error_worker(cfg, conn):
    conn.send(("error", "history.json failed after 6 attempts"))
    conn.close()


def _hanging_worker(cfg, conn):
    time.sleep(60)


def _crashing_worker(cfg, conn):
    os._exit(3)


def test_run_cycle_returns_child_result(tmp_path):
    result = _run_cycle(_cfg(tmp_path), target=_ok_worker)

    assert result == SyncResult(exported=2, skipped=5, failed=1)


def test_run_cycle_raises_child_error(tmp_path):
    with pytest.raises(CycleError, match="failed after 6 attempts"):
        _run_cycle(_cfg(tmp_path), target=_error_worker)


def test_run_cycle_kills_child_after_timeout(tmp_path):
    started = time.monotonic()

    with pytest.raises(CycleError, match="CYCLE_TIMEOUT"):
        _run_cycle(_cfg(tmp_path, cycle_timeout_seconds=1), target=_hanging_worker)

    assert time.monotonic() - started < 30


def test_run_cycle_reports_child_dying_without_result(tmp_path):
    with pytest.raises(CycleError, match="code 3"):
        _run_cycle(_cfg(tmp_path), target=_crashing_worker)
