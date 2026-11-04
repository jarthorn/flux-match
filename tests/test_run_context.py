import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from flux_match.config import Config
from flux_match.errors import ConfigError, RunStateError
from flux_match.run_context import (
    CONFIG_NAME,
    MANIFEST_NAME,
    create_run,
    latest_run_dir,
    open_run,
    run_dir_timestamp,
)

STARTED = datetime(2026, 10, 27, 9, 5)


class RunContextTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.output = self.root / "output"
        self.scenario = self.root / "greedy.toml"
        self.scenario.write_text('[selection]\nstrategy = "greedy"\n')

    def test_run_folder_named_by_timestamp_and_scenario_with_config_snapshot(self):
        ctx = create_run(self.scenario, self.output, STARTED)
        self.assertEqual(ctx.run_dir, self.output / "20261027-0905-greedy")
        # Snapshot is the resolved defaults + scenario merge, not a raw copy of the file.
        self.assertEqual(
            (ctx.run_dir / CONFIG_NAME).read_text(), Config.load(self.scenario).to_toml()
        )
        self.assertEqual(ctx.config.section("selection").get("strategy", str), "greedy")
        self.assertIn("AB", ctx.config.section("fetch").get_list("provinces", str))

    def test_overrides_are_in_config_snapshot_and_manifest(self):
        overrides = {"dispatch.bess_rte": 0.75, "fetch.provinces": ["ON"]}
        ctx = create_run(self.scenario, self.output, STARTED, overrides=overrides)
        snapshot = Config.load(ctx.run_dir / CONFIG_NAME, inherit_defaults=False)
        self.assertEqual(snapshot.section("dispatch").get("bess_rte", float), 0.75)
        self.assertEqual(snapshot.section("fetch").get_list("provinces", str), ["ON"])
        manifest = json.loads((ctx.run_dir / MANIFEST_NAME).read_text())
        self.assertEqual(manifest["config_overrides"], overrides)

    def test_invalid_override_creates_no_run_folder(self):
        with self.assertRaises(ConfigError):
            create_run(self.scenario, self.output, STARTED, overrides={"dispatch.nope": 1})
        self.assertFalse(self.output.exists())

    def test_existing_run_folder_is_never_reused(self):
        create_run(self.scenario, self.output, STARTED)
        with self.assertRaises(RunStateError):
            create_run(self.scenario, self.output, STARTED)

    def test_batch_run_folder_includes_sequence_and_skips_existing(self):
        first = create_run(self.scenario, self.output, STARTED, batch=True)
        self.assertEqual(first.run_dir, self.output / "20261027-0905-0001-greedy")
        # A non-batch folder or a hole in the sequence must not block later batch runs.
        (self.output / "20261027-0905-greedy").mkdir()
        (self.output / "20261027-0905-0003-greedy").mkdir()
        second = create_run(self.scenario, self.output, STARTED, batch=True)
        self.assertEqual(second.run_dir, self.output / "20261027-0905-0002-greedy")
        third = create_run(self.scenario, self.output, STARTED, batch=True)
        self.assertEqual(third.run_dir, self.output / "20261027-0905-0004-greedy")

    def test_invalid_scenario_creates_no_run_folder(self):
        self.scenario.write_text("not = [valid")
        with self.assertRaises(ConfigError):
            create_run(self.scenario, self.output, STARTED)
        self.assertFalse(self.output.exists())

    def test_step_requires_previous_step(self):
        ctx = create_run(self.scenario, self.output, STARTED)
        with self.assertRaisesRegex(RunStateError, "'select'"):
            ctx.begin_step("preprocess")

    def test_rerunning_a_step_invalidates_later_steps(self):
        ctx = create_run(self.scenario, self.output, STARTED)
        for step in ("select", "preprocess", "dispatch"):
            ctx.begin_step(step)
            ctx.complete_step(step)

        ctx = open_run(ctx.run_dir)
        ctx.begin_step("preprocess")
        ctx.require_complete("select")
        for step in ("preprocess", "dispatch"):
            with self.assertRaises(RunStateError):
                ctx.require_complete(step)

    def test_rerunning_a_step_discards_its_and_later_stats(self):
        ctx = create_run(self.scenario, self.output, STARTED)
        for step in ("select", "preprocess", "dispatch"):
            ctx.begin_step(step)
            ctx.record_stats(step, {"step": step})
            ctx.complete_step(step)

        ctx.begin_step("preprocess")
        self.assertEqual(ctx.read_stats("select"), {"step": "select"})
        self.assertIsNone(ctx.read_stats("preprocess"))
        self.assertIsNone(ctx.read_stats("dispatch"))

    def test_remove_intermediate_files_deletes_vre_file_only(self):
        ctx = create_run(self.scenario, self.output, STARTED, batch=True)
        self.assertTrue(ctx.batch)
        ctx.vre_hourly_path.write_text("x")
        ctx.selected_sites_path.write_text("y")
        ctx.remove_intermediate_files()
        ctx.remove_intermediate_files()  # already gone: not an error
        self.assertEqual(
            sorted(p.name for p in ctx.run_dir.iterdir()),
            ["config.toml", "run.json", "selected_sites.csv"],
        )

    def test_open_run_rejects_non_run_folder(self):
        with self.assertRaises(RunStateError):
            open_run(self.root)

    def test_latest_run_dir_picks_the_newest_timestamped_folder(self):
        older = create_run(self.scenario, self.output, STARTED)
        newer = create_run(self.scenario, self.output, datetime(2026, 10, 27, 21, 47))
        (self.output / "not-a-run").mkdir()
        self.assertEqual(latest_run_dir(self.output), newer.run_dir)
        self.assertNotEqual(latest_run_dir(self.output), older.run_dir)

    def test_latest_run_dir_fails_when_output_is_missing_or_empty(self):
        with self.assertRaisesRegex(RunStateError, "Output folder not found"):
            latest_run_dir(self.root / "missing")
        self.output.mkdir()
        with self.assertRaisesRegex(RunStateError, "No run folders found"):
            latest_run_dir(self.output)

    def test_run_dir_timestamp_is_the_yyyyMMdd_hhmm_prefix(self):
        ctx = create_run(self.scenario, self.output, STARTED)
        self.assertEqual(run_dir_timestamp(ctx.run_dir), "20261027-0905")
        self.assertEqual(run_dir_timestamp(self.root / "scratch"), "scratch")


if __name__ == "__main__":
    unittest.main()
