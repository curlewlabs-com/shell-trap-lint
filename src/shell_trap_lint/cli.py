"""The `shell-trap-lint` command: judge the EXIT traps of the shell files named.

    shell-trap-lint [--test] [--shfmt PATH] FILE...

Each file is judged by the script form (script_trap_check.py), or with
`--test` by the test form (cleanup_trap_check.py). Which files are tests is the
caller's to say, so the command reads exactly the files it is given. It prints
one line per finding and exits 1 on any finding; it also exits 1, with the
reason on stderr, when it could not judge, since a lint that did not run must
not read as clean. `--shfmt` names the shfmt to parse with, for a build that
provides its own rather than one on `PATH`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from shell_trap_lint.cleanup_trap_check import (
    ToolchainError,
    check_files,
    derive_toolchain,
    judge_file,
)
from shell_trap_lint.script_trap_check import judge_script, script_form
from shell_trap_lint.shfmt_ast import ShfmtUnavailable


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shell-trap-lint",
        description=(
            "Check that each shell file's EXIT trap cannot pass a run that "
            "stopped short, or, for a test, change its verdict."
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="judge the files as tests: a failed teardown must not fail them",
    )
    parser.add_argument(
        "--shfmt",
        default="shfmt",
        metavar="PATH",
        help="the shfmt to parse with (default: shfmt on PATH)",
    )
    parser.add_argument("files", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    try:
        tools = derive_toolchain(args.shfmt)
        if not args.test:
            script_form(tools.shfmt)
        findings = check_files(
            Path.cwd(), args.files, tools, judge_file if args.test else judge_script
        )
    except (ToolchainError, ShfmtUnavailable) as error:
        print(f"ERROR: shell-trap-lint could not run: {error}", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding)
    return 1 if findings else 0
