# Contributing

Thanks for your interest in improving `shell-trap-lint`. The bar for a change
is whether it keeps the lint failing closed: a trap it cannot read is reported,
never passed. A shape of shell it misreads, a sharper message, and clearer docs
are all welcome.

## Reporting a shape it gets wrong

Include the smallest shell file that shows it, what the lint printed, and what
the shell does with it. If a claim rests on how a shell behaves, name the shell
and its version, and how you ran it. Shells disagree here, and that
disagreement is the reason this lint exists.

## Running the checks

These are the checks CI runs on every push and pull request. The tests need
`shfmt` on `PATH`:

```sh
python3 -m unittest discover -s tests -t .
python3 -m pip install -r requirements-dev.txt
ruff format --check . && ruff check . && mypy
```

## Conventions

- **Tests ship with the change.** A bug fix or a feature includes a test in
  the same pull request. A test that guards a failure mode says which one.
- **Fail closed.** A construct the lint cannot read is a finding, with the
  reason.
- **Read shell through shfmt.** Do not add a second parser or a text pattern
  over the source. The module headers say why.
- **Standard library only at runtime.** shfmt is the only other requirement.
- **Pin dependencies exactly.** An action reference is its release's commit
  SHA with the version beside it, since a tag can be moved. The tools in
  `requirements-dev.txt` and the shfmt CI installs are exact versions.
  Renovate proposes their updates. A pin written inline needs a `# renovate:`
  comment above it saying what the literal is.
- **Comments explain why, not what.**
- **Keep tracked text ASCII.** Use plain hyphens and words instead of Unicode
  punctuation or decorative symbols.

## Submitting

Keep each pull request focused on one change, make sure the checks pass, and
describe the why in the pull request body. CI must be green before merge.

## Releasing

Every release gets a fixed `vMAJOR.MINOR.PATCH` tag. The repository blocks
updates and deletions of those tags, and publishing the GitHub Release makes
the tag immutable.

1. Bump `version` in `pyproject.toml` and merge that to `main`.
2. Tag the merge commit `vMAJOR.MINOR.PATCH` and push the tag.
3. Publish it: `gh release create vMAJOR.MINOR.PATCH --verify-tag`.
