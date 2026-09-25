// SPDX-License-Identifier: MIT OR Apache-2.0
//! FT002 — `#[pyclass(freelist = N)]` on a PyO3 version where it is unsound.
//!
//! # Why this rule is version-gated
//!
//! The specification this tool is built from defines FT002 as "flag
//! `freelist`, confidence *certain*, because PyO3 #4894 makes it unsound on
//! free-threaded builds". That was true when #4894 was open. It is **false
//! today**, and a rule that fires on correct code is worse than no rule.
//!
//! PyO3 **0.23.5** (2025-02-22) shipped PR **#4902**, *"Fix thread-unsafe
//! implementation of freelist pyclasses on the free-threaded build"*. Since
//! then the freelist lives in a `PyOnceLock<Mutex<PyObjectFreeList>>` and the
//! lock is taken on every allocation and every free.
//!
//! So the rule fires only below `0.23.5`. Above it, `freelist` is a
//! *performance* concern — the mutex costs more than the freelist saves, on
//! every build including GIL builds (PyO3 #6133, open) — and ftcheck does not
//! report performance, because it is a correctness tool and rating severity is
//! outside what it claims.
//!
//! # When the version is unknown
//!
//! The rule does not go silent and does not shout. It drops to `likely`, which
//! is hidden by default and available on request. Uncertainty is reported as
//! uncertainty.

use crate::rules::location_of;
use ftcheck_core::{Confidence, Finding, Producer};
use std::path::Path;
use syn::visit::Visit;

/// The first PyO3 release in which `freelist` is thread-safe.
pub const FIXED_IN: &str = "0.23.5";

fn fixed_in() -> semver::Version {
    semver::Version::parse(FIXED_IN).expect("FIXED_IN is a literal valid semver")
}

/// Does `#[pyclass(...)]` carry a `freelist` argument?
fn freelist_arg(attr: &syn::Attribute) -> bool {
    match attr.path().segments.last() {
        Some(seg) if seg.ident == "pyclass" => {}
        _ => return false,
    }
    let mut found = false;
    // `parse_nested_meta` returns Err for arguments it cannot walk; a malformed
    // attribute is the compiler's problem, not ours.
    let _ = attr.parse_nested_meta(|meta| {
        if meta.path.is_ident("freelist") {
            found = true;
        }
        // Consume any `= value` so parsing continues past it.
        let _ = meta.value().and_then(|v| v.parse::<syn::Expr>());
        Ok(())
    });
    found
}

struct Collector {
    hits: Vec<(String, proc_macro2::Span)>,
}

impl<'ast> Visit<'ast> for Collector {
    fn visit_item_struct(&mut self, node: &'ast syn::ItemStruct) {
        if node.attrs.iter().any(freelist_arg) {
            self.hits.push((node.ident.to_string(), node.ident.span()));
        }
        syn::visit::visit_item_struct(self, node);
    }

    fn visit_item_enum(&mut self, node: &'ast syn::ItemEnum) {
        if node.attrs.iter().any(freelist_arg) {
            self.hits.push((node.ident.to_string(), node.ident.span()));
        }
        syn::visit::visit_item_enum(self, node);
    }
}

pub fn check(
    file: &syn::File,
    pyo3_version: Option<&semver::Version>,
    path: &Path,
) -> Vec<Finding> {
    let confidence = match pyo3_version {
        Some(v) if *v >= fixed_in() => return Vec::new(), // fixed; flagging it is a false positive
        Some(_) => Confidence::Certain,
        None => Confidence::Likely, // unresolvable version is uncertainty, not silence
    };

    let mut collector = Collector { hits: Vec::new() };
    collector.visit_file(file);

    collector
        .hits
        .into_iter()
        .map(|(name, span)| {
            let message = match pyo3_version {
                Some(v) => format!(
                    "`{name}` uses `freelist`, which is thread-unsafe on free-threaded \
                     builds in PyO3 {v} (fixed in {FIXED_IN})"
                ),
                None => format!(
                    "`{name}` uses `freelist`; the PyO3 version could not be resolved, so \
                     whether this is unsound is unknown (fixed in {FIXED_IN})"
                ),
            };
            Finding {
                rule: "FT002".to_string(),
                message,
                confidence,
                producer: Producer::Lint,
                symbol: name,
                primary: location_of(span, path),
                stacks: vec![],
                justification: Some(format!(
                    "PyO3 #4894: freelist pyclasses were thread-unsafe on free-threaded \
                     builds until PR #4902, released in {FIXED_IN} (2025-02-22). Upgrade \
                     PyO3, or remove `freelist`."
                )),
            }
        })
        .collect()
}
