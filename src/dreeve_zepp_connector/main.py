"""CLI entrypoint: fetch new Zepp workouts and write them as .FIT files."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import decoder, fit_writer
from .config import Config, ConfigError
from .ledger import Ledger
from .zepp_client import ZeppDataClient


@dataclass
class SyncResult:
    exported: int
    skipped: int
    failed: int


def parse_since(since: str | None) -> datetime | None:
    """`since` is None (no lower bound), "all", "-Nd" (N days back), or an
    ISO date (YYYY-MM-DD)."""
    if not since or since == "all":
        return None
    if since.startswith("-") and since.endswith("d"):
        days = int(since[1:-1])
        return datetime.now(tz=UTC) - timedelta(days=days)
    return datetime.fromisoformat(since).replace(tzinfo=UTC)


def _filename_for(summary: dict, start_time: datetime) -> str:
    trackid = summary["trackid"]
    zepp_type = summary.get("type", "unknown")
    return f"{start_time.strftime('%Y-%m-%dT%H-%M-%SZ')}_{trackid}_{zepp_type}.fit"


def fetch_workouts(client: ZeppDataClient, cutoff: datetime | None, limit: int, page_size: int = 100) -> list[dict]:
    """Page back through workout history (newest-first) until `limit`
    workouts are collected or a page's oldest workout is already older than
    `cutoff` — whichever comes first. Zepp's history endpoint pages via a
    `trackid` cursor, not an offset, so a single capped call can't reliably
    reach an arbitrary `--since` date; this walks back as many pages as
    needed.
    """
    collected: list[dict] = []
    cursor: int | str | None = None
    page = 0
    while len(collected) < limit:
        page += 1
        started = time.monotonic()
        items, cursor = client.workouts_page(limit=min(page_size, limit - len(collected)), before_trackid=cursor)
        print(f"history page {page}: {len(items)} workout(s) in {time.monotonic() - started:.1f}s")
        if not items:
            break
        collected.extend(items)

        trackids = [int(w["trackid"]) for w in items if w.get("trackid")]
        oldest = min(trackids) if trackids else None
        if cutoff and oldest is not None and datetime.fromtimestamp(oldest, tz=UTC) < cutoff:
            break
        if cursor is None:
            break
    return collected[:limit]


def sync(cfg: Config, client: ZeppDataClient, ledger: Ledger, dry_run: bool = False) -> SyncResult:
    """One fetch+export cycle against an already-authenticated `client`.
    Shared by the one-shot CLI (`run()`, below) and `loop.py`'s recurring
    poll - callers own login/ledger persistence."""
    cutoff = parse_since(cfg.since)

    try:
        workouts = fetch_workouts(client, cutoff, limit=cfg.limit)
    except Exception as e:
        # Most likely a stale cached token whose re-login (see
        # ZeppDataClient._get) also failed - e.g. changed password.
        print(f"failed to fetch workout history: {e}", file=sys.stderr)
        return SyncResult(exported=0, skipped=0, failed=1)

    exported = skipped = failed = 0
    for i, summary in enumerate(workouts):
        trackid = summary.get("trackid")
        if not trackid:
            continue
        trackid = str(trackid)
        start_time = datetime.fromtimestamp(int(trackid), tz=UTC)

        if ledger.has(trackid):
            skipped += 1
            continue
        if cutoff and start_time < cutoff:
            continue

        filename = _filename_for(summary, start_time)
        device_id = summary.get("devicesource")
        device_name = cfg.device_names.get(str(device_id)) if device_id is not None else None
        if dry_run:
            # Printing device_id here (not just in the real export path)
            # doubles as the way to discover it for ZEPP_DEVICE_NAMES - no
            # extra API call needed, --dry-run already fetches summaries.
            # device_name is whatever KNOWN_DEVICE_NAMES/ZEPP_DEVICE_NAMES
            # already resolves it to, so an unset ZEPP_DEVICE_NAMES entry
            # doesn't mean "unknown" if the built-in table covers it.
            device_label = f"{device_name} " if device_name else ""
            print(f"(dry-run) would export {filename} ({device_label}device_id={device_id})")
            exported += 1
        else:
            # Stage is logged before each step (not just after) so a hang
            # shows up in the logs as the last "..." line with no follow-up.
            stage = "fetch detail"
            try:
                print(f"workout {trackid}: fetching detail...")
                started = time.monotonic()
                detail = client.workout_detail(trackid, source=summary.get("source"))
                fetch_s = time.monotonic() - started

                stage = "decode"
                print(f"workout {trackid}: decoding...")
                started = time.monotonic()
                points = decoder.parse_points(int(trackid), detail)
                splits = decoder.parse_kilometer_splits(detail)
                decode_s = time.monotonic() - started

                stage = "write FIT"
                print(f"workout {trackid}: writing FIT ({len(points)} track points)...")
                started = time.monotonic()
                output_path = cfg.watch_dir / filename
                fit_writer.write_fit(summary, points, output_path, splits=splits, device_name=device_name)
                write_s = time.monotonic() - started

                ledger.mark(trackid, filename, datetime.now(tz=UTC).isoformat())
                # Persist after every export so a cycle killed later (e.g. by
                # loop.py's CYCLE_TIMEOUT) doesn't lose already-finished work.
                ledger.save()
                print(
                    f"exported {filename} ({len(points)} track points; "
                    f"fetch {fetch_s:.1f}s, decode {decode_s:.1f}s, write {write_s:.1f}s)"
                )
                exported += 1
            except Exception as e:
                print(f"failed to export workout {trackid} at {stage}: {e}", file=sys.stderr)
                failed += 1

            if cfg.download_delay_seconds:
                time.sleep(cfg.download_delay_seconds)

        if cfg.max_downloads_per_cycle and exported >= cfg.max_downloads_per_cycle:
            remaining = len(workouts) - (i + 1)
            if remaining:
                print(
                    f"reached MAX_DOWNLOADS_PER_CYCLE ({cfg.max_downloads_per_cycle}); "
                    f"{remaining} workout(s) deferred to the next cycle"
                )
            break

    if not dry_run:
        if client.app_token and client.user_id:
            ledger.set_auth(client.app_token, client.user_id, cfg.country, cfg.email)
        ledger.save()

    return SyncResult(exported=exported, skipped=skipped, failed=failed)


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help='ISO date (e.g. 2026-07-24), "-Nd" offset (e.g. -30d), or "all" (default: no lower bound — '
        "everything up to --limit)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max workouts to fetch in total, paging back through history as needed (default 200, or LIMIT env)",
    )
    parser.add_argument("--watch-dir", help="folder to write .FIT files into (Dreeve's watch folder)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be exported, write nothing")
    args = parser.parse_args()

    try:
        cfg = Config.from_env(since_override=args.since, watch_dir_override=args.watch_dir, limit_override=args.limit)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    cfg.watch_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(cfg.ledger_path)

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
        try:
            client.login()
        except Exception as e:
            print(f"login failed: {e}", file=sys.stderr)
            return 1

    result = sync(cfg, client, ledger, dry_run=args.dry_run)

    print(f"done: {result.exported} exported, {result.skipped} already synced, {result.failed} failed")
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(run())
