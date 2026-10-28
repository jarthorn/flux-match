import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from flux_match.errors import ConfigError, RunStateError
from flux_match.run_context import (
    CONFIG_NAME,
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
        self.assertEqual((ctx.run_dir / CONFIG_NAME).read_text(), self.scenario.read_text())

    def test_existing_run_folder_is_never_reused(self):
        create_run(self.scenario, self.output, STARTED)
        with self.assertRaises(RunStateError):
            create_run(self.scenario, self.output, STARTED)

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
