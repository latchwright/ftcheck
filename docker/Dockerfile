# The Rust layer ftcheck adds on top of an existing TSan-instrumented CPython.
#
# ftcheck does NOT build or distribute a sanitizer CPython. The base image is
# maintained upstream by nascheme/cpython_sanity, rebuilt weekly, and already
# recommended by both the py-free-threading guide and the PyO3 guide. What that
# image lacks is the Rust half: a nightly toolchain, `rust-src` so std can be
# rebuilt instrumented, maturin — and ftcheck itself. That is all this file adds.
#
#   docker build -f docker/Dockerfile -t ftcheck-tsan .
#
# The build context is the repository root, so ftcheck is installed from the
# same checkout; .dockerignore keeps the context to ftcheck's own sources.
#
# Running it needs `--security-opt seccomp=unconfined` (or the host sysctl
# `vm.mmap_rnd_bits=28`) — see docs/ci.md.

ARG BASE=ghcr.io/nascheme/cpython-tsan:3.14t
FROM ${BASE}

# The nightly MUST share an LLVM major version with the clang that built the
# base image's CPython. A mismatch does not fail loudly — it hides races (it hid
# PyO3's own BorrowFlag race). nightly-2026-01-15 is LLVM 21.1.8; nightly
# switched to LLVM 22 before 2026-02-01. Bump this together with the base.
ARG RUST_NIGHTLY=nightly-2026-01-15

ENV RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:$PATH \
    FTCHECK_RUST_TOOLCHAIN=${RUST_NIGHTLY} \
    FTCHECK_TSAN_SUPPRESSIONS=/work/tsan_suppressions/cpython.txt

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --no-modify-path --profile minimal \
          --default-toolchain "${RUST_NIGHTLY}" --component rust-src \
 && chmod -R a+rwX /opt/rustup /opt/cargo

# The base image reaches CPython through pyenv shims, which mangle argv for
# console scripts (`ftcheck ci` arrives as `ftcheck /path/to/ftcheck ci`) and
# for `pip`. A stable path to the real interpreter goes ahead of them.
RUN ln -s "$(ls -d /work/.pyenv/versions/*/ | head -1)" /opt/cpython
ENV PATH=/opt/cpython/bin:$PATH \
    FTCHECK_PYTHON=/opt/cpython/bin/python3

# Refuse to produce an image whose LLVM versions disagree. CPython is read from
# its sysconfig data file rather than by running it: the interpreter is
# TSan-instrumented and cannot start inside `docker build`, which offers no way
# to relax ASLR.
RUN set -eu; \
    rust_llvm=$(rustc -vV | sed -n 's/^LLVM version: \([0-9]*\).*/\1/p'); \
    cc_llvm=$(grep -ho "'CC': 'clang-[0-9]*" /opt/cpython/lib/python3*/_sysconfigdata*.py \
              | head -1 | grep -o '[0-9]*$'); \
    echo "rustc LLVM ${rust_llvm}, CPython built with clang ${cc_llvm}"; \
    [ -n "${cc_llvm}" ] && [ "${rust_llvm}" = "${cc_llvm}" ] \
      || { echo "LLVM major mismatch: races would be hidden" >&2; exit 1; }

# maturin as its standalone binary, for the same reason: pip needs the
# interpreter. Python test dependencies are installed at run time, where the
# interpreter can start.
ARG MATURIN_VERSION=1.15.0
RUN curl -sSfL "https://github.com/PyO3/maturin/releases/download/v${MATURIN_VERSION}/maturin-x86_64-unknown-linux-musl.tar.gz" \
      | tar -xz -C /usr/local/bin maturin \
 && maturin --version

# ftcheck itself runs on an ordinary CPython, not the TSan one: only the code
# under test needs the sanitizer, and `ci`/`stress` start that interpreter as a
# subprocess. On the TSan interpreter even `ftcheck lint` and `--help` died
# without seccomp=unconfined (found by a first user). An ordinary interpreter
# also starts inside `docker build`, so ftcheck installs normally. Ubuntu's
# own python3 (3.14, a GIL build) is used: no extra download host.
# ftcheck's own extension is not instrumented and never enters the process
# under test.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/bin/python3 -c "import sys; assert not hasattr(sys, '_is_gil_enabled') or sys._is_gil_enabled(), 'expected an ordinary GIL build'" \
 && /usr/bin/python3 -m venv /opt/ftcheck
COPY pyproject.toml Cargo.toml Cargo.lock README.md LICENSE-MIT LICENSE-APACHE /opt/ftcheck-src/
COPY crates /opt/ftcheck-src/crates
COPY python/ftcheck /opt/ftcheck-src/python/ftcheck
# Built with the maturin binary already in the image and installed offline:
# ftcheck has no runtime dependencies, so nothing is fetched from PyPI.
RUN cd /opt/ftcheck-src \
 && CARGO_TARGET_DIR=/tmp/ftcheck-target \
      maturin build --release --locked --auditwheel skip --interpreter /opt/ftcheck/bin/python --out /tmp/ftcheck-wheel \
 && /opt/ftcheck/bin/python -m pip install --no-index --no-deps /tmp/ftcheck-wheel/*.whl \
 && ln -sf /opt/ftcheck/bin/ftcheck /usr/local/bin/ftcheck \
 && ftcheck --version \
 && rm -rf /tmp/ftcheck-target /tmp/ftcheck-wheel /opt/cargo/registry /opt/ftcheck-src

WORKDIR /src
