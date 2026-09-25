# Contributing

## Setup

```console
uv venv --python 3.13 .venv && source .venv/bin/activate
uv pip install maturin pytest ruff pyyaml
maturin develop
```

The compiled extension lives beside the Python sources in the installed package, so
`maturin develop` must run before `pytest`. Putting `python/` on `sys.path` instead would
shadow the package with a copy that has no extension in it.

## The full check

```console
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
ruff check python/ tests/
pytest tests/
./scripts/check-private.sh
./scripts/check-headers.sh
for d in fixtures/*/*/; do (cd "$d" && cargo check -q) || exit 1; done

# the sanitizer half: every fixture under ThreadSanitizer (Docker, ~15 minutes cold)
docker build -f docker/Dockerfile -t ftcheck-tsan .
FTCHECK_TSAN=1 pytest tests/test_tsan_ground_truth.py
```

A new fixture needs a `tests/` suite of its own, because `ftcheck ci` runs the project's
tests. See [fixtures/README.md](fixtures/README.md).

## Adding a rule

**Write the fixtures first.** The project's phasing puts ground truth before rules, and
that ordering is not ceremony: a rule written before its fixtures gets tested against
whatever it happens to do.

1. A **racy** fixture the rule must catch, in `fixtures/racy/`.
2. A **clean** fixture it must stay silent on, in `fixtures/clean/`. This is not optional.
   A rule proven only by what it catches is half proven, and a test asserts every rule has
   both.
3. Each gets an `expected.toml` with a **justification**. Empty is rejected.
4. Then the rule.

If ground truth is ready before the rule is, put the fixture in `fixtures/pending/` with
`pending = true`. It is reported as an expected failure and never silently skipped.

## Confidence

`certain` fires by default and fails CI. `likely` is hidden unless asked for and never
affects the exit code.

Put a rule at `likely` when the AST cannot distinguish the hazard from a correct pattern.
FT003 is the model: two locks in one function is frequently fine, so the tool reports it
and declines to insist.

## Before claiming a rule is needed

Ask what the framework already does. Three rules were cut from the original
specification because `rustc` rejects what they checked — `#[pyclass]` must be `Sync`, so
a `RefCell` field does not compile — and FT002 had to be version-gated because the bug it
described was fixed in PyO3 0.23.5. A check the compiler or the framework makes vacuous
is the most common failure in this kind of tool.

**Verify against the source, not memory.** The vendored crates in
`~/.cargo/registry/src/` are primary sources and this ecosystem moves quarterly.

## Branches, commits and pull requests

- `main` is always releasable and protected. Work happens on short-lived branches named for
  what they do (`fix/ft003-nested-guard`, `feat/sarif-rules`), merged by pull request once CI
  is green. Releases are tags (`v0.1.0`) with a CHANGELOG entry.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `ci:`, `chore:`.
- Sign off every commit (`git commit -s`). The sign-off certifies the
  [Developer Certificate of Origin](https://developercertificate.org/): that you wrote the
  change, or otherwise have the right to submit it under the project's licences.

## Licence headers

Every source file starts with an SPDX identifier, after the shebang if there is one:

```rust
// SPDX-License-Identifier: MIT OR Apache-2.0
```

```python
# SPDX-License-Identifier: MIT OR Apache-2.0
```

`./scripts/check-headers.sh` enforces it in CI. Fixtures and `tests/data/` are exempt,
because tests assert line numbers inside them.

## What the repository is for

Design notes under `docs/` are engineering records: they state a decision and the reason for
it. The repository carries no commercial content. A pre-release banner and an honest
limitations page are the only positioning the README does.

## What the tool is not allowed to say

It never claims code is safe. The strongest available statement is *"no race detected on
the exercised surface"*, and a test asserts the word "safe" never appears in the summary.
