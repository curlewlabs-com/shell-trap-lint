"""Which calls in a shell file install an EXIT trap, read from shfmt's AST.

The trap gates start here: cleanup_trap_check.py for unit tests and
script_trap_check.py for operational scripts judge each trap exit_traps
returns, and report each finding it raises about a call this reading cannot
tell apart from a trap install. What counts as reaching the `trap` builtin,
naming EXIT, and an unreadable action or signal list is policy, which
cleanup_trap_check.py's header states; this module carries it out.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from shell_trap_lint.shfmt_ast import (
    Node,
    UnreadableCommand,
    builtin_args,
    call_args,
    calls,
    expansion_types,
    line_of,
    literal_word,
    rewritten_literal,
    word_is_expansion,
    word_kind,
    word_pieces,
)

_ZERO = re.compile(r"\s*\+?0+\s*", re.ASCII)
_TRAP_LIST_OR_PRINT = re.compile(r"-[lp]+")
# What a wholly expanded argument may be built from and still be read as no
# evidence of a signal: a parameter's plain value (`$X`, `${X}`) or a command's
# output. A parameter expansion with an operator is `ParamExpOp`, not here.
_PLAIN_EXPANSIONS = frozenset({"ParamExp", "CmdSubst"})


def names_exit(signal: str) -> bool:
    return signal.lower() == "exit" or _ZERO.fullmatch(signal) is not None


def _may_name_exit(word: Node) -> bool:
    """Whether an argument can read as an EXIT signal: a static one that names
    it; one holding an unquoted literal the shell rewrites (`E*`, a glob that
    can match a file named EXIT), whatever else it holds; or one whose literal
    pieces, in order, can be completed into a name for it by what its
    expansions hold - `"${P}EXIT"`, `"E${MID}IT"`, or pieces drawn only from
    _ZERO's characters, as `" $Z"` is. An argument built wholly from parameter
    expansions and command substitutions is no evidence either way
    (cleanup_trap_check.py's out-of-scope list says why); one built from any
    other expansion - arithmetic (`$((0))`), an extended glob (`@(EXIT)`), a
    part type this reading does not know - may name EXIT."""
    if word_kind(word) == "static":
        return names_exit(literal_word(word))
    if rewritten_literal(word):
        return True
    pieces = word_pieces(word)
    if all(piece is None for piece in pieces):
        return not expansion_types(word) <= _PLAIN_EXPANSIONS
    pattern = "".join(".*" if piece is None else re.escape(piece) for piece in pieces)
    literals = "".join(piece for piece in pieces if piece)
    return bool(re.fullmatch(pattern, "exit", re.IGNORECASE)) or set(literals) <= set(
        " \t\n+0"
    )


def exit_traps[F](
    tree: Node, found: Callable[[int, str, str], F]
) -> tuple[list[tuple[Node, Node]], list[F]]:
    """Each call in `tree` that installs an EXIT trap, with its action word, and
    a finding for each trap this file cannot read."""
    traps: list[tuple[Node, Node]] = []
    findings: list[F] = []
    for call in calls(tree):
        line = line_of(call)
        words = call_args(call)
        if (
            len(words) >= 3
            and word_kind(words[0]) not in ("static", "path")
            and any(_may_name_exit(word) for word in words[2:])
        ):
            findings.append(
                found(
                    line,
                    "unreadable",
                    "this call's command is decided at run time and an argument "
                    "after its first may name EXIT, so it may install a cleanup "
                    "trap this gate cannot read. Name the command plainly.",
                )
            )
            continue
        try:
            rest = builtin_args(words, "trap")
        except UnreadableCommand as error:
            findings.append(
                found(
                    line,
                    "unreadable",
                    f"this call runs {error}, so this gate cannot tell whether it "
                    "installs a cleanup trap. Name the command plainly.",
                )
            )
            continue
        if not rest:
            continue
        first = literal_word(rest[0])
        # `--` ends trap's options and bash accepts `-l`/`-p`, so the action is
        # argument 1 only once those are consumed. `trap -p` in bash prints the
        # `trap -- ACTION SIGNAL` form, which is how it reaches a file. Read
        # after quote removal, since `trap "--" ...` ends options the same way.
        if first == "--":
            args = rest[1:]
        elif first.startswith("-") and len(first) > 1:
            if not _TRAP_LIST_OR_PRINT.fullmatch(first):
                findings.append(
                    found(
                        line,
                        "unreadable",
                        f"cleanup trap is invoked with the unrecognized option "
                        f"`{first}`, so this gate cannot tell what it installs. "
                        "Drop the option, or end the options with `--`.",
                    )
                )
            continue
        else:
            args = rest
        if len(args) < 2:
            continue
        action, signal_words = args[0], args[1:]
        # A signal list this file cannot read might or might not name EXIT, and
        # "might not" is the fail-open answer.
        if any(word_kind(word) != "static" for word in signal_words):
            findings.append(
                found(
                    line,
                    "unreadable",
                    "cleanup trap signal list holds an expansion, so this gate "
                    "cannot tell whether it includes EXIT. Write the signals "
                    "literally.",
                )
            )
            continue
        if not any(names_exit(literal_word(word)) for word in signal_words):
            continue
        if word_is_expansion(action):
            findings.append(
                found(
                    line,
                    "unreadable",
                    "cleanup trap action is an expansion, so its body is not "
                    "readable from this file. Inline the body or name a "
                    "same-file function.",
                )
            )
            continue
        traps.append((call, action))
    return traps, findings
