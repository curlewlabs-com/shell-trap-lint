"""Tests for cleanup_trap_check.py.

Each fixture is the text of one unit test, judged as `shell-trap-lint --test`
judges it. A case about the rm guard asserts only the rm and fail-closed findings
(`blocks`), since its fixture leaves the completion check out; a case about the
completion check sets everything else right, so the finding it asserts can come
from nothing else. The module header carries the mechanism and the audit each
case below makes executable.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from shell_trap_lint import cleanup_trap_check as gate
from shell_trap_lint.shfmt_ast import ShellParseError, parse_shell

GUARDED = 'rm -rf "$D" || echo WARN >&2'
CHECK = gate.CANONICAL_CHECK
TOOLS = gate.derive_toolchain()


def kinds(text: str, path: str = "t/test-a.sh") -> set[str]:
    return {finding.kind for finding in gate.judge_file(path, text, TOOLS)}


def blocks(text: str) -> bool:
    """A finding of the rm-guard or fail-closed kind."""
    return bool(kinds(text) & {"rm", "unreadable"})


def lacks_completion(text: str) -> bool:
    """A finding that the completion check or its lifecycle is missing."""
    return bool(kinds(text) & {"completion", "lifecycle"})


def suite(trap_line: str, before: str = "", after: str = "") -> str:
    """A suite whose sentinel lifecycle is right, around one trap line."""
    return f"#!/bin/sh\nD=x\n{before}SUITE_COMPLETE=0\n{trap_line}\n{after}SUITE_COMPLETE=1\n"


class TheRmGuard(unittest.TestCase):
    def test_bare_rm_blocks(self) -> None:
        # The shape that reported a test whose assertions all passed as broken,
        # when its temp repo's .git would not delete.
        self.assertTrue(blocks("#!/bin/sh\nD=x\ntrap 'rm -rf \"$D\"' EXIT INT TERM\n"))

    def test_guarded_rm_passes(self) -> None:
        self.assertFalse(blocks(f"#!/bin/sh\nD=x\ntrap '{GUARDED}' EXIT INT TERM\n"))

    def test_second_bare_rm_in_a_guarded_body_blocks(self) -> None:
        # A matcher asking only "does the body hold a ||" passes this.
        self.assertTrue(
            blocks(f"#!/bin/sh\nD=x\ntrap '{GUARDED}; rm -rf \"$D.log\"' EXIT\n")
        )

    def test_or_before_the_rm_guards_nothing(self) -> None:
        self.assertTrue(blocks("#!/bin/sh\nD=x\ntrap 'false || rm -rf \"$D\"' EXIT\n"))

    def test_rm_on_the_right_of_an_or_is_bare(self) -> None:
        self.assertTrue(
            blocks('#!/bin/sh\nD=x\ntrap \'rm -rf "$D" || rm -rf "$D.log"\' EXIT\n')
        )

    def test_third_rm_in_an_or_chain_is_bare(self) -> None:
        body = 'rm -rf "$D" || rm -rf "$D.a" || rm -rf "$D.b"'
        self.assertTrue(blocks(f"#!/bin/sh\nD=x\ntrap '{body}' EXIT\n"))

    def test_compound_left_operand_is_guarded(self) -> None:
        body = '{ rm -rf "$D"; rm -rf "$D.a"; } || echo WARN >&2'
        self.assertFalse(blocks(f"#!/bin/sh\nD=x\ntrap '{body}' EXIT\n"))

    def test_double_quoted_body_is_read(self) -> None:
        # A matcher reading only single-quoted bodies falls through to the
        # handler-name branch and allows this.
        self.assertTrue(blocks('#!/bin/sh\nD=x\ntrap "rm -rf $D" EXIT\n'))
        self.assertFalse(
            blocks('#!/bin/sh\nD=x\ntrap "rm -rf $D || echo WARN >&2" EXIT\n')
        )

    def test_adjacent_quoted_regions_are_one_body(self) -> None:
        # A scan stopping at the first closing quote never reaches the rm.
        hidden = "'echo '\\''done'\\''; rm -rf \"$D\"'"
        guarded = "'echo '\\''done'\\''; rm -rf \"$D\" || echo WARN >&2'"
        self.assertTrue(blocks(f"#!/bin/sh\nD=x\ntrap {hidden} EXIT\n"))
        self.assertFalse(blocks(f"#!/bin/sh\nD=x\ntrap {guarded} EXIT\n"))

    def test_multi_line_bodies_are_read(self) -> None:
        self.assertTrue(
            blocks("#!/bin/sh\nD=x\ntrap 'echo cleaning\n    rm -rf \"$D\"' EXIT\n")
        )
        self.assertFalse(
            blocks(f"#!/bin/sh\nD=x\ntrap 'echo cleaning\n    {GUARDED}' EXIT\n")
        )

    def test_trailing_comments_do_not_end_the_body(self) -> None:
        # An apostrophe or a double quote in a trailing comment once read as the
        # body's closing quote.
        self.assertTrue(
            blocks("#!/bin/sh\nD=x\ntrap 'rm -rf \"$D\"' EXIT # don't leak\n")
        )
        self.assertTrue(blocks('#!/bin/sh\nD=x\ntrap "rm -rf $D" EXIT # say "hi"\n'))
        self.assertFalse(
            blocks(f"#!/bin/sh\nD=x\ntrap '{GUARDED}' EXIT # don't leak\n")
        )

    def test_a_trap_inside_a_function_is_judged(self) -> None:
        # Indentation is the cheapest way to exempt a trap by accident.
        self.assertTrue(
            blocks("#!/bin/sh\nsetup() {\n    trap 'rm -rf \"$D\"' EXIT\n}\n")
        )


class HandlerDelegation(unittest.TestCase):
    def test_a_handler_holding_a_bare_rm(self) -> None:
        self.assertTrue(
            blocks('#!/bin/sh\ncleanup() {\n    rm -rf "$D"\n}\ntrap cleanup EXIT\n')
        )
        self.assertFalse(
            blocks(f"#!/bin/sh\ncleanup() {{\n    {GUARDED}\n}}\ntrap cleanup EXIT\n")
        )

    def test_definition_order_and_brace_style(self) -> None:
        self.assertTrue(
            blocks('#!/bin/sh\ntrap cleanup EXIT\ncleanup() {\n    rm -rf "$D"\n}\n')
        )
        self.assertTrue(
            blocks('#!/bin/sh\ncleanup()\n{\n    rm -rf "$D"\n}\ntrap cleanup EXIT\n')
        )

    def test_a_handler_from_another_file_fails_closed(self) -> None:
        self.assertEqual(
            kinds("#!/bin/sh\n. ./lib.sh\ntrap shared_cleanup EXIT\n"), {"unreadable"}
        )

    def test_an_inline_body_calling_a_hazardous_function(self) -> None:
        text = "#!/bin/sh\nD=x\ncleanup() {\n    rm -rf \"$D\"\n}\ntrap 'echo bye; cleanup' EXIT\n"
        self.assertTrue(blocks(text))
        guarded = text.replace("cleanup' EXIT", "cleanup || echo WARN >&2' EXIT")
        self.assertFalse(blocks(guarded))

    def test_hazards_propagate_transitively(self) -> None:
        helper = 'helper() {\n    rm -rf "$D"\n}\n'
        self.assertTrue(
            blocks(
                f"#!/bin/sh\nD=x\n{helper}cleanup() {{\n    helper\n}}\ntrap cleanup EXIT\n"
            )
        )
        self.assertTrue(
            blocks(
                f"#!/bin/sh\nD=x\n{helper}cleanup() {{\n    helper\n}}\ntrap 'echo bye; cleanup' EXIT\n"
            )
        )
        three = (
            '#!/bin/sh\nD=x\ninner() {\n    rm -rf "$D"\n}\nmid() {\n    inner\n}\n'
            "cleanup() {\n    mid\n}\ntrap cleanup EXIT\n"
        )
        self.assertTrue(blocks(three))

    def test_a_guard_anywhere_on_the_chain_stops_it(self) -> None:
        inner_guarded = (
            f"#!/bin/sh\nD=x\nhelper() {{\n    {GUARDED}\n}}\n"
            "cleanup() {\n    helper\n}\ntrap cleanup EXIT\n"
        )
        call_guarded = (
            '#!/bin/sh\nD=x\nhelper() {\n    rm -rf "$D"\n}\n'
            'cleanup() {\n    helper || echo "WARN" >&2\n}\ntrap cleanup EXIT\n'
        )
        self.assertFalse(blocks(inner_guarded))
        self.assertFalse(blocks(call_guarded))

    def test_mutual_recursion_converges(self) -> None:
        text = (
            '#!/bin/sh\nD=x\nping() {\n    pong\n    rm -rf "$D"\n}\n'
            "pong() {\n    ping\n}\ntrap ping EXIT\n"
        )
        self.assertTrue(blocks(text))


class HowRmIsReached(unittest.TestCase):
    def test_rm_through_a_wrapper_path_or_escape(self) -> None:
        # Each of these runs rm, so its failure still aborts the trap before the
        # completion check and publishes rm's status.
        for command in (
            "command rm",
            "command -- rm",
            "command -p rm",
            "exec rm",
            "env rm",
            "env A=1 rm",
            "env - /bin/rm",
            "/usr/bin/env A=1 rm",
            "/usr/bin/env - /bin/rm",
            "/usr/bin/env -- A=1 rm",
            "env -- -A=1 rm",
            "/usr/bin/env -- -A=1 rm",
            "/bin/rm",
            "\\rm",
            '"rm"',
        ):
            with self.subTest(command=command):
                self.assertIn(
                    "rm", kinds(suite(f"trap '{command} -rf \"$D\"; {CHECK}' EXIT"))
                )

    def test_a_guarded_or_query_form_passes(self) -> None:
        guarded = f"trap 'command rm -rf \"$D\" || echo WARN >&2; {CHECK}' EXIT"
        query = f"trap 'command -v rm >/dev/null || true; {GUARDED}; {CHECK}' EXIT"
        self.assertEqual(kinds(suite(guarded)), set())
        self.assertEqual(kinds(suite(query)), set())

    def test_a_wrapper_option_it_cannot_read_fails_closed(self) -> None:
        for command in ("env -i rm", "exec -a x rm"):
            with self.subTest(command=command):
                self.assertIn(
                    "unreadable",
                    kinds(suite(f"trap '{command} -rf \"$D\"; {CHECK}' EXIT")),
                )

    def test_each_unreadable_call_says_why(self) -> None:
        # The remedy differs by reason, so the finding carries the reader's
        # own reason rather than calling everything an option.
        for command, reason in (
            ("env -i rm", "a wrapper option this gate does not read"),
            ("/usr/bin/command rm", "a path in place of the builtin `command`"),
            ("env -- - rm", "whose `-` this gate does not decide"),
            ("./env rm", "an `env` other than the system's"),
            ('command "$SEP" rm', "an expansion where `command` reads"),
            ('command -- "$CMD"', "an expansion where `command` reads"),
        ):
            helper = f'helper() {{\n    {command} -rf "$D"\n}}\n'
            for where, text in (
                ("body", suite(f"trap '{command} -rf \"$D\"; {CHECK}' EXIT")),
                ("helper", suite(f"trap 'helper; {CHECK}' EXIT", before=helper)),
            ):
                with self.subTest(command=command, where=where):
                    messages = [
                        finding.message
                        for finding in gate.judge_file("t/test-a.sh", text, TOOLS)
                        if finding.kind == "unreadable"
                    ]
                    # An expansion after `command` may also name trap, so
                    # trap discovery can report the same call.
                    self.assertTrue(any(reason in message for message in messages))
                    self.assertFalse(
                        any(
                            "drop the option" in message.lower() for message in messages
                        )
                    )


class EndingBeforeTheCheck(unittest.TestCase):
    """An exit that can carry 0, or an exec, ends the trap before the completion
    check, so an aborted run can still exit 0 however exact the final check -
    and so may code this gate cannot read."""

    def test_in_the_body(self) -> None:
        # A value past 255 is unspecified by POSIX, so it may succeed. A
        # `return` in the trap's own code is read as an early end
        # (shell_reach.ending_spots says why).
        for early in (
            "return 0",
            "return",
            'eval "$X"',
            ". ./lib.sh",
            '"$CMD" stop',
            "exit 0",
            "exit",
            'exit "$rc"',
            "exit 256",
            "exit 257",
            "exec true",
            "command exit 0",
        ):
            with self.subTest(early=early):
                text = suite(f"trap '{GUARDED}; {early}; {CHECK}' EXIT")
                self.assertEqual(kinds(text), {"completion"})

    def test_a_failing_exit_ends_failing(self) -> None:
        # The safe direction: the trap ends non-zero.
        for failing in ("exit 1", "exit 255"):
            with self.subTest(failing=failing):
                text = suite(f"trap '{GUARDED}; {failing}; {CHECK}' EXIT")
                self.assertEqual(kinds(text), set())

    def test_through_a_function_guarded_or_not(self) -> None:
        helper = f"helper() {{\n    {GUARDED}\n    exit 0\n}}\n"
        for call in ("helper", "helper || echo WARN >&2"):
            with self.subTest(call=call):
                text = suite(f"trap '{call}; {CHECK}' EXIT", before=helper)
                self.assertEqual(kinds(text), {"completion"})

    def test_through_a_handler(self) -> None:
        handler = (
            f"helper() {{\n    exec true\n}}\n"
            f"cleanup() {{\n    helper\n    {CHECK}\n}}\n"
        )
        self.assertEqual(
            kinds(suite("trap cleanup EXIT", before=handler)), {"completion"}
        )


class QuoteRemoval(unittest.TestCase):
    """The shell reads a word after removing its quotes and backslashes, so a
    classifier must too: each of these is the plain form to the shell."""

    def test_an_escaped_trap_signal_or_wrapper(self) -> None:
        bare = 'rm -rf "$D"'
        for text in (
            f"#!/bin/sh\nD=x\ntr\\ap '{bare}' EXIT\n",
            f"#!/bin/sh\nD=x\ntrap '{bare}' E\\XIT\n",
            f"#!/bin/sh\nD=x\nco\\mmand trap '{bare}' EXIT\n",
            "#!/bin/sh\nD=x\ntrap '\\c\\ommand rm -rf \"$D\"' EXIT\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(blocks(text))

    def test_an_escaped_call_to_a_hazardous_helper(self) -> None:
        helper = 'cleanup() {\n    rm -rf "$D"\n}\n'
        text = suite(f"trap 'cl\\eanup; {CHECK}' EXIT", before=helper)
        self.assertIn("rm", kinds(text))


class EndingTheHandlerEarly(unittest.TestCase):
    def test_a_return_that_can_carry_zero(self) -> None:
        # `[ -n "$D" ] || return 0` is the realistic shape: the handler ends
        # before its final check, so an aborted run exits 0.
        for early in (
            "return 0",
            "return",
            'return "$rc"',
            "return 256",
            "return 257",
            "builtin return 0",
        ):
            with self.subTest(early=early):
                handler = f"cleanup() {{\n    {early}\n    {GUARDED}\n    {CHECK}\n}}\n"
                self.assertEqual(
                    kinds(suite("trap cleanup EXIT", before=handler)), {"completion"}
                )

    def test_a_failing_return_and_a_return_in_a_callee(self) -> None:
        failing = f"cleanup() {{\n    return 1\n    {CHECK}\n}}\n"
        callee = (
            "helper() {\n    return 0\n}\n"
            f"cleanup() {{\n    helper\n    {GUARDED}\n    {CHECK}\n}}\n"
        )
        self.assertEqual(kinds(suite("trap cleanup EXIT", before=failing)), set())
        # A callee's return only returns to the handler.
        self.assertEqual(kinds(suite("trap cleanup EXIT", before=callee)), set())

    def test_exit_and_exec_through_builtin_and_command(self) -> None:
        for early in ("builtin exit 0", "builtin exec true", "command exec true"):
            with self.subTest(early=early):
                self.assertEqual(
                    kinds(suite(f"trap '{GUARDED}; {early}; {CHECK}' EXIT")),
                    {"completion"},
                )


class ReasonsAreKept(unittest.TestCase):
    def test_a_parse_failure_names_shfmt_reason(self) -> None:
        text = '#!/bin/sh\nD=x\ntrap \'echo cleaning\n    rm -rf "$D"\n'
        with self.assertRaises(ShellParseError) as raised:
            parse_shell(text, "shfmt")
        (finding,) = gate.judge_file("t/test-a.sh", text, TOOLS)
        self.assertIn(str(raised.exception), finding.message)

    def test_an_unreadable_file_names_the_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (finding,) = gate.check_files(
                Path(directory), ["missing/unit-test-a.sh"], TOOLS
            )
        self.assertIn("No such file", finding.message)


class WhichDefinitionRuns(unittest.TestCase):
    """A name defined more than once, or inside a branch or another function, has
    its body decided at run time, so a safe definition can mask the one that
    runs. Only a single top-level definition resolves."""

    BAD = 'cleanup() {\n    rm -rf "$D"\n}\n'
    SAFE = f"cleanup() {{\n    {GUARDED}\n    {CHECK}\n}}\n"

    def test_a_later_safe_definition_does_not_mask_the_first(self) -> None:
        text = suite("trap cleanup EXIT", before=self.BAD + self.SAFE)
        self.assertIn("unreadable", kinds(text))

    def test_a_definition_in_a_branch_or_a_function(self) -> None:
        in_branch = f"if false; then\n{self.SAFE}fi\n"
        nested = f"setup() {{\n{self.SAFE}}}\n"
        for before in (self.BAD + in_branch, in_branch, nested):
            with self.subTest(before=before):
                self.assertIn(
                    "unreadable", kinds(suite("trap cleanup EXIT", before=before))
                )

    def test_a_reached_helper_defined_twice(self) -> None:
        helpers = f"helper() {{\n    {GUARDED}\n}}\nhelper() {{\n    true\n}}\n"
        text = suite(f"trap 'helper; {CHECK}' EXIT", before=helpers)
        self.assertIn("unreadable", kinds(text))

    def test_a_single_top_level_definition_resolves(self) -> None:
        self.assertEqual(kinds(suite("trap cleanup EXIT", before=self.SAFE)), set())


class FailClosed(unittest.TestCase):
    def test_expansions_this_file_cannot_read(self) -> None:
        self.assertEqual(
            kinds('#!/bin/sh\nCLEANUP="rm -rf /tmp/x"\ntrap "$CLEANUP" EXIT\n'),
            {"unreadable"},
        )
        self.assertEqual(
            kinds("#!/bin/sh\nD=x\nSIGS=EXIT\ntrap 'rm -rf \"$D\"' $SIGS\n"),
            {"unreadable"},
        )

    def test_a_string_bash_rewrites(self) -> None:
        # Bash translates `$"..."` and decodes `$'...'` before a command runs,
        # so no word holding one can be read: a 0 init, a failing exit code,
        # an action, a signal, or a trap written in escapes alike.
        trap = f"trap '{GUARDED}; {CHECK}' EXIT"
        for text in (
            suite(trap).replace("SUITE_COMPLETE=0", 'SUITE_COMPLETE=$"0"'),
            suite(trap).replace("SUITE_COMPLETE=1", 'SUITE_COMPLETE=$"1"'),
            suite(f"trap '{GUARDED}; exit $\"1\"; {CHECK}' EXIT"),
            suite(f"trap '{GUARDED}; {CHECK}' $\"EXIT\""),
            suite(f'trap $"{CHECK}" EXIT'),
            # \164\162\141\160 is trap.
            suite("$'\\164\\162\\141\\160' 'rm -rf \"$D\"' EXIT"),
        ):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), {"unreadable"})

    def test_an_action_built_with_quote_idioms(self) -> None:
        # The trap runs its action after quote removal, so `'\''` there is a
        # bare quote: these actions run `$'rm'`, set the sentinel through
        # `$'...'`, or are not shell at all - read as written, they looked
        # like escaped literals.
        q = "'\\''"
        for action in (
            f'{GUARDED}; ${q}rm{q} -rf "$D"; {CHECK}',
            f":; export ${q}SUITE_COMPLETE=1{q}; {CHECK}",
            f"echo it{q}s; {CHECK}",
        ):
            with self.subTest(action=action):
                self.assertEqual(kinds(suite(f"trap '{action}' EXIT")), {"unreadable"})

    def test_a_file_shfmt_cannot_parse(self) -> None:
        self.assertEqual(
            kinds('#!/bin/sh\nD=x\ntrap \'echo cleaning\n    rm -rf "$D"\n'),
            {"unreadable"},
        )

    def test_a_file_that_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            findings = gate.check_files(
                Path(directory), ["missing/unit-test-a.sh"], TOOLS
            )
        self.assertEqual([finding.kind for finding in findings], ["unreadable"])


class TheCompletionCheck(unittest.TestCase):
    def test_the_canonical_form_passes_the_whole_gate(self) -> None:
        self.assertEqual(
            kinds(suite(f"trap '{GUARDED}; {CHECK}' EXIT INT TERM")), set()
        )

    def test_a_guarded_trap_without_it(self) -> None:
        # The usual guarded trap: after an unbound variable, bash 3.2 reports a
        # suite that skipped its later assertions as passing.
        self.assertEqual(
            kinds(suite(f"trap '{GUARDED}' EXIT INT TERM")), {"completion"}
        )

    def test_saving_and_restoring_the_status(self) -> None:
        # The fix that looks right: the stale $? is what gets saved.
        trap = f"trap 'rc=$?; {GUARDED}; exit \"$rc\"' EXIT"
        self.assertEqual(kinds(suite(trap)), {"completion"})

    def test_forms_that_never_fail_the_run(self) -> None:
        for body in (
            f'{GUARDED}; [ "$SUITE_COMPLETE" = 1 ]',
            f'{GUARDED}; echo "$SUITE_COMPLETE"; exit 0',
            f"{CHECK}; {GUARDED}",
        ):
            with self.subTest(body=body):
                self.assertEqual(kinds(suite(f"trap '{body}' EXIT")), {"completion"})

    def test_one_accepted_form(self) -> None:
        # Any other way of writing the test is reported, the safe direction.
        other = f"trap '{GUARDED}; [ \"$SUITE_COMPLETE\" -eq 1 ] || exit 1' EXIT"
        self.assertEqual(kinds(suite(other)), {"completion"})
        spaced = f"trap '{GUARDED}\n    [ \"$SUITE_COMPLETE\" = 1 ]   ||   exit 1' EXIT"
        self.assertEqual(kinds(suite(spaced)), set())

    def test_a_handler_is_judged_on_its_own_body(self) -> None:
        with_check = f"cleanup() {{\n    {GUARDED}\n    {CHECK}\n}}\n"
        without = f"cleanup() {{\n    {GUARDED}\n}}\n"
        subshell = f"cleanup() (\n    {GUARDED}\n    {CHECK}\n)\n"
        self.assertEqual(kinds(suite("trap cleanup EXIT", before=with_check)), set())
        self.assertEqual(
            kinds(suite("trap cleanup EXIT", before=without)), {"completion"}
        )
        # An exit in a subshell body leaves the subshell, not the suite.
        self.assertEqual(
            kinds(suite("trap cleanup EXIT", before=subshell)), {"completion"}
        )

    def test_an_empty_trap(self) -> None:
        # It holds no rm, and still exits 0 after an abort on macOS /bin/sh.
        self.assertEqual(kinds(suite("trap '' EXIT")), {"completion"})

    def test_traps_that_need_no_check(self) -> None:
        self.assertEqual(kinds(suite("trap - EXIT")), set())
        self.assertFalse(
            lacks_completion(f"#!/bin/sh\nD=x\ntrap '{GUARDED}' INT TERM\n")
        )


class TheSentinelLifecycle(unittest.TestCase):
    TRAP = f"trap '{GUARDED}; {CHECK}' EXIT"

    def judge(self, text: str) -> set[str]:
        return kinds(text) & {"completion", "lifecycle"}

    def test_no_init_before_the_trap(self) -> None:
        # An inherited SUITE_COMPLETE=1 then passes an aborted run.
        self.assertEqual(
            self.judge(f"#!/bin/sh\n{self.TRAP}\nSUITE_COMPLETE=1\n"), {"lifecycle"}
        )
        self.assertEqual(
            self.judge(f"#!/bin/sh\n{self.TRAP}\nSUITE_COMPLETE=0\nSUITE_COMPLETE=1\n"),
            {"lifecycle"},
        )

    def test_no_final_assignment(self) -> None:
        self.assertEqual(
            self.judge(f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\n"), {"lifecycle"}
        )

    def test_code_after_the_final_assignment(self) -> None:
        # An abort in that code passes.
        text = (
            f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\nSUITE_COMPLETE=1\necho more\n"
        )
        self.assertEqual(self.judge(text), {"lifecycle"})

    def test_assignments_that_may_not_run_where_they_stand(self) -> None:
        cases = {
            "init only inside a function": (
                f"#!/bin/sh\nsetup() {{\n    SUITE_COMPLETE=0\n}}\n{self.TRAP}\nSUITE_COMPLETE=1\n"
            ),
            "final only inside a branch": (
                f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\nif true; then\n    SUITE_COMPLETE=1\nfi\n"
            ),
            "final backgrounded": f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\nSUITE_COMPLETE=1 &\n",
            # Bash assigns first, then the redirection's expansion aborts.
            "final redirected": (
                f'#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\nSUITE_COMPLETE=1 >"$UNSET"\n'
            ),
            "final negated": f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\n! SUITE_COMPLETE=1\n",
            "final as a command prefix": (
                f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\nSUITE_COMPLETE=1 true\n"
            ),
            "final appended": f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\nSUITE_COMPLETE+=1\n",
            "init exported": f"#!/bin/sh\nexport SUITE_COMPLETE=0\n{self.TRAP}\nSUITE_COMPLETE=1\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(self.judge(text), {"lifecycle"})

    def test_any_other_write_of_the_sentinel(self) -> None:
        # Set to 1 early, it stays 1 while the code after it can still abort,
        # so the trap passes that run whatever the final assignment says.
        for write in (
            "SUITE_COMPLETE=1",
            "f() {\n    SUITE_COMPLETE=1\n}",
            "if true; then\n    SUITE_COMPLETE=1\nfi",
            "unset SUITE_COMPLETE",
            "export SUITE_COMPLETE",
            "read SUITE_COMPLETE",
            # The shell joins the name after quote removal.
            'read SUITE"_COMPLETE"',
            "unset SUITE\\_COMPLETE",
        ):
            with self.subTest(write=write):
                text = (
                    f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\n{write}\n"
                    ': "$X"\nSUITE_COMPLETE=1\n'
                )
                self.assertEqual(self.judge(text), {"lifecycle"})

    def test_a_write_inside_the_inline_body(self) -> None:
        # The body is a string in the file's tree; setting 1 in it passes the
        # check that follows whatever the run did.
        trap = f"trap ':; SUITE_COMPLETE=1; {CHECK}' EXIT"
        text = f"#!/bin/sh\nSUITE_COMPLETE=0\n{trap}\nSUITE_COMPLETE=1\n"
        self.assertEqual(self.judge(text), {"lifecycle"})

    def test_a_name_written_in_ansi_c_escapes(self) -> None:
        # SUITE$'\x5f'COMPLETE is SUITE_COMPLETE once bash decodes it.
        text = (
            f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\n"
            "read SUITE$'\\x5f'COMPLETE\nSUITE_COMPLETE=1\n"
        )
        self.assertEqual(kinds(text), {"unreadable"})

    def test_a_plain_read_of_the_sentinel(self) -> None:
        text = (
            f"#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\n"
            'echo "$SUITE_COMPLETE"\nSUITE_COMPLETE=1\n'
        )
        self.assertEqual(self.judge(text), set())

    def test_a_handler_check_needs_the_lifecycle_too(self) -> None:
        text = f"#!/bin/sh\ncleanup() {{\n    {CHECK}\n}}\ntrap cleanup EXIT\n"
        self.assertEqual(self.judge(text), {"lifecycle"})

    def test_a_quoted_final_value(self) -> None:
        text = f'#!/bin/sh\nSUITE_COMPLETE=0\n{self.TRAP}\nSUITE_COMPLETE="1"\n'
        self.assertEqual(self.judge(text), set())


class TheFormOnThisHostsShell(unittest.TestCase):
    def test_the_canonical_form_fails_an_aborted_run(self) -> None:
        # /bin/sh on a Mac is the bash 3.2 sh that loses the abort status, so
        # this pins the claim the gate rests on wherever the suite runs there.
        form = (
            f"set -eu; D=$(mktemp -d); SUITE_COMPLETE=0; trap '{GUARDED}; {CHECK}' EXIT"
        )
        aborted = subprocess.run(
            ["/bin/sh", "-c", f'{form}; : "$NOT_SET_IN_THIS_TEST"; SUITE_COMPLETE=1'],
            capture_output=True,
            check=False,
        )
        completed = subprocess.run(
            ["/bin/sh", "-c", f"{form}; SUITE_COMPLETE=1"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(aborted.returncode, 1)
        self.assertEqual(completed.returncode, 0)


class TheToolchain(unittest.TestCase):
    def test_a_missing_shfmt_stops_the_gate(self) -> None:
        # Returning 0 here would pass the lint while examining nothing.
        with self.assertRaises(gate.ToolchainError):
            gate.derive_toolchain("/nonexistent/shfmt")


if __name__ == "__main__":
    unittest.main()
