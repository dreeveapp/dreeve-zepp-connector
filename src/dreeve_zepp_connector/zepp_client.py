"""
Zepp / Amazfit cloud data client.

Login goes through Zepp's *web-app* flow (`com.huami.webapp`), ported (with
attribution) from effectpears/zepp-downloader's `zepp_app_token.py`: three
plain `requests` calls — email/password -> access code, access code ->
login_token, login_token -> app_token. Confirmed (2026-08-26) not to log the
user's phone app out, unlike the `huami-token` library's `ZeppSession`
login, which registers as an Android device (`app_name=com.huami.midong`,
`device_model=android_phone`) and appears to kick the existing device's
session as a side effect.

Data queries are issued here against api-mifit.zepp.com with a header
template (`_DATA_HEADERS_TEMPLATE` below) matching that same Android
identity - confirmed (2026-08-26) to accept a web-app-issued app_token
without issue, despite the identity mismatch. Ported from `huami-token`'s
`HEADERS.ZEPP_DEVICES` constant (MIT); no longer a runtime dependency of
this project.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlparse

import requests

DATA_HOST = "api-mifit.zepp.com"

# (connect, read) seconds. `read` bounds the gap between received bytes, not
# the whole response - a slow trickle can still take longer overall, which
# is what loop.py's per-cycle CYCLE_TIMEOUT backstops.
_DATA_TIMEOUT = (10, 60)

# Zepp's web-app login identity, as opposed to huami-token's mobile
# `com.huami.midong` identity (see module docstring).
_WEB_APP_NAME = "com.huami.webapp"
_WEB_REDIRECT_URI = "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"
_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Android-app header identity used for data calls (history/detail), ported
# from huami-token's `HEADERS.ZEPP_DEVICES` constant - see module docstring.
_DATA_HEADERS_TEMPLATE = {
    "hm-privacy-diagnostics": "false",
    "country": "US",
    "appplatform": "android_phone",
    "hm-privacy-ceip": "true",
    "timezone": "Europe/London",
    "channel": "a100900101016",
    "vb": "202509151347",
    "cv": "151689_9.12.5",
    "appname": "com.huami.midong",
    "v": "2.0",
    "vn": "9.12.5",
    "lang": "en_US",
    "user-agent": "Zepp/9.12.5 (Pixel 4; Android 12; Density/2.75)",
    "accept-encoding": "gzip",
}


class ZeppClientError(RuntimeError):
    pass


def _data_headers(app_token: str) -> dict:
    h = _DATA_HEADERS_TEMPLATE.copy()
    h["apptoken"] = app_token
    h["x-request-id"] = str(uuid.uuid4())
    return h


def _web_login(http: requests.Session, email: str, password: str, country: str) -> tuple[str, str]:
    """Zepp web-app login. Returns `(app_token, user_id)`; raises
    `ZeppClientError` with the failing step and response body on failure."""
    reg_headers = {
        "app_name": _WEB_APP_NAME,
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://user.zepp.com",
        "referer": "https://user.zepp.com/",
        "x-request-id": str(uuid.uuid4()),
    }
    reg_resp = http.post(
        f"https://api-user.huami.com/registrations/{quote(email, safe='')}/tokens",
        data={
            "client_id": "HuaMi",
            "country_code": country,
            "json_response": "true",
            "name": email,
            "password": password,
            "redirect_uri": _WEB_REDIRECT_URI,
            "state": "REDIRECTION",
            "token": "access",
        },
        headers=reg_headers,
        allow_redirects=False,
    )

    access_code = None
    location = reg_resp.headers.get("Location", "")
    if location:
        access_code = parse_qs(urlparse(location).query).get("access", [None])[0]
    if not access_code and reg_resp.status_code == 200:
        try:
            data = reg_resp.json()
            access_code = data.get("access") or data.get("code")
        except ValueError:
            pass
    if not access_code:
        raise ZeppClientError(f"login failed at registration step (HTTP {reg_resp.status_code}): {reg_resp.text[:300]}")

    login_headers = {
        "app_name": _WEB_APP_NAME,
        "appname": _WEB_APP_NAME,
        "appplatform": "web",
        "origin": "https://user.zepp.com",
        "referer": "https://user.zepp.com/",
        "user-agent": _WEB_USER_AGENT,
    }
    login_resp = http.post(
        "https://api-mifit.zepp.com/v2/client/login",
        data={
            "allow_registration": "false",
            "app_name": _WEB_APP_NAME,
            "app_version": "1.0.0",
            "code": access_code,
            "country_code": country,
            "device_id": f"web_{uuid.uuid4()}",
            "device_model": "web",
            "dn": "api-mifit.zepp.com,api-user.zepp.com,api-watch.zepp.com,auth.zepp.com",
            "grant_type": "access_token",
            "source": _WEB_APP_NAME,
            "third_name": "huami",
        },
        headers=login_headers,
    )
    try:
        token_info = login_resp.json().get("token_info") or {}
    except ValueError:
        raise ZeppClientError(
            f"login failed at token-exchange step (HTTP {login_resp.status_code}): {login_resp.text[:300]}"
        ) from None
    login_token, user_id = token_info.get("login_token"), token_info.get("user_id")
    if not login_token or not user_id:
        raise ZeppClientError(f"login failed at token-exchange step: missing login_token/user_id in {token_info}")

    token_resp = http.get(
        "https://api-mifit.zepp.com/v1/client/app_tokens",
        params={
            "app_name": _WEB_APP_NAME,
            "dn": "api-mifit.zepp.com,api-user.zepp.com,auth.zepp.com",
            "login_token": login_token,
        },
        headers=login_headers,
    )
    try:
        app_token = token_resp.json().get("token_info", {}).get("app_token")
    except ValueError:
        app_token = None
    if not app_token:
        raise ZeppClientError(
            f"login failed at app-token step (HTTP {token_resp.status_code}): {token_resp.text[:300]}"
        )

    return app_token, str(user_id)


@dataclass
class ZeppDataClient:
    email: str
    password: str
    country: str = "US"
    max_retries: int = 5
    retry_base_delay: float = 2.0
    _http: requests.Session = field(default_factory=requests.Session)
    _source_cache: dict[str, str] = field(default_factory=dict)
    _app_token: str | None = field(default=None, init=False, repr=False)
    _user_id: str | None = field(default=None, init=False, repr=False)

    # ---- auth -----------------------------------------------------------

    def login(self) -> None:
        self._app_token, self._user_id = _web_login(self._http, self.email, self.password, self.country)

    def use_cached_auth(self, app_token: str, user_id: str) -> None:
        """Skip login and reuse a previously obtained app_token/user_id
        (e.g. from `Ledger.cached_auth()`). If it's actually expired, `_get()`
        transparently re-logs-in on the first 401/403 it hits."""
        self._app_token = app_token
        self._user_id = user_id

    @property
    def app_token(self) -> str | None:
        return self._app_token

    @property
    def user_id(self) -> str | None:
        return self._user_id

    def _ensure(self) -> None:
        if not self._app_token:
            self.login()

    def _backoff_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self.retry_base_delay * (2**attempt)

    def _get(self, path: str, params: dict, _retry: bool = True) -> dict:
        self._ensure()
        token = self._app_token
        assert token is not None, "_ensure() must set _app_token or raise"
        url = f"https://{DATA_HOST}{path}"

        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.get(url, headers=_data_headers(token), params=params, timeout=_DATA_TIMEOUT)
            except requests.exceptions.RequestException as e:
                if attempt >= self.max_retries:
                    raise ZeppClientError(f"{path} failed after {attempt + 1} attempts: {e}") from e
                time.sleep(self._backoff_delay(attempt))
                continue

            if r.status_code == 429:
                if attempt >= self.max_retries:
                    raise ZeppClientError(f"{path} -> 429 after {attempt + 1} attempts, giving up")
                time.sleep(self._backoff_delay(attempt, retry_after=r.headers.get("Retry-After")))
                continue

            if r.status_code in (401, 403) and _retry:
                # Cached/expired token - drop it and let the next _ensure() log
                # in fresh, then replay this call once.
                self._app_token = None
                return self._get(path, params, _retry=False)

            try:
                return r.json()
            except ValueError:
                raise ZeppClientError(f"{path} -> {r.status_code} non-JSON: {r.text[:300]}") from None

        raise ZeppClientError(f"{path} failed after {self.max_retries + 1} attempts")

    # ---- data -----------------------------------------------------------

    def workouts(self, limit: int = 50) -> list[dict]:
        """List the most recent workout/sport sessions (single page). Each
        has a `trackid` for detail. For paging further back, use
        `workouts_page()` directly."""
        return self.workouts_page(limit=limit)[0]

    def workouts_page(self, limit: int = 50, before_trackid: int | str | None = None) -> tuple[list[dict], int | None]:
        """One page of workout summaries, newest-first.

        Zepp's history endpoint pages via a `trackid` cursor rather than an
        offset: pass the previous call's returned cursor as `before_trackid`
        to continue further into the past. Returns `(items, next_cursor)`;
        `next_cursor` is None once there are no more pages.
        """
        params = {"source": "run.mi.com", "userid": self.user_id, "limit": str(limit)}
        if before_trackid is not None:
            params["trackid"] = str(before_trackid)
        j = self._get("/v1/sport/run/history.json", params)
        items = _extract_list(j)
        for w in items:
            if isinstance(w, dict) and w.get("trackid") and w.get("source"):
                self._source_cache[str(w["trackid"])] = w["source"]
        data = j.get("data", j)
        next_cursor = data.get("next") if isinstance(data, dict) else None
        if next_cursor in (None, -1):
            next_cursor = None
        return items, next_cursor

    def workout_detail(self, trackid: str, source: str | None = None) -> dict:
        """Full raw track for one workout: GPS, pace, HR series, altitude, gait.

        `source` auto-resolves from recent workouts if omitted. Unlike
        zepp-mcp, the encoded track-data strings are returned in full,
        untouched, for decoding by `decoder.py`.
        """
        trackid = str(trackid)
        if source is None:
            source = self._source_cache.get(trackid)
            if source is None:
                self.workouts(limit=200)  # populate source cache
                source = self._source_cache.get(trackid, "run.mi.com")
        j = self._get(
            "/v1/sport/run/detail.json",
            {"trackid": str(trackid), "source": source, "userid": self.user_id},
        )
        return j.get("data", j)


def _maybe_b64_json(raw):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}


def _extract_list(j: dict) -> list[dict]:
    data = j.get("data", j)
    if isinstance(data, dict):
        summary = data.get("summary")
        if isinstance(summary, str):
            with contextlib.suppress(Exception):
                summary = json.loads(summary)
        if isinstance(summary, dict) and isinstance(summary.get("data"), list):
            return summary["data"]
        if isinstance(summary, list):
            return summary
        if isinstance(data.get("items"), list):
            return data["items"]
    if isinstance(data, list):
        return data
    return [{"_raw": j}]
