"""Environment-based configuration, loaded from a local .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .known_devices import KNOWN_DEVICE_NAMES

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _parse_device_names(raw: str | None) -> dict[str, str]:
    """`ZEPP_DEVICE_NAMES` format: `<device_id>=<name>;<device_id>=<name>`.
    `device_id` is Zepp's `devicesource` summary field (also embedded in
    `source`, e.g. `run.9568513.huami.com`) - a stable per-physical-device
    ID, so an account with multiple watches can map each to its real model.
    Malformed entries are skipped rather than raising - a bad mapping should
    degrade to "no device name for that workout," not crash the export."""
    if not raw:
        return {}
    mapping: dict[str, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        device_id, name = entry.split("=", 1)
        device_id, name = device_id.strip(), name.strip()
        if device_id and name:
            mapping[device_id] = name
    return mapping


@dataclass
class Config:
    email: str
    password: str
    watch_dir: Path
    state_dir: Path
    ledger_path: Path
    since: str | None
    limit: int
    country: str
    device_names: dict[str, str]
    max_retries: int
    retry_base_delay: float
    download_delay_seconds: float
    max_downloads_per_cycle: int | None
    poll_interval_seconds: int
    health_port: int
    cycle_timeout_seconds: int = 1800

    @classmethod
    def from_env(
        cls,
        since_override: str | None = None,
        watch_dir_override: str | None = None,
        limit_override: int | None = None,
    ) -> Config:
        email = os.environ.get("ZEPP_EMAIL")
        password = os.environ.get("ZEPP_PASSWORD")
        if not email or not password:
            raise ConfigError("Set ZEPP_EMAIL and ZEPP_PASSWORD (in .env or env).")

        # Deliberately separate directories (matching dreeve-garmin-connector's
        # WATCH_DIR/STATE_DIR convention): WATCH_DIR is Dreeve's watch folder
        # (just .FIT files - nothing else should land there), STATE_DIR holds
        # the ledger (already-exported trackids + the cached app_token/user_id
        # credential) - keeping it out of the watch folder means Dreeve never
        # sees it and it can be backed up/mounted independently.
        watch_dir = Path(watch_dir_override or os.environ.get("WATCH_DIR", "./output")).expanduser()
        state_dir = Path(os.environ.get("STATE_DIR", "./state")).expanduser()
        ledger_path = Path(os.environ.get("LEDGER_PATH", state_dir / "ledger.json")).expanduser()
        since = since_override or os.environ.get("SINCE")
        limit = limit_override if limit_override is not None else int(os.environ.get("LIMIT", "200"))
        country = os.environ.get("ZEPP_COUNTRY", "US")
        # KNOWN_DEVICE_NAMES covers common devices out of the box (see its
        # module docstring for the source); ZEPP_DEVICE_NAMES overrides or
        # adds entries it doesn't know about.
        device_names = {**KNOWN_DEVICE_NAMES, **_parse_device_names(os.environ.get("ZEPP_DEVICE_NAMES"))}
        max_retries = int(os.environ.get("ZEPP_MAX_RETRIES", "5"))
        retry_base_delay = float(os.environ.get("ZEPP_RETRY_BASE_DELAY", "2.0"))
        download_delay_seconds = float(os.environ.get("DOWNLOAD_DELAY_SECONDS", "0"))
        raw_max_downloads = os.environ.get("MAX_DOWNLOADS_PER_CYCLE")
        max_downloads_per_cycle = int(raw_max_downloads) if raw_max_downloads else None
        poll_interval_seconds = int(os.environ.get("POLL_INTERVAL", "3600"))
        health_port = int(os.environ.get("HEALTH_PORT", "8080"))
        # Hard wall-clock cap on one loop.py sync cycle (0 disables) - see
        # loop._run_cycle for why this has to be a separate process.
        cycle_timeout_seconds = int(os.environ.get("CYCLE_TIMEOUT", "1800"))

        return cls(
            email=email,
            password=password,
            watch_dir=watch_dir,
            state_dir=state_dir,
            ledger_path=ledger_path,
            since=since,
            limit=limit,
            country=country,
            device_names=device_names,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            download_delay_seconds=download_delay_seconds,
            max_downloads_per_cycle=max_downloads_per_cycle,
            poll_interval_seconds=poll_interval_seconds,
            health_port=health_port,
            cycle_timeout_seconds=cycle_timeout_seconds,
        )
