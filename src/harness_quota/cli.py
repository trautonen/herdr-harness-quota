from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_REFRESH_INTERVAL, DEFAULT_STALE_MARKER_AGE, PROVIDERS, VERSION
from .presentation import format_chip
from .report import build_report
from .service import refresh_in_background, refresh_provider
from .storage import cache_directory, read_snapshot


def snapshot_age(snapshot: dict[str, Any]) -> int:
    return int(time.time()) - int(snapshot["capturedAt"])


def cmd_chip(provider: str, max_age: int, refresh_after: int) -> int:
    providers = PROVIDERS if provider == "all" else (provider,)
    chips = []
    for current_provider in providers:
        snapshot = read_snapshot(current_provider, max_age=None)
        if read_snapshot(current_provider, refresh_after) is None:
            refresh_in_background(current_provider)
        stale = snapshot is not None and snapshot_age(snapshot) > max_age
        chip = format_chip(current_provider, snapshot, stale=stale)
        if chip:
            chips.append(chip)
    print("  ".join(chips))
    return 0


def cmd_report(
    provider: str,
    refresh: str,
    stale_after: int,
    timeout: float,
    pretty: bool,
    require_all: bool,
) -> int:
    report = build_report(provider, refresh, stale_after, timeout)
    if pretty:
        print(json.dumps(report, indent=2))
    else:
        print(json.dumps(report, separators=(",", ":")))
    if require_all and any(item["status"] != "ok" for item in report["providers"]):
        return 1
    return 0


def cmd_refresh(provider: str, timeout: float) -> int:
    providers = PROVIDERS if provider == "all" else (provider,)
    errors = []
    for current_provider in providers:
        try:
            refresh_provider(current_provider, timeout)
        except (RuntimeError, OSError) as error:
            errors.append(f"{current_provider}: {error}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return 0


def cmd_details(max_age: int) -> int:
    lines = []
    for provider in PROVIDERS:
        snapshot = read_snapshot(provider, max_age=None)
        chip = format_chip(provider, snapshot)
        if chip:
            age = snapshot_age(snapshot)
            name = provider.capitalize() + ("?" if age > max_age else "")
            lines.append(f"{name:7} {chip[3:]}  ({age}s old)")
        else:
            lines.append(f"{provider.capitalize():7} unavailable")
    print("\n".join(lines))
    return 0


def nonnegative_integer(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        prog="harness-quota",
        description="Read normalized Claude, Codex, and Cursor quota.",
    )
    argument_parser.add_argument(
        "-p", "--provider", dest="report_provider", choices=(*PROVIDERS, "all"), default="all"
    )
    argument_parser.add_argument(
        "--refresh", dest="report_refresh", choices=("stale", "always", "never"), default="stale"
    )
    argument_parser.add_argument(
        "--stale-after", type=nonnegative_integer, default=DEFAULT_REFRESH_INTERVAL
    )
    argument_parser.add_argument(
        "--timeout", dest="report_timeout", type=positive_float, default=12.0
    )
    argument_parser.add_argument("--pretty", action="store_true")
    argument_parser.add_argument("--require-all", action="store_true")
    argument_parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = argument_parser.add_subparsers(dest="command")

    chip = subparsers.add_parser("chip", help="print compact quota text")
    chip.add_argument("provider", choices=(*PROVIDERS, "all"))
    chip.add_argument("--max-age", type=nonnegative_integer, default=DEFAULT_STALE_MARKER_AGE)
    chip.add_argument("--refresh-after", type=nonnegative_integer, default=DEFAULT_REFRESH_INTERVAL)

    refresh = subparsers.add_parser("refresh", help="fetch current quota")
    refresh.add_argument("provider", choices=(*PROVIDERS, "all"))
    refresh.add_argument("--timeout", type=float, default=12.0)

    details = subparsers.add_parser("details", help="print current quota details")
    details.add_argument("--max-age", type=nonnegative_integer, default=DEFAULT_STALE_MARKER_AGE)

    return argument_parser


def run_background_refresh(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[1] not in PROVIDERS:
        print("herdr-harness-quota: invalid background refresh provider", file=sys.stderr)
        return 2
    provider = arguments[1]
    lock_path: Path | None = None
    try:
        lock_path = cache_directory() / f".{provider}-refresh.lock"
        refresh_provider(provider)
    except (RuntimeError, OSError) as error:
        print(f"herdr-harness-quota: {error}", file=sys.stderr)
        return 1
    finally:
        if lock_path is not None:
            lock_path.unlink(missing_ok=True)
    return 0


def main(arguments: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    if raw_arguments[:1] == ["_background-refresh"]:
        return run_background_refresh(raw_arguments)

    args = parser().parse_args(raw_arguments)
    try:
        if args.command is None:
            return cmd_report(
                args.report_provider,
                args.report_refresh,
                args.stale_after,
                args.report_timeout,
                args.pretty,
                args.require_all,
            )
        if args.command == "chip":
            return cmd_chip(args.provider, args.max_age, args.refresh_after)
        if args.command == "refresh":
            return cmd_refresh(args.provider, args.timeout)
        if args.command == "details":
            return cmd_details(args.max_age)
    except (RuntimeError, OSError) as error:
        print(f"herdr-harness-quota: {error}", file=sys.stderr)
        return 1
    return 2
