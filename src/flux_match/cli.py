"""Command-line entry point: `flux-match <command>`. See README.md for usage."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from flux_match import paths, selection
from flux_match.config import Config
from flux_match.errors import FluxMatchError
from flux_match.run_context import RUN_STEPS, RunContext, create_run, open_run
from flux_match.steps import dispatch, fetch, preprocess

DEFAULT_SCENARIO = "default"

RUN_STEP_FUNCTIONS: dict[str, Callable[[RunContext], None]] = {
    "select": selection.run,
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


def _new_run(scenario: str) -> RunContext:
    ctx = create_run(paths.scenario_path(scenario))
    print(f"Created run folder {ctx.run_dir}")
    return ctx


def _cmd_all(args: argparse.Namespace) -> None:
    ctx = _new_run(args.scenario)
    print("[fetch]")
    fetch.run(ctx.config)
    for step in RUN_STEPS:
        _run_step(ctx, step)


def _cmd_fetch(args: argparse.Namespace) -> None:
    fetch.run(Config.load(paths.scenario_path(args.scenario)))


def _cmd_step(args: argparse.Namespace) -> None:
    ctx = open_run(args.run) if args.run else _new_run(args.scenario)
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

    def add_run(p: argparse.ArgumentParser, required: bool) -> None:
        p.add_argument(
            "--run",
            type=Path,
            required=required,
            help="existing run folder; this step's outputs are overwritten in place",
        )

    p = commands.add_parser("all", help="run steps 1-4 in a new run folder")
    add_scenario(p)
    p.set_defaults(handler=_cmd_all)

    p = commands.add_parser("fetch", help="step 1: download CODERS data into data/coders/")
    add_scenario(p)
    p.set_defaults(handler=_cmd_fetch)

    p = commands.add_parser("select", help="step 2: select sites (new run folder unless --run)")
    group = p.add_mutually_exclusive_group()
    add_scenario(group)
    add_run(group, required=False)
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
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except FluxMatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
