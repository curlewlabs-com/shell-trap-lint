"""Reading shfmt's JSON AST of shell source.

`shfmt --to-json` parses shell exactly, so a gate asks it what a script says
rather than matching text: a word inside a comment or a string is not a call,
and a word's quoted regions are joined by the parser. This module owns that
reading - running shfmt, walking the tree, a word's text, and which command a
call runs through the `command`, `builtin`, `exec` and `env` wrappers - so
each gate decides its own policy over the same reading.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator, Sequence
from typing import Any, TypeGuard

Node = dict[str, Any]


# is_node(value) narrows to dict[Unknown, Unknown], which strict
# type checking rejects wherever the result is read; these say what the
# check establishes, with the same runtime test.
def is_node(value: object) -> TypeGuard[Node]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


COMMAND_OPTIONS = re.compile(r"-[pvV]+")


class ShfmtUnavailable(Exception):
    """shfmt could not be run."""


class ShellParseError(Exception):
    """shfmt could not read a file or a trap body as shell."""


def parse_shell(text: str, shfmt: str) -> Node:
    """shfmt's AST for `text`, read in its bash dialect whatever the shebang:
    bash's grammar is a superset of POSIX sh's, so a construct bash gives
    meaning - `$"..."`, `$'...'` - is marked as such in any script, and a
    trap body re-parsed from a string reads as its file does. Raises
    ShfmtUnavailable when shfmt cannot run and ShellParseError when it
    rejects the text."""
    try:
        result = subprocess.run(
            [shfmt, "--language-dialect", "bash", "--to-json"],
            input=text,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ShfmtUnavailable(f"shfmt could not be run: {error}") from error
    if result.returncode != 0:
        raise ShellParseError(result.stderr.strip() or "shfmt exited non-zero")
    try:
        tree = json.loads(result.stdout)
    except ValueError as error:
        raise ShellParseError(f"shfmt emitted no JSON tree: {error}") from error
    if not is_node(tree):
        raise ShellParseError("shfmt emitted a JSON tree that is not an object")
    return tree


def walk(value: object) -> Iterator[Node]:
    """Every object in the tree, a node before its children, in source order."""
    if is_node(value):
        yield value
        for child in value.values():
            yield from walk(child)
    elif is_list(value):
        for child in value:
            yield from walk(child)


def calls(value: object) -> Iterator[Node]:
    return (node for node in walk(value) if node.get("Type") == "CallExpr")


# Nodes whose commands run in a child shell.
_CHILD_SHELLS = frozenset({"Subshell", "CmdSubst", "ProcSubst", "CoprocClause"})


def current_shell_calls(value: object) -> Iterator[Node]:
    """The calls that run in the shell itself, leaving out `( ... )`,
    `$( ... )`, `<( ... )`, a `coproc` (Bash Reference Manual, "Coprocesses") and a
    backgrounded statement: each runs in a
    subshell environment (POSIX, XCU "Shell Execution Environment"), so an
    `exit` or `return` there ends only that subshell. Kept: a pipeline's
    commands, since a shell may run them in the current environment, and
    bash's `${ ...; }` and `${| ...; }`, which shfmt marks on its command
    substitution node and bash runs in the current environment (Bash
    Reference Manual, "Command Substitution")."""
    if is_node(value):
        current_shell_substitution = value.get("TempFile") or value.get("ReplyVar")
        if (
            value.get("Type") in _CHILD_SHELLS and not current_shell_substitution
        ) or value.get("Background"):
            return
        if value.get("Type") == "CallExpr":
            yield value
        for child in value.values():
            yield from current_shell_calls(child)
    elif is_list(value):
        for child in value:
            yield from current_shell_calls(child)


def offset(node: Node) -> int:
    return int(node["Pos"]["Offset"])


def line_of(node: Node) -> int:
    return int(node["Pos"]["Line"])


def call_args(call: Node) -> list[Node]:
    args = call.get("Args")
    return args if is_list(args) else []


def word_text(word: object) -> str:
    """The text a Word is written as, quotes dropped but escapes kept, an
    expansion becoming the inert `X`: for showing a word in a message only. A
    trap body is re-parsed from literal_word, which removes quotes as the
    shell does."""
    if not is_node(word):
        return ""
    text: list[str] = []
    for part in list_member(word, "Parts"):
        kind = part.get("Type")
        if kind == "Lit":
            text.append(str(part.get("Value", "")))
        elif kind == "SglQuoted":
            text.append(str(part.get("Value") or ""))
        elif kind == "DblQuoted":
            text.append(
                "".join(
                    str(inner.get("Value", "")) if inner.get("Type") == "Lit" else "X"
                    for inner in _inner_parts(part)
                )
            )
        else:
            text.append("X")
    return "".join(text)


_UNQUOTED_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_DOUBLE_QUOTED_ESCAPE = re.compile(r"\\([$`\"\\])")


def literal_word(word: object) -> str:
    """The text the shell reads a word as, after quote removal: what a
    classifier compares, and what a trap action is re-parsed from. Its static
    pieces are what the shell evaluates when the trap fires; each expansion is
    an approximation, the inert `X` below, which cannot show any command
    structure the expansion's value carries. A backslash
    outside quotes escapes the next character and a backslash-newline joins
    lines, so `tr\\ap` runs trap and `E\\XIT` names EXIT; inside double quotes
    a backslash escapes only `$`, a backtick, `"` and itself. An expansion
    becomes the inert `X`: re-parsed, the action keeps its own command
    structure while `$D` may stand for anything harmless; blanking it would
    delete arguments and could join words."""
    if not is_node(word):
        return ""
    text: list[str] = []
    for part in list_member(word, "Parts"):
        kind = part.get("Type")
        if kind == "Lit":
            raw = str(part.get("Value", "")).replace("\\\n", "")
            text.append(_UNQUOTED_ESCAPE.sub(r"\1", raw))
        elif kind == "SglQuoted":
            text.append(str(part.get("Value") or ""))
        elif kind == "DblQuoted":
            text.append(
                "".join(
                    _DOUBLE_QUOTED_ESCAPE.sub(
                        r"\1", str(inner.get("Value", "")).replace("\\\n", "")
                    )
                    if inner.get("Type") == "Lit"
                    else "X"
                    for inner in _inner_parts(part)
                )
            )
        else:
            text.append("X")
    return "".join(text)


def list_member(node: Node, key: str) -> list[Any]:
    """The list `node` holds under `key`, empty when it holds nothing."""
    items: list[Any] = node.get(key) or []
    return items


def node_member(node: Node, key: str) -> Node:
    """The object `node` holds under `key`, empty when it holds nothing."""
    member: Node = node.get(key) or {}
    return member


def _inner_parts(part: Node) -> list[Node]:
    parts = part.get("Parts")
    return parts if is_list(parts) else []


def word_is_expansion(word: Node) -> bool:
    """True when nothing in the word is literal, so its text stands for nothing
    this file can read."""
    for part in list_member(word, "Parts"):
        kind = part.get("Type")
        if kind in ("Lit", "SglQuoted"):
            return False
        if kind == "DblQuoted" and any(
            inner.get("Type") == "Lit" for inner in _inner_parts(part)
        ):
            return False
    return True


# The characters an unquoted literal may hold and still read as written. The
# set is closed: any other - a pathname pattern's `*`, `?` or `[` (POSIX XCU
# "Pathname Expansion"), a leading `~` ("Tilde Expansion"), bash's brace list
# `{` - leaves the word unknown. A lone `[` or `]`, the test command's, cannot
# form a pattern.
_STATIC_LITERAL = re.compile(r"[A-Za-z0-9_./:@%+,=-]*|\[|\]")


def _many_fields(node: Node) -> bool:
    """A parameter expansion that can yield several fields inside double
    quotes: `"$@"`, an array subscript, `"${!prefix@}"`. Any subscript counts,
    since this reading does not tell `[@]` from `[0]`."""
    return node.get("Type") == "ParamExp" and bool(
        node_member(node, "Param").get("Value") == "@"
        or node.get("Index")
        or node.get("Names")
    )


def field_count_fixed(word: Node) -> bool:
    """Whether a word is one field whatever its expansions hold: no unquoted
    expansion, which splits and globs, no unquoted literal the shell rewrites
    (a brace list such as `{:,EXIT}`, a glob), and no multi-field expansion
    inside double quotes (`"$@"`, a subscript, `"${!p@}"`), which can be any
    number."""
    if rewritten_literal(word):
        return False
    for part in list_member(word, "Parts"):
        kind = part.get("Type")
        if kind not in ("Lit", "SglQuoted", "DblQuoted"):
            return False
        if kind == "DblQuoted" and any(_many_fields(node) for node in walk(part)):
            return False
    return True


def word_kind(word: Node) -> str:
    """How the shell reads a word: static, a path, or unknown, and nothing
    else. "static": its text is fixed as written - unquoted literals of
    _STATIC_LITERAL's characters (escapes included), single- and double-quoted
    text. "path": an expansion
    decides part of it, but every expansion sits inside double quotes and
    yields one field there, and a literal `/` is present, so its first field
    holds that slash. "unknown": anything else - an unquoted expansion, a
    multi-field one (`"$@"`, a subscript, `"${!p@}"`), a literal the shell
    rewrites, `$"..."` (translated at run time; Bash Reference Manual,
    "Locale-Specific Translation") and `$'...'` (escapes this reading does not
    decode)."""
    static, slash = True, False
    for part in list_member(word, "Parts"):
        kind = part.get("Type")
        if part.get("Dollar") or kind not in ("Lit", "SglQuoted", "DblQuoted"):
            return "unknown"
        if kind == "Lit":
            value = str(part.get("Value", ""))
            if not _STATIC_LITERAL.fullmatch(_UNQUOTED_ESCAPE.sub("", value)):
                return "unknown"
            slash = slash or "/" in value
        elif kind == "SglQuoted":
            slash = slash or "/" in str(part.get("Value"))
        else:
            for inner in _inner_parts(part):
                if inner.get("Type") == "Lit":
                    slash = slash or "/" in str(inner.get("Value"))
                elif any(_many_fields(node) for node in walk(inner)):
                    return "unknown"
                else:
                    static = False
    if static:
        return "static"
    return "path" if slash else "unknown"


def rewritten_literal(word: Node) -> bool:
    """Whether an unquoted literal in the word holds a character outside
    _STATIC_LITERAL's set - a pathname pattern, a tilde, a brace list - so the
    shell can rewrite it into text its pieces do not show."""
    return any(
        part.get("Type") == "Lit"
        and not _STATIC_LITERAL.fullmatch(
            _UNQUOTED_ESCAPE.sub("", str(part.get("Value", "")))
        )
        for part in list_member(word, "Parts")
    )


# The members of a plain `$X` / `${X}` node: its type, its name, and where its
# `$` and braces sit. Any other member set is an operator, known or not.
_PLAIN_PARAM_MEMBERS = frozenset(
    {"Type", "Param", "Short", "Dollar", "Rbrace", "Pos", "End"}
)


def _part_type(part: Node) -> str:
    kind = str(part.get("Type"))
    if kind == "ParamExp" and any(
        value for key, value in part.items() if key not in _PLAIN_PARAM_MEMBERS
    ):
        return "ParamExpOp"
    return kind


def expansion_types(word: Node) -> set[str]:
    """The AST type of each part of the word that is not literal text, a
    double-quoted string's inner parts included; a `$"..."` or `$'...'` string
    is `DollarQuoted`, since bash rewrites it whole, and a parameter expansion
    with anything beyond a plain `$X` / `${X}` shape - a length, a default, a
    substring, a subscript, or a member a later shfmt adds - is `ParamExpOp`,
    since its text need not be the parameter's value."""
    types: set[str] = set()
    for part in list_member(word, "Parts"):
        kind = str(part.get("Type"))
        # A ParamExp carries a `Dollar` position too; only on a quote is it the
        # `$"..."` / `$'...'` flag.
        if kind in ("DblQuoted", "SglQuoted") and part.get("Dollar"):
            types.add("DollarQuoted")
        elif kind == "DblQuoted":
            types.update(
                _part_type(inner)
                for inner in _inner_parts(part)
                if inner.get("Type") != "Lit"
            )
        elif kind not in ("Lit", "SglQuoted"):
            types.add(_part_type(part))
    return types


def word_pieces(word: Node) -> list[str | None]:
    """The word as the shell reads it, in order: each literal piece after quote
    removal (literal_word's escapes), and None for each expansion, whose text
    is decided at run time - `$"..."` and `$'...'` included."""
    pieces: list[str | None] = []
    for part in list_member(word, "Parts"):
        kind = part.get("Type")
        if part.get("Dollar"):
            pieces.append(None)
        elif kind == "Lit":
            raw = str(part.get("Value", "")).replace("\\\n", "")
            pieces.append(_UNQUOTED_ESCAPE.sub(r"\1", raw))
        elif kind == "SglQuoted":
            pieces.append(str(part.get("Value") or ""))
        elif kind == "DblQuoted":
            pieces.extend(
                _DOUBLE_QUOTED_ESCAPE.sub(
                    r"\1", str(inner.get("Value", "")).replace("\\\n", "")
                )
                if inner.get("Type") == "Lit"
                else None
                for inner in _inner_parts(part)
            )
        else:
            pieces.append(None)
    return pieces


def word_has_expansion(word: Node) -> bool:
    """True when the word's text is not fixed as written (word_kind)."""
    return word_kind(word) != "static"


def special_quote_line(tree: object) -> int | None:
    """The line of the first `$"..."` or `$'...'` string in `tree`, or None.
    Bash rewrites a translated string into its translation for the current
    locale, and an ANSI-C string by decoding its escapes, before the command
    runs (Bash Reference Manual, "Locale-Specific Translation" and "ANSI-C
    Quoting"), so a word holding one has no text this reading can classify.
    shfmt marks each with `Dollar` in the bash dialect parse_shell reads."""
    for node in walk(tree):
        if node.get("Type") in ("DblQuoted", "SglQuoted") and node.get("Dollar"):
            return line_of(node)
    return None


def strip_positions(value: object) -> object:
    """The tree with every source position removed, so one statement compares
    equal wherever it is written. shfmt gives each position as an object holding
    an Offset, whatever the field is called."""
    if is_node(value):
        return {
            key: strip_positions(child)
            for key, child in value.items()
            if not (is_node(child) and "Offset" in child)
        }
    if is_list(value):
        return [strip_positions(child) for child in value]
    return value


class UnreadableCommand(Exception):
    """A call whose command this reading cannot pin down - a wrapper option it
    does not read, a path in place of a builtin, an env operand it does not
    decide. The message is the whole reason, for a gate to print as is."""


# The paths read as the system's env. POSIX names the utility, not where it
# lives, so this list is the gate's choice; unwrap raises for any other.
SYSTEM_ENV = ("/usr/bin/env", "/bin/env")


def unwrap(args: Sequence[Node], wrappers: Sequence[str]) -> list[Node] | None:
    """The words from the command a call runs onward, read through `wrappers`
    (any of `command`, `builtin`, `exec`, `env`) with their options: `command`
    takes `[-pvV]`, where a `v` or `V` makes the call a query that runs nothing
    (None); `env` takes `NAME=value` assignments and a lone `-` (its `-i`);
    each takes `--`, which ends the operands of all but env, whose assignments
    may follow it - any word holding `=` there, a dash-led name included.
    Words are read after quote removal. Raises UnreadableCommand for any other
    option, since what it does to the command is not read here, and for a lone
    `-` after env's `--`, since this reading does not decide whether that is
    `-i` or the command. `env` is a program: SYSTEM_ENV's paths name the
    system's, read as bare `env`, while any other path to an `env` - `./env`,
    `"$DIR/env"` - names some program that may read its operands its own way,
    and raises. `command`, `builtin` and `exec` are builtins, and the
    shell runs a name holding a `/` as a file (POSIX, XCU "Command Search and
    Execution"), so a path to one of them raises as well. Wrappers outside
    `wrappers` (`nice`, `timeout`, `xargs`) are the command itself."""
    index, wrapper = 0, None
    while index < len(args):
        if wrapper is not None:
            # The command a wrapper runs, after its `--` included.
            _settled_operand(args[index], wrapper)
        word = literal_word(args[index])
        if "/" in word and word.rsplit("/", 1)[-1] in wrappers:
            path, word = word, word.rsplit("/", 1)[-1]
            if word != "env":
                raise UnreadableCommand(
                    f"`{path}`, a path in place of the "
                    f"builtin `{word}`, which runs whatever file the path names"
                )
            if path not in SYSTEM_ENV:
                raise UnreadableCommand(
                    f"`{path}`, an `env` other than the system's, whose "
                    "operands this gate does not read"
                )
        if word not in wrappers:
            return list(args[index:])
        index, wrapper = index + 1, word
        ended = False
        while index < len(args):
            _settled_operand(args[index], word)
            option = literal_word(args[index])
            assignment = "=" in option and not option.startswith("-")
            if ended:
                if option == "-":
                    raise UnreadableCommand(
                        f"`{word} -- -`, whose `-` this gate does not decide "
                        "between env's `-i` and the command"
                    )
                if "=" not in option:
                    break
                index += 1
            elif option == "--":
                index += 1
                if word != "env":
                    break
                ended = True
            elif word == "command" and COMMAND_OPTIONS.fullmatch(option):
                if "v" in option or "V" in option:
                    return None
                index += 1
            elif word == "env" and (option == "-" or assignment):
                # A lone `-` is env's --ignore-environment: the command
                # still runs, with an empty environment.
                index += 1
            elif option.startswith("-") and option != "-":
                raise UnreadableCommand(
                    f"`{word} {option}`, a wrapper option this gate does not read"
                )
            else:
                break
    return None


def _settled_operand(operand: Node, wrapper: str) -> None:
    """Raise UnreadableCommand when a word where `wrapper` reads an option,
    `--`, an assignment or the command is not static (word_kind): its run-time
    value decides which of those it is - `command "$SEP" rm` with `SEP=--`,
    `env "$ASSIGN" rm` with `ASSIGN=A=1`. After `command` or `builtin`, a path
    is the command: an expansion before its literal `/` can at most make it an
    option cluster those wrappers reject, running nothing. Not after `exec`,
    whose `-a` takes an attached argument (`exec "$D/x" rm` with D=-a runs
    rm), nor after env, which takes any word holding `=` as an assignment
    (`"$D/x"` with D=A=)."""
    kind = word_kind(operand)
    if kind == "static" or (kind == "path" and wrapper in ("command", "builtin")):
        return
    raise UnreadableCommand(
        f"`{wrapper} {word_text(operand)}`, an expansion where `{wrapper}` "
        "reads an option, an assignment or the command"
    )


def effective_command(args: Sequence[Node]) -> str | None:
    """The basename of the command a call runs, through `command`, `exec` and
    `env`: `\\rm`, `/bin/rm`, `command -- rm`, `exec rm` and `env A=1 rm` all
    run rm. None when the call runs nothing. Raises UnreadableCommand as
    unwrap does."""
    rest = unwrap(args, ("command", "exec", "env"))
    return literal_word(rest[0]).rsplit("/", 1)[-1] if rest else None


def builtin_args(
    args: Sequence[Node], builtin: str, wrap: str | None = None
) -> list[Node] | None:
    """The words after `builtin` when the call runs that builtin, else None.

    The shell reaches a builtin through the same wrappers as any command, each
    with its own option table: `command` takes `[-pvV]`, where a
    `v` or `V` anywhere in the cluster makes the call a query that runs nothing;
    `builtin` takes none. `--` ends a wrapper's options, after which the next
    word is a command name - the builtin or a further wrapper, never an option.
    Raises UnreadableCommand for an expansion among a wrapper's operands
    (_settled_operand)."""
    while args:
        if wrap is not None:
            _settled_operand(args[0], wrap)
        word = literal_word(args[0])
        if word == builtin:
            return list(args[1:])
        if word in ("command", "builtin"):
            args, wrap = args[1:], word
        elif wrap is None:
            return None
        elif word == "--":
            if len(args) > 1:
                _settled_operand(args[1], wrap)
            args, wrap = args[1:], None
        elif wrap == "command" and COMMAND_OPTIONS.fullmatch(word):
            if "v" in word or "V" in word:
                return None
            args = args[1:]
        else:
            return None
    return None


def mentions_beyond_read(value: object, name: str) -> bool:
    """Whether `value` does anything with the variable `name` but read it
    plainly. A bare `$name` is the one use allowed; every other mention - an
    assignment, `read`, a `for` variable, `export`, `unset`, `${name:=...}` -
    counts, rather than being told apart, so a gate can fail closed on it. A
    word is compared whole, after quote removal, as the shell reads it, so
    `read RUN"_COMPLETE"` and `read RUN\\_COMPLETE` name RUN_COMPLETE too, and
    so does any word naming it as an identifier (_names_beyond_read). A
    `$'...'` piece, whose escapes bash decodes, never reaches here: the gates
    refuse a file or trap body holding one first (special_quote_line)."""
    if is_node(value):
        if (
            value.get("Type") == "ParamExp"
            and node_member(value, "Param").get("Value") == name
        ):
            return any(
                member
                for key, member in value.items()
                if key not in _PLAIN_PARAM_MEMBERS
            )
        if _names_beyond_read(str(value.get("Value", "")), name):
            return True
        if (
            value.get("Type") in (None, "Word")
            and is_list(value.get("Parts"))
            and _names_beyond_read(literal_word(value), name)
        ):
            return True
        return any(mentions_beyond_read(child, name) for child in value.values())
    if is_list(value):
        return any(mentions_beyond_read(child, name) for child in value)
    return False


def _names_beyond_read(text: str, name: str) -> bool:
    """Whether literal text names `name` as an identifier anywhere but in a
    plain `$name` or `${name}` read: `let "name = 1"`, `read 'name[0]'` and
    `name=1` all write it through a word the shell reads as a name. Text a
    command only prints is counted too, the safe direction."""
    reads = re.compile(
        rf"\$\{{{re.escape(name)}\}}|\${re.escape(name)}(?![A-Za-z0-9_])"
    )
    bare = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    return bare.search(reads.sub("", text)) is not None
