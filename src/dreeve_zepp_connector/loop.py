"""Continuous polling daemon: runs `main.sync()` on a `POLL_INTERVAL`
cadence instead of relying on external cron, with `/healthz`/`/status`
endpoints for monitoring when run as a long-lived container (see `health.py`).

Each cycle runs in its own short-lived child process (see `_run_cycle`), so
a hung cycle can be killed after `CYCLE_TIMEOUT` and whatever memory a cycle
used is handed back to the OS when it ends, instead of piling up in one
process that stays alive for weeks.
"""

from __future__ import annotations

import multiprocessing
import signal
import sys
import time
from datetime import UTC, datetime
from multiprocessing.connection import Connection

from .config import Config, ConfigError
from .health import HealthServer, HealthState
from .ledger import Ledger
from .main import SyncResult, sync
from .zepp_client import ZeppDataClient


class CycleError(RuntimeError):
    pass


def _authenticated_client(cfg: Config, ledger: Ledger) -> ZeppDataClient:
    """Cached auth from the ledger if it matches `cfg`, else a fresh login
    (raises on login failure)."""
    client = ZeppDataClient(
        email=cfg.email,
        password=cfg.password,
        country=cfg.country,
        max_retries=cfg.max_retries,
        retry_base_delay=cfg.retry_base_delay,
    )
    cached_auth = ledger.cached_auth()
    if cached_auth and cached_auth.get("email") == cfg.email and cached_auth.get("country") == cfg.country:
        client.use_cached_auth(cached_auth["app_token"], cached_auth["user_id"])
    else:
        client.login()
    return client


def _cycle_worker(cfg: Config, conn: Connection) -> None:
    """Child-process entrypoint for one sync cycle. Re-reads the ledger from
    disk (the parent holds no ledger state across cycles) and sends back
    `("ok", (exported, skipped, failed))` or `("error", message)`."""
    # The parent decides when to stop - a Ctrl-C (delivered to the whole
    # process group) or SIGTERM shouldn't abort a cycle half-way.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        ledger = Ledger(cfg.ledger_path)
        client = _authenticated_client(cfg, ledger)
        result = sync(cfg, client, ledger, dry_run=False)
        conn.send(("ok", (result.exported, result.skipped, result.failed)))
    except Exception as e:
        conn.send(("error", str(e)))
    finally:
        conn.close()


def _run_cycle(cfg: Config, target=_cycle_worker) -> SyncResult:
    """Run one cycle in a child process, killing it if it's still running
    after `cfg.cycle_timeout_seconds` (0 = no limit). A thread can't be
    stopped from outside in Python, so a separate process is the only way to
    actually enforce a limit here. Raises `CycleError` on timeout, on an
    error inside the cycle, or if the child dies without reporting back.
    `target` is swappable for tests."""
    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=target, args=(cfg, send_conn), name="sync-cycle", daemon=True)
    proc.start()
    send_conn.close()

    try:
        proc.join(cfg.cycle_timeout_seconds or None)
        if proc.is_alive():
            proc.kill()
            proc.join()
            raise CycleError(
                f"cycle killed after exceeding CYCLE_TIMEOUT ({cfg.cycle_timeout_seconds}s) - "
                "see the last logged stage above for where it was stuck"
            )
        try:
            # poll() is also True at EOF (child exited without sending), in
            # which case recv() raises EOFError.
            if not recv_conn.poll():
                raise EOFError
            status, payload = recv_conn.recv()
        except EOFError:
            raise CycleError(f"cycle process exited (code {proc.exitcode}) without reporting a result") from None
    finally:
        recv_conn.close()

    if status != "ok":
        raise CycleError(payload)
    exported, skipped, failed = payload
    return SyncResult(exported=exported, skipped=skipped, failed=failed)


def run() -> int:
    # Line-buffer stdout even when it isn't a terminal (Docker, systemd, a log
    # file), so the per-stage progress lines show up as they happen.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    cfg.watch_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    # Authenticate once up front, so bad credentials fail at startup instead of
    # once per cycle, and save the token so every cycle's child process reuses
    # it instead of logging in again.
    ledger = Ledger(cfg.ledger_path)
    try:
        client = _authenticated_client(cfg, ledger)
    except Exception as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    if client.app_token and client.user_id:
        ledger.set_auth(client.app_token, client.user_id, cfg.country, cfg.email)
        ledger.save()

    state = HealthState()
    server = HealthServer(state, cfg.health_port)
    server.start()
    print(f"health server listening on :{cfg.health_port} (/healthz, /status)")

    stop = False

    def _handle_signal(signum, _frame) -> None:
        nonlocal stop
        print(f"received signal {signum}, stopping after current cycle")
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    timeout_label = f"{cfg.cycle_timeout_seconds}s" if cfg.cycle_timeout_seconds else "none"
    print(f"starting poll loop: every {cfg.poll_interval_seconds}s (cycle timeout: {timeout_label})")
    while not stop:
        cycle_start = datetime.now(tz=UTC)
        print("cycle started")
        try:
            result = _run_cycle(cfg)
            state.record_success(cycle_start, result)
            elapsed = (datetime.now(tz=UTC) - cycle_start).total_seconds()
            print(
                f"cycle done in {elapsed:.0f}s: {result.exported} exported, "
                f"{result.skipped} already synced, {result.failed} failed"
            )
        except Exception as e:
            state.record_failure(cycle_start, str(e))
            print(f"cycle failed: {e}", file=sys.stderr)

        for _ in range(cfg.poll_interval_seconds):
            if stop:
                break
            time.sleep(1)

    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(run())
