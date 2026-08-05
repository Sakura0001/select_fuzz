from select_fuzz.modes.fuzz.telemetry import FuzzStageTelemetry


def test_stage_telemetry_tracks_current_workers_and_bounded_duration_aggregates() -> None:
    telemetry = FuzzStageTelemetry()

    telemetry.set_stage("db0:reader-primary:0", "waiting_for_generated_sql")
    telemetry.set_stage("db0:reader-replica:1", "fetching")
    telemetry.observe("generation_wait_ns", 10)
    telemetry.observe("generation_wait_ns", 30)
    telemetry.observe("read_fetch_ns", 7)

    snapshot = telemetry.snapshot()

    assert snapshot["stages"] == {
        "fetching": 1,
        "waiting_for_generated_sql": 1,
    }
    assert snapshot["durations"]["generation_wait_ns"] == {
        "count": 2,
        "total_ns": 40,
        "max_ns": 30,
    }
    assert snapshot["durations"]["read_fetch_ns"]["count"] == 1

    telemetry.remove_worker("db0:reader-primary:0")
    assert telemetry.snapshot()["stages"] == {"fetching": 1}
