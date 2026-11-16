"""What shell code can reach through the functions its file defines.

For a body of shell - a trap action, a function - this reads shfmt's AST
(shfmt_ast.py) for which unguarded call runs `rm`, whose failure under `set -e`
publishes its own status; which call can end the shell early with status 0
(`exit`, `exec`, in a function's own body `return`, and code this reading
cannot see - ending_spots); and which function a call reaches, transitively,
when the file defines it once at the top level. A name defined more than once,
or inside a branch or another function, has its body decided at run time and
is reported as unreadable rather than resolved. An `rm` under the left operand
of a `||` is guarded, the shell's own rule; a guard does not stop an early end.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from shell_trap_lint.shfmt_ast import (
    Node,
    UnreadableCommand,
    call_args,
    calls,
    current_shell_calls,
    effective_command,
    line_of,
    literal_word,
    offset,
    unwrap,
    walk,
    word_kind,
)


def guarded_offsets(body: object, or_op: object) -> set[int]:
    """Every call inside the left operand of a `||`. The shell suppresses
    errexit for that whole operand, so nesting is inherited."""
    return {
        offset(call)
        for node in walk(body)
        if node.get("Type") == "BinaryCmd" and node.get("Op") == or_op
        for call in calls(node.get("X"))
    }


Spot = tuple[int, str]


def unguarded_rm_lines(body: object, or_op: object) -> tuple[list[int], list[Spot]]:
    """Lines of each unguarded call that runs `rm`, and each unguarded call whose
    command this reading cannot pin down, with the reason (UnreadableCommand)."""
    guarded = guarded_offsets(body, or_op)
    removes: list[int] = []
    unknown: list[Spot] = []
    for call in calls(body):
        if offset(call) in guarded:
            continue
        try:
            command = effective_command(call_args(call))
        except UnreadableCommand as error:
            unknown.append((line_of(call), str(error)))
            continue
        if command == "rm":
            removes.append(line_of(call))
    return removes, unknown


def ending_spots(body: object, *, returns: bool, paths_run_files: bool) -> list[Spot]:
    """Each call in `body` that can end a trap early with status 0, read through
    `command` and `builtin`: an `exit` whose code is not a literal 1-255, and
    an `exec` running a command, which replaces the shell - guarded or not,
    since a successful exec never returns - and anything that runs code this
    reading cannot see: `eval`, `.` / `source`, and a command named by an
    expansion, any of which may be an exit, unless that name is a path
    (word_kind), which the shell runs as a file in its own process (POSIX, XCU
    "Command Search and Execution"). `paths_run_files` False voids that: the
    file defines a function whose name holds a `/`, which bash outside POSIX
    mode accepts (Bash Reference Manual, "Bash POSIX Mode", lists the names
    POSIX mode rejects) and runs by such a path, or it runs `.`, `source` or
    `eval`, any of which can define one out of this reading's sight. Code in a
    child shell - `( ... )`, `$( ... )`, a backgrounded statement - cannot
    end the trap, so none of it is read (shfmt_ast.current_shell_calls). A
    function a sourced lib defines under a plain name (`sh`, `rm`, `command`)
    is outside this same-file reading, as the gate header states. An exit
    code counts as failing only when it
    is a static literal 1 through 255. `returns` adds `return`, for the trap's
    own code - its inline body or its handler's body. A trap can run while a
    function or dot script executes, and a `return` it runs then leaves that
    function (Bash Reference Manual, `return`, on a return a trap handler
    executes); POSIX leaves `return` outside one unspecified (XCU `return`).
    So a `return` in the trap's own code is read as able to end it with 0. In
    a function that code calls, a `return` only returns to the trap's code.
    An `exit 1` ends the trap failing, the safe
    direction, which is why the completion check's own `exit 1` is not one."""
    spots: list[Spot] = []
    for call in current_shell_calls(body):
        try:
            rest = unwrap(call_args(call), ("command", "builtin"))
        except UnreadableCommand as error:
            spots.append((line_of(call), str(error)))
            continue
        if not rest:
            continue
        command = word_kind(rest[0])
        if command == "path" and paths_run_files:
            continue
        if command != "static":
            spots.append((line_of(call), "a command named by an expansion"))
            continue
        name = literal_word(rest[0])
        if name in ("eval", ".", "source"):
            spots.append(
                (line_of(call), f"`{name}`, which runs code this gate does not read")
            )
        elif name == "exec" and len(rest) > 1:
            spots.append((line_of(call), "`exec`"))
        elif name == "exit" or (returns and name == "return"):
            code = literal_word(rest[1]) if len(rest) > 1 else ""
            fixed = len(rest) > 1 and word_kind(rest[1]) == "static"
            # POSIX defines an `exit` or `return` value only within 0-255
            # and leaves anything larger unspecified, so a shell may succeed on
            # `exit 256` or `exit 257` alike. Only a literal 1 through 255 is
            # known to end the trap failing; every other code is read as able
            # to carry 0, the fail-closed direction.
            if not (fixed and code.isdigit() and 1 <= int(code) <= 255):
                spots.append((line_of(call), f"`{name} {code}`".replace(" `", "`")))
    return spots


def unguarded_calls(body: object, or_op: object, names: Iterable[str]) -> list[str]:
    """Same-file functions the body calls outside a `||` guard, sorted, by the
    name the shell reads after quote removal. The guard test is the `rm` one:
    `cleanup || echo WARN` suppresses errexit for whatever cleanup does, so
    following it would report a safe trap."""
    known = set(names)
    guarded = guarded_offsets(body, or_op)
    return sorted(
        {
            literal_word(call_args(call)[0])
            for call in calls(body)
            if call_args(call)
            and literal_word(call_args(call)[0]) in known
            and offset(call) not in guarded
        }
    )


def all_calls(body: object, names: Iterable[str]) -> list[str]:
    """Same-file functions the body calls at all in its own shell, sorted -
    guarded or not, since a guard does not stop an early end inside the
    callee, while a call in a child shell (current_shell_calls) cannot end
    this one."""
    known = set(names)
    return sorted(
        {
            literal_word(call_args(call)[0])
            for call in current_shell_calls(body)
            if call_args(call) and literal_word(call_args(call)[0]) in known
        }
    )


@dataclass(frozen=True)
class Reach:
    """Per-function hazards: `status` through unguarded calls, `ending`
    through any call (function_hazards). `paths_run_files` is False when the
    file defines a function whose name holds a `/`, or runs `.`, `source` or
    `eval` (ending_spots)."""

    status: dict[str, Hazard | None]
    ending: dict[str, Hazard | None]
    bodies: dict[str, Node]
    paths_run_files: bool


@dataclass(frozen=True)
class Hazard:
    """What a function can reach: `kind` is `rm` (a bare `rm`), `unreadable` (a
    definition this gate cannot pin down, or a call whose command it cannot),
    or `ends` (an early end ending_spots names)."""

    line: int
    chain: tuple[str, ...]
    kind: str
    detail: str = ""


def single_definitions(tree: Node) -> tuple[dict[str, Node], dict[str, int]]:
    """The functions defined exactly once, at the top level, by name - and the
    line of the first definition of every other name. A second definition, or
    one inside a branch or another function, means which body a call runs is
    decided at run time, so those names resolve to nothing here."""
    every: dict[str, list[Node]] = {}
    for node in walk(tree):
        if node.get("Type") == "FuncDecl":
            every.setdefault(str(node["Name"]["Value"]), []).append(node)
    top = {
        id(statement.get("Cmd"))
        for statement in tree.get("Stmts") or []
        if (statement.get("Cmd") or {}).get("Type") == "FuncDecl"
    }
    single = {
        name: nodes[0]
        for name, nodes in every.items()
        if len(nodes) == 1 and id(nodes[0]) in top
    }
    ambiguous = {
        name: line_of(nodes[0]) for name, nodes in every.items() if name not in single
    }
    return single, ambiguous


def _propagate(
    seeds: dict[str, Hazard | None], edges: dict[str, list[str]]
) -> dict[str, Hazard | None]:
    """Each function's first reachable hazard, one hop per pass. Exactly as many
    passes as functions: a longer chain must revisit a function, which adds
    nothing a shorter chain lacked, so mutual recursion converges."""
    hazards = dict(seeds)
    for _ in edges:
        previous = dict(hazards)
        for name in sorted(edges):
            if hazards[name] is not None:
                continue
            via = next((callee for callee in edges[name] if previous[callee]), None)
            hop = previous[via] if via is not None else None
            if hop is not None:
                hazards[name] = Hazard(
                    hop.line, (name, *hop.chain), hop.kind, hop.detail
                )
    return hazards


def _defines_unseen(tree: Node) -> bool:
    """Whether the file runs code that can define functions out of this
    reading's sight - `.`, `source` or `eval`, anywhere in it, through
    `command` and `builtin` - or a call whose command it cannot pin down: a
    wrapper it cannot read, or a command word decided at run time, which may
    be any of those. Code in a child shell defines nothing here, so it is
    not read (shfmt_ast.current_shell_calls)."""
    for call in current_shell_calls(tree):
        try:
            rest = unwrap(call_args(call), ("command", "builtin"))
        except UnreadableCommand:
            return True
        if not rest:
            continue
        if word_kind(rest[0]) not in ("static", "path"):
            return True
        if literal_word(rest[0]) in (".", "source", "eval"):
            return True
    return False


def function_hazards(tree: Node, or_op: object) -> Reach:
    """Each defined function's reachable hazards: the status-publishing one
    (a bare `rm`, through unguarded calls), and the one that ends a trap early
    (ending_spots, through any call, since a guard does not stop an early end).

    Transitive, because delegation nests: `cleanup() { helper; }` holds no `rm`
    of its own while helper's bare `rm` still publishes its status. A name with
    no single top-level definition is its own `unreadable` hazard in each map."""
    single, ambiguous = single_definitions(tree)
    names = [*single, *ambiguous]
    paths_run_files = not _defines_unseen(tree) and not any(
        "/" in name for name in names
    )
    status: dict[str, Hazard | None] = {}
    ending: dict[str, Hazard | None] = {}
    guarded_edges: dict[str, list[str]] = {}
    any_edges: dict[str, list[str]] = {}
    for name, line in ambiguous.items():
        status[name] = ending[name] = Hazard(
            line,
            (name,),
            "unreadable",
            "a function defined more than once, or inside a branch or another "
            "function, whose body is decided at run time",
        )
        guarded_edges[name] = any_edges[name] = []
    for name, node in single.items():
        body = node.get("Body")
        removes, unknown = unguarded_rm_lines(body, or_op)
        if unknown:
            status[name] = Hazard(unknown[0][0], (name,), "unreadable", unknown[0][1])
        elif removes:
            status[name] = Hazard(removes[0], (name,), "rm")
        else:
            status[name] = None
        ends = ending_spots(body, returns=False, paths_run_files=paths_run_files)
        ending[name] = Hazard(ends[0][0], (name,), "ends", ends[0][1]) if ends else None
        guarded_edges[name] = unguarded_calls(body, or_op, names)
        any_edges[name] = all_calls(body, names)
    return Reach(
        _propagate(status, guarded_edges),
        _propagate(ending, any_edges),
        single,
        paths_run_files,
    )
