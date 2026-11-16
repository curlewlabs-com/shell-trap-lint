"""Lint a shell file's EXIT traps for a run that stopped short but exits 0, and
a test whose failed teardown fails it. See the README.

The command is the interface (cli.py); the modules below it are how it reads
shell, through shfmt's AST.
"""

import sys

# Checked here, before any other module of the package is imported, so an older
# interpreter gets this message rather than a syntax error from a newer form.
if sys.version_info < (3, 12):  # noqa: UP036 - the point is to run on older ones
    sys.exit("shell-trap-lint needs Python 3.12 or newer")
