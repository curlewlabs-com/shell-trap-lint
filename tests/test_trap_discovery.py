"""Tests for trap_discovery.py: which calls install an EXIT trap, and which
calls this reading cannot tell apart from one. Each case reads one file's text
through exit_traps alone, returning the actions it found and the kinds of the
findings it raised."""

from __future__ import annotations

import unittest

from shell_trap_lint.shfmt_ast import literal_word, parse_shell
from shell_trap_lint.trap_discovery import exit_traps

ACTION = 'rm -rf "$D"'


def discover(source: str) -> tuple[list[str], list[str]]:
    traps, findings = exit_traps(
        parse_shell(source + "\n", "shfmt"), lambda line, kind, message: kind
    )
    return [literal_word(action) for _, action in traps], findings


class WhatIsNotATrap(unittest.TestCase):
    def test_text_that_only_mentions_one(self) -> None:
        # The gate's own documentation quotes the shape to avoid, a suite
        # passes `sh -c 'trap ...'` as fixture data, and a word can only look
        # like a wrapper.
        for source in (
            f"# Bad example: trap '{ACTION}' EXIT",
            "sh -c 'trap \"\" TERM; rm -rf /tmp/zz; sleep 1'",
            'commandeer trap-cleanup "$D"',
        ):
            with self.subTest(source=source):
                self.assertEqual(discover(source), ([], []))

    def test_a_trap_not_naming_exit_and_a_print(self) -> None:
        # Only the EXIT trap decides the status a test runner reads.
        for source in (f"trap '{ACTION}' INT TERM", "trap -p EXIT"):
            with self.subTest(source=source):
                self.assertEqual(discover(source), ([], []))

    def test_a_reset_is_found_for_the_gate_to_pass(self) -> None:
        self.assertEqual(discover("trap - EXIT"), (["-"], []))


class TheExitSignalWord(unittest.TestCase):
    def test_every_way_the_shells_write_exit(self) -> None:
        # sh, dash and bash take EXIT in any letter case and as any all-zero
        # number, so each of these installs the trap `EXIT` does.
        for signal in ("EXIT", "exit", "Exit", "0", "00", "+0", "' 0'"):
            with self.subTest(signal=signal):
                self.assertEqual(discover(f"trap '{ACTION}' {signal}"), ([ACTION], []))

    def test_what_no_shell_reads_as_exit(self) -> None:
        for signal in ("SIGEXIT", "' EXIT'", "10", "INT"):
            with self.subTest(signal=signal):
                self.assertEqual(discover(f"trap '{ACTION}' {signal}"), ([], []))


class HowTheBuiltinIsReached(unittest.TestCase):
    def test_trap_options(self) -> None:
        for source in (f"trap -- '{ACTION}' EXIT", f"trap \"--\" '{ACTION}' EXIT"):
            with self.subTest(source=source):
                self.assertEqual(discover(source), ([ACTION], []))
        self.assertEqual(discover(f"trap -z '{ACTION}' EXIT"), ([], ["unreadable"]))

    def test_the_command_word_is_read_through_quotes(self) -> None:
        self.assertEqual(discover(f"tr'ap' '{ACTION}' EXIT"), ([ACTION], []))

    def test_wrappers_that_install_a_trap(self) -> None:
        for wrapper in ("command", "command -p", "builtin", "command -- command"):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(
                    discover(f"{wrapper} trap '{ACTION}' EXIT"), ([ACTION], [])
                )

    def test_wrapper_forms_that_install_nothing(self) -> None:
        # `v`/`V` make `command` a query; `builtin` takes no options; an
        # unknown `command` option and an option after `--` run no `trap`.
        for wrapper in (
            "command -v",
            "command -V",
            "command -pv",
            "command -x",
            "builtin -p",
            "command -- -p",
        ):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(discover(f"{wrapper} trap '{ACTION}' EXIT"), ([], []))


class ARunTimeCommand(unittest.TestCase):
    def test_an_expansion_where_a_wrapper_reads_its_command(self) -> None:
        # With SEP=--, or CMD=trap after the `--`, this installs the trap, so
        # its reading is unknown.
        for call in ('command "$SEP" trap', 'command -- "$CMD"', '"$CMD"'):
            with self.subTest(call=call):
                self.assertEqual(
                    discover(f"{call} '{ACTION}' EXIT"), ([], ["unreadable"])
                )

    def test_exit_anywhere_in_the_signal_list(self) -> None:
        # EXIT before another signal, an EXIT an expansion completes ("${P}EXIT"
        # with P empty, "E${MID}IT" with MID=X, zeros, a padded zero), and a
        # glob a file named EXIT would match, after a run-time command or
        # `trap` itself.
        for call in (
            f"\"$CMD\" '{ACTION}' EXIT TERM",
            f'"$CMD" \'{ACTION}\' "${{P}}EXIT"',
            f'"$CMD" \'{ACTION}\' "E${{MID}}IT"',
            f'"$CMD" \'{ACTION}\' "$Z"00',
            f'"$CMD" \'{ACTION}\' " $Z"',
            f"\"$CMD\" '{ACTION}' E*",
            f"trap '{ACTION}' \"${{P}}EXIT\"",
            f"trap '{ACTION}' INT \"$SIG\"",
        ):
            with self.subTest(call=call):
                self.assertEqual(discover(call), ([], ["unreadable"]))

    def test_a_wholly_expanded_argument_that_is_no_plain_value(self) -> None:
        # Arithmetic can yield 0, an extended glob can match a file named EXIT,
        # and a parameter operator writes its own text (`${#X}` is 0 when X is
        # empty, `${X:+EXIT}` is EXIT when X is set); only a parameter's plain
        # value or a command's output is no evidence.
        for argument in ('"$((0))"', "$((0))", "@(EXIT)", '"${#X}"', '"${X:+EXIT}"'):
            with self.subTest(argument=argument):
                self.assertEqual(
                    discover(f"\"$CMD\" '{ACTION}' {argument}"), ([], ["unreadable"])
                )

    def test_a_run_time_command_that_cannot_be_a_trap(self) -> None:
        # Arguments whose literal pieces cannot become EXIT, or a command that
        # is a path, install no EXIT trap whatever the expansion holds. An
        # argument built wholly from parameter expansions and command
        # substitutions is out of scope (cleanup_trap_check.py's header says
        # why); any other wholly expanded one is not.
        for call in (
            '"$CMD" a b',
            '"$CMD" a "$B" "$C/d"',
            '"$CMD" a "$(c)" ${P}',
            '"$D/x.sh" a EXIT',
        ):
            with self.subTest(call=call):
                self.assertEqual(discover(call), ([], []))


if __name__ == "__main__":
    unittest.main()
