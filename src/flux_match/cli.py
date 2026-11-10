"""Command-line entry point: `flux-match <command>`. See README.md for usage."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from flux_match import paths
from flux_match.config import Config, parse_override
from flux_match.errors import ConfigError, FluxMatchError
from flux_match.run_context import RUN_STEPS, RunContext, create_run, open_run
from flux_match.steps import select
from flux_match.steps.dispatch import dispatch
from flux_match.steps.fetch import fetch
from flux_match.steps.preprocess import preprocess

DEFAULT_SCENARIO = "balanced"

RUN_STEP_FUNCTIONS: dict[str, Callable[[RunContext], None]] = {
    "select": select.run,
    "preprocess": preprocess.run,
    "dispatch": dispatch.run,
}
if tuple(RUN_STEP_FUNCTIONS) != RUN_STEPS:
    raise RuntimeError("RUN_STEP_FUNCTIONS must list exactly the steps in RUN_STEPS, in order")


def _run_step(ctx: RunContext, step: str) -> None:
    print(f"[{step}] {ctx.run_dir}")
    ctx.begin_step(step)
    RUN_STEP_FUNCTIONS[step](ctx)
    ctx.complete_step(step)


def _new_run(args: argparse.Namespace) -> RunContext:
    ctx = create_run(
        paths.scenario_path(args.scenario),
        batch=getattr(args, "batch", None),
        overrides=_overrides(args),
    )
    print(f"Created run folder {ctx.run_dir}")
    return ctx


def _override_arg(text: str) -> tuple[str, Any]:
    try:
        return parse_override(text)
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for key, value in args.config:
        if key in overrides:
            raise ConfigError(f"Config override '{key}' is given more than once")
        overrides[key] = value
    return overrides


def _cmd_all(args: argparse.Namespace) -> None:
    ctx = _new_run(args)
    try:
        print("[fetch]")
        fetch.run(ctx.config)
        for step in RUN_STEPS:
            _run_step(ctx, step)
    finally:
        # Also on failure, so that a batch run folder never holds more than the three results.
        if ctx.batch:
            ctx.remove_intermediate_files()


def _cmd_fetch(args: argparse.Namespace) -> None:
    fetch.run(Config.load(paths.scenario_path(args.scenario), overrides=_overrides(args)))


def _cmd_step(args: argparse.Namespace) -> None:
    if args.run and getattr(args, "config", None):
        raise ConfigError("--config cannot be used with --run, which uses the run's config.toml")
    ctx = open_run(args.run) if args.run else _new_run(args)
    _run_step(ctx, args.command)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flux-match", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def add_scenario(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--scenario",
            default=DEFAULT_SCENARIO,
            help="scenario name in scenarios/, or a path to a .toml file (default: %(default)s)",
        )

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config",
            type=_override_arg,
            nargs="+",
            action="extend",
            default=[],
            metavar="KEY=VALUE",
            help="override a config value from the scenario or defaults.toml, e.g. "
            "dispatch.bess_rte=0.9 or 'fetch.demand_years=[2023, 2024]'; may be repeated",
        )

    def add_run(p: argparse.ArgumentParser, required: bool) -> None:
        p.add_argument(
            "--run",
            type=Path,
            required=required,
            help="existing run folder; this step's outputs are overwritten in place",
        )

    p = commands.add_parser("all", help="run steps 1-4 in a new run folder")
    add_scenario(p)
    add_config(p)
    p.add_argument(
        "--batch",
        metavar="NAME",
        help="create the run in output/NAME/NNNN (next free index), keeping only config.toml, "
        "run.json and selected_sites.csv",
    )
    p.set_defaults(handler=_cmd_all)

    p = commands.add_parser("fetch", help="step 1: download CODERS data into data/coders/")
    add_scenario(p)
    add_config(p)
    p.set_defaults(handler=_cmd_fetch)

    p = commands.add_parser("select", help="step 2: select sites (new run folder unless --run)")
    group = p.add_mutually_exclusive_group()
    add_scenario(group)
    add_run(group, required=False)
    add_config(p)
    p.set_defaults(handler=_cmd_step)

    for step, help_text in [
        ("preprocess", "step 3: hourly wind and solar per balancing area"),
        ("dispatch", "step 4: hourly battery dispatch and net demand"),
    ]:
        p = commands.add_parser(step, help=help_text)
        add_run(p, required=True)
        p.set_defaults(handler=_cmd_step)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(paths.PROJECT_ROOT / ".env")
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except FluxMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
