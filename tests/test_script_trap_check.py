"""Tests for script_trap_check.py: an operational script's EXIT trap must fail a
run that stopped short.

Each fixture is one script's text. The forms accepted are the status-keeping
script form and the unit-test completion check; the module header says what
else is and is not judged.
"""

from __future__ import annotations

import unittest

from shell_trap_lint import cleanup_trap_check as gate
from shell_trap_lint import script_trap_check as scripts

TOOLS = gate.derive_toolchain()
STATUS = '_trap_rc=$?; [ "$RUN_COMPLETE" = 1 ] || [ "$_trap_rc" -ne 0 ] || _trap_rc=1'
SCRIPT_TRAP = f'trap \'{STATUS}; rm -f "$T"; exit "$_trap_rc"\' EXIT'


def kinds(text: str) -> set[str]:
    return {finding.kind for finding in scripts.judge_script("s.sh", text, TOOLS)}


class TheScriptForm(unittest.TestCase):
    def test_the_form_passes(self) -> None:
        text = f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nRUN_COMPLETE=1\n"
        self.assertEqual(kinds(text), set())

    def test_a_multi_line_body_and_a_trap_inside_a_function(self) -> None:
        multi = f'trap \'\n    {STATUS}\n    rm -f "$T"\n    exit "$_trap_rc"\n\' EXIT'
        in_function = f"start() {{\n    RUN_COMPLETE=0\n    {SCRIPT_TRAP}\n}}\n"
        self.assertEqual(kinds(f"#!/bin/sh\nRUN_COMPLETE=0\n{multi}\n"), set())
        self.assertEqual(kinds(f"#!/bin/sh\n{in_function}start\n"), set())

    def test_an_unconverted_trap(self) -> None:
        # Traps that let an unbound variable on bash 3.2 turn the abort into
        # exit 0 - including a trap set inside a
        # function and reset afterwards, and one on the numeric EXIT.
        for trap in (
            "trap 'rm -f \"$T\"' EXIT",
            "trap 'rm -f \"$T\"' 0 1 2 3 15",
            "trap 'rm -f \"$T\"' EXIT INT TERM",
        ):
            with self.subTest(trap=trap):
                self.assertEqual(
                    kinds(f"#!/bin/sh\nRUN_COMPLETE=0\n{trap}\n"), {"completion"}
                )

    def test_the_form_out_of_order_or_incomplete(self) -> None:
        for body in (
            f'rm -f "$T"; {STATUS}; exit "$_trap_rc"',
            f'{STATUS}; rm -f "$T"',
            f'{STATUS}; exit "$_trap_rc"; rm -f "$T"',
            '_trap_rc=$?; rm -f "$T"; exit "$_trap_rc"',
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    kinds(f"#!/bin/sh\nRUN_COMPLETE=0\ntrap '{body}' EXIT\n"),
                    {"completion"},
                )

    def test_the_sentinel_set_ahead_of_the_trap(self) -> None:
        # Without it an aborted run reads a value the environment exported.
        self.assertEqual(kinds(f"#!/bin/sh\n{SCRIPT_TRAP}\n"), {"completion"})
        self.assertEqual(
            kinds(f"#!/bin/sh\n{SCRIPT_TRAP}\nRUN_COMPLETE=0\n"), {"completion"}
        )


class TheCleanupKeepsTheStatus(unittest.TestCase):
    """The cleanup between the status line and the final exit must not end the
    trap early or overwrite the status it saved - in the body, or in any
    same-file function the body reaches."""

    def trap(self, cleanup: str) -> str:
        return f"trap '{STATUS}; {cleanup}; exit \"$_trap_rc\"' EXIT"

    def test_in_the_body(self) -> None:
        for cleanup in (
            "_trap_rc=0",
            "exit 0",
            "read _trap_rc",
            ": ${_trap_rc:=0}",
            # The shell joins the name after quote removal.
            'read _trap"_rc"',
            "read _trap\\_rc",
            "return 0",
            # Code this gate cannot read may be an exit.
            'eval "$X"',
            "source ./lib.sh",
            '"$CMD" stop',
            '"$@"/x stop',
            "ex?t 0",
        ):
            with self.subTest(cleanup=cleanup):
                text = f"#!/bin/sh\nRUN_COMPLETE=0\n{self.trap(cleanup)}\n"
                self.assertEqual(kinds(text), {"completion"})

    def test_in_a_reached_function(self) -> None:
        for body in ("_trap_rc=0", "exit 0", "exec true", ". ./lib.sh", "$CMD stop"):
            with self.subTest(body=body):
                helper = f"helper() {{\n    {body}\n}}\n"
                text = f"#!/bin/sh\n{helper}RUN_COMPLETE=0\n{self.trap('helper')}\n"
                self.assertEqual(kinds(text), {"completion"})

    def test_a_function_this_gate_cannot_pin_down(self) -> None:
        # Defined inside a branch, its body is decided at run time.
        nested = "if true; then\n    helper() {\n        true\n    }\nfi\n"
        text = f"#!/bin/sh\n{nested}RUN_COMPLETE=0\n{self.trap('helper')}\n"
        self.assertEqual(kinds(text), {"completion"})

    def test_a_plain_read_and_a_failing_exit_pass(self) -> None:
        helper = 'cleanup() {\n    [ "$1" -eq 0 ] || echo failed >&2\n}\n'
        reads = (
            f"#!/bin/sh\n{helper}RUN_COMPLETE=0\n{self.trap('cleanup "$_trap_rc"')}\n"
        )
        failing = f"#!/bin/sh\nRUN_COMPLETE=0\n{self.trap('rm -f "$T" || exit 1')}\n"
        self.assertEqual(kinds(reads), set())
        self.assertEqual(kinds(failing), set())

    def test_a_path_that_may_name_a_function(self) -> None:
        # Bash outside POSIX mode runs `function a/b` for "$D/b" with D=a.
        named = "function a/b {\n    exit 0\n}\n"
        text = f"#!/bin/bash\n{named}RUN_COMPLETE=0\n{self.trap('"$D/b"')}\n"
        self.assertEqual(kinds(text), {"completion"})

    def test_a_path_in_a_script_that_sources_another(self) -> None:
        # The sourced lib may define the function the path names, or one
        # named `command`; in a subshell, whatever runs ends only that.
        cases: tuple[tuple[str, set[str]], ...] = (
            ('"$D/b"', {"completion"}),
            ('command "$D/b"', {"completion"}),
            ('("$D/b")', set()),
        )
        for call, expected in cases:
            with self.subTest(call=call):
                text = f"#!/bin/sh\n. ./lib.sh\nRUN_COMPLETE=0\n{self.trap(call)}\n"
                self.assertEqual(kinds(text), expected)

    def test_a_script_named_by_a_path_passes(self) -> None:
        # A name holding a `/` runs a file in its own process, which cannot
        # end this shell, whatever the expansion before it holds.
        text = f"#!/bin/sh\nRUN_COMPLETE=0\n{self.trap('"$SCRIPT_DIR/x.sh" stop')}\n"
        self.assertEqual(kinds(text), set())

    def test_a_body_spot_counts_lines_from_the_body(self) -> None:
        # The body is parsed from the trap's string, so its line 2 is not the
        # file's.
        trap = f"trap '{STATUS}\nexit 0\nexit \"$_trap_rc\"' EXIT"
        findings = scripts.judge_script(
            "s.sh", f"#!/bin/sh\nRUN_COMPLETE=0\n{trap}\n", TOOLS
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("on line 2 of the trap body", findings[0].message)


class TheSentinelRunsFirst(unittest.TestCase):
    """`RUN_COMPLETE=0` counts only as a plain, foreground statement ahead of the
    trap in its own statement list; anywhere else it may not have run when the
    trap fires, and an exported 1 would pass an aborted run."""

    def test_an_init_that_may_not_have_run(self) -> None:
        for before in (
            "setup() {\n    RUN_COMPLETE=0\n}\n",
            "if false; then\n    RUN_COMPLETE=0\nfi\n",
            "(RUN_COMPLETE=0)\n",
            "RUN_COMPLETE=0 &\n",
        ):
            with self.subTest(before=before):
                # A backgrounded one is also a write outside a boundary.
                self.assertIn(
                    "completion", kinds(f"#!/bin/sh\n{before}{SCRIPT_TRAP}\n")
                )


class SuccessBoundaries(unittest.TestCase):
    """A 1 is only written where nothing after it can abort: directly before an
    `exit`, or as the script's last top-level statement."""

    def test_where_a_one_may_stand(self) -> None:
        for after in (
            "RUN_COMPLETE=1\n",
            "if true; then\n    RUN_COMPLETE=1\n    exit 0\nfi\nRUN_COMPLETE=1\n",
            'case "$1" in\na)\n    RUN_COMPLETE=1\n    exit 0\n    ;;\nesac\nRUN_COMPLETE=1\n',
        ):
            with self.subTest(after=after):
                self.assertEqual(
                    kinds(f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\n{after}"), set()
                )

    def test_a_one_with_code_after_it(self) -> None:
        for text in (
            # Bash assigns first, then the redirection's expansion aborts.
            f'#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nRUN_COMPLETE=1 >"$UNSET"\nexit 0\n',
            f'#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nRUN_COMPLETE=1 >"$UNSET"\n',
            f"#!/bin/sh\nRUN_COMPLETE=0\nRUN_COMPLETE=1\n{SCRIPT_TRAP}\nexit 0\n",
            f'#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nRUN_COMPLETE=1\n: "$X"\n',
            f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nf() {{\n    RUN_COMPLETE=1\n}}\n",
        ):
            with self.subTest(text=text):
                self.assertIn("lifecycle", kinds(text))

    def test_an_exit_that_can_abort_after_the_one(self) -> None:
        # Under set -u the exit's own expansion aborts with the sentinel
        # already 1, which macOS /bin/sh then publishes as success.
        for exit_line in (
            'exit "$UNSET"',
            'exit 0 >"$LOG"',
            "A=$B exit 0",
            "exit 0 1",
            "exit x",
        ):
            with self.subTest(exit_line=exit_line):
                text = (
                    f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\n"
                    f"if true; then\n    RUN_COMPLETE=1\n    {exit_line}\nfi\n"
                    "RUN_COMPLETE=1\n"
                )
                self.assertIn("lifecycle", kinds(text))

    def test_a_string_bash_rewrites(self) -> None:
        # Bash translates `$"..."` and decodes `$'...'` before the command
        # runs, in the file or a trap body.
        for text in (
            f'#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nRUN_COMPLETE=1\n$"exit" 0\n',
            f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nRUN_COMPLETE=1\n$'exit' 0\n",
            f'#!/bin/sh\nRUN_COMPLETE=0\ntrap \'{STATUS}; $"x"; exit "$_trap_rc"\' EXIT\n',
        ):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), {"unreadable"})

    def test_an_action_built_with_quote_idioms(self) -> None:
        # Run after quote removal, this cleanup holds `$'exit' 0`.
        q = "'\\''"
        trap = f"trap '{STATUS}; ${q}exit{q} 0; exit \"$_trap_rc\"' EXIT"
        self.assertEqual(kinds(f"#!/bin/sh\nRUN_COMPLETE=0\n{trap}\n"), {"unreadable"})

    def test_writes_inside_a_trap_body(self) -> None:
        # The file's tree holds an inline body as a string.
        unit = f"trap ':; SUITE_COMPLETE=1; {gate.CANONICAL_CHECK}' EXIT"
        status = f"trap '{STATUS}; RUN_COMPLETE=1; exit \"$_trap_rc\"' EXIT"
        for sentinel, trap in (("SUITE_COMPLETE", unit), ("RUN_COMPLETE", status)):
            with self.subTest(sentinel=sentinel):
                text = f"#!/bin/sh\n{sentinel}=0\n{trap}\n{sentinel}=1\n"
                self.assertIn("lifecycle", kinds(text))

    def test_a_name_written_in_ansi_c_escapes(self) -> None:
        # RUN$'\x5f'COMPLETE is RUN_COMPLETE once bash decodes it; the gate
        # refuses the file or the trap body that holds it.
        for text in (
            f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nread RUN$'\\x5f'COMPLETE\nRUN_COMPLETE=1\n",
            f'#!/bin/sh\nRUN_COMPLETE=0\ntrap \'{STATUS}; read _trap$"x"_rc; exit "$_trap_rc"\' EXIT\n',
        ):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), {"unreadable"})

    def test_any_other_write(self) -> None:
        for write in (
            "unset RUN_COMPLETE",
            "export RUN_COMPLETE=1",
            "RUN_COMPLETE=2",
            # Compound statements write through their own words too.
            "for RUN_COMPLETE in 1; do :; done",
            "((RUN_COMPLETE = 1))",
            "while read -r RUN_COMPLETE; do :; done </dev/null",
            # The shell joins the name after quote removal.
            'read RUN"_COMPLETE"',
            "read RUN\\_COMPLETE",
        ):
            with self.subTest(write=write):
                text = f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\n{write}\nRUN_COMPLETE=1\n"
                self.assertIn("lifecycle", kinds(text))


class TheUnitTestForm(unittest.TestCase):
    """A script may carry the test form's completion check instead of the
    script form."""

    def test_a_handler_or_inline_check_passes(self) -> None:
        check = gate.CANONICAL_CHECK
        handler = f'cleanup() {{\n    rm -f "$T"\n    {check}\n}}\n'
        self.assertEqual(
            kinds(f"#!/bin/sh\nSUITE_COMPLETE=0\n{handler}trap cleanup EXIT\n"), set()
        )
        inline = f"trap 'rm -f \"$T\"; {check}' EXIT"
        self.assertEqual(kinds(f"#!/bin/sh\nSUITE_COMPLETE=0\n{inline}\n"), set())

    def test_an_early_end_before_the_check(self) -> None:
        # The check ends the code, so anything that can end it first - in the
        # handler, the inline body, or a helper either calls - skips it.
        check = gate.CANONICAL_CHECK
        for early in ("exit 0", "exec true", 'eval "$X"', ". ./lib.sh", '"$CMD" x'):
            helper = f"helper() {{\n    {early}\n}}\n"
            cases = {
                "inline": f"trap '{early}; {check}' EXIT",
                "handler": f"cleanup() {{\n    {early}\n    {check}\n}}\ntrap cleanup EXIT",
                "helper": f"{helper}trap 'helper; {check}' EXIT",
            }
            for where, trap in cases.items():
                with self.subTest(early=early, where=where):
                    text = f"#!/bin/sh\nSUITE_COMPLETE=0\n{trap}\nSUITE_COMPLETE=1\n"
                    self.assertEqual(kinds(text), {"completion"})

    def test_a_return_ends_only_the_traps_own_code(self) -> None:
        # In the handler or the inline body a `return` is read as ending the
        # trap (shell_reach.ending_spots says why); in a helper the handler
        # calls, it only returns to the handler, which still runs its check.
        check = gate.CANONICAL_CHECK
        handler = f"cleanup() {{\n    return 0\n    {check}\n}}\ntrap cleanup EXIT"
        inline = f"trap 'return 0; {check}' EXIT"
        helper = f"helper() {{\n    return 0\n}}\ncleanup() {{\n    helper\n    {check}\n}}\ntrap cleanup EXIT"
        cases: tuple[tuple[str, set[str]], ...] = (
            (handler, {"completion"}),
            (inline, {"completion"}),
            (helper, set()),
        )
        for trap, expected in cases:
            with self.subTest(trap=trap):
                text = f"#!/bin/sh\nSUITE_COMPLETE=0\n{trap}\nSUITE_COMPLETE=1\n"
                self.assertEqual(kinds(text), expected)

    def test_a_handler_without_it(self) -> None:
        handler = 'cleanup() {\n    rm -f "$T"\n}\n'
        self.assertEqual(
            kinds(f"#!/bin/sh\nRUN_COMPLETE=0\n{handler}trap cleanup EXIT\n"),
            {"completion"},
        )


class WhatIsLeftAlone(unittest.TestCase):
    def test_resets_and_non_exit_traps(self) -> None:
        for trap in (
            "trap - EXIT",
            "trap - 0 1 2 3 15",
            "trap 'rm -f \"$T\"' INT TERM",
        ):
            with self.subTest(trap=trap):
                self.assertEqual(kinds(f"#!/bin/sh\n{trap}\n"), set())

    def test_a_bare_rm_is_the_scripts_own_policy(self) -> None:
        text = f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\nRUN_COMPLETE=1\n"
        self.assertNotIn("rm", kinds(text))


class TheSavedStatusThroughAQuotedOperand(unittest.TestCase):
    def test_let_on_trap_rc_in_the_cleanup_is_a_write(self) -> None:
        # `let "_trap_rc = 0" || :` overwrites a saved failure, and the final
        # exit then publishes success.
        trap = f'trap \'{STATUS}; let "_trap_rc = 0" || :; exit "$_trap_rc"\' EXIT'
        text = f"#!/bin/sh\nRUN_COMPLETE=0\n{trap}\nRUN_COMPLETE=1\n"
        self.assertEqual(kinds(text), {"completion"})

    def test_a_rebinding_inside_an_inline_body_is_unreadable(self) -> None:
        trap = f'trap \'exit() {{ :; }}; {STATUS}; rm -f "$T"; exit "$_trap_rc"\' EXIT'
        text = f"#!/bin/sh\nRUN_COMPLETE=0\n{trap}\nRUN_COMPLETE=1\n"
        self.assertIn("unreadable", kinds(text))

    def test_let_on_the_sentinel_is_outside_a_boundary(self) -> None:
        text = (
            f"#!/bin/sh\nRUN_COMPLETE=0\n{SCRIPT_TRAP}\n"
            'let "RUN_COMPLETE = 1"\nRUN_COMPLETE=1\n'
        )
        self.assertEqual(kinds(text), {"lifecycle"})


if __name__ == "__main__":
    unittest.main()
