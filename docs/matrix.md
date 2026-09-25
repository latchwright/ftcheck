# `ftcheck matrix` — free-threaded wheel jobs

Many projects that ship free-threaded wheels hand-roll the release jobs for them. Check
first what your maturin version's `maturin generate-ci github` emits for your project.
maturin's own source (its `main` branch, September 2026) discovers free-threaded 3.14+
interpreters and can generate free-threaded jobs; if your generated workflow already
covers the interpreters and platforms you need, you do not need this.

`ftcheck matrix` prints the jobs as a GitHub Actions workflow, with every interpreter
named explicitly rather than discovered:

```console
$ ftcheck matrix . --python 3.14t --python 3.15t --abi3t > .github/workflows/free-threaded.yml
```

- **Linux** (manylinux and musllinux, x86-64 and aarch64) through `PyO3/maturin-action`,
  naming each interpreter explicitly (`-i python3.14t python3.15t`), so the build does
  not depend on which interpreters discovery picks up.
- **macOS and Windows**, installing each interpreter with `actions/setup-python` and
  handing maturin its exact path, so a runner with several Pythons cannot pick the wrong
  one.
- **`--abi3t`** adds a PEP 803 job for Python 3.15+, emitted **disabled** (`if: false`).
  abi3t support in maturin and PyO3 is new; enable the job once you have confirmed that
  the versions you use build an abi3t wheel for your project.
- `[tool.maturin] manifest-path` is read from `pyproject.toml` and passed through.

Defaults: `3.14t` and `3.15t`; `manylinux-x86_64`, `manylinux-aarch64`,
`musllinux-x86_64`, `macos-arm64`, `windows-x86_64`. All platforms:
`manylinux-x86_64 manylinux-aarch64 musllinux-x86_64 musllinux-aarch64 macos-arm64
macos-x86_64 windows-x86_64`.

**This belongs in maturin.** The generated file says so at the top, and the logic is kept
small. When maturin's own generated workflow covers your project, delete these jobs.

The output has not been run on GitHub by this project yet: it is checked for valid YAML and
structure, and its interpreter arguments follow what public PyO3 projects use in their own
free-threaded release workflows.
