import json
from datetime import UTC, datetime
from pathlib import Path

from dreeve_zepp_connector.decoder import parse_kilometer_splits, parse_points

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_workout_detail.json").read_text())
START_TIME = 1_700_000_000


def test_parse_points_decodes_all_channels():
    points = parse_points(START_TIME, FIXTURE)

    assert len(points) == 3
    assert [p.time for p in points] == [datetime.fromtimestamp(START_TIME + offset, tz=UTC) for offset in (0, 10, 20)]
    assert [round(p.latitude, 8) for p in points] == [40.0, 40.00001, 40.00002]
    assert [round(p.longitude, 8) for p in points] == [-73.95, -73.94999, -73.94998]
    assert [round(p.altitude, 2) for p in points] == [10.0, 11.0, 12.0]
    assert [p.heart_rate for p in points] == [120, 130, 150]
    assert [p.cadence for p in points] == [170, 180, 160]
    assert [round(p.speed, 5) for p in points] == [2.5, 3.0, 3.5]
    # currentDistance's value column is centimeters, like altitude - must be
    # divided by 100 to get meters (confirmed live: a real 20km ride decoded
    # as 2017km without this conversion).
    assert [round(p.distance, 2) for p in points] == [0.0, 30.0, 60.0]
    assert [p.power for p in points] == [100, 150, 120]
    # gait's stride column is centimeters too, like altitude/currentDistance -
    # step_length converts to millimeters (confirmed live against a real
    # Zepp-app FIT export's step_length/avg_step_length fields). Middle/last
    # values reflect the ported Interpolate class's integer floor-division
    # slopes (see the "Interpolation artifacts" quirk) - the fixture's raw
    # stride deltas (80->82->81) don't divide evenly by the 10s timestep,
    # unlike altitude's (1000->1100->1200), so this isn't a clean passthrough.
    assert [p.step_length for p in points] == [800.0, 800.0, 720.0]


def test_parse_points_reports_none_for_channels_entirely_absent():
    # speed/currentDistance/power_meter are often just missing altogether
    # (e.g. older workouts, devices without the sensor) - interpolating an
    # empty channel would otherwise fabricate zeros across every point.
    detail = dict(FIXTURE)
    del detail["speed"]
    del detail["currentDistance"]
    del detail["power_meter"]

    points = parse_points(START_TIME, detail)

    assert all(p.speed is None for p in points)
    assert all(p.distance is None for p in points)
    assert all(p.power is None for p in points)


def test_parse_points_reports_none_cadence_and_step_length_when_gait_absent():
    # Real for swims: no gait/stride sensor data at all. Must decode to
    # None, not the fabricated 0 that an empty channel interpolates to.
    detail = dict(FIXTURE)
    del detail["gait"]

    points = parse_points(START_TIME, detail)

    assert all(p.cadence is None for p in points)
    assert all(p.step_length is None for p in points)


def test_parse_points_decodes_swim_stroke_cadence_when_gait_absent():
    # stroke_speed (swim stroke rate, strokes/sec) is mutually exclusive
    # with gait (footpod cadence) in practice - confirmed live: one is
    # always empty when the other has data. Needs x60 for strokes/minute -
    # confirmed live against a real Zepp-app FIT export's per-record
    # cadence, which our decoded x60 values matched exactly.
    detail = dict(FIXTURE)
    del detail["gait"]
    detail["stroke_speed"] = "0,0.20000;10,0.30000;10,0.40000"

    points = parse_points(START_TIME, detail)

    assert all(p.cadence is None for p in points)
    assert [round(p.stroke_cadence, 2) for p in points] == [12.0, 18.0, 24.0]


def test_parse_points_returns_empty_for_trackless_workout():
    assert parse_points(START_TIME, {}) == []
    assert parse_points(START_TIME, {"longitude_latitude": ""}) == []


def test_parse_points_treats_all_missing_altitude_as_none():
    # Some workouts (no barometer reading, certain indoor-with-GPS modes)
    # report NO_VALUE (-2000000) for every altitude sample. Real accounts hit
    # this - it must decode to `None`, not the literal sentinel scaled to
    # -20000.0 meters.
    detail = dict(FIXTURE)
    detail["altitude"] = "-2000000;-2000000;-2000000"

    points = parse_points(START_TIME, detail)

    assert [p.altitude for p in points] == [None, None, None]


# Real (anonymized-nothing-sensitive: just a geohash grid cell and timing/HR
# numbers) `kilo_pace` entries from a live account, captured 2026-08-27 while
# reverse-engineering the field - see parse_kilometer_splits()'s docstring.
REAL_KILO_PACE = (
    "0,177,ez1z787g2p,-1,110,177,177763,20,0,0,12,14,0,0,0;"
    "1,167,ez1z72k61c,-1,132,345,167711,26,0,0,11,17,0,0,0;"
    "2,166,ez1z5pv84e,-1,120,511,166505,22,0,0,1,7,0,0,0"
)


def test_parse_kilometer_splits_decodes_real_sample():
    splits = parse_kilometer_splits({"kilo_pace": REAL_KILO_PACE})

    assert [s.index for s in splits] == [0, 1, 2]
    assert [s.duration_ms for s in splits] == [177763, 167711, 166505]
    assert [s.avg_heart_rate for s in splits] == [110, 132, 120]


def test_parse_kilometer_splits_rejects_non_positive_duration():
    # Some workouts use -1 as a sentinel in the duration column. Encoding
    # that as a lap gives total_elapsed_time = -0.001s, which fit_tool
    # rejects ("encoded value -1 is not in valid range") - fall back to a
    # single whole-workout lap rather than crashing the export.
    assert parse_kilometer_splits({"kilo_pace": "0,177,ez1z787g2p,-1,110,177,-1,20,0,0,12,14,0,0,0"}) == []
    assert parse_kilometer_splits({"kilo_pace": "0,0,ez1z787g2p,-1,110,0,0,20,0,0,12,14,0,0,0"}) == []


def test_parse_kilometer_splits_returns_empty_when_absent():
    assert parse_kilometer_splits({}) == []
    assert parse_kilometer_splits({"kilo_pace": ""}) == []


def test_parse_kilometer_splits_bails_out_on_unexpected_shape():
    # A parse failure must fall back to a single whole-workout lap, not
    # guess at a field layout that doesn't match what was confirmed live.
    assert parse_kilometer_splits({"kilo_pace": "0,177,ez1z787g2p"}) == []  # too few fields
    assert parse_kilometer_splits({"kilo_pace": "not,a,number,at,all,here,either"}) == []
