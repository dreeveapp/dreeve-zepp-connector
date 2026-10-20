"""Build a .FIT activity file from a decoded Zepp workout."""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime
from pathlib import Path

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity,
    Event,
    EventType,
    FileType,
    Manufacturer,
    Sport,
    SubSport,
)

from .decoder import ExportablePoint, KilometerSplit

EARTH_RADIUS_M = 6371000.0

# Fallback for genuinely generic/miscellaneous workouts. NOT (Sport.GENERIC,
# SubSport.GENERIC) - that combo is rejected outright ("Unsupported FIT
# sport 0 (sub sport 0)") by at least some deployed Dreeve versions, whose
# FitSportType::resolveGeneric() apparently predates the "always fall back
# to WORKOUT" fix present on Dreeve's current GitHub master. Sport.
# FITNESS_EQUIPMENT has its own long-standing, non-generic match arm ending
# in `default => SportType::WORKOUT`, so it resolves reliably either way.
_WORKOUT_FALLBACK = (Sport.FITNESS_EQUIPMENT, SubSport.GENERIC)

# Zepp workout `type` codes -> FIT (Sport, SubSport). See zepp-mcp's README
# for the code table this is based on; extend as new codes turn up (a warning
# is printed for any unmapped type actually seen, to help spot gaps).
TYPE_MAP: dict[int, tuple[Sport, SubSport]] = {
    1: (Sport.RUNNING, SubSport.GENERIC),
    6: (Sport.WALKING, SubSport.GENERIC),
    7: (Sport.RUNNING, SubSport.TRAIL),  # Trailrunning
    8: (Sport.RUNNING, SubSport.TREADMILL),
    9: (Sport.CYCLING, SubSport.GENERIC),
    10: (Sport.CYCLING, SubSport.INDOOR_CYCLING),
    11: (Sport.FITNESS_EQUIPMENT, SubSport.ELLIPTICAL),
    13: (Sport.MOUNTAINEERING, SubSport.GENERIC),
    14: (Sport.SWIMMING, SubSport.LAP_SWIMMING),
    15: (Sport.SWIMMING, SubSport.OPEN_WATER),
    16: _WORKOUT_FALLBACK,  # "free training" in the app
    17: (Sport.TENNIS, SubSport.GENERIC),
    18: (Sport.SOCCER, SubSport.GENERIC),
    19: (Sport.CROSS_COUNTRY_SKIING, SubSport.GENERIC),
    21: (Sport.JUMP_ROPE, SubSport.GENERIC),
    22: (Sport.HIKING, SubSport.GENERIC),
    23: (Sport.FITNESS_EQUIPMENT, SubSport.INDOOR_ROWING),
    24: (Sport.FITNESS_EQUIPMENT, SubSport.GENERIC),  # Indoor Fitness
    27: (Sport.TRAINING, SubSport.YOGA),
    39: (Sport.MULTISPORT, SubSport.GENERIC),
    42: (Sport.SNOWBOARDING, SubSport.GENERIC),
    47: (Sport.CYCLING, SubSport.MOUNTAIN),  # MTB
    49: (Sport.TRAINING, SubSport.STRENGTH_TRAINING),
    70: (Sport.ROCK_CLIMBING, SubSport.GENERIC),
    71: (Sport.GENERIC, SubSport.GENERIC),  # Ballet
    72: (Sport.GENERIC, SubSport.GENERIC),  # Bauchtanz
    73: (Sport.GENERIC, SubSport.GENERIC),  # Squaredance
    74: (Sport.GENERIC, SubSport.GENERIC),  # Street Dance
    75: (Sport.GENERIC, SubSport.GENERIC),  # Turniertanz
    76: (Sport.GENERIC, SubSport.GENERIC),  # Tanzen
    77: (Sport.GENERIC, SubSport.GENERIC),  # Zumba
    78: (Sport.CRICKET, SubSport.GENERIC),  # Cricket
    79: (Sport.BASEBALL, SubSport.GENERIC),  # Baseball
    80: (Sport.GENERIC, SubSport.GENERIC),  # Bowling
    81: (Sport.RACKET, SubSport.SQUASH),  # Squash
    82: (Sport.RUGBY, SubSport.GENERIC),  # Rugby
    85: (Sport.BASKETBALL, SubSport.GENERIC),  # Basketball
    86: (Sport.BASEBALL, SubSport.GENERIC),  # Softball
    87: (Sport.GENERIC, SubSport.GENERIC),  # Gateball
    88: (Sport.VOLLEYBALL, SubSport.GENERIC),
    89: (Sport.RACKET, SubSport.TABLE_TENNIS),
    90: (Sport.HOCKEY, SubSport.GENERIC),  # Hockey
    91: (Sport.TEAM_SPORT, SubSport.GENERIC),  # Handball - no dedicated FIT Sport value
    92: (Sport.RACKET, SubSport.BADMINTON),  # Badminton
    93: (Sport.ARCHERY, SubSport.GENERIC),
    94: (Sport.GENERIC, SubSport.GENERIC),  # equestrian
    96: (Sport.GENERIC, SubSport.GENERIC),  # Karate
    97: (Sport.BOXING, SubSport.GENERIC),  # Boxen
    98: (Sport.GENERIC, SubSport.GENERIC),  # Judo
    99: (Sport.GENERIC, SubSport.GENERIC),  # Ringen
    100: (Sport.GENERIC, SubSport.GENERIC),  # Tai Chi
    101: (Sport.GENERIC, SubSport.GENERIC),  # Muay Thai
    102: (Sport.GENERIC, SubSport.GENERIC),  # Taekwondo
    103: (Sport.GENERIC, SubSport.GENERIC),  # Kampfsport
    104: (Sport.GENERIC, SubSport.GENERIC),  # Kickboxen
    105: (Sport.ALPINE_SKIING, SubSport.RESORT),
    140: (Sport.KAYAKING, SubSport.GENERIC),
    148: (Sport.GENERIC, SubSport.GENERIC),  # Fechten
    178: (Sport.SNOWSHOEING, SubSport.GENERIC),
    223: _WORKOUT_FALLBACK,  # "just movement" in the app
}
_warned_types: set[int] = set()


def _in_range(value, lo: float, hi: float):
    """The ported decoder interpolates gapped samples using integer
    floor-division slopes (see decoder.Interpolate) - over a wide gap
    between two real samples this can under/overshoot into physically
    impossible territory (e.g. negative cadence). FIT encoding then rejects
    the out-of-range value outright, so drop it here instead of crashing."""
    return value if value is not None and lo <= value <= hi else None


def _to_int(value) -> int | None:
    """Zepp's API mixes ints, floats, and numeric strings like "375.0" for
    the same fields across workouts - `int("375.0")` raises, so route
    through `float()` first."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _sport_for(zepp_type) -> tuple[Sport, SubSport]:
    raw_code = _to_int(zepp_type)
    # -1 is not a real Zepp type code; used as the TYPE_MAP/_warned_types key
    # when raw_code is None, so those containers can stay set[int]/dict[int, ...].
    code = raw_code if raw_code is not None else -1
    if code not in TYPE_MAP and code not in _warned_types:
        _warned_types.add(code)
        print(f"warning: unmapped Zepp workout type {raw_code}, exporting as a generic workout", file=sys.stderr)
    return TYPE_MAP.get(code, _WORKOUT_FALLBACK)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _synthetic_records(
    start_ms: int, end_ms: int, avg_heart_rate: int | None, total_distance: float, target_count: int = 60
) -> list[RecordMessage]:
    """Evenly-spaced placeholder records for a workout with no decoded GPS
    track (pool swims, strength training, ...).

    Dreeve's own FIT importer (FitFileParser.php) throws "No FIT 'record'
    messages found" and rejects the file outright if `record` is empty -
    unconditionally, before it even looks at sport/session data. It doesn't
    need GPS, just a non-empty record stream, matching how real Garmin/Wahoo
    pool-swim FIT files carry per-interval records without positions.
    """
    duration_ms = max(end_ms - start_ms, 0)
    step_count = 0 if duration_ms == 0 else max(1, min(target_count, duration_ms // 1000))

    records = []
    for i in range(step_count + 1):
        record = RecordMessage()
        record.timestamp = start_ms + round(i * duration_ms / step_count) if step_count else start_ms
        if avg_heart_rate:
            record.heart_rate = avg_heart_rate
        if total_distance:
            record.distance = total_distance * (i / step_count if step_count else 1.0)
        records.append(record)
    return records


def _fit_cadence(point: ExportablePoint) -> int | None:
    """FIT's `cadence` field for this point, from whichever source the
    workout has - `gait` (walk/run) and `stroke_speed` (swim) are mutually
    exclusive per workout (confirmed live: one is always empty when the
    other has data), and need different conversions:
    - `gait` cadence is total steps/min (both feet); FIT uses the
      single-leg convention (half that) - confirmed live against a real
      Zepp-app FIT export, where our raw decoded avg/max cadence (159/174)
      was almost exactly 2x theirs (79/88).
    - `stroke_speed`-derived `stroke_cadence` is already strokes/minute
      (decoder applies the ×60 from strokes/second) and needs no further
      conversion - confirmed live: our values matched a real Zepp-app FIT
      export's per-record cadence exactly, unlike `gait`'s.
    """
    cadence = _in_range(point.cadence, 1, 255)
    if cadence is not None:
        return round(cadence / 2)
    stroke_cadence = _in_range(point.stroke_cadence, 1, 255)
    return round(stroke_cadence) if stroke_cadence is not None else None


def _point_stats(
    points: list[ExportablePoint], start_ms: int, end_ms: int
) -> tuple[float | None, int | None, int | None, float | None]:
    """`(max_speed, avg_cadence, max_cadence, avg_step_length)` over points
    whose timestamp falls in `[start_ms, end_ms]`. Used for both session-wide
    stats and per-lap stats (same computation, different window) - summary
    only reliably carries `max_heart_rate`/`min_heart_rate`; its
    `avg_cadence`/`max_cadence` are always 0 in practice (confirmed live),
    so those are computed here from decoded points instead."""
    speeds, cadences, step_lengths = [], [], []
    for point in points:
        t = _millis(point.time)
        if not (start_ms <= t <= end_ms):
            continue
        speed = _in_range(point.speed, 0, 50)
        if speed is not None:
            speeds.append(speed)
        cadence = _fit_cadence(point)
        if cadence is not None:
            cadences.append(cadence)
        step_length = _in_range(point.step_length, 1, 3000)
        if step_length is not None:
            step_lengths.append(step_length)

    return (
        max(speeds) if speeds else None,
        round(sum(cadences) / len(cadences)) if cadences else None,
        max(cadences) if cadences else None,
        sum(step_lengths) / len(step_lengths) if step_lengths else None,
    )


def _build_records(points: list[ExportablePoint]) -> tuple[list[RecordMessage], float, float]:
    """Build the record stream for a workout with a decoded GPS track.

    Returns `(records, total_distance, total_work_j)`. Prefers Zepp's own
    per-sample `currentDistance` over a haversine sum of the GPS points when
    it was decoded (device/sensor-fused, more accurate than re-deriving
    distance from raw lat/lon) - but never mixes the two within one workout.
    `total_work_j` is the trapezoidal integral of decoded `power` samples
    over time (joules), for FIT's `total_work` field - 0.0 if no power data.
    """
    have_device_distance = any(p.distance is not None for p in points)

    records = []
    distance_m = 0.0
    total_work_j = 0.0
    prev_point: ExportablePoint | None = None
    for point in points:
        if have_device_distance:
            distance_m = point.distance if point.distance is not None else distance_m
        elif prev_point is not None:
            distance_m += _haversine_m(prev_point.latitude, prev_point.longitude, point.latitude, point.longitude)

        record = RecordMessage()
        record.timestamp = _millis(point.time)
        record.position_lat = point.latitude
        record.position_long = point.longitude
        record.distance = distance_m

        altitude = _in_range(point.altitude, -500, 12000)
        if altitude is not None:
            record.altitude = altitude
        heart_rate = _in_range(point.heart_rate, 1, 255)
        if heart_rate is not None:
            record.heart_rate = int(heart_rate)
        cadence = _fit_cadence(point)
        if cadence is not None:
            record.cadence = cadence
        speed = _in_range(point.speed, 0, 50)  # 50 m/s = 180 km/h, generous upper bound for decode noise
        if speed is not None:
            record.speed = speed
        power = _in_range(point.power, 0, 3000)
        if power is not None:
            record.power = int(power)
            if prev_point is not None and prev_point.power is not None:
                dt = (point.time - prev_point.time).total_seconds()
                total_work_j += (prev_point.power + power) / 2 * dt
        step_length = _in_range(point.step_length, 1, 3000)
        if step_length is not None:
            record.step_length = step_length

        records.append(record)
        prev_point = point

    return records, distance_m, total_work_j


def _apply_point_stats(lap: LapMessage, points: list[ExportablePoint], start_ms: int, end_ms: int) -> None:
    max_speed, avg_cadence, max_cadence, avg_step_length = _point_stats(points, start_ms, end_ms)
    if max_speed is not None:
        lap.max_speed = max_speed
    if avg_cadence is not None:
        lap.avg_cadence = avg_cadence
    if max_cadence is not None:
        lap.max_cadence = max_cadence
    if avg_step_length is not None:
        lap.avg_step_length = avg_step_length


def _build_laps(
    start_ms: int,
    end_ms: int,
    total_distance: float,
    total_elapsed_s: float,
    avg_heart_rate: int | None,
    total_calories: int | None,
    sport: Sport,
    sub_sport: SubSport,
    splits: list[KilometerSplit],
    points: list[ExportablePoint],
) -> list[LapMessage]:
    """One lap per completed kilometer (from `splits`) plus a trailing
    partial lap for the remainder, when `splits` looks consistent with
    `total_distance`; otherwise a single lap spanning the whole workout."""
    # A non-positive duration can't be encoded (FIT's total_elapsed_time is
    # uint32, scaled by 1000, so even -1ms becomes the invalid encoded value
    # -1). parse_kilometer_splits() already drops such splits, but check here
    # too so a caller-supplied split can never crash the whole file.
    splits_valid = (
        bool(splits) and len(splits) == int(total_distance // 1000) and all(s.duration_ms > 0 for s in splits)
    )
    if not splits_valid:
        lap = LapMessage()
        lap.timestamp = end_ms
        lap.start_time = start_ms
        lap.total_elapsed_time = total_elapsed_s
        lap.total_timer_time = total_elapsed_s
        lap.total_distance = total_distance
        if total_calories is not None:
            lap.total_calories = total_calories
        if avg_heart_rate:
            lap.avg_heart_rate = avg_heart_rate
        if total_elapsed_s > 0:
            lap.avg_speed = total_distance / total_elapsed_s
        _apply_point_stats(lap, points, start_ms, end_ms)
        lap.sport = sport
        lap.sub_sport = sub_sport
        return [lap]

    laps = []
    cursor_ms = start_ms
    for split in splits:
        split_end_ms = cursor_ms + split.duration_ms
        lap = LapMessage()
        lap.start_time = cursor_ms
        lap.timestamp = split_end_ms
        lap.total_elapsed_time = split.duration_ms / 1000
        lap.total_timer_time = lap.total_elapsed_time
        lap.total_distance = 1000.0
        if split.duration_ms > 0:
            lap.avg_speed = 1000.0 / (split.duration_ms / 1000)
        if split.avg_heart_rate:
            lap.avg_heart_rate = split.avg_heart_rate
        _apply_point_stats(lap, points, cursor_ms, split_end_ms)
        lap.sport = sport
        lap.sub_sport = sub_sport
        laps.append(lap)
        cursor_ms = split_end_ms

    remaining_distance = max(total_distance - 1000.0 * len(splits), 0.0)
    if remaining_distance > 0 or cursor_ms < end_ms:
        lap = LapMessage()
        lap.start_time = cursor_ms
        lap.timestamp = end_ms
        lap.total_elapsed_time = max((end_ms - cursor_ms) / 1000, 0.0)
        lap.total_timer_time = lap.total_elapsed_time
        lap.total_distance = remaining_distance
        if avg_heart_rate:
            lap.avg_heart_rate = avg_heart_rate
        _apply_point_stats(lap, points, cursor_ms, end_ms)
        lap.sport = sport
        lap.sub_sport = sub_sport
        laps.append(lap)
    return laps


def build_fit(
    summary: dict,
    points: list[ExportablePoint],
    splits: list[KilometerSplit] | None = None,
    device_name: str | None = None,
) -> bytes:
    """Build a FIT activity file's bytes from a workout summary + decoded points.

    `summary` is a raw item from `ZeppDataClient.workouts()`; `points` is the
    (possibly empty, for indoor/strength workouts with no GPS track) output
    of `decoder.parse_points()`. `splits` is `decoder.parse_kilometer_splits()`'s
    output - used for real per-kilometer laps when it looks consistent with
    the workout's total distance, otherwise ignored. `device_name` sets a
    `DeviceInfoMessage` product name - Zepp's API doesn't expose the
    recording device's model anywhere in workout data, so this can only ever
    be a name the caller already knows out-of-band (e.g. from config), never
    auto-detected here.
    """
    zepp_type = summary.get("type")
    sport, sub_sport = _sport_for(zepp_type)

    start_time = datetime.fromtimestamp(int(summary["trackid"]), tz=UTC)
    if points:
        end_time = points[-1].time
    else:
        run_time_s = float(summary.get("run_time") or 0)
        end_time = datetime.fromtimestamp(start_time.timestamp() + run_time_s, tz=UTC)

    start_ms, end_ms = _millis(start_time), _millis(end_time)
    total_elapsed_s = max((end_ms - start_ms) / 1000, 0.0)
    total_calories = _to_int(summary.get("calorie"))
    avg_heart_rate = _to_int(summary.get("avg_heart_rate"))
    max_heart_rate = _to_int(summary.get("max_heart_rate"))
    max_heart_rate = max_heart_rate if max_heart_rate and max_heart_rate > 0 else None
    avg_power = _to_int(summary.get("average_power"))
    avg_power = avg_power if avg_power and avg_power > 0 else None
    max_power = _to_int(summary.get("max_power"))
    max_power = max_power if max_power and max_power > 0 else None
    # avg_cadence/max_cadence in the summary are always 0 in practice
    # (confirmed live) - unlike avg_heart_rate/max_heart_rate, not usable.
    # avg_stride_length is real, but in centimeters like altitude - matches
    # a real Zepp-app FIT export's avg_step_length (millimeters) exactly
    # once ×10'd.
    avg_stride_cm = _to_int(summary.get("avg_stride_length"))
    summary_avg_step_length = avg_stride_cm * 10 if avg_stride_cm and avg_stride_cm > 0 else None

    builder = FitFileBuilder(auto_define=True, min_string_size=50)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.ZEPP.value
    file_id.product = 0
    file_id.time_created = start_ms
    builder.add(file_id)

    if device_name:
        device_info = DeviceInfoMessage()
        device_info.timestamp = start_ms
        device_info.manufacturer = Manufacturer.ZEPP.value
        device_info.product_name = device_name
        builder.add(device_info)

    start_event = EventMessage()
    start_event.event = Event.TIMER
    start_event.event_type = EventType.START
    start_event.timestamp = start_ms
    builder.add(start_event)

    total_work_j = 0.0
    if points:
        records, total_distance, total_work_j = _build_records(points)
    else:
        total_distance = float(summary.get("dis") or 0)
        records = _synthetic_records(start_ms, end_ms, avg_heart_rate, total_distance)
    builder.add_all(records)

    stop_event = EventMessage()
    stop_event.event = Event.TIMER
    stop_event.event_type = EventType.STOP
    stop_event.timestamp = end_ms
    builder.add(stop_event)

    laps = _build_laps(
        start_ms,
        end_ms,
        total_distance,
        total_elapsed_s,
        avg_heart_rate,
        total_calories,
        sport,
        sub_sport,
        splits or [],
        points,
    )
    for lap in laps:
        if avg_power is not None:
            lap.avg_power = avg_power
        if max_power is not None:
            lap.max_power = max_power
        builder.add(lap)

    max_speed, avg_cadence, max_cadence, computed_avg_step_length = _point_stats(points, start_ms, end_ms)
    avg_step_length = summary_avg_step_length if summary_avg_step_length is not None else computed_avg_step_length

    session = SessionMessage()
    session.timestamp = end_ms
    session.start_time = start_ms
    session.total_elapsed_time = total_elapsed_s
    session.total_timer_time = total_elapsed_s
    session.total_distance = total_distance
    if total_elapsed_s > 0:
        session.avg_speed = total_distance / total_elapsed_s
    if max_speed is not None:
        session.max_speed = max_speed
    if total_calories is not None:
        session.total_calories = total_calories
    if avg_heart_rate:
        session.avg_heart_rate = avg_heart_rate
    if max_heart_rate is not None:
        session.max_heart_rate = max_heart_rate
    if avg_cadence is not None:
        session.avg_cadence = avg_cadence
    if max_cadence is not None:
        session.max_cadence = max_cadence
    if avg_step_length is not None:
        session.avg_step_length = avg_step_length
    if avg_power is not None:
        session.avg_power = avg_power
    if max_power is not None:
        session.max_power = max_power
    if total_work_j > 0:
        session.total_work = round(total_work_j)
    session.sport = sport
    session.sub_sport = sub_sport
    session.first_lap_index = 0
    session.num_laps = len(laps)
    builder.add(session)

    activity = ActivityMessage()
    activity.timestamp = end_ms
    activity.total_timer_time = total_elapsed_s
    activity.num_sessions = 1
    activity.type = Activity.MANUAL
    builder.add(activity)

    return builder.build().to_bytes()


def write_fit(
    summary: dict,
    points: list[ExportablePoint],
    output_path: Path,
    splits: list[KilometerSplit] | None = None,
    device_name: str | None = None,
) -> None:
    """Write the FIT file atomically: build to a .tmp file, then rename."""
    data = build_fit(summary, points, splits=splits, device_name=device_name)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    tmp_path.rename(output_path)
