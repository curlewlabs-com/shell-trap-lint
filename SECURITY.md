# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately via
[GitHub's security advisory feature](https://github.com/curlewlabs-com/shell-trap-lint/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## Scope

The lint reads the files it is given and runs `shfmt` to parse them. It never
runs the shell it reads. The security surface is:

- **The `shfmt` it runs.** This is the first `shfmt` on `PATH`, trusted the
  way `python3` is. File contents reach it on stdin, never as arguments or
  through a shell.
- **The files it reads.** These are the paths named on the command line, read
  as UTF-8.
- **A false clean.** A trap the lint passes that can still exit 0 after an
  abort is a defect in what the lint promises. Report it as a bug, or
  privately if you think it matters more than that.
