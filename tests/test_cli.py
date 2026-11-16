"""Tests for the `shell-trap-lint` command: which form judges a file, and that a
lint that could not run never exits clean."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shell_trap_lint.cli import main

# Right for a script and wrong for a test: the script form does not judge the
# rm guard, so only `--test` reports the bare `rm`.
BARE_RM_WITH_CHECK = (
    "#!/bin/sh\nD=x\nSUITE_COMPLETE=0\n"
    'trap \'rm -rf "$D"; [ "$SUITE_COMPLETE" = 1 ] || exit 1\' EXIT\n'
    "SUITE_COMPLETE=1\n"
)
NO_COMPLETION = "#!/bin/sh\nD=x\ntrap 'rm -rf \"$D\" || echo WARN >&2' EXIT\n"


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = main(argv)
    return status, out.getvalue(), err.getvalue()


class TheCommand(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def write(self, name: str, text: str) -> str:
        (self.root / name).write_text(text, encoding="utf-8")
        return name

    def test_test_selects_the_test_form(self) -> None:
        path = self.write("a.sh", BARE_RM_WITH_CHECK)
        self.assertEqual(run([path])[0], 0)
        status, out, _ = run(["--test", path])
        self.assertEqual(status, 1)
        self.assertIn("a.sh:4:", out)

    def test_a_finding_in_either_form_fails(self) -> None:
        path = self.write("b.sh", NO_COMPLETION)
        self.assertEqual(run([path])[0], 1)
        self.assertEqual(run(["--test", path])[0], 1)

    def test_a_file_with_no_trap_is_clean(self) -> None:
        path = self.write("c.sh", "#!/bin/sh\necho hi\n")
        self.assertEqual(run([path]), (0, "", ""))

    def test_a_missing_file_is_reported_not_skipped(self) -> None:
        status, out, _ = run(["missing.sh"])
        self.assertEqual(status, 1)
        self.assertIn("missing.sh:0:", out)

    def test_no_shfmt_on_path_does_not_pass(self) -> None:
        # Returning 0 here would pass a lint that examined nothing.
        path = self.write("d.sh", "#!/bin/sh\necho hi\n")
        with mock.patch.dict(os.environ, {"PATH": str(self.root)}):
            status, out, err = run([path])
        self.assertEqual((status, out), (1, ""))
        self.assertIn("could not run", err)


if __name__ == "__main__":
    unittest.main()
