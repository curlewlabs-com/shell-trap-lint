"""The script form: a script's EXIT trap cannot pass a run that stopped short.

After a `set -u` abort, bash 3.2 enters the EXIT trap with a stale `$?` and
exits 0; cleanup_trap_check.py's header has the mechanism, and its trap
discovery (wrappers, options, quoting, the numeric EXIT) is the one used here.

A script propagates exact exit codes and often has several success exits, so
its trap keeps a failing status instead of rewriting it, and only an unfinished
exit 0 becomes 1:

    RUN_COMPLETE=0
    trap '_trap_rc=$?; [ "$RUN_COMPLETE" = 1 ] || [ "$_trap_rc" -ne 0 ] || _trap_rc=1; <cleanup>; exit "$_trap_rc"' EXIT
    ...
    [ "$rc" -eq 0 ] || exit "$rc"
    RUN_COMPLETE=1    # directly before each `exit 0`, and at the end
    exit 0

Each EXIT trap in a script must carry that form - the `_trap_rc` capture,
the status line and the final exit, compared as ASTs - or the unit-test
completion check, with its sentinel set to 0 ahead of the trap, and with
nothing in the trap, its handler or a same-file function they reach that can
end it before that check runs - shell_reach.ending_spots names what can,
including code this reading cannot see (`eval`, `source`, a command named by
an expansion - a path exempt only in a file that defines no function out of
this reading's sight) and leaving out what runs in a subshell. Every other
write of a sentinel must sit at a success boundary: `=1` directly before an
`exit` in its statement list, or as the script's last top-level statement. A 1
set anywhere else is already set while code after it can still abort - and so
is one before `exit "$rc"`, whose own expansion can abort under `set -u`, which
is why that `exit` carries at most a literal code and a script exits a
non-zero status before setting the 1. Any other write - an `unset`, an
`export`, a `for` variable, a value other than 0 or 1, a write inside a trap
body - is reported rather than read. A missing `=1` fails the script, the
visible direction. Not judged: the rm guard, since a cleanup failure failing a
script is that script's existing behavior; and, as for unit tests, a function
a sourced lib defines (cleanup_trap_check.py's out-of-scope list says why).
"""

from __future__ import annotations

from functools import cache

from shell_trap_lint.cleanup_trap_check import (
    HANDLER_NAME,
    SPECIAL_QUOTE_MSG,
    Finding,
    Toolchain,
    ToolchainError,
    completing_handlers,
    ends_with_check,
    parse_file,
    plain_value,
)
from shell_trap_lint.shell_reach import Reach, all_calls, ending_spots, function_hazards
from shell_trap_lint.shfmt_ast import (
    Node,
    ShellParseError,
    ShfmtUnavailable,
    line_of,
    literal_word,
    mentions_beyond_read,
    parse_shell,
    special_quote_line,
    strip_positions,
    walk,
    word_has_expansion,
)
from shell_trap_lint.trap_discovery import exit_traps

WHY = "the shell-trap-lint README"


SCRIPT_FORM = (
    '_trap_rc=$?; [ "$RUN_COMPLETE" = 1 ] || [ "$_trap_rc" -ne 0 ] || '
    '_trap_rc=1; <cleanup>; exit "$_trap_rc"'
)


SCRIPT_MSG = (
    "EXIT trap does not fail a run that stopped short - after a `set -u` abort, "
    "macOS /bin/sh enters the trap with a stale `$?` and the script exits 0. Set "
    f"`RUN_COMPLETE=0` before the trap and write it as `trap '{SCRIPT_FORM}' "
    "EXIT`, with `RUN_COMPLETE=1` directly before each `exit 0` and at the "
    f"script's end ({WHY} says why)."
)


def _initialized_before(tree: Node, name: str, trap: Node) -> bool:
    """A plain, foreground `name=0` earlier in the trap's own statement list,
    so it has run whenever the trap is installed. One in an uncalled function,
    a branch, a subshell or the background might not have, and an aborted run
    would then read whatever the environment exported."""
    for node in walk(tree):
        for value in node.values():
            if not isinstance(value, list):
                continue
            for index, statement in enumerate(value):
                if isinstance(statement, dict) and statement.get("Cmd") is trap:
                    return any(
                        plain_value(earlier, name) == "0" for earlier in value[:index]
                    )
    return False


def _early_end(
    holder: object, reach: Reach, *, returns: bool, inline: bool
) -> str | None:
    """Where a trap's code can end before its completion check runs - an
    ending_spots spot, `return` included in the trap's own code - in the code
    itself or in any same-file function it reaches. `inline` code was parsed
    from the trap's string, so its lines count from the body's first. None when
    it runs to its check."""
    spots = ending_spots(holder, returns=returns, paths_run_files=reach.paths_run_files)
    if spots:
        of = " of the trap body" if inline else ""
        return f"{spots[0][1]} on line {spots[0][0]}{of}"
    for name in all_calls(holder, reach.ending):
        hazard = reach.ending[name]
        if hazard is not None:
            return f"`{name}()` reaches {hazard.detail} on line {hazard.line}"
    return None


def _cleanup_breaks_status(middle: list[Node], reach: Reach) -> str | None:
    """Why the script form's cleanup - everything between the status line and
    the final exit, and every same-file function it reaches - would lose the
    saved status: an early end, or any use of `_trap_rc` beyond reading it.
    None when it keeps it."""
    holder: Node = {"Stmts": middle}
    if mentions_beyond_read(middle, "_trap_rc"):
        return "the cleanup writes `_trap_rc`"
    early = _early_end(holder, reach, returns=True, inline=True)
    if early:
        return early
    pending, seen = all_calls(holder, reach.bodies), set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        body = reach.bodies[name].get("Body")
        if mentions_beyond_read(body, "_trap_rc"):
            return f"`{name}()` writes `_trap_rc`"
        pending.extend(all_calls(body, reach.bodies))
    return None


EARLY_MSG = (
    "EXIT trap can end before its completion check - {reason} - so a run that "
    "stopped short can still exit 0. Keep an `exit` that can carry 0, `exec`, "
    "`eval`, `source` and expanded command names out of the trap and the "
    'cleanup it calls, or run them in a subshell, as `("$SCRIPT_DIR/x.sh")` '
    f"does ({WHY} says why)."
)
BOUNDARY_MSG = (
    "`{name}` is written here outside a success boundary: only `{name}=0` ahead "
    "of a trap, and `{name}=1` directly before an `exit` with nothing to expand "
    "or as the script's last top-level statement, keep a later abort failing. "
    f"Move it to one of those places ({WHY} says why)."
)


def _is_settled_exit(statement: Node) -> bool:
    """A bare `exit`, or `exit` with one literal code, with no assignment, no
    redirection and nothing to expand: under `set -u` an expansion can abort
    the exit itself, after the sentinel it follows is already 1, and macOS
    /bin/sh answers an extra or non-numeric argument with a usage error rather
    than the exit written."""
    command = statement.get("Cmd") or {}
    args = command.get("Args") or []
    return (
        not statement.get("Background")
        and not statement.get("Redirs")
        and command.get("Type") == "CallExpr"
        and not command.get("Assigns")
        and 1 <= len(args) <= 2
        and not any(word_has_expansion(word) for word in args)
        and literal_word(args[0]) == "exit"
        and (len(args) == 1 or literal_word(args[1]).isdigit())
    )


def _is_statement_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) and "Cmd" in item for item in value)
    )


def _own_part(value: object) -> object:
    """`value` without the statement lists nested in it - a loop's body, a
    branch, a function's block - which boundary_violations visits on their own,
    so a compound statement answers only for its own words: a `for` variable,
    an arithmetic assignment, a redirection."""
    if isinstance(value, dict):
        return {
            key: _own_part(child)
            for key, child in value.items()
            if not _is_statement_list(child)
        }
    if isinstance(value, list):
        return [_own_part(child) for child in value]
    return value


def boundary_violations(tree: Node, name: str) -> list[int]:
    """Lines of each simple statement that writes the sentinel `name` outside a
    success boundary. A boundary is `name=0`, or `name=1` directly followed by
    an `exit` in the same statement list, or `name=1` as the file's last
    top-level statement; a 1 anywhere else is already set while code after it
    can still abort, and any other write - by any kind of statement - is not
    read here. The `exit` must be one that cannot itself abort
    (_is_settled_exit)."""
    top = tree.get("Stmts") or []
    lines: list[int] = []
    for node in walk(tree):
        for value in node.values():
            if not _is_statement_list(value):
                continue
            for index, statement in enumerate(value):
                if not mentions_beyond_read(_own_part(statement), name):
                    continue
                written = plain_value(statement, name)
                if written == "0":
                    continue
                if written == "1" and (
                    (index + 1 < len(value) and _is_settled_exit(value[index + 1]))
                    or (value is top and index == len(top) - 1)
                ):
                    continue
                lines.append(line_of(statement))
    return lines


def judge_script(path: str, text: str, tools: Toolchain) -> list[Finding]:
    """Every finding for one operational script's EXIT traps: each must carry a
    completion check, in the script form or the unit-test one, with nothing in
    the trap or its cleanup that can end it first, and with its sentinel set to
    0 ahead of it. The header says what is and is not judged."""

    def found(line: int, kind: str, message: str) -> Finding:
        return Finding(path, line, kind, message)

    tree = parse_file(path, text, tools, found)
    if isinstance(tree, Finding):
        return [tree]
    completing = completing_handlers(tree, tools.canon)
    reach = function_hazards(tree, tools.or_op)
    traps, findings = exit_traps(tree, found)
    for call, action in traps:
        body = literal_word(action).strip(" \t\n")
        if body == "-":
            continue
        form: str | None = None
        reason: str | None = None
        if HANDLER_NAME.fullmatch(body):
            if completing.get(body, False):
                form = "SUITE_COMPLETE"
                reason = _early_end(
                    reach.bodies[body].get("Body"), reach, returns=True, inline=False
                )
        elif body:
            try:
                body_tree = parse_shell(body + "\n", tools.shfmt)
            except ShellParseError as error:
                findings.append(
                    found(
                        line_of(call),
                        "unreadable",
                        f"cleanup trap body is not parseable shell ({error}), so "
                        "this gate cannot tell whether it fails a run that stopped "
                        "short.",
                    )
                )
                continue
            special = special_quote_line(body_tree)
            if special is not None:
                findings.append(
                    found(
                        line_of(call),
                        "unreadable",
                        SPECIAL_QUOTE_MSG.format(
                            where="This cleanup trap body",
                            line=f"its line {special}",
                        ),
                    )
                )
                continue
            # The file's tree holds this body as a string, so a sentinel write
            # in it is out of boundary_violations' sight.
            for name in ("RUN_COMPLETE", "SUITE_COMPLETE"):
                if mentions_beyond_read(body_tree, name):
                    findings.append(
                        found(
                            line_of(call), "lifecycle", BOUNDARY_MSG.format(name=name)
                        )
                    )
            statements = body_tree.get("Stmts") or []
            if ends_with_check(body_tree, tools.canon):
                form = "SUITE_COMPLETE"
                reason = _early_end(body_tree, reach, returns=True, inline=True)
            elif len(statements) >= 3 and (
                strip_positions(statements[0]),
                strip_positions(statements[1]),
                strip_positions(statements[-1]),
            ) == script_form(tools.shfmt):
                form = "RUN_COMPLETE"
                reason = _cleanup_breaks_status(statements[2:-1], reach)
        if form is None or not _initialized_before(tree, form, call):
            findings.append(found(line_of(call), "completion", SCRIPT_MSG))
        elif reason is not None:
            findings.append(
                found(line_of(call), "completion", EARLY_MSG.format(reason=reason))
            )
    for name in ("RUN_COMPLETE", "SUITE_COMPLETE"):
        for line in boundary_violations(tree, name):
            findings.append(found(line, "lifecycle", BOUNDARY_MSG.format(name=name)))
    return findings


@cache
def script_form(shfmt: str) -> tuple[object, object, object]:
    """The script form's `_trap_rc` capture, status line and final exit, parsed
    from SCRIPT_FORM by shfmt itself so an upgrade that reshapes the AST moves
    them too. Raises ToolchainError when the parse loses that shape."""
    try:
        tree = parse_shell(SCRIPT_FORM.replace("<cleanup>", "true") + "\n", shfmt)
    except (ShellParseError, ShfmtUnavailable) as error:
        raise ToolchainError(f"shfmt rejected the script form: {error}") from error
    statements = tree.get("Stmts") or []
    if len(statements) != 4:
        raise ToolchainError("could not read shfmt AST for the script form")
    return (
        strip_positions(statements[0]),
        strip_positions(statements[1]),
        strip_positions(statements[3]),
    )
