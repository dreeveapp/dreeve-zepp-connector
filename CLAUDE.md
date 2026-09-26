# dreeve-zepp-connector

## What this is

A standalone Python CLI (not an MCP server) that pulls workouts from a Zepp /
Amazfit cloud account and writes them as `.FIT` files into a local folder —
intended to be [Dreeve](https://github.com/dreeveapp/dreeve)'s watch folder,
alongside its existing [Garmin connector](https://github.com/dreeveapp/dreeve-garmin-connector).

Why this exists: Garmin Connect lets you download a device's *original*
`.FIT` file directly, so the Garmin connector just re-hosts a file Garmin
already produced. Zepp has no equivalent export — its cloud API only returns
JSON with per-sample track data encoded as delta-compressed strings. This
tool decodes that raw format and *synthesizes* a `.FIT` file from it.

Sibling project: `../zepp-mcp` (an MCP server exposing the same Zepp cloud
data to AI agents). This project ported and extended `zepp-mcp`'s auth/fetch
client rather than depending on it directly.

## Architecture (current, v1)

- **`zepp_client.py`** — Zepp cloud auth + fetch, ported from `zepp-mcp`'s
  `huami_client.py` with its token-saving truncation of GPS/HR/altitude
  fields removed (this project needs the full data). Login goes through
  Zepp's *web-app* flow (`com.huami.webapp`), not the `huami-token` lib's
  `ZeppSession` — see the "Login flow" quirk below. Paginates workout
  history via a `trackid` cursor (`workouts_page()`/`data.next`) — the API
  has no offset-based pagination.
- **`decoder.py`** — decodes Zepp's encoded `longitude_latitude` /
  `heart_rate` / `altitude` / `gait` / `time` fields into track points.
  Ported (with attribution) from the MIT-licensed
  `rolandsz/Mi-Fit-and-Zepp-workout-exporter`, itself based on
  `mireq/MiFitDataExport` — this is the one place the actual encoding
  scheme is documented; don't re-derive it from scratch. Also decodes
  `speed` / `currentDistance` / `power_meter` / `gait`'s stride column /
  `kilo_pace` — these are *not* handled by that reference project at all
  (it captures them as opaque strings, never decodes them), so this part is
  this project's own reverse-engineering from live account data, not a
  port — see the "speed/distance/power decoding" and "kilo_pace splits"
  quirks below.
- **`fit_writer.py`** — builds `.FIT` bytes via the `fit-tool` PyPI library.
  `TYPE_MAP` maps Zepp's numeric workout `type` codes to FIT `Sport`/
  `SubSport`. Synthesizes placeholder records for track-less workouts
  (strength, pool swims, table tennis, ...) — this is required, not
  optional (see Known quirks below). Builds one `LapMessage` per completed
  kilometer when `decoder.parse_kilometer_splits()` output looks consistent
  with the workout's total distance, else a single whole-workout lap.
  `_point_stats()` computes `max_speed`/`avg_cadence`/`max_cadence`/
  `avg_step_length` from decoded points (session-wide and per-lap, via the
  same helper) since the summary's own `avg_cadence`/`max_cadence` fields
  are always 0 in practice.
- **`ledger.py`** — local JSON file (`STATE_DIR/ledger.json` by default,
  deliberately separate from `WATCH_DIR` — see the "WATCH_DIR vs STATE_DIR"
  quirk below) tracking already-exported `trackid`s so re-runs skip them.
  Also caches the last successful `app_token`/`user_id` (keyed to
  `email`+`country`) so re-runs skip the login network round-trip entirely
  unless the cached token actually gets rejected — see "app_token caching"
  quirk below.
- **`main.py`** — CLI entrypoint (`--since`, `--limit`, `--watch-dir`,
  `--dry-run`). Pages back through history until either the `--since`
  cutoff is covered or `--limit` total workouts are collected. `run()`
  itself only does argv parsing, config/client/ledger setup, and printing —
  the actual fetch+export cycle is `sync(cfg, client, ledger, dry_run)`,
  factored out so `loop.py` (below) can call it repeatedly against one
  already-authenticated `client`/`ledger` pair without re-parsing argv or
  re-logging-in each cycle.
- **`loop.py`** (v2) — continuous daemon: calls `main.sync()` on a
  `POLL_INTERVAL`-second cadence (env, default 3600) instead of relying on
  external cron. Does the same cached-auth-or-login resolution as
  `main.run()` once up front, then reuses that `client`/`ledger` across
  cycles. Starts a `health.HealthServer`, and traps `SIGTERM`/`SIGINT` to
  finish the in-flight cycle and shut the health server down cleanly (so
  `docker stop` doesn't kill it mid-write). This is the Docker image's
  default command; `dreeve-zepp-connector` (the one-shot CLI) is still
  available by overriding the container command.
- **`health.py`** (v2) — stdlib-only (`http.server`) `/healthz` (liveness,
  always `200` while the process is up) and `/status` (JSON: cycle count,
  last cycle's timing/result, last error) endpoints, for monitoring when
  `loop.py` runs as a long-lived container. `HealthState` is a small
  lock-guarded snapshot written by the loop thread and read by the HTTP
  handler's thread. No web-framework dependency added — two JSON routes
  didn't justify one.

## Known quirks / gotchas

Documented here so they don't get silently re-broken or re-derived:

- **Zepp's encoding**: `time`, both halves of `longitude_latitude`, and
  `heart_rate`'s value column are delta-encoded (need cumulative sum);
  `altitude` and `gait`'s stride/cadence columns are absolute but sampled on
  their own irregular timestamps (need interpolation onto a unified
  timeline). Lat/lon scale ÷1e8, altitude in centimeters. `NO_VALUE =
  -2000000` is the sentinel for "no altitude reading at all."
- **`speed`/`currentDistance`/`power_meter` decoding (added 2026-08-27,
  reverse-engineered, not from the upstream reference project).** Same
  `;`-delimited/`<delta_time>,<value>`-pair shape as `heart_rate`, but
  *unlike* `heart_rate` the value column is already absolute — only the
  time column needs cumulative summing. **`currentDistance`'s value column
  is centimeters, like `altitude`** — easy to miss since the raw sample
  values (e.g. `95.00000`) look like plausible meters at a glance; confirmed
  live when a real 20km ride decoded as 2017km without the `/100`. `speed`
  is already m/s, no conversion needed (cross-checked against a real ride's
  `dis`/`run_time` average). `power_meter` carries a Zepp-computed
  *running*-power estimate (watts) — confirmed live: present with a running
  workout whose summary `average_power` was populated, empty for a cycling
  workout on the same account with no paired power meter
  (`average_power: -1`). A workout showing no power in the output is far
  more likely "device never recorded it" than a decoding bug — check the
  workout's own `average_power`/`max_power` summary fields (sentinel `-1`
  means absent) before assuming otherwise. A field that's completely absent
  for a given workout must decode to `None` for every point, not `0` —
  `interpolate_column` fills an empty channel with zeros, so
  `decoder.parse_points()` checks emptiness before interpolation runs.
- **`kilo_pace` per-kilometer splits (added 2026-08-27, reverse-engineered,
  not from the upstream reference project — most speculative of these
  additions).** `;`-separated entries, one per *completed* kilometer only
  (validated across many real workouts: entry count always equals
  `floor(total_distance_m / 1000)` exactly). Each entry is a `,`-separated
  tuple; only `field[0]` (0-based split index), `field[4]` (that split's avg
  heart rate), and `field[6]` (that split's precise duration in
  milliseconds — `field[1]`, a rounded-seconds duration, is
  `floor(field[6]/1000)` for every entry checked) are used. `field[2]` looks
  like a geohash of the split-boundary location; `field[3]` and `field[7:]`
  are unconfirmed and deliberately left unused rather than guessed at.
  `decoder.parse_kilometer_splits()` bails out to `[]` (never raises) on any
  unexpected shape, and `fit_writer._build_laps()` additionally cross-checks
  split count against the workout's total distance before trusting
  it — either gate failing falls back to a single whole-workout lap rather
  than emit a wrong split.
- **There is no device-model field anywhere in Zepp's workout API data.**
  Checked live across summary and detail responses — `deviceid`/`sn` are
  opaque serial numbers, nothing human-readable. Confirmed against a real
  Zepp-app-exported FIT file too: its `DeviceInfoMessage.product_name`
  ("Amazfit Balance 2") comes from the phone app's local Bluetooth-pairing
  knowledge, not anything present in the cloud API this project talks to —
  so there's no way to recover it server-side, ever. `ZEPP_DEVICE_NAMES`
  (env var, see `config.py`'s `_parse_device_names()`) used to be the only
  option: a user-supplied `device_id=name;device_id=name` mapping, keyed by
  the summary's `devicesource` field (stable per physical device, also
  embedded in `source`, e.g. `run.9568513.huami.com`) — supports accounts
  with more than one watch. **`known_devices.py` (added 2026-08-31)** now
  covers this for common devices without any env var: a static
  `deviceSource` → model-name table scraped from Zepp's own device-list
  docs (https://docs.zepp.com/docs/1.0/reference/related-resources/device-list/),
  parsed from that page's raw server-rendered HTML (not an AI summary) for
  accuracy — 103 entries, no ID collisions. `config.py` merges it as a
  fallback under `ZEPP_DEVICE_NAMES`, so an explicit env entry still
  overrides or adds to it. Confirmed live: also tried hitting
  `huami-token`'s plaintext `GET
  https://api-mifit.zepp.com/users/{user_id}/devices` endpoint (same
  `_data_headers()`/`DATA_HOST` auth this project already uses, just a
  different path) against a real account — works, returns each device's
  `deviceSource`/`productId`/`deviceId`/`sn`, but still **no human-readable
  model name field**, so calling it at runtime wouldn't add anything over
  the static table; not wired up. Also tried `market/devices/{id}/watch/builtin`
  directly (the endpoint the URL with `cipher_data` on `api-mifit-de2.zepp.com`
  pointed at) — turns out the `cipher_data` param isn't actually required:
  a plain `GET https://api-mifit.zepp.com/market/devices/{deviceSource}/watch/builtin`
  with this project's existing auth headers also returns `200`, on both
  `api-mifit.zepp.com` and `api-mifit-de2.zepp.com`. But it's the wrong
  endpoint for this anyway — confirmed live against all 3 real devices on
  the test account, response shape is
  `[{"id":0,"name":"","builtin_id":0,"image":"","device_image":"<url>","official_builtin":false}]`
  with `name` empty for every device. This is the device's *builtin
  watchface market listing* (`name` would be a watchface's name, not the
  watch's), not a device-identity lookup — the only per-device-distinct
  field is `device_image` (a product photo URL), not a usable text name.
  So: no endpoint anywhere in Zepp's cloud API, plaintext or cipher, at any
  of these hosts, exposes the device model as a string — `known_devices.py`
  (a static table, not a live lookup) is confirmed to be the only path
  short of `ZEPP_DEVICE_NAMES`. The table is necessarily incomplete (new
  devices ship after 2026-08-31, and
  non-Zepp-OS devices are only partially documented on that page) —
  `ZEPP_DEVICE_NAMES` remains how to cover anything it's missing.
- **FIT's `cadence` field uses a single-leg convention; Zepp's `gait`
  cadence is total steps/min (both feet).** Confirmed live: our raw decoded
  avg/max cadence (159/174) was almost exactly 2x a real Zepp-app FIT
  export's (79/88) for the same running workout. `fit_writer._fit_cadence()`
  halves it. This conversion is applied only where cadence is written to
  FIT output — `decoder.ExportablePoint.cadence` stays the true, undivided
  steps/min value, since that's the more useful raw decoded fact.
- **Swimming's cadence (stroke rate) comes from a completely different
  field, `stroke_speed`, not `gait`.** `gait` is empty for swims (no
  footpod underwater) — confirmed live, which is why swim exports had no
  cadence chart at all before this was added. `stroke_speed` is empty for
  runs/rides (confirmed live too - genuinely mutually exclusive per
  workout). Format is the same `<delta_time>,<value>` pairing as
  `speed`/`power_meter`, in strokes/second; `decoder.parse_track_data()`
  applies ×60 for strokes/minute. Unlike `gait`'s cadence, this needs *no*
  further halving for FIT output — confirmed live: our decoded ×60 values
  matched a real Zepp-app FIT export's per-record cadence sequence exactly
  ([16,16,16,16,16,19,19,...] both sides). `fit_writer._fit_cadence()`
  picks whichever of `point.cadence` (halved) / `point.stroke_cadence`
  (used as-is) is present — they're never both set for the same workout.
- **Interpolation artifacts**: the ported decoder interpolates gaps using
  integer floor-division slopes (faithful to the upstream reference). Across
  a wide gap between real samples this can under/overshoot into physically
  impossible values (observed live: cadence of `-46`). `fit_writer._in_range`
  clamps/drops these per-field rather than crashing.
- **Mixed types from the API**: summary fields (`calorie`, `avg_heart_rate`,
  `type`) come back as a mix of ints, floats, and numeric strings like
  `"375.0"` across different workouts. Always route through `_to_int()`,
  never a bare `int(...)`.
- **Dreeve rejects record-less FIT files, unconditionally.** Confirmed
  against Dreeve's own `FitFileParser.php`: it throws `"No FIT 'record'
  messages found"` and rejects the file before it even looks at sport or
  session data, regardless of how complete the Session/Lap summary is. This
  is why `fit_writer.py` synthesizes a placeholder record stream (timestamp
  + repeated avg heart rate + a linear distance ramp) for any workout with
  no decoded GPS track — removing that would silently break every indoor/
  strength/pool-swim/racket-sport export again.
- **Login flow was switched off `huami-token`'s `ZeppSession` on
  2026-08-26.** `ZeppSession.login()` registers as an Android device
  (`app_name=com.huami.midong`, `device_model=android_phone`) and was found
  to log the user's phone app out as a side effect — unacceptable since the
  phone app is used alongside this connector. `zepp_client._web_login()`
  replaces it with Zepp's *web-app* login (`app_name=com.huami.webapp`,
  `device_model=web`), ported (with attribution) from
  `effectpears/zepp-downloader`'s `zepp_app_token.py` — three plain
  `requests` calls (registration -> access code -> login_token ->
  app_token) instead of `huami-token`'s encrypted mobile handshake.
  Confirmed live (2026-08-26) not to disconnect the phone app. Data-call
  headers (`_DATA_HEADERS_TEMPLATE` in `zepp_client.py`) still use
  `huami-token`'s Android-app identity (`com.huami.midong`) — confirmed to
  accept a web-app-issued `app_token` fine despite the mismatch — but that
  constant is now just inlined/ported (with attribution), not imported;
  `huami-token` is no longer a runtime dependency at all. Login's
  `country_code` defaults to `"US"` (`ZEPP_COUNTRY` env var to override); the old
  `huami-token`-hardcoded-region quirk no longer applies to login, but a
  wrong `ZEPP_COUNTRY` is now the first thing to check if login itself
  starts failing for a specific account.
- **`app_token` caching (added 2026-08-26).** `main.py` checks
  `ledger.cached_auth()` before calling `client.login()` — if a token was
  cached from a prior run for the same `email`+`country`, it's handed to
  `client.use_cached_auth()` and the login network round-trip is skipped
  entirely. `ZeppDataClient._get()` detects a rejected/expired token via a
  401/403 response, drops it, and transparently re-logs-in once before
  replaying the failed call — no explicit expiry/TTL tracking, purely
  reactive. The refreshed (or first-ever) token is written back to
  `ledger.json` at the end of a non-dry-run. Because `ledger.json` now holds
  a live credential, `Ledger.save()` chmods it `0o600`; it was already
  gitignored. Note this doesn't apply to `--dry-run`, which never touches
  the ledger.
- **A newer, encrypted API variant exists.** Live app traffic has been
  observed calling region-specific hosts (e.g. `api-mifit-de2.zepp.com`)
  with a single encrypted `cipher_data` query param instead of this tool's
  plaintext `trackid`/`source`/`userid` params. Not implemented — the
  plaintext endpoint still works as of 2026-08 — but it's the lead to chase
  if that ever changes (see README's "Notes on the underlying API").
- **Ball/team sports go through the same endpoint.** Volleyball, table
  tennis, etc. all showed up via the normal `run/history.json` endpoint —
  there's no separate "ball games" API as first suspected. A workout
  "missing" from output is far more likely a wrong/stale `trackid` than a
  real API gap — verify directly against the account before assuming a new
  endpoint is needed.
- **`TYPE_MAP` coverage** (extend as new codes surface — an unmapped type
  prints a warning instead of failing silently). The original 14 codes below
  (1–17, 49, 88/89, 140, 223) were verified against this project's own
  account per the Verification section; codes 18–178 were added in
  `f96943c` (2026-08-28, contributed by @effectpears) from
  `zepp-downloader`'s broader table and **have not been cross-checked
  against a real account showing those `type` values** — same caveat as the
  "⚠️ Type-code conflict" callout below: a code correct for one
  device/firmware/app version may not be portable. Two entries in that
  commit referenced `Sport` enum members that don't exist in `fit-tool`
  0.9.16 (`FIELD_HOCKEY`, `HANDBALL`) and crashed *every* import of
  `fit_writer.py` — not just workouts with those types, since `TYPE_MAP` is
  a dict literal evaluated at module-import time — until fixed 2026-08-30
  (`Sport.HOCKEY`, and `Sport.TEAM_SPORT` for handball, which has no
  dedicated FIT value). `tests/test_fit_writer.py`'s
  `test_type_map_covers_every_code_with_valid_fit_tool_enum_members` now
  builds a `.FIT` file for every `TYPE_MAP` code so a bad enum member fails
  `uv run pytest` immediately instead of surfacing at deploy time.

  | code | sport | code | sport |
  |------|-------|------|-------|
  | 1 | running | 79 | baseball |
  | 6 | walking | 80 | bowling (generic) |
  | 7 | trail running | 81 | squash |
  | 8 | treadmill | 82 | rugby |
  | 9 | outdoor cycling | 85 | basketball |
  | 10 | indoor cycling | 86 | softball (baseball) |
  | 11 | elliptical | 87 | gateball (generic) |
  | 13 | mountaineering | 88 | volleyball |
  | 14 | pool swimming | 89 | table tennis |
  | 15 | open water swimming | 90 | hockey |
  | 16 | free training (generic) | 91 | handball (team sport, generic) |
  | 17 | tennis | 92 | badminton |
  | 18 | soccer | 93 | archery |
  | 19 | cross-country skiing | 94 | equestrian (generic) |
  | 21 | jump rope | 96 | karate (generic) |
  | 22 | hiking | 97 | boxing |
  | 23 | indoor rowing | 98 | judo (generic) |
  | 24 | indoor fitness (generic) | 99 | wrestling (generic) |
  | 27 | yoga | 100 | tai chi (generic) |
  | 39 | multisport | 101 | muay thai (generic) |
  | 42 | snowboarding | 102 | taekwondo (generic) |
  | 47 | mountain biking | 103 | martial arts (generic) |
  | 49 | strength training | 104 | kickboxing (generic) |
  | 70 | rock climbing | 105 | alpine skiing (resort) |
  | 71–77 | dance styles (generic) | 140 | kayaking |
  | 78 | cricket | 148 | fencing (generic) |
  |  |  | 178 | snowshoeing |
  |  |  | 223 | generic movement |

- **`--limit` default is 200** (env `LIMIT`). A full historical backfill
  needs an explicit higher `--limit`/`LIMIT` (or `--since all --limit <N>`)
  — 200 is a reasonable cap for "catch up the last month or two" but will
  silently cap a deep backfill.
- **`WATCH_DIR` (env, was `OUTPUT_DIR` pre-2026-08-30) vs `STATE_DIR` (env,
  new) are deliberately separate directories**, matching
  `dreeve-garmin-connector`'s convention: `WATCH_DIR` is Dreeve's watch
  folder and should contain nothing but `.FIT` files; `STATE_DIR` (default
  `./state`) holds `ledger.json` (already-exported `trackid`s + the cached
  login credential) so it's never mistaken for a workout file or swept up
  by whatever's watching `WATCH_DIR`. `LEDGER_PATH` still overrides the
  ledger's exact file path independently of `STATE_DIR` if needed. Both
  `main.run()` and `loop.run()` `mkdir(parents=True, exist_ok=True)` both
  directories on startup — `Ledger.save()` would create `STATE_DIR` anyway
  on first write, but creating it eagerly matters for Docker (an empty
  volume mount needs to exist for `/status` to look right before the first
  cycle finishes).
- **`docker-compose.yml`'s published health port must be kept in sync via
  `${HEALTH_PORT:-8080}` in *two* places, not one (added 2026-08-30, after
  hardcoding it broke on a host with something else already on 8080).**
  Compose reads `.env` for its own `${VAR}` substitution (used in the
  `ports:` mapping) completely separately from the `env_file: - .env`
  directive that populates the *container's* environment - a shell-exported
  `HEALTH_PORT` (not written into the `.env` file) satisfies the first but
  not the second. So `HEALTH_PORT: ${HEALTH_PORT:-8080}` is listed
  explicitly under `environment:` too, not left to `env_file` pass-through
  alone - otherwise the host-side port mapping and the app's actual
  listening port can silently diverge (container never becomes reachable on
  the "wrong" port). If this ever needs a `docker run` invocation without
  compose, `-p $HEALTH_PORT:$HEALTH_PORT -e HEALTH_PORT` both still need
  setting for the same reason. Don't need `/healthz`/`/status` reachable
  from outside the container at all? Drop the `ports:` section entirely -
  Docker's own `HEALTHCHECK` runs inside the container's network namespace
  and doesn't need the port published to work.
- **Rate-limit backoff lives only in `ZeppDataClient._get()`, not
  `_web_login()`.** `_get()` retries connection errors and HTTP 429s with
  exponential backoff (`ZEPP_RETRY_BASE_DELAY * 2**attempt`, env
  `ZEPP_MAX_RETRIES` caps attempts, default 5/2.0s), honoring a 429's
  `Retry-After` header when present. Login's three plain `requests` calls
  are unchanged — login failures are auth-shaped (bad password/country),
  not rate-limit-shaped, so retrying them blindly would just mask a real
  credential problem. The existing reactive 401/403-drop-token-and-relogin
  behavior in `_get()` is a separate, orthogonal path (one-shot, no
  backoff) — added first during v1, untouched by the v2 backoff work.
- **`MAX_DOWNLOADS_PER_CYCLE` needs no cursor/offset bookkeeping across
  cycles.** `sync()` just breaks out of its per-workout loop once the cap
  is hit; whatever's left in that cycle's `workouts` list simply isn't in
  the ledger yet, so the *next* `sync()` call re-fetches the same
  newest-first list and `ledger.has()` transparently skips everything
  already exported, picking up right where the last cycle left off. This
  also applies during `--dry-run` (the cap breaks the loop either way) —
  only `DOWNLOAD_DELAY_SECONDS`' actual `time.sleep()` is skipped in
  dry-run, since dry-run never calls `workout_detail()` to begin with.
- **Each `loop.py` cycle runs in its own child process (added 2026-09-26,
  after a field report: a v1.0.0 container went 10 days without syncing one
  9h / ~32k-point workout, sitting at 30–100% CPU and growing to its 512MB
  cap, while its logs showed almost nothing; a one-shot manual run then
  exported it in seconds).** The root cause was never pinned down. The fix
  targets what made it undiagnosable and unrecoverable instead:
  (1) `_run_cycle()` starts a `spawn` child per cycle and `kill()`s it after
  `CYCLE_TIMEOUT` (env, default 1800s, 0 = no limit). A thread can't be
  stopped from outside in Python, so a separate process is the only way to
  enforce the limit, and it also hands a cycle's memory back to the OS
  after every cycle. The child re-reads `ledger.json` and reuses the cached
  token. The parent logs in once at startup and saves the token, so
  children don't log in again each cycle. (2) `sync()` logs each stage
  *before* it starts (`fetching detail...`, `decoding...`,
  `writing FIT...`) and saves the ledger after *every* export, so a killed
  cycle keeps finished work and the last logged line shows where it hung.
  `Ledger.save()` is now write-then-rename for the same reason.
  (3) stdout is unbuffered (`PYTHONUNBUFFERED=1` in the Dockerfile, plus
  `line_buffering` in `loop.run()`). Before this, stdout was block-buffered
  under Docker, which is the likely reason "logs were mostly silence".
  (4) data calls use `timeout=(10, 60)`. `requests`' read timeout only
  limits the gap between received bytes, not the whole response, which is
  why the cycle timeout is still needed on top of it.
- **`health.py`'s `/healthz` is a liveness check, not a correctness
  check.** It returns `200` as long as the process/HTTP server is up,
  regardless of whether the last sync cycle failed - `/status`'s
  `last_error`/`last_result` is where cycle-level failures actually show
  up. A container can be "healthy" while every cycle is failing (e.g. bad
  credentials) - that's intentional (liveness vs. readiness are different
  questions) but worth knowing when wiring up alerting on `/status` instead
  of just the `HEALTHCHECK`.

## V2 — daemon hardening (built) / still pending

Items 1–5 below (Docker packaging, scheduled polling, health endpoints,
rate-limit backoff, per-cycle throttling) are now built — see `loop.py`,
`health.py`, the `Config`/`ZeppDataClient` fields listed above, and the
Docker files (`Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`).
`dreeve-garmin-connector` (the layout this was modeled on) wasn't available
locally while building this, so file/env-var naming follows what this
document already specified, not a literal copy of that repo.

Still pending, deliberately not attempted blind:

1. **Proper pool-swim fidelity (maybe)** — current synthetic-record
   approach gets swims past Dreeve's import gate and shows correct
   sport/duration/calories/avg-HR, but has no real per-length data
   (`LengthMessage`, `pool_length`, `num_lengths`, SWOLF). Investigate
   whether Zepp's API exposes lap-level swim data at all before investing
   here — unconfirmed either way.
2. **`cipher_data` endpoint support (fallback only)** — only needed if the
   current plaintext detail endpoint stops working; see the quirk above.

## Comparison: `effectpears/zepp-downloader` (reviewed 2026-08-24)

A friend independently released [zepp-downloader](https://github.com/effectpears/zepp-downloader)
(single-file script, tuned against an Amazfit Stratos 3), same goal as this
project. Full comparison done by cloning and reading its source; findings
below so they don't need re-deriving.

**What we do better:**
- Login via email/password vs. their original manual "pull the `apptoken`
  header out of devtools" flow — though as of 2026-08-26 our login flow
  itself now *is* their contributor's follow-up script, `zepp_app_token.py`
  (see the "Login flow" quirk above): it turned out `huami-token`'s
  `ZeppSession` login logs the phone app out, which their plain-`requests`
  web-app flow doesn't. We still keep the email/password UX (their
  companion script prompts once and caches the resulting `app_token`
  manually into `.env`; ours does the same caching automatically, into
  `ledger.json` — see "`app_token` caching" quirk above); the underlying
  HTTP calls are now shared lineage.
- Real historical backfill: our `fetch_workouts()` pages back through
  history via the `trackid` cursor until `--since`/`--limit` is satisfied.
  Theirs only ever fetches the single most recent `FETCH_LIMIT` (default
  20) activities per run — no pagination, so it can only "catch up since
  last cron tick," not backfill deep history.
- Decoder fidelity: we delta-decode `time`, `heart_rate`, and both lat/lon
  halves, and interpolate `altitude`/`gait` onto a unified timeline. Theirs
  treats `heart_rate` as a flat non-delta int list — likely wrong for
  devices/accounts where HR is delta-encoded like ours documents above.
- Track-less workout placeholders: both synthesize records (Dreeve
  requires a non-empty `record` stream), but ours ramps 60 points with
  HR+distance; theirs only emits 1–2 records.

**Real gaps worth considering (not yet acted on):**
- No retry/backoff on HTTP calls at all in our `zepp_client._get()` — a
  single flaky request just fails that workout. Theirs retries with
  exponential backoff and gives specific 401/403/429 messages. Overlaps
  with V2 item 4 below but is currently *fully unbuilt*, not just
  unthrottled.
- No lock file (`fcntl.flock`) preventing two overlapping runs from racing
  on the ledger/output dir — matters once V2's scheduled polling exists.
- No rotating file log (we only `print()`) — would help debugging once
  this runs headless via cron/Docker.
- `TYPE_MAP` coverage: they map several codes we don't — 7 (trail run), 11
  (elliptical), 12 (indoor rowing), 13/16 (mountaineering), 18 (alpine
  skiing), 19 (cross-country skiing), 20 (snowboarding), 24 (indoor
  fitness), 27 (yoga), 39 (triathlon/multisport, with a `type % 1000`
  prefix-strip for `1000 < type < 2000` composite-activity legs that we
  don't handle at all).

**⚠️ Type-code conflict to investigate — do not blindly merge their table:**
their code **17 = HIKING**; ours (`fit_writer.TYPE_MAP`) has **17 =
TENNIS**. Their script was tuned specifically for an Amazfit Stratos 3, so
`type` codes may not be portable 1:1 across devices/firmware/app versions —
same numeric code could mean different things depending on device
generation. Before importing any of their mapping, verify actual `type`
values seen from the account in question (per the "Ball/team sports" quirk
above: confirm against real API responses, don't assume). Open question:
is this a genuine per-device code collision, or did one of the two projects
mis-map it?

## Verification

- `uv run pytest` — unit tests for decoder math (delta-decoding,
  interpolation, speed/distance/power decoding, `kilo_pace` split parsing),
  pagination/cutoff logic (`fetch_workouts`), `fit_writer` edge cases
  (range clamping, decimal-string summary fields, synthetic records for
  track-less workouts, per-kilometer lap building and its single-lap
  fallback, device-info opt-in), `ZeppDataClient._get()`'s retry/backoff
  (429 + `Retry-After`, connection errors, retry exhaustion, 401/403
  orthogonality), `health.HealthState`/`HealthServer` (including a real
  `ThreadingHTTPServer` on an OS-assigned port), and `sync()`'s
  `DOWNLOAD_DELAY_SECONDS`/`MAX_DOWNLOADS_PER_CYCLE` throttling.
- **v2 daemon hardening (2026-08-30) live-tested**: ran
  `python -m dreeve_zepp_connector.loop` against the real account
  (`POLL_INTERVAL=8`, `LIMIT=1`) — confirmed `/healthz` and `/status`
  responded correctly mid-run, a real new workout was fetched/exported on
  the first cycle and correctly reported as `already synced` (not
  re-downloaded) on subsequent cycles, and `SIGINT` triggered a clean
  "finish current cycle, stop health server, exit" shutdown.
- Live-tested end-to-end against a real Zepp account: 200 workouts (the
  `--limit` default — there's more/older history beyond it, untested)
  across the 14 `TYPE_MAP` sport types above exported cleanly, 0 failures,
  imported successfully into a real Dreeve instance (including
  previously-failing pool swims and other track-less activity types).
- **Speed/power/distance/lap additions (2026-08-27) validated against real
  account data**: built actual `.FIT` files for a real cycling and running
  workout, re-parsed them with `fit_tool` and checked the decoded values -
  distance matched the summary's `dis` field within ~1m over 6-20km,
  `total_work` (computed from integrated power) matched `average_power ×
  duration` within rounding, per-km lap paces were physically plausible,
  and the cycling workout correctly showed no power data (matching its
  `average_power: -1`) while the running workout showed real watts. Also
  smoke-tested `build_fit()` against 42 real workouts sampled across all 17
  distinct `type` codes present in the account (3 per type) - 0 exceptions.
- **Cross-checked against 3 real `.FIT` files exported by the Zepp app
  itself** (open water swim, cycling, running - same account, same
  workouts as above) - by far the strongest validation available, since
  it's the official product's own output, not just internal consistency
  checks. Confirmed: `kilo_pace`-derived lap count matches exactly (21 and
  7 laps); `total_distance` matches within ~1cm; device manufacturer ID
  (339) matches `Manufacturer.ZEPP`; `max_speed`, `max_heart_rate`, and
  `avg_step_length` now match exactly after being added; running cadence
  was found and fixed to be 2x too high (see the cadence-convention quirk
  above) before it matched; the swim's cadence chart was found to be
  entirely missing (the `gait` field is empty for swims - no footpod
  underwater) and fixed by decoding `stroke_speed` instead (see the
  "stroke rate" quirk above) - per-record cadence sequence then matched
  exactly. One deliberate difference: **the official
  export leaves `total_work` unset for the running workout despite having
  `avg_power`/`max_power` populated** - possibly Zepp doesn't trust a
  running-power *estimate* enough to publish a derived energy figure. This
  project's `total_work` (computed by integrating decoded power) was kept
  anyway, per explicit user decision (2026-08-27) - flagging here in case
  that's revisited.
