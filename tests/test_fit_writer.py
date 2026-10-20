from datetime import UTC, datetime

from fit_tool.fit_file import FitFile

from dreeve_zepp_connector.decoder import ExportablePoint, KilometerSplit
from dreeve_zepp_connector.fit_writer import TYPE_MAP, build_fit

START_TRACKID = 1_700_000_000


def _messages(data: bytes, name: str):
    return [r.message for r in FitFile.from_bytes(data).records if type(r.message).__name__ == name]


def _session_and_lap(data: bytes):
    messages = [r.message for r in FitFile.from_bytes(data).records]
    session = next(m for m in messages if type(m).__name__ == "SessionMessage")
    lap = next(m for m in messages if type(m).__name__ == "LapMessage")
    return session, lap


def test_build_fit_accepts_decimal_string_summary_fields():
    # Zepp's API returns numeric summary fields as strings like "375.0" for
    # some workouts - a bare int("375.0") raises ValueError.
    summary = {
        "trackid": str(START_TRACKID),
        "type": "1",
        "dis": "5000.0",
        "calorie": "375.0",
        "avg_heart_rate": "152.0",
        "run_time": "1800.0",
    }

    data = build_fit(summary, [])
    session, lap = _session_and_lap(data)

    assert session.total_calories == 375
    assert session.avg_heart_rate == 152
    assert lap.total_calories == 375
    assert lap.avg_heart_rate == 152
    assert session.sport == 1  # Sport.RUNNING, resolved despite "type" being a string


def test_build_fit_omits_altitude_when_entirely_missing():
    points = [
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID + i, tz=UTC),
            latitude=40.0 + i * 0.0001,
            longitude=-73.95,
            altitude=None,
            heart_rate=120,
            cadence=170,
        )
        for i in range(3)
    ]
    summary = {"trackid": str(START_TRACKID), "type": 1, "dis": 100, "calorie": 10, "avg_heart_rate": 120}

    data = build_fit(summary, points)  # must not raise

    records = [r.message for r in FitFile.from_bytes(data).records if type(r.message).__name__ == "RecordMessage"]
    assert len(records) == 3
    assert all(r.altitude is None for r in records)


def test_build_fit_never_emits_generic_generic_sport():
    # Confirmed live: at least some deployed Dreeve versions reject
    # sport=0/sub_sport=0 outright ("Unsupported FIT sport 0 (sub sport 0)"),
    # even though it's a valid FIT combo and Dreeve's current GitHub master
    # has a fallback that would accept it. Never emit that exact pair -
    # covers both an explicitly-mapped generic type and a totally unknown one.
    for zepp_type in (16, 223, 9999):
        summary = {"trackid": str(START_TRACKID), "type": zepp_type, "run_time": "60"}
        session, _ = _session_and_lap(build_fit(summary, []))
        assert (session.sport, session.sub_sport) != (0, 0), f"type {zepp_type} resolved to (0, 0)"


def test_build_fit_emits_synthetic_records_for_trackless_workouts():
    # Dreeve's FitFileParser.php unconditionally rejects any FIT file with
    # zero `record` messages, regardless of how complete the Session/Lap
    # summary data is - confirmed against its source. Pool swims, strength
    # training, etc. have no decoded GPS track, so build_fit must synthesize
    # a placeholder record stream rather than leaving `record` empty.
    summary = {
        "trackid": str(START_TRACKID),
        "type": 14,  # pool swim
        "dis": "1000.0",
        "calorie": "300.0",
        "avg_heart_rate": "130.0",
        "run_time": "1800.0",
    }

    data = build_fit(summary, [])

    records = [r.message for r in FitFile.from_bytes(data).records if type(r.message).__name__ == "RecordMessage"]
    assert len(records) > 1
    assert records[0].timestamp == START_TRACKID * 1000
    assert records[-1].timestamp == (START_TRACKID + 1800) * 1000
    assert all(r.heart_rate == 130 for r in records)
    assert records[0].distance == 0
    assert records[-1].distance == 1000.0
    assert all(r.position_lat is None for r in records)


def test_build_fit_drops_out_of_range_interpolated_values():
    # decoder's floor-division interpolation can under/overshoot into
    # physically impossible values across a wide gap between real samples
    # (seen live: cadence of -46). FIT's uint8/uint16 encoding would raise on
    # these; build_fit must drop just the offending field, not the point.
    point = ExportablePoint(
        time=datetime.fromtimestamp(START_TRACKID, tz=UTC),
        latitude=40.0,
        longitude=-73.95,
        altitude=-20000.0,  # NO_VALUE sentinel leaking through unscaled
        heart_rate=-46,
        cadence=-46,
    )
    summary = {"trackid": str(START_TRACKID), "type": 1, "dis": 0, "calorie": 0, "avg_heart_rate": 0}

    data = build_fit(summary, [point])  # must not raise

    record = next(r.message for r in FitFile.from_bytes(data).records if type(r.message).__name__ == "RecordMessage")
    assert record.altitude is None
    assert record.heart_rate is None
    assert record.cadence is None


def test_build_fit_prefers_decoded_distance_over_haversine():
    # Points ~0.001 deg apart (~111m via haversine) but the decoder reports
    # real, sensor-fused distance - that value must win, not get silently
    # replaced by a recomputed haversine sum.
    points = [
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID, tz=UTC),
            latitude=40.0,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=120,
            cadence=170,
            distance=0.0,
        ),
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID + 60, tz=UTC),
            latitude=40.001,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=150,
            cadence=170,
            distance=500.0,
        ),
    ]
    summary = {"trackid": str(START_TRACKID), "type": 1, "dis": 500, "calorie": 50, "avg_heart_rate": 135}

    data = build_fit(summary, points)
    records = _messages(data, "RecordMessage")
    session, _ = _session_and_lap(data)

    assert records[-1].distance == 500.0  # decoded value, not the ~111m haversine sum
    assert session.total_distance == 500.0


def test_build_fit_decodes_speed_power_and_computes_total_work():
    points = [
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID + i, tz=UTC),
            latitude=40.0,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=120,
            cadence=80,
            speed=5.0,
            distance=float(i * 5),
            power=200,
        )
        for i in range(11)  # 10 seconds at a constant 200W
    ]
    summary = {
        "trackid": str(START_TRACKID),
        "type": 1,
        "dis": 50,
        "calorie": 10,
        "avg_heart_rate": 120,
        "average_power": 200,
        "max_power": 200,
    }

    data = build_fit(summary, points)
    session, _ = _session_and_lap(data)
    records = _messages(data, "RecordMessage")

    assert all(r.power == 200 for r in records)
    assert all(round(r.speed, 3) == 5.0 for r in records)
    assert session.avg_power == 200
    assert session.max_power == 200
    # Trapezoidal integral of a constant 200W over 10s = 2000 J.
    assert session.total_work == 2000


def test_build_fit_uses_stroke_cadence_unhalved_for_swims():
    # Unlike gait cadence (halved for FIT's single-leg convention), swim
    # stroke_cadence is already strokes/minute and must pass through as-is -
    # confirmed live against a real Zepp-app FIT export.
    points = [
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID + i, tz=UTC),
            latitude=40.0,
            longitude=-73.95,
            altitude=None,
            heart_rate=120,
            cadence=None,
            stroke_cadence=24.0,
        )
        for i in range(3)
    ]
    summary = {"trackid": str(START_TRACKID), "type": 15, "dis": 100, "calorie": 10, "avg_heart_rate": 120}

    data = build_fit(summary, points)
    records = _messages(data, "RecordMessage")
    session, _ = _session_and_lap(data)

    assert all(r.cadence == 24 for r in records)
    assert session.avg_cadence == 24
    assert session.max_cadence == 24


def test_build_fit_omits_power_fields_when_no_power_data():
    # Confirmed live: cycling workouts without a paired power meter report
    # average_power/max_power as -1 (sentinel), not missing/None - must not
    # be emitted as literal -1 watts.
    summary = {
        "trackid": str(START_TRACKID),
        "type": 9,
        "dis": 0,
        "calorie": 0,
        "avg_heart_rate": 0,
        "average_power": -1,
        "max_power": -1,
        "run_time": "60",
    }

    data = build_fit(summary, [])
    session, lap = _session_and_lap(data)

    assert session.avg_power is None
    assert session.max_power is None
    assert session.total_work is None
    assert lap.avg_power is None


def test_build_fit_builds_per_kilometer_laps_when_splits_consistent():
    points = [
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID, tz=UTC),
            latitude=40.0,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=120,
            cadence=170,
            distance=0.0,
        ),
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID + 600, tz=UTC),
            latitude=40.02,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=150,
            cadence=170,
            distance=2500.0,
        ),
    ]
    splits = [
        KilometerSplit(index=0, duration_ms=300_000, avg_heart_rate=140),
        KilometerSplit(index=1, duration_ms=250_000, avg_heart_rate=145),
    ]
    summary = {"trackid": str(START_TRACKID), "type": 1, "dis": 2500, "calorie": 200, "avg_heart_rate": 135}

    data = build_fit(summary, points, splits=splits)
    laps = _messages(data, "LapMessage")
    session, _ = _session_and_lap(data)

    # 2 full-km splits (2000m) + one trailing partial lap for the remaining 500m.
    assert len(laps) == 3
    assert laps[0].total_distance == 1000.0
    assert laps[0].total_elapsed_time == 300.0
    assert laps[0].avg_heart_rate == 140
    assert laps[1].total_distance == 1000.0
    assert laps[1].total_elapsed_time == 250.0
    assert laps[2].total_distance == 500.0
    assert laps[2].total_elapsed_time == 50.0
    assert session.num_laps == 3


def test_build_fit_falls_back_to_single_lap_when_splits_inconsistent():
    # Split count not matching floor(total_distance / 1000) means the
    # kilo_pace data doesn't correspond to this workout's distance - fall
    # back to one whole-workout lap rather than emit misleading splits.
    points = [
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID, tz=UTC),
            latitude=40.0,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=120,
            cadence=170,
            distance=0.0,
        ),
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID + 600, tz=UTC),
            latitude=40.02,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=150,
            cadence=170,
            distance=2500.0,
        ),
    ]
    splits = [KilometerSplit(index=i, duration_ms=100_000, avg_heart_rate=140) for i in range(5)]
    summary = {"trackid": str(START_TRACKID), "type": 1, "dis": 2500, "calorie": 200, "avg_heart_rate": 135}

    data = build_fit(summary, points, splits=splits)
    laps = _messages(data, "LapMessage")

    assert len(laps) == 1
    assert laps[0].total_distance == 2500.0


def test_build_fit_falls_back_to_single_lap_when_split_duration_invalid():
    # Workout 1764847391 failed with "total_elapsed_time encoded value -1 is
    # not in valid range", which is what a -1ms split duration encodes to.
    # A bad split must fall back to one whole-workout lap, not fail the
    # whole export.
    points = [
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID, tz=UTC),
            latitude=40.0,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=120,
            cadence=170,
            distance=0.0,
        ),
        ExportablePoint(
            time=datetime.fromtimestamp(START_TRACKID + 600, tz=UTC),
            latitude=40.02,
            longitude=-73.95,
            altitude=10.0,
            heart_rate=150,
            cadence=170,
            distance=1500.0,
        ),
    ]
    splits = [KilometerSplit(index=0, duration_ms=-1, avg_heart_rate=140)]
    summary = {"trackid": str(START_TRACKID), "type": 1, "dis": 1500, "calorie": 100, "avg_heart_rate": 135}

    data = build_fit(summary, points, splits=splits)
    laps = _messages(data, "LapMessage")

    assert len(laps) == 1
    assert laps[0].total_distance == 1500.0
    assert laps[0].total_elapsed_time == 600.0


def test_build_fit_emits_device_info_only_when_name_given():
    # Zepp's API doesn't expose the recording device's model anywhere in
    # workout data - device_name can only ever be a caller-supplied value,
    # never auto-detected, so it must be entirely opt-in.
    summary = {"trackid": str(START_TRACKID), "type": 1, "dis": 0, "calorie": 0, "avg_heart_rate": 0, "run_time": "60"}

    data_without = build_fit(summary, [])
    data_with = build_fit(summary, [], device_name="My Watch")

    assert _messages(data_without, "DeviceInfoMessage") == []
    device_infos = _messages(data_with, "DeviceInfoMessage")
    assert len(device_infos) == 1
    assert device_infos[0].product_name == "My Watch"


def test_type_map_covers_every_code_with_valid_fit_tool_enum_members():
    # Every (Sport, SubSport) pair in TYPE_MAP is evaluated at *module import
    # time* (it's a dict literal), so a typo'd enum member (e.g.
    # `Sport.FIELD_HOCKEY` - not a real fit-tool value, `Sport.HOCKEY` is)
    # crashes every import of this module, not just the workout that would
    # have used it. This test additionally exercises build_fit() for each
    # code, matching the manual smoke test described in CLAUDE.md's
    # Verification section, to catch a valid-but-wrong mapping too.
    for zepp_type in TYPE_MAP:
        summary = {
            "trackid": str(START_TRACKID),
            "type": zepp_type,
            "dis": 1000,
            "calorie": 100,
            "avg_heart_rate": 120,
            "run_time": "600",
        }
        build_fit(summary, [])
