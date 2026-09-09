from __future__ import annotations

import re

from select_fuzz.config import NodeRole
from select_fuzz import performance
from select_fuzz.performance import templates as builtin_templates
from select_fuzz.performance.calibration import CostModel
from select_fuzz.performance.models import ScaleKnobs
from select_fuzz.performance.templates import (
    CpuDenseGroupSortTemplate,
    CpuDenseJoinTemplate,
    CpuDenseRangeSortTemplate,
    CpuDenseScanTemplate,
    CpuDenseSetupManifest,
)
from select_fuzz.performance.tree import parse_tree


def test_cost_model_seeds_scale_from_reference_estimated_work() -> None:
    template = CpuDenseScanTemplate(seed=3, case_id="case_3")
    initial = ScaleKnobs()
    plans = {
        role: parse_tree("-> Table scan on cpu_data (cost=1 rows=25000)")
        for role in (NodeRole.BASELINE, NodeRole.CUSTOM_OFF)
    }

    seeded = CostModel(row_cap=50_000_000).seed_scale(template, initial, plans)

    assert seeded.table_rows == 400_000
    assert seeded.scan_rows == 400_000


def test_cpu_dense_template_carries_reproducible_ddl_dml_and_bounded_sql() -> None:
    template = CpuDenseScanTemplate(seed=9, case_id="case_9")
    scale = ScaleKnobs(table_rows=500, scan_rows=400)

    manifest = template.data_manifest(scale)

    assert isinstance(manifest, CpuDenseSetupManifest)
    assert manifest.template_id == template.template_id
    assert manifest.expected_row_count == 500
    assert any(statement.startswith("CREATE TABLE") for statement in manifest.setup_statements)
    assert any(statement.startswith("INSERT INTO") for statement in manifest.setup_statements)
    assert all("cte_max_recursion_depth" not in sql for sql in manifest.setup_statements)
    assert "SUM(" in template.render(scale)
    assert "id <= 400" in template.render(scale)
    assert template.render(scale).endswith("ORDER BY 1")


def test_cpu_dense_templates_only_render_pq_supported_query_shapes() -> None:
    unsupported = re.compile(
        r"\b(?:SHA2|CONCAT|REPEAT|OVER|WINDOW|GROUP_CONCAT|BIT_XOR|DIV)\b",
        re.IGNORECASE,
    )
    scale = ScaleKnobs(table_rows=1000, scan_rows=800, sort_rows=500)
    for name in builtin_templates.__all__:
        if name.endswith("Template"):
            template = getattr(builtin_templates, name)(seed=1, case_id="pq_template")
            assert unsupported.search(template.render(scale)) is None, name


def test_unsupported_window_template_is_not_exported_or_constructible() -> None:
    assert not hasattr(builtin_templates, "CpuDenseWindowTemplate")
    assert not hasattr(performance, "CpuDenseWindowTemplate")


def test_cpu_dense_catalog_consumes_range_join_group_and_sort_knobs() -> None:
    scale = ScaleKnobs(
        table_rows=1000,
        scan_rows=800,
        range_selectivity=0.25,
        join_build_rows=200,
        join_probe_rows=700,
        join_fanout=3,
        aggregate_input_rows=600,
        aggregate_groups=40,
        sort_rows=500,
    )
    templates = (
        CpuDenseRangeSortTemplate(seed=1, case_id="range", initial_scale=scale),
        CpuDenseJoinTemplate(seed=2, case_id="join", initial_scale=scale),
        CpuDenseGroupSortTemplate(seed=3, case_id="group", initial_scale=scale),
    )

    rendered = [template.render(scale) for template in templates]

    assert all("ORDER BY" in sql for sql in rendered)
    assert "250" in rendered[0]
    assert "200" in rendered[1] and "700" in rendered[1]
    assert "40" in rendered[2] and "600" in rendered[2]
