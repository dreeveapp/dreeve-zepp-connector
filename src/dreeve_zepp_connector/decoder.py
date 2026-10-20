"""
Decoder for Zepp's encoded per-sample workout track fields.

Ported from rolandsz/Mi-Fit-and-Zepp-workout-exporter's `base_exporter.py`
(MIT), itself based on https://github.com/mireq/MiFitDataExport. Adapted to
read from the raw `workout_detail()` dict instead of a pydantic model, and
simplified by dropping the device-specific "BIP gap fixing" branch (an
always-disabled quirk workaround in the source project not needed here).

Field format, as reverse-engineered by the above projects:
- Each field (`time`, `longitude_latitude`, `altitude`, `heart_rate`, `gait`)
  is a `;`-delimited string of samples; `longitude_latitude`/`heart_rate`/
  `gait` samples are further `,`-delimited sub-fields.
- `time`, both halves of `longitude_latitude`, and `heart_rate`'s value
  column are delta-encoded (each sample is relative to the previous one) and
  must be cumulatively summed.
- `altitude` and `gait`'s stride/cadence columns are absolute values, but
  sampled on their own irregular timestamps and need interpolating onto a
  unified timeline (the union of the GPS/HR/step timestamps).
- Latitude/longitude are scaled by 1e8; altitude is in centimeters; `gait`'s
  stride column is *also* centimeters (confirmed 2026-08-27 against a real
  Zepp-app FIT export's `avg_step_length`/`step_length` fields, which are
  millimeters - `ExportablePoint.step_length` applies the ×10 conversion).

`speed`, `currentDistance`, `power_meter`, and `stroke_speed` are NOT
handled by the upstream reference project (it captures them as opaque
strings but never decodes them) - the parsing below is this project's own
reverse-engineering from live account data (2026-08-27), not a port. Same
`;`-delimited/`,`-delimited-pair shape as `heart_rate`, but unlike
`heart_rate` the value column is already absolute (no cumulative sum
needed) - confirmed by `currentDistance` being monotonically non-decreasing
as raw values, and by `speed`/`power_meter` values being small and
non-monotonic like a live sensor reading rather than a shrinking/growing
delta chain. `power_meter` carries a Zepp-computed running-power estimate
(watts) on accounts without a dedicated cycling power meter - it's
genuinely absent (not a decoding gap) for workouts where the device had
nothing to estimate power from. `stroke_speed` is swimming's stroke-rate
equivalent of `gait`'s cadence column - genuinely mutually exclusive with
`gait` (confirmed live: `gait` is empty for a swim, `stroke_speed` is empty
for runs/rides) - in strokes/second, needing ×60 for FIT's strokes/minute
cadence convention (confirmed live against a real Zepp-app FIT export's
per-record cadence: our decoded ×60 values matched exactly, unlike
`gait`'s cadence, which separately needs ÷2 - see the "cadence convention"
quirk). `kilo_pace` (per-kilometer splits) is parsed separately by
`parse_kilometer_splits()` below.
"""

from __future__ import annotations

import array
from bisect import bisect_left
from collections import namedtuple
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import accumulate

NO_VALUE = -2000000

# speed (m/s) and currentDistance (m) are stored scaled by this factor while
# passing through the integer-based interpolation machinery below, then
# divided back out in parse_points() - keeps sub-integer precision (e.g.
# 0.874 m/s) through code that otherwise only deals in ints.
_FLOAT_SCALE = 1000

RawTrackData = namedtuple(
    "RawTrackData",
    [
        "times",
        "lat",
        "lon",
        "alt",
        "hrtimes",
        "hr",
        "steptimes",
        "stride",
        "cadence",
        "spdtimes",
        "spd",
        "disttimes",
        "dist",
        "powertimes",
        "power",
        "stroketimes",
        "stroke",
    ],
)
Position = namedtuple("Position", ["lat", "lon", "alt"])
TrackPoint = namedtuple(
    "TrackPoint",
    ["time", "position", "hr", "stride", "cadence", "speed", "distance", "power", "stroke"],
)


@dataclass
class ExportablePoint:
    time: datetime
    latitude: float
    longitude: float
    altitude: float | None
    heart_rate: float | None
    cadence: float | None
    speed: float | None = None
    distance: float | None = None
    power: int | None = None
    step_length: float | None = None
    stroke_cadence: float | None = None


@dataclass
class KilometerSplit:
    """One `kilo_pace` split. Field layout reverse-engineered from live
    account data - see `parse_kilometer_splits()`."""

    index: int
    duration_ms: int
    avg_heart_rate: int | None


class Interpolate:
    def __init__(self, x_list, y_list):
        intervals = zip(x_list, x_list[1:], y_list, y_list[1:], strict=False)
        self.x_list = x_list
        self.y_list = y_list
        self.slopes = [(y2 - y1) // ((x2 - x1) or 1) for x1, x2, y1, y2 in intervals]

    def __getitem__(self, x):
        i = bisect_left(self.x_list, x) - 1
        if i >= len(self.slopes):
            return self.y_list[-1]
        if i < 0:
            return self.y_list[0]
        return self.y_list[i] + self.slopes[i] * (x - self.x_list[i])


def _samples(raw: str | None) -> list[str]:
    return list(filter(None, raw.split(";"))) if raw else []


def _pair_samples(raw: str | None, scale: float = 1.0) -> tuple[array.array, array.array]:
    """Parse a `<delta_time>,<absolute_value>` sample field (`speed`,
    `currentDistance`, `power_meter`) into `(times, values)`. Unlike
    `heart_rate`'s same-shaped field, the value column here is already
    absolute - only `times` needs cumulative summing by the caller. Values
    are pre-multiplied by `scale` and rounded to int to survive the
    integer-based interpolation in `interpolate_column`."""
    times = array.array("q")
    values = array.array("q")
    for sample in _samples(raw):
        t, v = sample.split(",", 1)
        times.append(int(t or 1))
        values.append(round(float(v) * scale))
    return times, values


def parse_track_data(detail: dict) -> RawTrackData:
    spdtimes, spd = _pair_samples(detail.get("speed"), scale=_FLOAT_SCALE)
    # currentDistance's value column is centimeters, like altitude - not
    # meters (confirmed live: without /100 a 20km ride decoded as 2017km,
    # matching a clean 100x error). _FLOAT_SCALE/100 folds that unit
    # conversion into the same pre-scale that survives interpolation.
    disttimes, dist = _pair_samples(detail.get("currentDistance"), scale=_FLOAT_SCALE / 100)
    powertimes, power = _pair_samples(detail.get("power_meter"))
    # stroke_speed is strokes/second - ×60 folds the strokes/minute
    # conversion into the same pre-scale that survives interpolation.
    stroketimes, stroke = _pair_samples(detail.get("stroke_speed"), scale=_FLOAT_SCALE * 60)
    return RawTrackData(
        times=array.array("q", [int(v) for v in _samples(detail.get("time"))]),
        lat=array.array("q", [int(v.split(",")[0]) for v in _samples(detail.get("longitude_latitude"))]),
        lon=array.array("q", [int(v.split(",")[1]) for v in _samples(detail.get("longitude_latitude"))]),
        alt=array.array("q", [int(v) for v in _samples(detail.get("altitude"))]),
        hrtimes=array.array("q", [int(v.split(",")[0] or 1) for v in _samples(detail.get("heart_rate"))]),
        hr=array.array("q", [int(v.split(",")[1]) for v in _samples(detail.get("heart_rate"))]),
        steptimes=array.array("q", [int(v.split(",")[0]) for v in _samples(detail.get("gait"))]),
        stride=array.array("q", [int(v.split(",")[2]) for v in _samples(detail.get("gait"))]),
        cadence=array.array("q", [int(v.split(",")[3]) for v in _samples(detail.get("gait"))]),
        spdtimes=spdtimes,
        spd=spd,
        disttimes=disttimes,
        dist=dist,
        powertimes=powertimes,
        power=power,
        stroketimes=stroketimes,
        stroke=stroke,
    )


def interpolate_column(data, original_points, new_points):
    data = array.array("q", data)
    old_value = NO_VALUE
    for old_value in data:
        if old_value != NO_VALUE:
            break
    for i, value in enumerate(data):
        if value == NO_VALUE:
            data[i] = old_value
        else:
            old_value = value

    if len(new_points) == 0:
        return array.array("q", [])
    if len(original_points) == 0:
        return array.array("q", [0] * len(new_points))
    if len(original_points) == 1:
        return array.array("q", [original_points[0]] * len(new_points))
    interpolate = Interpolate(original_points, data)
    return array.array("q", (interpolate[point] for point in new_points))


def track_points(track_data: RawTrackData):
    for time, lat, lon, alt, hr, stride, cadence, spd, dist, power, stroke in zip(
        track_data.times,
        track_data.lat,
        track_data.lon,
        track_data.alt,
        track_data.hr,
        track_data.stride,
        track_data.cadence,
        track_data.spd,
        track_data.dist,
        track_data.power,
        track_data.stroke,
        strict=False,
    ):
        # NO_VALUE survives interpolation only when a channel has zero valid
        # samples for the whole workout (e.g. no barometer data) - the
        # forward-fill in interpolate_column can't fix what was never there.
        altitude = None if alt == NO_VALUE else alt / 100
        yield TrackPoint(
            time=time,
            position=Position(lat=lat / 100000000, lon=lon / 100000000, alt=altitude),
            hr=hr,
            stride=stride,
            cadence=cadence,
            speed=spd / _FLOAT_SCALE,
            distance=dist / _FLOAT_SCALE,
            power=power,
            stroke=stroke / _FLOAT_SCALE,
        )


def interpolate_data(track_data: RawTrackData) -> RawTrackData:
    track_times = array.array("q", accumulate(track_data.times))
    hr_times = array.array("q", accumulate(track_data.hrtimes))
    step_times = array.array("q", accumulate(track_data.steptimes))
    spd_times = array.array("q", accumulate(track_data.spdtimes))
    dist_times = array.array("q", accumulate(track_data.disttimes))
    power_times = array.array("q", accumulate(track_data.powertimes))
    stroke_times = array.array("q", accumulate(track_data.stroketimes))

    times = sorted(
        set(track_times)
        .union(hr_times)
        .union(step_times)
        .union(spd_times)
        .union(dist_times)
        .union(power_times)
        .union(stroke_times)
    )

    return track_data._replace(
        times=times,
        lat=interpolate_column(accumulate(track_data.lat), track_times, times),
        lon=interpolate_column(accumulate(track_data.lon), track_times, times),
        alt=interpolate_column(track_data.alt, track_times, times),
        hrtimes=times,
        hr=interpolate_column(accumulate(track_data.hr), hr_times, times),
        steptimes=times,
        stride=interpolate_column(track_data.stride, step_times, times),
        cadence=interpolate_column(track_data.cadence, step_times, times),
        spdtimes=times,
        spd=interpolate_column(track_data.spd, spd_times, times),
        disttimes=times,
        dist=interpolate_column(track_data.dist, dist_times, times),
        powertimes=times,
        power=interpolate_column(track_data.power, power_times, times),
        stroketimes=times,
        stroke=interpolate_column(track_data.stroke, stroke_times, times),
    )


def parse_points(start_time: int, detail: dict) -> list[ExportablePoint]:
    """Decode a `workout_detail()` response into a list of track points.

    `start_time` is the workout's start (unix seconds) — Zepp's `trackid`
    doubles as this timestamp. Returns [] if the workout has no GPS track
    (e.g. an indoor/strength session).
    """
    track_data = parse_track_data(detail)

    if not track_data.lat:
        return []

    # A totally absent field (e.g. no power meter / no power estimate for
    # this workout, or no gait sensor at all - real for swims) interpolates
    # to all-zeros, not None - checked here, before interpolation, so it can
    # be reported as "no data" rather than a fabricated 0. `cadence` and
    # `stride` share one flag since both come from the same `gait` field.
    have_speed = bool(track_data.spd)
    have_dist = bool(track_data.dist)
    have_power = bool(track_data.power)
    have_gait = bool(track_data.stride)
    have_stroke = bool(track_data.stroke)

    return [
        ExportablePoint(
            time=datetime.fromtimestamp(point.time + start_time, tz=UTC),
            latitude=point.position.lat,
            longitude=point.position.lon,
            altitude=point.position.alt,
            heart_rate=point.hr,
            cadence=point.cadence if have_gait else None,
            speed=point.speed if have_speed else None,
            distance=point.distance if have_dist else None,
            power=point.power if have_power else None,
            step_length=(point.stride * 10) if have_gait else None,
            stroke_cadence=point.stroke if have_stroke else None,
        )
        for point in track_points(interpolate_data(track_data))
    ]


def parse_kilometer_splits(detail: dict) -> list[KilometerSplit]:
    """Best-effort decode of Zepp's `kilo_pace` field into per-kilometer
    splits, for building real per-km laps instead of one lap spanning the
    whole workout.

    This is this project's own reverse-engineering from live account data
    (2026-08-27), not a port - `kilo_pace` isn't handled by the upstream
    reference project at all. Format: `;`-separated entries, each a
    `,`-separated tuple. Confirmed against real workouts:
    - entry count == floor(total_distance_m / 1000) (only whole completed
      kilometers get a split - exact match across every workout checked).
    - field[0] is the 0-based split index.
    - field[4] is that split's average heart rate.
    - field[5] is the cumulative elapsed time in whole seconds (monotonic).
    - field[6] is that split's own duration in milliseconds (precise -
      field[1], the rounded-seconds duration, is `floor(field[6] / 1000)`
      for every entry checked).
    - field[2] looks like a geohash of the split-boundary location; the
      rest (field[3] and field[7:]) are unconfirmed and intentionally
      unused here rather than guessed at.

    Returns `[]` (never raises) if `kilo_pace` is absent or doesn't match
    this shape - a parse failure should silently fall back to a single
    whole-workout lap, not corrupt lap data with a bad guess.
    """
    raw = detail.get("kilo_pace")
    if not raw:
        return []
    splits = []
    for entry in filter(None, raw.split(";")):
        fields = entry.split(",")
        if len(fields) < 7:
            return []
        try:
            index = int(fields[0])
            duration_ms = round(float(fields[6]))
            avg_hr = int(float(fields[4]))
        except ValueError:
            return []
        # Some workouts can carry a -1 sentinel instead of a real duration
        # here. Treating it as a duration would produce a -0.001s lap, which
        # FIT cannot encode ("total_elapsed_time encoded value -1 is not in
        # valid range") - bail out to the single-lap fallback instead of
        # trusting the whole layout.
        if duration_ms <= 0:
            return []
        splits.append(
            KilometerSplit(index=index, duration_ms=duration_ms, avg_heart_rate=avg_hr if avg_hr > 0 else None)
        )
    return splits
