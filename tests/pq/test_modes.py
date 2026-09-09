"""Offline acceptance tests for PQ modes; every database boundary is a fake."""

from __future__ import annotations

import csv
import importlib
import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from select_fuzz.pq import compare, fast, materializer, performance, report
from select_fuzz.pq.config import Endpoint, PQConfig
from select_fuzz.pq.generator import PQGeneratedQuery
from select_fuzz.pq.oracle import CompareOutcome


def config(**changes):
    base = PQConfig(endpoint=Endpoint("127.0.0.1", password="do-not-save-this"),
                    serial_endpoint=Endpoint("127.0.0.1", user="serial_test_user"))
    values = {f.name: getattr(base, f.name) for f in fields(base)}
    values.update(queries_per_round=1, rounds=1, max_regenerations=3, seed=100,
                  materialize_rows=2, materialize_tables=2, perf_repeats=2,
                  perf_warmups=1, perf_dops=(), max_duration_seconds=600.0)
    values.update(changes)
    return NS(**values)


def rows(values=((1,),), *, ms=2, **changes):
    values = tuple(tuple(r) for r in values)
    data = dict(status="OK", rows=values, columns=("x",), type_codes=(3,),
                error="", errno=0, elapsed_ms=ms, elapsed_ns=int(ms * 1_000_000),
                row_count=len(values), total_rows=len(values), complete=True,
                warnings=(), warnings_complete=True, fallback=False)
    data.update(changes)
    return NS(**data)


def differential(sql="SELECT x FROM t0", *, verdict="MATCH", pq=None, seq=None, **changes):
    data = dict(sql=sql, explain_text="Gather: 4 workers, parallel scan on t0",
                seq_explain_text="Table scan on t0", pq_triggered=True,
                pq_result=pq or rows(), seq_result=seq or rows(ms=4),
                outcome=CompareOutcome(verdict, "test evidence"), shape="scan",
                seed=0, decimal_columns=(), skip_reason="",
                plan_dop=4, scan_rows=20000, evidence="plan_confirmed_no_reported_fallback",
                is_mismatch=verdict in {"MISMATCH", "ROW_COUNT", "ERROR_PARITY"})
    data.update(changes)
    return NS(**data)


class FakeConnection:
    def __init__(self):
        self.executed = []
        self.used = []
        self.closed = False
        self.result = rows()

    def execute(self, sql, **kwargs):
        self.executed.append(sql)
        return self.result

    def use_database(self, database):
        self.used.append(database)

    def close(self):
        self.closed = True


class FakeRunner:
    def __init__(self):
        self._pq, self._seq = FakeConnection(), FakeConnection()
        self.closed = False
        self.dop = 4
        self.dops = []
        self.diff_calls = []
        self.executions = []
        self.plans = []
        self.plan_rule = lambda sql, serial, n: (not serial, "")
        self.results = []
        self.scan_rows = 20000

    def explain(self, sql, serial=False):
        self.plans.append((sql, serial, self.dop))
        triggered, error = self.plan_rule(sql, serial, len(self.plans))
        text = (f"Gather: {self.dop} workers, parallel scan on t0" if triggered
                else "Table scan on t0")
        return NS(text=text, triggered=triggered, error=error, dop=self.dop if triggered else 0,
                  scan_rows=self.scan_rows, rows=(), columns=())

    def run_differential(self, sql, *, compare_mode, decimal_columns=(), reverse=False):
        self.diff_calls.append((sql, compare_mode, decimal_columns, reverse, self.dop))
        pq_plan = self.explain(sql)
        seq_plan = self.explain(sql, serial=True)
        if pq_plan.error or seq_plan.error or not pq_plan.triggered or seq_plan.triggered:
            return differential(sql, verdict="INCONCLUSIVE", pq_triggered=False,
                                skip_reason="FRESH_GATE_REJECTED",
                                pq=rows(status="SKIPPED"), seq=rows(status="SKIPPED"))
        self.executions.append((sql, reverse, self.dop))
        item = self.results.pop(0) if self.results else differential(sql)
        result = item(sql, self.dop) if callable(item) else item
        result.sql = sql
        result.explain_text = pq_plan.text
        result.seq_explain_text = seq_plan.text
        result.plan_dop = pq_plan.dop
        result.scan_rows = pq_plan.scan_rows
        return result

    def set_dop(self, dop):
        self.dop = dop
        self.dops.append(dop)

    def use_database(self, database):
        self._pq.use_database(database)
        self._seq.use_database(database)

    def snapshot(self):
        return {"pq": {"session": {"parallel_default_dop": self.dop}},
                "seq": {"session": {"parallel_default_dop": 0}}}

    def close(self):
        self.closed = True
        self._pq.close()
        self._seq.close()


@pytest.fixture
def harness(monkeypatch):
    runner = FakeRunner()
    generated, setups, references = [], [], []
    schema = materializer.build_schema_spec("pq_hardening", table_count=2).schema

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def generate(self, *, seed, shape=None, pq_friendly=False):
            generated.append((seed, shape, pq_friendly))
            return NS(sql=f"SELECT x FROM t0 /* seed={seed} */", shape=shape or "scan",
                      seed=seed, has_order_by=False, compare_mode="multiset", decimal_columns=(0,))

    def setup(conn, **kwargs):
        setups.append((conn, kwargs))
        return schema

    def reference(endpoint, **kwargs):
        conn = FakeConnection()
        references.append((conn, endpoint, kwargs))
        return conn

    def write_setup(cfg, artifacts_dir):
        path = Path(artifacts_dir) / "setup.sql"
        path.write_text("CREATE DATABASE pq_hardening;\nCREATE TABLE t0(x INT);\n"
                        "INSERT INTO t0 VALUES (1), (2);\n", encoding="utf-8")
        (path.parent / "cleanup.sql").write_text("DROP DATABASE pq_hardening;\n")
        return path

    modules = [fast, compare, performance, materializer]
    try:
        modules.append(importlib.import_module("select_fuzz.pq.mode_support"))
    except ModuleNotFoundError:
        pass
    for mod in modules:
        monkeypatch.setattr(mod, "build_runner", lambda cfg: runner, raising=False)
        monkeypatch.setattr(mod, "PQGenerator", Generator, raising=False)
        monkeypatch.setattr(mod, "materialize", setup, raising=False)
        monkeypatch.setattr(mod, "PQConnection", reference, raising=False)
        monkeypatch.setattr(mod, "write_setup", write_setup, raising=False)
    return NS(runner=runner, generated=generated, setups=setups, references=references,
              modules=modules)


def invoke(mode, cfg, path, **kwargs):
    result = {"fast": fast.run_fast, "compare": compare.run_compare,
              "performance": performance.run_performance}[mode](cfg, artifacts_dir=path, **kwargs)
    return result.stats if mode == "fast" else result


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("mode", ["fast", "compare", "performance"])
def test_all_modes_retry_same_shape_with_nonoverlapping_seeds_and_log_zero_successes(
    mode, harness, tmp_path,
):
    harness.runner.plan_rule = lambda *args: (False, "")
    stats = invoke(mode, config(queries_per_round=2), tmp_path)
    assert harness.generated == [(100, None, True), (101, "scan", True), (102, "scan", True),
                                 (103, None, True), (104, "scan", True), (105, "scan", True)]
    assert stats.total_attempts == 6 and stats.regenerated == 4
    assert stats.executed == stats.accepted == stats.compared == 0
    assert stats.errors == 0 and stats.skipped == 6
    assert stats.by_shape == {"scan": 6}
    assert stats.shape_stats["scan"]["skipped"] == 6
    assert not harness.runner.diff_calls
    attempts = jsonl(tmp_path / "attempts.jsonl")
    assert len(attempts) == 6
    assert all(a["pq_friendly"] is True for a in attempts)
    assert all(a["sql"] and a["pq_plan"]["text"] and a["serial_plan"]["text"]
               and a["skip_reason"] for a in attempts)
    assert json.loads((tmp_path / "summary.json").read_text())["stats"]["total_attempts"] == 6


@pytest.mark.parametrize("mode", ["fast", "compare", "performance"])
@pytest.mark.parametrize("gate", ["serial_pq", "explain_error", "fresh_lost"])
def test_plan_rejections_never_count_as_matches(mode, gate, harness, tmp_path):
    def plan(sql, serial, n):
        if gate == "serial_pq":
            return True, ""
        if gate == "explain_error":
            return not serial, "broken explain"
        return (not serial and n <= 2), ""
    harness.runner.plan_rule = plan
    stats = invoke(mode, config(max_regenerations=1), tmp_path)
    assert stats.matches == 0
    assert not harness.runner.executions
    assert not harness.runner._pq.executed
    assert stats.skipped == 1
    assert jsonl(tmp_path / "attempts.jsonl")[0]["skip_reason"]


@pytest.mark.parametrize("mode", ["fast", "compare", "performance"])
def test_setup_failures_close_connections_and_leave_failure_summary(
    mode, harness, monkeypatch, tmp_path,
):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture setup failed")
    for mod in harness.modules:
        monkeypatch.setattr(mod, "materialize", fail)
    with pytest.raises(RuntimeError, match="fixture setup failed"):
        invoke(mode, config(), tmp_path)
    assert harness.runner.closed
    assert "fixture setup failed" in (tmp_path / "summary.json").read_text()
    assert (tmp_path / "attempts.jsonl").exists()


def test_fast_precision_is_observation_and_forwards_decimal_contract(harness, tmp_path):
    harness.runner.results = [differential(verdict="PRECISION_VARIANCE")]
    result = fast.run_fast(config(), artifacts_dir=tmp_path)
    assert not result.bugs
    assert result.stats.precision == result.stats.compared == result.stats.accepted == 1
    assert result.stats.matches == result.stats.mismatches == 0
    assert harness.runner.diff_calls[0][2] == (0,)


@pytest.mark.parametrize("defect", ["fallback", "complete", "warnings_complete"])
def test_fast_rejects_inadmissible_results_even_when_outcome_says_match(
    defect, harness, tmp_path,
):
    bad = rows(**{defect: defect == "fallback"})
    harness.runner.results = [differential(pq=bad)]
    result = fast.run_fast(config(max_regenerations=1), artifacts_dir=tmp_path)
    assert result.stats.matches == 0 and not result.bugs
    assert result.stats.skipped == 1


def test_compare_runs_reference_when_pq_equals_serial_and_keeps_all_relations(harness, tmp_path):
    harness.runner.results = [differential(pq=rows(((2,),)), seq=rows(((2,),)))]
    ref_endpoint = Endpoint("127.0.0.2")
    stats = compare.run_compare(config(), reference_endpoint=ref_endpoint, artifacts_dir=tmp_path)
    ref, _, kwargs = harness.references[0]
    assert ref.executed == [harness.runner.diff_calls[0][0]]
    assert kwargs["reference"] is True and kwargs["dop"] == 0
    assert kwargs["query_timeout_ms"] == 60000
    assert len(harness.setups) == 2
    assert harness.setups[0][1] == harness.setups[1][1]
    assert stats.mismatches == 1 and stats.matches == 0
    finding = stats.findings[0]
    assert finding.attribution == "SHARED_ENGINE_REFERENCE_DIVERGENCE"
    assert {k: v.verdict for k, v in finding.relations.items()} == {
        "pq_serial": "MATCH", "pq_reference": "MISMATCH", "serial_reference": "MISMATCH"}
    assert ref.closed
    assert "SHARED_ENGINE_REFERENCE_DIVERGENCE" in (tmp_path / "findings.jsonl").read_text()


def test_compare_curated_strings_use_multiset_and_limit_requires_explicit_contract(
    harness, tmp_path,
):
    explicit = PQGeneratedQuery("SELECT x FROM t0 ORDER BY x LIMIT 1", True, "exact", "scan", 77)
    stats = compare.run_compare(config(), curated=["SELECT x FROM t0 ORDER BY x",
                                "SELECT x FROM t0 LIMIT 1", explicit], artifacts_dir=tmp_path)
    assert not harness.generated
    assert [(call[0], call[1]) for call in harness.runner.diff_calls] == [
        ("SELECT x FROM t0 ORDER BY x", "multiset"), (explicit.sql, "exact")]
    assert stats.total == 3 and stats.compared == 2 and stats.skipped == 1
    assert "LIMIT" in jsonl(tmp_path / "attempts.jsonl")[1]["skip_reason"]


def test_performance_warmup_excluded_alternates_and_has_no_extra_rowcount_query(harness, tmp_path):
    harness.runner.results = [differential(pq=rows(ms=100), seq=rows(ms=100)),
                              differential(pq=rows(ms=2), seq=rows(ms=8)),
                              differential(pq=rows(ms=4), seq=rows(ms=8))]
    stats = performance.run_performance(config(), artifacts_dir=tmp_path)
    assert not harness.runner._pq.executed and not harness.runner._seq.executed
    assert [c[3] for c in harness.runner.diff_calls] == [False, True, False]
    assert len(stats.records) == 1
    record = stats.records[0]
    assert record.pq_ms == 3 and record.non_pq_ms == 8
    assert record.pq_times_ms == (2, 4) and record.serial_times_ms == (8, 8)
    assert record.scan_rows == 20000 and record.actual_scan_rows is None
    assert record.returned_rows == 1
    with (tmp_path / "pq_performance.csv").open() as fh:
        evidence = next(csv.DictReader(fh))
    assert evidence["explain_text"] and evidence["seq_explain_text"]
    assert evidence["triggered"] == "True" and evidence["requested_dop"] == "4"


@pytest.mark.parametrize("reason", ["MISMATCH", "INCONCLUSIVE", "fallback", "incomplete", "error"])
def test_performance_rejects_entire_candidate_if_any_sample_invalid(reason, harness, tmp_path):
    invalid = differential(verdict=reason if reason.isupper() else "MATCH")
    if reason == "fallback":
        invalid.pq_result.fallback = True
    elif reason == "incomplete":
        invalid.pq_result.complete = False
    elif reason == "error":
        invalid.pq_result.status = "ERROR"
    harness.runner.results = [differential(), invalid]
    stats = performance.run_performance(config(max_regenerations=1), artifacts_dir=tmp_path)
    assert stats.records == []
    assert stats.skipped == 1
    assert jsonl(tmp_path / "attempts.jsonl")[0]["skip_reason"]


def test_performance_accepts_one_preconfigured_dop_and_balances_samples(harness, tmp_path):
    harness.runner.results = [differential(pq=rows(ms=2), seq=rows(ms=10))] * 4
    stats = performance.run_performance(config(perf_dops=(4,), perf_warmups=0, perf_repeats=4),
                                       artifacts_dir=tmp_path)
    assert len({c[0] for c in harness.runner.diff_calls}) == 1
    assert [r.requested_dop for r in stats.records] == [4]
    assert not harness.runner.dops
    assert all("SMALL_DATA_OVERHEAD" not in r.flags for r in stats.records)
    assert [c[3] for c in harness.runner.diff_calls] == [False, True, False, True]


def test_report_uses_actual_parameters_complete_setup_and_candidate_language(harness, tmp_path):
    harness.runner.results = [differential(verdict="MISMATCH", pq=rows(((3,),)),
                                          seq=rows(((1,),)))]
    result = fast.run_fast(config(dop_on=7), artifacts_dir=tmp_path)
    markdown = "\n".join(p.read_text() for p in tmp_path.glob("*.md"))
    assert "candidate" in markdown.lower()
    assert "setup.sql" in markdown and "seed" in markdown.lower()
    assert "Table scan on t0" in markdown
    assert "test evidence" in markdown
    assert "do-not-save-this" not in markdown + (tmp_path / "summary.json").read_text()
    assert "global, SUPER-gated" not in markdown and "parallel_default_dop=ON" not in markdown
    assert "INSERT INTO" in result.setup_sql


def test_existing_artifacts_are_preserved(harness, tmp_path):
    previous = tmp_path / "summary.json"
    previous.write_text("earlier run")
    stats = invoke("fast", config(), tmp_path)
    assert previous.read_text() == "earlier run"
    assert Path(stats.artifacts_dir) != tmp_path
    assert (Path(stats.artifacts_dir) / "summary.json").is_file()


def test_parameters_without_evidence_do_not_invent_global_settings():
    assert "global" not in report.parameters_text().lower()
    assert "parallel_default_dop=4" not in report.parameters_text()


@pytest.mark.parametrize("mode", ["fast", "compare", "performance"])
def test_modes_require_serial_endpoint_before_fixture_creation(mode, harness, tmp_path):
    with pytest.raises(ValueError, match="serial_endpoint"):
        invoke(mode, config(serial_endpoint=None), tmp_path)
    assert not harness.setups
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("same_server", [False, True])
def test_preconfigured_endpoints_materialize_identical_fixture_once_per_server(
    same_server, harness, tmp_path,
):
    serial = Endpoint("127.0.0.1" if same_server else "127.0.0.2", user="serial_test_user")
    invoke("fast", config(serial_endpoint=serial), tmp_path)
    assert [conn for conn, _ in harness.setups] == (
        [harness.runner._pq] if same_server else [harness.runner._pq, harness.runner._seq])
    assert all(setup == harness.setups[0][1] for _, setup in harness.setups)
    assert harness.runner._pq.used == harness.runner._seq.used == ["pq_hardening"]


def test_performance_uses_plan_dop_without_setting_expected_value(harness, tmp_path):
    harness.runner.dop = 8
    stats = performance.run_performance(config(serial_endpoint=Endpoint("127.0.0.2"),
                                              dop_on=4), artifacts_dir=tmp_path)
    assert not harness.runner.dops
    assert stats.records[0].requested_dop == 4
    assert stats.records[0].dop == stats.records[0].planned_dop == 8


def test_performance_rejects_sweep_before_fixture_creation(harness, tmp_path):
    with pytest.raises(ValueError, match="preconfigured"):
        performance.run_performance(config(serial_endpoint=Endpoint("127.0.0.2"),
                                           perf_dops=(2, 8)), artifacts_dir=tmp_path)
    assert not harness.setups
    assert not harness.runner.dops
    assert not list(tmp_path.iterdir())
