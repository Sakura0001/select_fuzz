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


def test_same_stage_update_preserves_original_entry_time_and_worker_group() -> None:
    current = [100]
    telemetry = FuzzStageTelemetry(clock_ns=lambda: current[0])

    telemetry.set_stage("db0:reader-primary:0", "waiting_for_generated_sql")
    current[0] = 150
    telemetry.set_stage("db0:reader-primary:0", "waiting_for_generated_sql")
    current[0] = 200

    snapshot = telemetry.snapshot()

    assert snapshot["stage_details"] == {
        "waiting_for_generated_sql": {
            "count": 1,
            "max_age_ns": 100,
            "oldest_workers": (
                {
                    "worker": "db0:reader-primary:0",
                    "age_ns": 100,
                },
            ),
        }
    }
    assert snapshot["worker_groups"] == {
        "reader_primary": {"waiting_for_generated_sql": 1}
    }


def test_stage_change_resets_age_and_oldest_workers_are_bounded() -> None:
    current = [100]
    telemetry = FuzzStageTelemetry(clock_ns=lambda: current[0])
    for worker_id in range(5):
        current[0] += 10
        telemetry.set_stage(
            f"db0:reader-replica:{worker_id}",
            "reader_executing",
        )
    current[0] = 200
    telemetry.set_stage("db0:reader-replica:0", "reader_fetching")
    current[0] = 250

    snapshot = telemetry.snapshot()

    executing = snapshot["stage_details"]["reader_executing"]
    assert executing["count"] == 4
    assert executing["max_age_ns"] == 130
    assert len(executing["oldest_workers"]) == 3
    assert snapshot["stage_details"]["reader_fetching"]["max_age_ns"] == 50
