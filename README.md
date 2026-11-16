# shell-trap-lint

## What it is

A lint for the EXIT traps in shell scripts and shell tests. It reports a trap
that can let a run that stopped short exit 0, and, for a test, a trap whose
failed cleanup can fail a test that passed. It reads each file through
[shfmt](https://github.com/mvdan/sh)'s syntax tree rather than its text. A
trap whose action or signals it cannot read statically is reported as a
finding, not skipped. The constructs it cannot see at all are listed under
[What it reads](#what-it-reads-and-what-it-does-not).

## Why it exists

An EXIT trap runs as a script exits, and its last command sets the status the
script exits with. That causes two problems that `set -eu` does not prevent
and that no common linter checks for.

**An aborted run can exit 0.** When `set -u` stops a script on an unbound
variable, bash 3.2 runs the EXIT trap with a stale `$?` (the status of the
last command that completed) and exits with the trap's own status. bash 3.2
is the `/bin/sh` and `/bin/bash` macOS ships. Every assertion or step after
the unbound reference is skipped, and the run reports success. Saving
`rc=$?` inside the trap saves the same stale value. Even `trap '' EXIT` exits
0.

**A trap's failing command replaces the run's status.** Under `set -e`, a
command that fails inside the trap trips errexit there, so a test whose
assertions all passed fails because `rm -rf "$D"` hit `Directory not empty`.
The failure then looks like an assertion failure, and the reader looks for an
assertion that never failed.

Both were measured with this script:

```sh
set -eu
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
echo "$UNSET_VAR"
```

The second column swaps the trap for `trap 'false' EXIT` on a run that
completes.

| shell | `set -u` abort exits | failing trap on a completed run exits |
| --- | --- | --- |
| macOS `/bin/sh` (bash 3.2.57) | **0** | 1 |
| macOS `/bin/bash` (bash 3.2.57) | **0** | 1 |
| dash | 2 | 1 |
| bash 5 | 1 | 1 |

The trap cannot read anything that tells an abort from a pass. So the fix is
for the script to record that it finished, and for the trap to check that
record. This lint checks that every EXIT trap does both, in one exact form.

## How to use it

It needs Python 3.12 or newer and `shfmt` on `PATH`. It has no Python
dependencies.

```sh
pip install git+https://github.com/curlewlabs-com/shell-trap-lint@v0.1.0

shell-trap-lint deploy.sh scripts/*.sh   # scripts
shell-trap-lint --test tests/*.sh        # tests
```

It judges exactly the files you name. It prints one `path:line: message` per
finding and exits 1 if there is any. It also exits 1, with the reason on
stderr, when it cannot run at all (for example, no `shfmt` on `PATH`), so a
lint that examined nothing never reads as clean. `python3 -m shell_trap_lint`
works the same way.

A test with no EXIT trap passes, and so does `trap - EXIT`, which installs
nothing. In the script form, `RUN_COMPLETE` and `SUITE_COMPLETE` are reserved
in every file it judges, whether or not the file has a trap: each write of
either must sit where the forms below allow one.

### Scripts: the script form

A script often has more than one success exit and its own exit codes, so the
trap keeps any failing status and only turns an unfinished 0 into 1:

```sh
RUN_COMPLETE=0
trap '_trap_rc=$?; [ "$RUN_COMPLETE" = 1 ] || [ "$_trap_rc" -ne 0 ] || _trap_rc=1; rm -f "$TMP"; exit "$_trap_rc"' EXIT
...
[ "$rc" -eq 0 ] || exit "$rc"
RUN_COMPLETE=1
exit 0
```

- `RUN_COMPLETE=0` comes before the trap, in the same statement list.
  Otherwise an aborted run reads whatever value the environment exported.
- The trap begins with the `_trap_rc` capture and the status line, and ends
  with `exit "$_trap_rc"`. These are compared as syntax trees, so whitespace
  and how the trap's action is quoted do not matter. Your cleanup goes
  between them.
- `RUN_COMPLETE=1` appears only directly before an `exit` that has a literal
  code or none, or as the script's last top-level statement. A 1 set anywhere
  else is already set while later code can still abort. `exit "$rc"` can
  itself abort under `set -u`, so exit with a failing status before setting
  the 1.
- Nothing in the trap, or in a function it calls, may end it early: no
  `exit` that can carry 0, `exec`, `eval`, `.`/`source`, or a command named
  by an expansion. Run those in a subshell if you need them.

A script may use the test form's completion check instead.

### Tests: the test form (`--test`)

A test's exit code is its verdict, so the trap must not change it either way:

```sh
SUITE_COMPLETE=0
trap 'rm -rf "$D" || echo "WARN: cleanup failed (test result unaffected)" >&2; [ "$SUITE_COMPLETE" = 1 ] || exit 1' EXIT
...
SUITE_COMPLETE=1
```

- Every `rm` the trap runs, directly or through a function it calls, is
  guarded: it sits in the left operand of a `||`. A failed teardown then
  prints a warning instead of failing the test.
- The last statement of the trap body, or of the function it names, is
  exactly `[ "$SUITE_COMPLETE" = 1 ] || exit 1`.
- `SUITE_COMPLETE=0` is a top-level statement before the trap.
  `SUITE_COMPLETE=1` is the file's last top-level statement. The sentinel is
  written nowhere else.
- So a test has one success exit, its end. A skip path that exits 0 early
  fails the run, which is the visible direction. Setting `SUITE_COMPLETE=1`
  before it is a finding. The script form allows a `=1` directly before an
  `exit`, and accepts this completion check, but does not check the `rm`
  guard.

The script form does not check the `rm` guard. A script that fails because its
cleanup failed is behaving as that script always has.

### What it reads, and what it does not

shfmt parses each file in its bash dialect, so the lint sees what the shell
sees. A `trap` inside a comment or a string is not a trap. A body built from
adjacent quoted pieces, or split over several lines, is read whole. A handler
function is resolved in any definition order. It reads the EXIT signal as the
shells do (`EXIT`, `Exit`, `0`, `00`), reads `trap` through `command` and
`builtin`, and reads `rm` through `command`, `exec` and `env`.

It reports, rather than guesses at:

- a trap whose action, signal list or command word is an expansion;
- a handler this file does not define, or defines more than once, inside a
  branch, or inside another function;
- a file or trap body shfmt cannot parse;
- a `$"..."` or `$'...'` string.

It reads one file at a time, and it reads syntax, not what the shell
rebinds at run time. It does not see:

- functions defined in a sourced library;
- the contents of an `eval` string;
- teardown verbs other than `rm`;
- wrappers other than `command`, `exec` and `env`.

Known issues below lists three more constructs it misses.

The module headers in `src/shell_trap_lint/` list every case handled and
every case left out.

## Known issues

Each of these passes a file the forms would reject, where the lint should
report it as unreadable instead. None of them is a shape that appears by
accident: each matters only when a script is written to get past the lint.

- **Aliases are not expanded.** `shopt -s expand_aliases; alias on_exit=trap;
  on_exit ':' EXIT` installs an EXIT trap the lint does not see, because bash
  expands an alias before it parses and shfmt does not.
- **A same-file function can shadow a builtin the forms rely on.** With
  `exit() { :; }` defined, the trap's `|| exit 1` calls the function, and an
  early `builtin exit 0` then exits 0. The forms are compared as written and do
  not check what `exit` or `[` is bound to.
- **A sentinel written through a quoted operand is not seen as a write.**
  `let "SUITE_COMPLETE = 1"`, `let "RUN_COMPLETE = 1"`,
  `read 'SUITE_COMPLETE[0]'`, and `let "_trap_rc = 0"` in the script form's
  cleanup each change the value the trap reads. The lint counts a word as a
  write only when the whole word, or one side of an `=` in it, is exactly the
  name, so a space or an index hides it.

## License

MIT. See [LICENSE](LICENSE).
