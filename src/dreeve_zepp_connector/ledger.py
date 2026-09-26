"""Tracks which workouts have already been exported, so re-runs skip them,
and caches the Zepp app_token/user_id so re-runs don't have to log in again."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self._entries: dict[str, dict] = {}
        self._auth: dict | None = None
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and ("entries" in data or "auth" in data):
                self._entries = data.get("entries", {})
                self._auth = data.get("auth")
            else:
                self._entries = data  # pre-auth-caching ledger: flat trackid map

    def has(self, trackid: str) -> bool:
        return str(trackid) in self._entries

    def mark(self, trackid: str, filename: str, exported_at: str) -> None:
        self._entries[str(trackid)] = {"filename": filename, "exported_at": exported_at}

    def cached_auth(self) -> dict | None:
        """`{"app_token", "user_id", "country", "email"}` from the last
        successful login, or None if there's no cached auth."""
        return self._auth

    def set_auth(self, app_token: str, user_id: str, country: str, email: str) -> None:
        self._auth = {"app_token": app_token, "user_id": user_id, "country": country, "email": email}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {"entries": self._entries}
        if self._auth is not None:
            data["auth"] = self._auth
        # Write-then-rename so a process killed mid-save (loop.py's
        # CYCLE_TIMEOUT kills the cycle's child process) never leaves a
        # truncated ledger behind. chmod before the rename: the ledger now
        # doubles as an app_token cache - keep it off-limits to other local
        # users, same as the .env file it's derived from.
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True))
        with contextlib.suppress(OSError):
            tmp_path.chmod(0o600)
        tmp_path.replace(self.path)
