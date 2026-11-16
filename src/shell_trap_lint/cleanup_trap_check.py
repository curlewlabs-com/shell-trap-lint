"""The test form: a unit test's EXIT trap cannot change its verdict in either
direction.

A failed teardown is not a failed test, and a run that stopped short is not a
passed test. A test runner reads a shell test's exit code as its verdict, and
that is the only thing the test hands back.

WHY THE GUARD. Under `set -e` an EXIT trap whose command fails trips errexit
INSIDE the trap and publishes the trap's status, so `trap 'rm -rf "$D"' EXIT`
makes a scratch directory that will not delete indistinguishable from an
assertion that failed: a test whose assertions all passed is reported broken
when its `rm -rf` hits `Directory not empty`, and the reader goes hunting for
an assertion that never failed. The fix each trap carries is `|| echo ...
>&2` on its `rm`, which keeps errexit out of the trap and leaves a pass at 0
and a failure whose cleanup also failed non-zero.

WHY THE COMPLETION CHECK. A run that ABORTED has no status left to keep. When
`set -u` stops a script on an unbound variable, bash 3.2 - macOS's /bin/sh and
/bin/bash - enters the EXIT trap with a stale `$?`, the status of the last
command that completed, and exits with the trap's own final status, so the
suite skips every assertion after the unbound reference and exits 0. Saving
`rc=$?` in the trap saves the same stale value, and even `trap '' EXIT` exits
0. dash and bash 5 exit non-zero there. Nothing the trap can read tells an
abort from a pass, so the suite records that it finished:

    SUITE_COMPLETE=0
    trap 'rm -rf "$D" || echo "WARN: ..." >&2; [ "$SUITE_COMPLETE" = 1 ] || exit 1' EXIT
    ...
    SUITE_COMPLETE=1    # the suite's last top-level statement

A run that never reaches the assignment exits 1 whatever the shell. So does a
skip path that exits 0 early, the visible direction: the lifecycle below allows
no earlier write of the 1.
The check accepts one form rather than judging what a body means: the final
statement of the trap body, or of the same-file brace handler it names, must be
exactly that line, compared as an AST against the line parsed at run time with
source positions removed. The lifecycle is read from top-level statements only,
since an assignment inside a function or a branch may never run:
`SUITE_COMPLETE=0` ahead of the trap - without it an aborted run reads whatever
the environment exported, and an inherited 1 passes it on bash 3.2 - and
`SUITE_COMPLETE=1` as the file's last top-level statement, so no top-level code
can run, and abort, after it - and no other write of the sentinel anywhere,
since an earlier 1 would already be set when such an abort reached the trap.
Any mention but a plain `$SUITE_COMPLETE` read counts as a write
(shfmt_ast.mentions_beyond_read). A plain assignment only: a prefix on a command
(`SUITE_COMPLETE=1 cmd`) sets it for that command, a backgrounded one sets it in
a subshell, and `+=` or an index does not write the value it shows.

WHY AN AST AND NOT A PATTERN. A scanner over the source text fails open on
shell's grammar: a handler named above its own definition, a split-brace
`f()`, a `||` before the `rm` it was meant to guard, a multi-line body, a
trailing comment whose apostrophe reads as the body's closing quote, the numeric
`trap ... 0` form, and a body assembled from adjacent quoted regions each let an
unguarded trap through one. So the question goes to shfmt (`--to-json`): a
`trap` in a comment or a string is not a CallExpr, and a body's parts are joined
by the parser. An `rm` counts as guarded when it sits anywhere in the LEFT
operand of a `||`, which is the shell's own rule: `false || rm -rf "$D"` is
caught, `rm a || rm b` guards only `rm a`.

WHAT IS READ. Anything not statically readable is reported, never skipped.
Handled by the parser: indentation and traps inside functions; comments,
including one quoting the bad shape; `trap` inside a string payload
(`sh -c 'trap "" TERM; ...'`); every quoting form of a body, adjacent quoted
regions included; multi-line bodies; function delegation in any definition order
or brace style.
Handled here (which call installs an EXIT trap is trap_discovery.py's to read):
  * The EXIT signal word as the shells read it: case-insensitively (`Exit`
    installs an EXIT trap in sh, dash and bash alike) and as any all-zero number
    (`0` and `00` everywhere, `+0` and a padded ` 0` in bash). No shell measured
    accepts a `SIG` prefix on EXIT, so `SIGEXIT` is not an alias.
  * Same-file handler resolution, TRANSITIVELY: a handler whose helper holds the
    bare `rm` is a hazard, propagated across unguarded calls to a fixed point
    bounded by the function count, so mutual recursion converges. A guarded call
    (`helper || echo ...`) does not propagate. A handler this file does not
    define is reported, and so is a name defined more than once or inside a
    branch or another function, since which body runs is then decided at run
    time and a safe definition could mask the one that runs.
  * Every word a classifier compares - a builtin, a wrapper, an option, a
    signal, a function name, the sentinel's value - read after quote removal,
    as the shell reads it, so `tr\\ap`, `E\\XIT` and `cl\\eanup` are trap, EXIT
    and cleanup (shfmt_ast.literal_word).
  * The command an `rm` check reads, through `command` (its `-p` and `--`),
    `exec` and `env` (its assignments and a lone `-`), by basename, so
    `/bin/rm`, `\\rm` and `command -- rm` are rm. A call whose command the gate
    cannot pin down - a wrapper option it does not read, a path in place of a
    builtin - is reported with the reason.
  * Anything that ends the trap before its completion check: an `exit` that can
    carry 0, an `exec` running a command, or code this gate cannot read that
    may be an exit - `eval`, `.` / `source`, a command named by an expansion
    that is not a path - read through `command` and `builtin`, in the body or
    in any function it reaches - guarded or not, since a guard stops none of
    them - and a `return` that can carry 0 in the trap's own code, its inline
    body or its handler's body, read as able to end the trap with 0
    (shell_reach.ending_spots cites the contracts). A `return` in a function the
    trap's code calls only returns to that code. An `exit` or `return` whose code
    is a static literal 1 through 255 ends the trap failing, the safe
    direction; any other code is read as able to carry 0, since POSIX leaves a
    value outside 0-255 unspecified. shell_reach.py answers these reach
    questions.
  * `trap`'s option surface: `--` ends options (quoted or not), `-l`/`-p`
    install nothing, any other option is reported.
  * How the shell reaches the builtin: bare `trap`, `command trap` with its
    `[-pvV]` options (`v`/`V` make it a query that installs nothing) and `--`,
    and `builtin trap`, which takes no options.
  * The command word read through the parser, so `tr"ap"` resolves; no
    source-text prefilter stands in front of the parse, since one skipped those
    files whole.
  * An action word that is entirely an expansion, or a signal list holding any
    expansion: reported. So is a call whose own command word is decided at run
    time (shfmt_ast.word_kind: neither static nor a path) when an argument after
    its first may name EXIT (`trap_discovery._may_name_exit`), the shape of `"$CMD" 'action'
    EXIT TERM` with CMD=trap; one whose arguments cannot is not read as a trap.
  * `trap - EXIT` installs nothing and passes; `trap '' EXIT` is reported for
    the missing completion check.
  * A file that rebinds a command name this gate reads: reported, since the
    shell would run something other than what is read. That is an alias
    definition (dash and bash run as sh expand an alias in a script before
    they parse it, bash with `expand_aliases`; shfmt does not) and a function
    named for one of READ_BY_NAME.
  * A trap call with an argument that can expand to several words or none - an
    unquoted expansion, `"$@"`, a subscript - since every argument after it
    moves: reported (trap_discovery.exit_traps).
  * A file or a body shfmt cannot parse: reported. So is one holding a
    `$"..."` or `$'...'` string, which bash rewrites before the command runs,
    so no word in it can be read as written (shfmt_ast.special_quote_line).
  * The `||` operator's AST constant and the canonical check's AST are derived
    at run time from known snippets, so an shfmt upgrade that renames or retypes
    them cannot turn every trap into "guarded"; failing to derive either stops
    the gate.
Out of scope, each fail-open and named:
  * Teardown verbs other than `rm` (`rmdir`, `find -delete`), and wrappers
    other than `command`, `exec` and `env` (`nice`, `timeout`, `xargs`): each a
    new shape a reviewer sees.
  * The rm check does not read a command name that is an expansion
    (`"$RM" -rf "$D"`) as `rm` - the early-end check does count one, unless it
    is a path in a file that defines no function out of sight, or runs in a
    subshell (shell_reach.ending_spots). Nor does either read an expansion
    carrying command structure (`"cleanup; $EXTRA"`): expansions become an
    inert `X` before the body is re-parsed, which keeps the body's own
    structure but cannot see structure inside a value. Only a deliberate
    workaround writes one.
  * A run-time command word's argument built wholly from plain parameter
    expansions (`$X`, `${X}`) and command substitutions, or one that splits
    into several (`"$CMD" action "$SIG"`, `"$CMD" $ARGS` with CMD=trap), is not
    read as a possible EXIT signal: doing so reports every script that runs
    `"$VAR" args`, a common form that installs no trap, to catch one only a
    deliberate workaround writes. Any other wholly expanded argument -
    arithmetic, an extended glob, a parameter operator such as `${#X}` or
    `${X:+EXIT}` - is read as one (trap_discovery._may_name_exit).
  * `eval` carrying a trap in a literal string: a second interpreter this gate
    does not run.
  * A function a sourced lib defines - under a new name, or under one the trap
    calls as a command or a same-file helper (`kill`, `cleanup`, even `exit`) -
    for the rm check and the early-end check alike. The gate reads one file:
    reading the lib would take a package's lint outside its own files, and
    failing closed on every call a lib could shadow makes unreadable the traps
    of most files here, which source one, while a cleanup body also calls
    `printf`, `git` and `mkdir`. A file that sources a lib, or runs `eval`,
    does lose the expanded-path exemption (shell_reach.ending_spots). A handler
    named directly (`trap shared_cleanup EXIT`) IS reported.
Conservative in the safe direction: an `rm` whose failure an `if`, `while` or
`!` consumes is safe but still reported; the fix is the same guard.

"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import override

from shell_trap_lint.shell_reach import (
    Hazard,
    Reach,
    all_calls,
    ending_spots,
    function_hazards,
    single_definitions,
    unguarded_calls,
    unguarded_rm_lines,
)
from shell_trap_lint.shfmt_ast import (
    Node,
    ShellParseError,
    ShfmtUnavailable,
    UnreadableCommand,
    builtin_args,
    call_args,
    calls,
    line_of,
    literal_word,
    mentions_beyond_read,
    offset,
    parse_shell,
    special_quote_line,
    strip_positions,
    walk,
)
from shell_trap_lint.trap_discovery import exit_traps

SENTINEL = "SUITE_COMPLETE"
CANONICAL_CHECK = '[ "$SUITE_COMPLETE" = 1 ] || exit 1'
WHY = "the shell-trap-lint README"

HANDLER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

GUARD_HINT = '`|| echo "WARN: cleanup failed (test result unaffected): ..." >&2`'
COMPLETION_MSG = (
    "EXIT trap does not fail a run that stopped short - after a `set -u` abort, "
    "macOS /bin/sh enters the trap with a stale `$?` and the suite exits 0. Set "
    "`SUITE_COMPLETE=0` before the trap, make "
    '`[ "$SUITE_COMPLETE" = 1 ] || exit 1` the last statement of its body or '
    "handler, exactly as written here, and make `SUITE_COMPLETE=1` the last "
    f"statement of the suite ({WHY} says why)."
)
ENDS_EARLY_MSG = (
    "EXIT trap can end before its completion check - {where} can end it, so a "
    "run that stopped short can still exit 0. Keep `exit`, `exec`, `eval`, "
    "`source` and expanded command names out of the trap, or run them in a "
    "subshell; the completion check is its exit."
)
SPECIAL_QUOTE_MSG = (
    "{where} uses a `$\"...\"` or `$'...'` string ({line}), which bash rewrites "
    "before the command runs - translating the first for the locale, decoding "
    "the second's escapes - so this gate cannot read what it runs. Write it as "
    "a plain quoted string."
)
LIFECYCLE_MSG = (
    "EXIT trap ends with the SUITE_COMPLETE check, but this file does not "
    "assign `SUITE_COMPLETE=0` as a top-level statement before the trap, end "
    "with `SUITE_COMPLETE=1` as its last top-level statement, and write it "
    "nowhere else. Without the first, an aborted run reads a value inherited "
    "from the environment; with a 1 anywhere but last, an abort in the code "
    f"after it passes. Move or add the plain assignment ({WHY} has the "
    "form)."
)

Reporter = Callable[[int, str, str], "Finding"]


class ToolchainError(Exception):
    """shfmt is missing or its AST lost a shape this gate derives at run time."""


@dataclass(frozen=True)
class Finding:
    """One reported trap. `kind` is `rm` (a status-publishing `rm`),
    `completion` (no completion check), `lifecycle` (the check without the
    sentinel's assignments), or `unreadable` (fail-closed: this gate could not
    judge the trap)."""

    path: str
    line: int
    kind: str
    message: str

    @override
    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def ends_with_check(statements_holder: object, canon: object) -> bool:
    if not isinstance(statements_holder, dict):
        return False
    statements = statements_holder.get("Stmts") or []
    return bool(statements) and strip_positions(statements[-1]) == canon


def completing_handlers(tree: Node, canon: object) -> dict[str, bool]:
    """Which single top-level functions end with the completion check
    themselves. Direct only: a handler that delegates the check is reported,
    the safe direction. A brace body only, since an `exit` in a subshell body
    leaves the subshell."""
    single, _ = single_definitions(tree)
    done: dict[str, bool] = {}
    for name, node in single.items():
        body = (node.get("Body") or {}).get("Cmd") or {}
        done[name] = body.get("Type") == "Block" and ends_with_check(body, canon)
    return done


# The statement keys that say where it sits, not how it runs.
_BARE_STATEMENT = frozenset({"Pos", "End", "Position", "Comments", "Cmd", "Semicolon"})


def plain_value(statement: Node, name: str = SENTINEL) -> str | None:
    """The value a top-level statement assigns to the sentinel, when it is a
    plain assignment that runs in this shell with nothing around it. Any
    other statement key fails closed: bash assigns before it expands a
    redirection, so `SUITE_COMPLETE=1 >"$UNSET"` aborts with the 1 already
    set, and `&`, `!` or a coprocess change where or how it runs."""
    command = statement.get("Cmd") or {}
    if command.get("Type") != "CallExpr" or any(
        value for key, value in statement.items() if key not in _BARE_STATEMENT
    ):
        return None
    assigns = command.get("Assigns") or []
    if call_args(command) or len(assigns) != 1:
        return None
    assign = assigns[0]
    if (assign.get("Name") or {}).get("Value") != name:
        return None
    if assign.get("Append") or assign.get("Index") or assign.get("Array"):
        return None
    return literal_word(assign.get("Value"))


def lifecycle_holds(tree: Node, trap_offset: int) -> bool:
    """The sentinel is set to 0 by a top-level statement ahead of the trap, to 1
    by the file's last top-level statement, and written nowhere else. An
    earlier write of 1 - or an `unset`, `read` or `export` of it - leaves it 1
    while code after it can still abort, so the trap would pass that run."""
    statements = tree.get("Stmts") or []
    inits = [
        statement
        for statement in statements
        if plain_value(statement) == "0" and offset(statement) < trap_offset
    ]
    if not (
        inits
        and statements
        and plain_value(statements[-1]) == "1"
        and offset(statements[-1]) > trap_offset
    ):
        return False
    allowed = {id(statement) for statement in inits} | {id(statements[-1])}
    return not any(
        id(statement) not in allowed and mentions_beyond_read(statement, SENTINEL)
        for statement in statements
    )


@dataclass(frozen=True)
class Toolchain:
    shfmt: str
    or_op: object
    canon: object


def derive_toolchain(shfmt: str = "shfmt") -> Toolchain:
    """The AST shapes this gate compares against, read from shfmt itself.

    A hardcoded `||` constant that stopped matching would mark every `rm`
    guarded, and the type is not ours to assume: shfmt emitted `12` through
    v3.13.1 and `"||"` from v3.14.0. The canonical check parsed here moves with
    any upgrade that reshapes the AST. Failing to derive either stops the gate,
    since a constant that matches nothing checks nothing."""
    try:
        or_tree = parse_shell("a || b\n", shfmt)
        canon_tree = parse_shell(CANONICAL_CHECK + "\n", shfmt)
    except ShfmtUnavailable as error:
        raise ToolchainError(str(error)) from error
    except ShellParseError as error:
        raise ToolchainError(f"shfmt rejected a known snippet: {error}") from error
    binary = next(
        (node for node in walk(or_tree) if node.get("Type") == "BinaryCmd"), None
    )
    if binary is None or binary.get("Op") is None:
        raise ToolchainError("could not read shfmt AST for the || operator")
    statements = canon_tree.get("Stmts") or []
    if not statements:
        raise ToolchainError("could not read shfmt AST for the completion check")
    return Toolchain(shfmt, binary["Op"], strip_positions(statements[0]))


def parse_file(
    path: str, text: str, tools: Toolchain, found: Reporter
) -> Node | Finding:
    try:
        tree = parse_shell(text, tools.shfmt)
    except ShellParseError as error:
        return found(
            0,
            "unreadable",
            f"shfmt could not parse this file ({error}), so its cleanup traps were "
            "not checked.",
        )
    special = special_quote_line(tree)
    if special is not None:
        return found(
            special,
            "unreadable",
            SPECIAL_QUOTE_MSG.format(where="This file", line=f"line {special}"),
        )
    rebinding = rebinding_line(tree)
    if rebinding is not None:
        line, what = rebinding
        return found(line, "unreadable", REBINDING_MSG.format(what=what))
    return tree


# The commands this gate reads by name - the forms' `exit` and `[`, the
# wrappers it reads through, and what can end a trap early. A function of one
# of these names runs in place of the builtin, so the forms would read as
# written and do something else.
READ_BY_NAME = frozenset(
    {
        "[",
        "test",
        "exit",
        "return",
        "exec",
        "eval",
        "source",
        ".",
        "trap",
        "command",
        "builtin",
        "env",
        "rm",
    }
)
REBINDING_MSG = (
    "This file {what}, which changes what a command name runs before this "
    "gate's reading of it applies, so its traps were not checked. Remove it."
)


def rebinding_line(tree: Node) -> tuple[int, str] | None:
    """The first place a file rebinds a command name this gate reads: an
    alias definition, which the shell expands before it parses and shfmt does
    not, or a function named for a command in READ_BY_NAME."""
    for node in walk(tree):
        if node.get("Type") == "FuncDecl":
            name = str((node.get("Name") or {}).get("Value", ""))
            if name in READ_BY_NAME:
                return line_of(node), f"defines a function named `{name}`"
    for call in calls(tree):
        try:
            rest = builtin_args(call_args(call), "alias")
        except UnreadableCommand:
            continue
        if rest and any("=" in literal_word(word) for word in rest):
            return line_of(call), "defines an alias"
    return None


def judge_file(path: str, text: str, tools: Toolchain) -> list[Finding]:
    """Every finding for one unit test's EXIT traps."""

    def found(line: int, kind: str, message: str) -> Finding:
        return Finding(path, line, kind, message)

    tree = parse_file(path, text, tools, found)
    if isinstance(tree, Finding):
        return [tree]
    hazards = function_hazards(tree, tools.or_op)
    completing = completing_handlers(tree, tools.canon)
    traps, findings = exit_traps(tree, found)
    for call, action in traps:
        findings.extend(
            judge_body(
                literal_word(action).strip(" \t\n"),
                line_of(call),
                offset(call),
                tree,
                hazards,
                completing,
                tools,
                found,
            )
        )
    return findings


def _chain(hazard: Hazard) -> str:
    return " -> ".join(name + "()" for name in hazard.chain)


def _hazard_finding(hazard: Hazard, line: int, found: Reporter) -> Finding:
    where = f"`{_chain(hazard)}`"
    if hazard.kind == "rm":
        return found(
            line,
            "rm",
            f"EXIT trap delegates to {where}, whose `rm` on line {hazard.line} can "
            "end non-zero and publish its own status as the test result. Add "
            f"{GUARD_HINT} after it.",
        )
    if hazard.kind == "ends":
        return found(
            line,
            "completion",
            ENDS_EARLY_MSG.format(
                where=f"{where} ({hazard.detail} on line {hazard.line})"
            ),
        )
    return found(
        line,
        "unreadable",
        f"EXIT trap reaches {where}, which this gate cannot judge at line "
        f"{hazard.line}: {hazard.detail}. Define the function once at the top "
        "level, or name its commands plainly.",
    )


def judge_body(
    body: str,
    line: int,
    trap_offset: int,
    tree: Node,
    hazards: Reach,
    completing: dict[str, bool],
    tools: Toolchain,
    found: Reporter,
) -> list[Finding]:
    """The findings for one EXIT trap's action, already read as text."""
    if body == "-":
        return []
    if body == "":
        return [found(line, "completion", COMPLETION_MSG)]
    findings: list[Finding] = []
    if HANDLER_NAME.fullmatch(body):
        if body not in hazards.status:
            return [
                found(
                    line,
                    "unreadable",
                    f"cleanup trap names handler `{body}`, which is not defined in "
                    "this file - this gate resolves same-file functions only, so "
                    "it fails closed rather than assume the handler guards its "
                    "rm. Inline the body or define the handler here.",
                )
            ]
        status, ending = hazards.status[body], hazards.ending[body]
        if status is not None:
            findings.append(_hazard_finding(status, line, found))
        handler = hazards.bodies.get(body)
        returns = (
            ending_spots(
                handler.get("Body"),
                returns=True,
                paths_run_files=hazards.paths_run_files,
            )
            if handler
            else []
        )
        if returns:
            spot_line, what = returns[0]
            findings.append(
                found(
                    line,
                    "completion",
                    ENDS_EARLY_MSG.format(
                        where=f"`{body}()` ({what} on line {spot_line})"
                    ),
                )
            )
        elif ending is not None:
            findings.append(_hazard_finding(ending, line, found))
        if status is not None and status.kind == "unreadable":
            return findings
        if not completing.get(body, False):
            findings.append(found(line, "completion", COMPLETION_MSG))
        elif not lifecycle_holds(tree, trap_offset):
            findings.append(found(line, "lifecycle", LIFECYCLE_MSG))
        return findings
    # An inline body is shell source carried inside a word, so it needs its own
    # parse - the file's AST holds it as a string.
    try:
        body_tree = parse_shell(body + "\n", tools.shfmt)
    except ShellParseError as error:
        return [
            found(
                line,
                "unreadable",
                f"cleanup trap body is not parseable shell ({error}), so this gate "
                "cannot tell whether its rm is guarded.",
            )
        ]
    special = special_quote_line(body_tree)
    if special is not None:
        return [
            found(
                line,
                "unreadable",
                SPECIAL_QUOTE_MSG.format(
                    where="This cleanup trap body", line=f"its line {special}"
                ),
            )
        ]
    removes, unknown = unguarded_rm_lines(body_tree, tools.or_op)
    reached = [
        hazards.status[name]
        for name in unguarded_calls(body_tree, tools.or_op, hazards.status)
    ]
    reached_ends = [
        hazards.ending[name] for name in all_calls(body_tree, hazards.ending)
    ]
    if removes or any(h is not None and h.kind == "rm" for h in reached):
        findings.append(
            found(
                line,
                "rm",
                "EXIT trap can end non-zero - an unguarded `rm` here publishes its "
                f"own status as the test result. Add {GUARD_HINT} after it.",
            )
        )
    if unknown:
        spot_line, what = unknown[0]
        findings.append(
            found(
                line,
                "unreadable",
                f"cleanup trap runs {what} (its line {spot_line}), so this gate "
                "cannot tell whether the command is an unguarded `rm`. Name the "
                "command plainly, or guard the call.",
            )
        )
    for hazard in (*reached, *reached_ends):
        if hazard is not None and hazard.kind == "unreadable":
            findings.append(_hazard_finding(hazard, line, found))
            break
    ends = ending_spots(
        body_tree, returns=True, paths_run_files=hazards.paths_run_files
    )
    ending = next((h for h in reached_ends if h is not None and h.kind == "ends"), None)
    if ends:
        spot_line, what = ends[0]
        findings.append(
            found(
                line,
                "completion",
                ENDS_EARLY_MSG.format(
                    where=f"the body ({what} on its line {spot_line})"
                ),
            )
        )
    elif ending is not None:
        findings.append(_hazard_finding(ending, line, found))
    if not ends_with_check(body_tree, tools.canon):
        findings.append(found(line, "completion", COMPLETION_MSG))
    # The file's tree holds this body as a string, so lifecycle_holds cannot see
    # a sentinel write in it.
    elif mentions_beyond_read(body_tree, SENTINEL) or not lifecycle_holds(
        tree, trap_offset
    ):
        findings.append(found(line, "lifecycle", LIFECYCLE_MSG))
    return findings


Judge = Callable[[str, str, Toolchain], list[Finding]]


def check_files(
    root: Path, files: Sequence[str], tools: Toolchain, judge: Judge | None = None
) -> list[Finding]:
    """Every finding across `files`, paths relative to `root`, each judged by
    `judge` - judge_file, as a unit test, unless a caller hands another. A file
    that cannot be read is reported, never skipped."""
    judge = judge or judge_file
    findings: list[Finding] = []
    for path in files:
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            findings.append(
                Finding(
                    path,
                    0,
                    "unreadable",
                    f"could not read this file ({error}), so its cleanup traps "
                    "were not checked.",
                )
            )
            continue
        # An AST this gate does not expect raises from here with its traceback:
        # the gate is wrong, and a finding would hide which part.
        findings.extend(judge(path, text, tools))
    return findings
