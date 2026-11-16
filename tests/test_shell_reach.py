"""Tests for shell_reach.py: what a body reaches through its file's functions."""

from __future__ import annotations

import unittest

from shell_trap_lint.shell_reach import (
    all_calls,
    ending_spots,
    function_hazards,
    unguarded_rm_lines,
)
from shell_trap_lint.shfmt_ast import Node, parse_shell, walk

OR_OP = next(
    node["Op"]
    for node in walk(parse_shell("a || b\n", "shfmt"))
    if node.get("Type") == "BinaryCmd"
)


def tree(source: str) -> Node:
    return parse_shell(source, "shfmt")


class RmReach(unittest.TestCase):
    def test_only_an_unguarded_rm(self) -> None:
        removes, unknown = unguarded_rm_lines(
            tree("rm a\nrm b || true\ncommand rm c\n"), OR_OP
        )
        self.assertEqual((removes, unknown), ([1, 3], []))

    def test_a_guard_stops_propagation(self) -> None:
        # `helper || echo` suppresses errexit for all of helper, so its bare rm
        # cannot publish a status through that call.
        source = "helper() {\n    rm x\n}\nsafe() {\n    helper || echo w\n}\nbare() {\n    helper\n}\n"
        reach = function_hazards(tree(source), OR_OP)
        self.assertIsNone(reach.status["safe"])
        hazard = reach.status["bare"]
        assert hazard is not None
        self.assertEqual((hazard.kind, hazard.chain), ("rm", ("bare", "helper")))


class EndingReach(unittest.TestCase):
    def test_a_guard_does_not_stop_an_early_end(self) -> None:
        source = "helper() {\n    exit 0\n}\ncleanup() {\n    helper || echo w\n}\n"
        hazard = function_hazards(tree(source), OR_OP).ending["cleanup"]
        assert hazard is not None
        self.assertEqual((hazard.kind, hazard.chain), ("ends", ("cleanup", "helper")))

    def test_code_this_reading_cannot_see(self) -> None:
        # Any of these may run an `exit` the gate never reads.
        for source in (
            'eval "$X"',
            ". ./lib",
            "source ./lib",
            '"$C" a',
            "${C}x a",
            "$D/x a",
            # With SEP=--, this is an exit.
            'command "$SEP" exit 0',
            "~ a",
            "ex?t 0",
            '"$@"/x a',
            # A code decided at run time may be 0.
            'exit $"1"',
            "exit $'1'",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    len(
                        ending_spots(
                            tree(source + "\n"), returns=False, paths_run_files=True
                        )
                    ),
                    1,
                )

    def test_a_path_runs_in_its_own_process(self) -> None:
        for source in ('"$D/x.sh" a', '"$D"/x a', "/bin/true", '[ "$X" = 1 ]'):
            with self.subTest(source=source):
                self.assertEqual(
                    ending_spots(
                        tree(source + "\n"), returns=False, paths_run_files=True
                    ),
                    [],
                )

    def test_a_slash_named_function_voids_the_path_exemption(self) -> None:
        # Bash outside POSIX mode accepts the name and runs it by that path.
        caller = 'helper() {\n    "$D/b"\n}\n'
        named = function_hazards(
            tree("function a/b {\n    exit 0\n}\n" + caller), OR_OP
        )
        plain = function_hazards(tree("b() {\n    exit 0\n}\n" + caller), OR_OP)
        self.assertFalse(named.paths_run_files)
        self.assertIsNotNone(named.ending["helper"])
        self.assertTrue(plain.paths_run_files)
        self.assertIsNone(plain.ending["helper"])

    def test_a_file_that_sources_another_voids_it(self) -> None:
        # The sourced file may define `a/b` - or `command`, so that is no way
        # around it; a subshell is, since whatever runs there ends only it.
        caller = (
            'helper() {\n    "$D/b"\n}\nviacommand() {\n    command "$D/b"\n}\n'
            'viasubshell() {\n    ("$D/b")\n}\n'
        )
        for runs in (
            ". ./lib.sh",
            "source ./lib.sh",
            'eval "$X"',
            '"$CMD" x',
            "command . ./lib.sh",
            "builtin source ./lib.sh",
        ):
            with self.subTest(runs=runs):
                reach = function_hazards(tree(f"{runs}\n{caller}"), OR_OP)
                self.assertFalse(reach.paths_run_files)
                self.assertIsNotNone(reach.ending["helper"])
                self.assertIsNotNone(reach.ending["viacommand"])
                self.assertIsNone(reach.ending["viasubshell"])

    def test_sourcing_in_a_child_shell_defines_nothing_here(self) -> None:
        # A function a subshell sources ends with the subshell.
        for runs in ("(. ./lib.sh)", "x=$(source ./lib.sh)", ". ./lib.sh &"):
            with self.subTest(runs=runs):
                reach = function_hazards(
                    tree(f'{runs}\nhelper() {{\n    "$D/b"\n}}\n'), OR_OP
                )
                self.assertTrue(reach.paths_run_files)
                self.assertIsNone(reach.ending["helper"])

    def test_a_child_shell_ends_only_itself(self) -> None:
        for source in (
            "(exit 0)",
            "x=$(exit 0)",
            "exit 0 &",
            "(helper)",
            "cat <(exit 0)",
            "coproc { exit 0; }",
        ):
            with self.subTest(source=source):
                body = tree(f"helper() {{\n    exit 0\n}}\n{source}\n")
                own = {"Stmts": body["Stmts"][1:]}
                self.assertEqual(
                    ending_spots(own, returns=True, paths_run_files=True), []
                )
                self.assertEqual(all_calls(own, ["helper"]), [])

    def test_a_substitution_bash_runs_in_this_shell(self) -> None:
        # `${ ...; }` and `${| ...; }` run in the current environment, so an
        # exit there ends the trap.
        for source in ("x=${ exit 0; }", "y=${| exit 0; }"):
            with self.subTest(source=source):
                spots = ending_spots(
                    tree(source + "\n"), returns=True, paths_run_files=True
                )
                self.assertEqual(len(spots), 1)

    def test_return_ends_only_where_asked(self) -> None:
        body = tree("return 0\nexit 1\n")
        self.assertEqual(ending_spots(body, returns=False, paths_run_files=True), [])
        self.assertEqual(
            [
                line
                for line, _ in ending_spots(body, returns=True, paths_run_files=True)
            ],
            [1],
        )


class Definitions(unittest.TestCase):
    def test_mutual_recursion_converges(self) -> None:
        source = "ping() {\n    pong\n    rm x\n}\npong() {\n    ping\n}\n"
        reach = function_hazards(tree(source), OR_OP)
        self.assertIsNotNone(reach.status["ping"])
        self.assertIsNotNone(reach.status["pong"])

    def test_a_name_defined_twice_is_unreadable(self) -> None:
        reach = function_hazards(
            tree("f() {\n    true\n}\nf() {\n    true\n}\n"), OR_OP
        )
        hazard = reach.status["f"]
        assert hazard is not None
        self.assertEqual(hazard.kind, "unreadable")
        self.assertNotIn("f", reach.bodies)


if __name__ == "__main__":
    unittest.main()
