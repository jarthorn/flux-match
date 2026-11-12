import csv
import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from flux_match import paths
from flux_match.config import dump_toml
from flux_match.errors import DataError, RunStateError
from flux_match.viz.batch_summary import (
    INVARIANTS_FILENAME,
    SUMMARY_FILENAME,
    batch_dir_for,
    flatten_config,
    summary_stats,
    write_batch_summary,
)

BASE_CONFIG = {
    "fetch": {"demand_years": [2023, 2024]},
    "selection": {
        "wind_capacity_ratio": 1,
        "strategy": "greedy",
        "greedy": {"ranking_metric": "annual_capacity"},
    },
    "dispatch": {"bess_rte": 0.88, "bess_duration": 4},
}


def dispatch_stats(
    peaks: dict[str, int] | None = None, curtailment_total: float | None = 12.5
) -> dict[str, Any]:
    return {
        "net_demand_pct": {"A.a": 40.0, "total": 41.5},
        "curtailment_pct": {"A.a": curtailment_total, "total": curtailment_total},
        "satisfied_demand_hours_pct": {"A.a": 3.0, "total": 2.5},
        "peak_net_demand_mw": peaks or {"A.a": 100, "B.a": 300, "C.a": 200},
    }


class BatchSummaryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name)
        self.batch_dir = self.output / "batch"
        self.batch_dir.mkdir()

    def add_run(self, index: int, config: dict[str, Any], **manifest: Any) -> Path:
        run_dir = self.batch_dir / f"{index:04d}"
        run_dir.mkdir()
        paths.config_path(run_dir).write_text(dump_toml(config))
        manifest = {
            "batch_name": "batch",
            "batch_index": index,
            "started": f"2026-11-05T08:0{index}:00",
            "completed_steps": ["select", "preprocess", "dispatch"],
            "dispatch_stats": dispatch_stats(),
            **manifest,
        }
        paths.manifest_path(run_dir).write_text(json.dumps(manifest))
        return run_dir

    def with_overrides(self, **overrides: Any) -> dict[str, Any]:
        config = json.loads(json.dumps(BASE_CONFIG))
        for dotted, value in overrides.items():
            *parents, leaf = dotted.split("__")
            table = config
            for part in parents:
                table = table[part]
            table[leaf] = value
        return config

    def summarize(self) -> tuple[list[dict[str, str]], list[str], dict[str, Any]]:
        write_batch_summary(self.batch_dir)
        with (self.batch_dir / SUMMARY_FILENAME).open(newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            header = list(reader.fieldnames or [])
        invariants = tomllib.loads((self.batch_dir / INVARIANTS_FILENAME).read_text())
        return rows, header, invariants

    def test_varying_values_in_csv_and_invariants_in_toml(self):
        # Created out of order, to check rows are sorted by batch_index.
        self.add_run(2, self.with_overrides(selection__wind_capacity_ratio=2))
        self.add_run(1, self.with_overrides())
        rows, header, invariants = self.summarize()
        self.assertEqual(
            header,
            [
                "batch_index",
                "started",
                "selection.wind_capacity_ratio",
                "net_demand_pct",
                "curtailment_pct",
                "satisfied_demand_hours_pct",
                "peak_net_demand_mw",
                "peak_net_demand_ba",
            ],
        )
        self.assertEqual([r["batch_index"] for r in rows], ["1", "2"])
        self.assertEqual([r["selection.wind_capacity_ratio"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["started"], "2026-11-05T08:01:00")
        self.assertEqual(rows[0]["net_demand_pct"], "41.5")
        self.assertEqual(rows[0]["peak_net_demand_mw"], "300")
        self.assertEqual(rows[0]["peak_net_demand_ba"], "B.a")
        expected = json.loads(json.dumps(BASE_CONFIG))
        del expected["selection"]["wind_capacity_ratio"]
        self.assertEqual(invariants, expected)

    def test_every_config_value_in_exactly_one_output(self):
        self.add_run(1, self.with_overrides())
        self.add_run(2, self.with_overrides(dispatch__bess_rte=0.9, fetch__demand_years=[2024]))
        rows, header, invariants = self.summarize()
        csv_keys = set(header) & set(flatten_config(BASE_CONFIG))
        toml_keys = set(flatten_config(invariants))
        self.assertEqual(csv_keys, {"dispatch.bess_rte", "fetch.demand_years"})
        self.assertFalse(csv_keys & toml_keys)
        self.assertEqual(csv_keys | toml_keys, set(flatten_config(BASE_CONFIG)))
        self.assertEqual([r["fetch.demand_years"] for r in rows], ["[ 2023, 2024 ]", "[ 2024 ]"])

    def test_int_and_float_of_same_value_are_invariant(self):
        self.add_run(1, self.with_overrides(dispatch__bess_duration=4, fetch__demand_years=[2023]))
        self.add_run(
            2, self.with_overrides(dispatch__bess_duration=4.0, fetch__demand_years=[2023.0])
        )
        _, header, invariants = self.summarize()
        self.assertNotIn("dispatch.bess_duration", header)
        self.assertNotIn("fetch.demand_years", header)
        self.assertEqual(invariants["dispatch"]["bess_duration"], 4)
        self.assertIs(type(invariants["dispatch"]["bess_duration"]), int)
        self.assertEqual(invariants["fetch"]["demand_years"], [2023])

    def test_bool_and_number_vary(self):
        self.add_run(1, self.with_overrides(dispatch__bess_duration=1))
        self.add_run(2, self.with_overrides(dispatch__bess_duration=True))
        rows, _, invariants = self.summarize()
        self.assertNotIn("bess_duration", invariants["dispatch"])
        self.assertEqual([r["dispatch.bess_duration"] for r in rows], ["1", "true"])

    def test_key_missing_from_some_runs_varies_and_is_blank(self):
        cluster = self.with_overrides(selection__strategy="cluster")
        del cluster["selection"]["greedy"]
        cluster["selection"]["cluster"] = {"cluster_count": 3}
        self.add_run(1, self.with_overrides())
        self.add_run(2, cluster)
        rows, _, invariants = self.summarize()
        self.assertNotIn("greedy", invariants["selection"])
        self.assertNotIn("cluster", invariants["selection"])
        self.assertEqual(rows[0]["selection.greedy.ranking_metric"], "annual_capacity")
        self.assertEqual(rows[1]["selection.greedy.ranking_metric"], "")
        self.assertEqual(rows[0]["selection.cluster.cluster_count"], "")
        self.assertEqual(rows[1]["selection.strategy"], "cluster")

    def test_single_run_has_everything_invariant(self):
        self.add_run(1, self.with_overrides())
        rows, header, invariants = self.summarize()
        self.assertEqual(len(rows), 1)
        self.assertEqual(invariants, BASE_CONFIG)
        self.assertNotIn("selection.strategy", header)

    def test_rerun_replaces_outputs_and_ignores_them_as_inputs(self):
        self.add_run(1, self.with_overrides())
        self.summarize()
        self.add_run(2, self.with_overrides(selection__wind_capacity_ratio=2))
        rows, _, invariants = self.summarize()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("wind_capacity_ratio", invariants["selection"])

    def test_bad_runs_are_all_reported_and_nothing_written(self):
        self.add_run(1, self.with_overrides())
        self.add_run(2, self.with_overrides(), completed_steps=["select", "preprocess"])
        paths.manifest_path(self.add_run(3, self.with_overrides())).unlink()
        self.add_run(4, self.with_overrides(), batch_index=5)
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaisesRegex(DataError, "3 of 4 run folders"):
            write_batch_summary(self.batch_dir)
        self.assertIn("0002: the dispatch step has not completed", stderr.getvalue())
        self.assertIn("0003: missing run.json", stderr.getvalue())
        self.assertIn("0004: batch_index 5 does not match", stderr.getvalue())
        self.assertFalse((self.batch_dir / SUMMARY_FILENAME).exists())
        self.assertFalse((self.batch_dir / INVARIANTS_FILENAME).exists())

    def test_empty_batch_fails(self):
        with self.assertRaisesRegex(DataError, "No run folders"):
            write_batch_summary(self.batch_dir)

    def test_batch_folder_must_exist(self):
        self.assertEqual(batch_dir_for("batch", self.output), self.batch_dir)
        with self.assertRaisesRegex(RunStateError, "Batch folder not found"):
            batch_dir_for("nope", self.output)


class SummaryStatsTest(unittest.TestCase):
    def test_null_curtailment_is_zero(self):
        self.assertEqual(
            summary_stats(dispatch_stats(curtailment_total=None))["curtailment_pct"], 0.0
        )

    def test_peak_tie_takes_first_area(self):
        stats = summary_stats(dispatch_stats(peaks={"A.a": 5, "B.a": 7, "C.a": 7}))
        self.assertEqual((stats["peak_net_demand_ba"], stats["peak_net_demand_mw"]), ("B.a", 7))

    def test_null_in_other_totals_fails(self):
        stats = dispatch_stats()
        stats["net_demand_pct"]["total"] = None
        with self.assertRaisesRegex(DataError, r"net_demand_pct\.total must be a number"):
            summary_stats(stats)

    def test_missing_total_fails(self):
        stats = dispatch_stats()
        del stats["satisfied_demand_hours_pct"]["total"]
        with self.assertRaisesRegex(DataError, r"satisfied_demand_hours_pct\.total is missing"):
            summary_stats(stats)

    def test_empty_peaks_fail(self):
        stats = dispatch_stats()
        stats["peak_net_demand_mw"] = {}
        with self.assertRaisesRegex(DataError, "peak_net_demand_mw is missing or empty"):
            summary_stats(stats)


if __name__ == "__main__":
    unittest.main()
