"""Tests for shfmt_ast.py: what shfmt's tree says a word and a call are."""

from __future__ import annotations

import unittest

from shell_trap_lint.shfmt_ast import (
    ShellParseError,
    ShfmtUnavailable,
    UnreadableCommand,
    builtin_args,
    call_args,
    calls,
    effective_command,
    expansion_types,
    literal_word,
    mentions_beyond_read,
    parse_shell,
    special_quote_line,
    strip_positions,
    word_has_expansion,
    word_is_expansion,
    word_kind,
    word_pieces,
    word_text,
)


def first_call(source: str) -> list[dict[str, object]]:
    return call_args(next(calls(parse_shell(source + "\n", "shfmt"))))


class WordText(unittest.TestCase):
    def test_quotes_join_and_expansions_become_inert(self) -> None:
        # word_text renders a word for a message: `$D` stays one harmless word
        # rather than vanishing and joining its neighbors, and an escaped quote
        # keeps its backslash as written. A trap body is re-parsed from
        # literal_word instead (TrapActionText below).
        words = [
            word_text(word) for word in first_call("""echo 'a'"b"c "$D" $E 'x'\\''y'""")
        ]
        self.assertEqual(words, ["echo", "abc", "X", "X", "x\\'y"])

    def test_a_word_that_is_only_an_expansion(self) -> None:
        args = first_call('echo "$D" $E "a$D" lit')
        self.assertEqual(
            [word_is_expansion(word) for word in args[1:]], [True, True, False, False]
        )


class SpecialQuotes(unittest.TestCase):
    def test_strings_bash_rewrites(self) -> None:
        # `$"..."` is translated and `$'...'` decoded before a command runs,
        # whatever the shebang: parse_shell reads every file as bash.
        for source, line in (
            ('echo $"x"\n', 1),
            ('#!/bin/sh\necho a$"x"\n', 2),
            ('#!/bin/sh\necho \\\\$"x"\n', 2),
            ("#!/bin/sh\necho $'\\164rap'\n", 2),
        ):
            with self.subTest(source=source):
                self.assertEqual(special_quote_line(parse_shell(source, "shfmt")), line)

    def test_a_dollar_that_is_not_one(self) -> None:
        for source in (
            '#!/bin/sh\necho \\$"x"\n',
            "#!/bin/sh\necho '$'\"x\" '^$'\n",
            '#!/bin/sh\necho "$" "$$"\n',
        ):
            with self.subTest(source=source):
                self.assertIsNone(special_quote_line(parse_shell(source, "shfmt")))


class WordKind(unittest.TestCase):
    def test_static_path_or_unknown(self) -> None:
        # Closed: a word is static only when every part is known to read as
        # written, a path only when its first field must keep a literal `/`,
        # and unknown otherwise - a rewrite, a split, a translation alike.
        cases = {
            "lit": "static",
            '"a"': "static",
            "'b'": "static",
            "\\*": "static",
            "[": "static",
            "]": "static",
            "/bin/rm": "static",
            '"$D/x"': "path",
            '"$D"/x': "path",
            '"$(dirname "$0")/x"': "path",
            '"$D"': "unknown",
            "a$D": "unknown",
            '"$(x)"': "unknown",
            "$D/x": "unknown",
            "~": "unknown",
            "~/x": "unknown",
            "ex?t": "unknown",
            "e*": "unknown",
            "[ab]x": "unknown",
            "{a,b}": "unknown",
            "{a,b/c}": "unknown",
            "./*.sh": "unknown",
            '"$@"/x': "unknown",
            '"${a[@]}/x"': "unknown",
            '"${!p@}/x"': "unknown",
            '$"x"': "unknown",
            "$'x'": "unknown",
        }
        args = first_call("echo " + " ".join(cases))
        kinds = [word_kind(word) for word in args[1:]]
        self.assertEqual(dict(zip(cases, kinds, strict=True)), cases)
        self.assertEqual(
            [word_has_expansion(word) for word in args[1:4]], [False, False, False]
        )


class TrapActionText(unittest.TestCase):
    def test_an_escaped_quote_joins_as_a_bare_quote(self) -> None:
        # The string a trap evaluates: `'\''` leaves a bare `'`, which word_text
        # would keep escaped.
        word = first_call("""echo 'x'\\''y'""")[1]
        self.assertEqual(literal_word(word), "x'y")
        self.assertEqual(word_text(word), "x\\'y")


class WordPieces(unittest.TestCase):
    def test_literal_pieces_in_order_after_quote_removal(self) -> None:
        args = first_call('echo "E${MID}IT" a\\"b$C \'d\' "$E" $\'x\'')
        self.assertEqual(
            [word_pieces(word) for word in args[1:]],
            [["E", None, "IT"], ['a"b', None], ["d"], [None], [None]],
        )


class ExpansionTypes(unittest.TestCase):
    def test_each_non_literal_part_by_type(self) -> None:
        # A ParamExp carries a `Dollar` position of its own; only a quote's
        # flag marks a `$"..."` string.
        args = first_call('echo "$((0))" @(EXIT) "$(c)" ${P} $X $"x" a"$B" "${#X}"')
        self.assertEqual(
            [expansion_types(word) for word in args[1:]],
            [
                {"ArithmExp"},
                {"ExtGlob"},
                {"CmdSubst"},
                {"ParamExp"},
                {"ParamExp"},
                {"DollarQuoted"},
                {"ParamExp"},
                {"ParamExpOp"},
            ],
        )


class PlainParameterShape(unittest.TestCase):
    def test_an_unknown_member_is_an_operator(self) -> None:
        # A member a later shfmt might add is read as an operator, never as a
        # plain value.
        word = first_call("echo ${X}")[1]
        self.assertEqual(expansion_types(word), {"ParamExp"})
        parts = word["Parts"]
        assert isinstance(parts, list)
        parts[0]["Future"] = {"Type": "Word"}
        self.assertEqual(expansion_types(word), {"ParamExpOp"})

    def test_an_unknown_member_is_more_than_a_plain_read(self) -> None:
        # Named apart from `X`, the inert text literal_word gives an expansion.
        word = first_call("echo ${SENTINEL}")[1]
        self.assertFalse(mentions_beyond_read(word, "SENTINEL"))
        parts = word["Parts"]
        assert isinstance(parts, list)
        parts[0]["Future"] = {"Type": "Word"}
        self.assertTrue(mentions_beyond_read(word, "SENTINEL"))


class LiteralWord(unittest.TestCase):
    def test_quote_removal(self) -> None:
        # What the shell reads: an unquoted backslash escapes the next
        # character; inside double quotes it escapes only $, `, " and itself.
        words = [
            literal_word(word)
            for word in first_call("""tr\\ap E\\XIT "a\\$b\\c" 'x\\y' \\\\z""")
        ]
        self.assertEqual(words, ["trap", "EXIT", "a$b\\c", "x\\y", "\\z"])


class Positions(unittest.TestCase):
    def test_the_same_statement_compares_equal_wherever_written(self) -> None:
        here = parse_shell('x=1\n[ "$A" = 1 ] || exit 1\n', "shfmt")["Stmts"][1]
        there = parse_shell('[ "$A" = 1 ]   ||   exit 1\n', "shfmt")["Stmts"][0]
        self.assertEqual(strip_positions(here), strip_positions(there))


class EffectiveCommand(unittest.TestCase):
    def test_through_wrappers_paths_and_escapes(self) -> None:
        for source in (
            "rm x",
            '"rm" x',
            "\\rm x",
            "/bin/rm x",
            "command rm x",
            "command -p -- rm x",
            "exec rm x",
            "env A=1 B=2 rm x",
            # A path after a wrapper is the command, whatever precedes its `/`.
            'command "$D/rm" x',
            "env - rm x",
            "/usr/bin/env A=1 rm x",
            "/bin/env A=1 rm x",
            "/usr/bin/env - /bin/rm x",
            # env's assignments may follow its `--`, a dash-led name included.
            "env -- A=1 rm x",
            "/usr/bin/env -- A=1 rm x",
            "env -- -A=1 rm x",
            "/usr/bin/env -- -A=1 rm x",
        ):
            with self.subTest(source=source):
                self.assertEqual(effective_command(first_call(source)), "rm")

    def test_a_query_runs_nothing(self) -> None:
        self.assertIsNone(effective_command(first_call("command -v rm")))
        self.assertIsNone(effective_command(first_call("command -pV rm")))

    def test_an_option_it_does_not_read(self) -> None:
        # A path in place of a builtin runs a file (POSIX, Command Search and
        # Execution).
        for source in (
            "env -i rm x",
            "exec -a name rm x",
            "command -x rm",
            "/usr/bin/command rm x",
            # An env at any other path may read its operands its own way.
            "./env A=1 rm x",
            '"$DIR/env" A=1 rm x',
            # An expansion's value decides whether it is an option, `--`, an
            # assignment or the command.
            'command "$SEP" rm x',
            'env "$ASSIGN" rm x',
            'exec "$X" rm x',
            # env takes a word holding `=` as an assignment, path or not, and
            # an expansion can supply the `=` (D=A=).
            'env "$D/x=1" rm x',
            'env "$D/x" rm x',
            # exec's -a takes an attached argument (D=-a runs rm).
            'exec "$D/x" rm x',
            # After `--` the expansion is the command, still decided at run time.
            'command -- "$CMD" -rf x',
            'exec -- "$CMD" -rf x',
            # Whether env takes this `-` as `-i` or as the command is left
            # undecided.
            "env -- - rm x",
        ):
            with self.subTest(source=source), self.assertRaises(UnreadableCommand):
                effective_command(first_call(source))


class BuiltinArgs(unittest.TestCase):
    def test_how_the_shell_reaches_a_builtin(self) -> None:
        for source in (
            "trap a EXIT",
            "command trap a EXIT",
            "builtin trap a EXIT",
            "command -- command trap a EXIT",
        ):
            with self.subTest(source=source):
                rest = builtin_args(first_call(source), "trap")
                self.assertEqual(
                    [word_text(word) for word in rest or []], ["a", "EXIT"]
                )

    def test_an_expansion_among_a_wrappers_operands(self) -> None:
        # With SEP=--, or CMD=trap after the `--`, these install the trap.
        for source in (
            'command "$SEP" trap a EXIT',
            'command -- "$CMD" a EXIT',
            'builtin -- "$CMD" a EXIT',
        ):
            with self.subTest(source=source), self.assertRaises(UnreadableCommand):
                builtin_args(first_call(source), "trap")

    def test_forms_that_run_something_else(self) -> None:
        for source in (
            "command -v trap a EXIT",
            "builtin -p trap a EXIT",
            "command -- -p trap a",
            "echo trap",
        ):
            with self.subTest(source=source):
                self.assertIsNone(builtin_args(first_call(source), "trap"))


class Failures(unittest.TestCase):
    def test_a_missing_shfmt_and_a_rejected_text(self) -> None:
        with self.assertRaises(ShfmtUnavailable):
            parse_shell("true\n", "/nonexistent/shfmt")
        with self.assertRaises(ShellParseError):
            parse_shell("if then fi (\n", "shfmt")


if __name__ == "__main__":
    unittest.main()
