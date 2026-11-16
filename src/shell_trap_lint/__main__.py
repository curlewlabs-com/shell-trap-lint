"""`python3 -m shell_trap_lint`, the command-line entry point; cli.py owns it."""

from shell_trap_lint.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
