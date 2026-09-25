// SPDX-License-Identifier: MIT OR Apache-2.0
//! FT001 — `static mut` or raw-pointer access reachable from Python.
//!
//! Confidence: **certain**. Both shapes are unsynchronised access to shared
//! mutable memory from code any number of Python threads can enter at once.
//!
//! ## What this rule deliberately does not do
//!
//! Reachability is **direct containment only**. A `#[pyfunction]` that calls a
//! helper which touches a `static mut` is not flagged. Whole-program
//! reachability needs call-graph analysis across crate boundaries, and a rule
//! that claims it without doing it is worse than one that states its limit.
//! The limit is documented in `docs/limitations.md` and pinned by a test.
//!
//! ## Two exemptions, both from corpus false positives
//!
//! - Methods of a `#[pyclass(unsendable)]` type. PyO3 confines such objects to
//!   their creating thread, so their pointers cannot be shared.
//! - Derefs of the `view` parameter of `__getbuffer__`/`__releasebuffer__`.
//!   CPython passes a `Py_buffer` owned by the requester, one per request, so
//!   writing through it is not writing to shared state.

use crate::pyo3_surface::EntryPoint;
use crate::rules::location_of;
use ftcheck_core::{Confidence, Finding, Producer};
use std::collections::HashSet;
use std::path::Path;
use syn::visit::Visit;

/// Names of every `static mut` declared anywhere in the file.
pub fn static_mut_names(file: &syn::File) -> HashSet<String> {
    struct Collector {
        names: HashSet<String>,
    }
    impl<'ast> Visit<'ast> for Collector {
        fn visit_item_static(&mut self, node: &'ast syn::ItemStatic) {
            if matches!(node.mutability, syn::StaticMutability::Mut(_)) {
                self.names.insert(node.ident.to_string());
            }
            syn::visit::visit_item_static(self, node);
        }
    }
    let mut collector = Collector {
        names: HashSet::new(),
    };
    collector.visit_file(file);
    collector.names
}

enum Hazard {
    StaticMut(String),
    RawDeref,
}

struct Scanner<'a> {
    statics: &'a HashSet<String>,
    /// Raw-pointer parameters owned by the caller for this call alone.
    caller_owned: HashSet<String>,
    unsafe_depth: usize,
    hits: Vec<(Hazard, proc_macro2::Span)>,
}

/// Buffer-protocol slots whose pointer parameters belong to the requester.
const BUFFER_SLOTS: [&str; 2] = ["__getbuffer__", "__releasebuffer__"];

fn caller_owned_pointers(sig: &syn::Signature) -> HashSet<String> {
    if !BUFFER_SLOTS.contains(&sig.ident.to_string().as_str()) {
        return HashSet::new();
    }
    sig.inputs
        .iter()
        .filter_map(|arg| match arg {
            syn::FnArg::Typed(t) if matches!(*t.ty, syn::Type::Ptr(_)) => match &*t.pat {
                syn::Pat::Ident(p) => Some(p.ident.to_string()),
                _ => None,
            },
            _ => None,
        })
        .collect()
}

fn is_caller_owned(expr: &syn::Expr, owned: &HashSet<String>) -> bool {
    matches!(expr, syn::Expr::Path(p)
        if p.path.get_ident().is_some_and(|i| owned.contains(&i.to_string())))
}

impl<'ast, 'a> Visit<'ast> for Scanner<'a> {
    fn visit_expr_unsafe(&mut self, node: &'ast syn::ExprUnsafe) {
        self.unsafe_depth += 1;
        syn::visit::visit_expr_unsafe(self, node);
        self.unsafe_depth -= 1;
    }

    fn visit_expr_unary(&mut self, node: &'ast syn::ExprUnary) {
        // A deref inside `unsafe` is a raw-pointer deref: safe derefs of
        // references, Box and friends need no unsafe block. That makes the
        // heuristic precise without type information.
        if self.unsafe_depth > 0
            && matches!(node.op, syn::UnOp::Deref(_))
            && !is_caller_owned(&node.expr, &self.caller_owned)
        {
            self.hits.push((Hazard::RawDeref, node.span()));
        }
        syn::visit::visit_expr_unary(self, node);
    }

    fn visit_expr_path(&mut self, node: &'ast syn::ExprPath) {
        if let Some(ident) = node.path.get_ident() {
            let name = ident.to_string();
            if self.statics.contains(&name) {
                self.hits.push((Hazard::StaticMut(name), node.span()));
            }
        }
        syn::visit::visit_expr_path(self, node);
    }
}

use syn::spanned::Spanned as _;

/// Run FT001 over one entry point. Emits at most one finding per entry point:
/// a method with six racy lines has one bug, not six.
pub fn check(
    entry: &EntryPoint<'_>,
    statics: &HashSet<String>,
    unsendable: &HashSet<String>,
    path: &Path,
) -> Option<Finding> {
    if entry
        .type_name
        .as_ref()
        .is_some_and(|t| unsendable.contains(t))
    {
        return None;
    }
    let mut scanner = Scanner {
        statics,
        caller_owned: caller_owned_pointers(entry.sig),
        unsafe_depth: 0,
        hits: Vec::new(),
    };
    scanner.visit_block(entry.block);

    let (hazard, span) = scanner.hits.into_iter().next()?;
    let (message, detail) = match hazard {
        Hazard::StaticMut(name) => (
            format!(
                "`{}` reaches the `static mut` `{}` with no synchronisation",
                entry.symbol, name
            ),
            "A `static mut` has no synchronisation of its own. Under the GIL only one \
             thread could be inside this function; on a free-threaded build any number \
             can. Use an atomic, a `Mutex`, or `OnceLock`.",
        ),
        Hazard::RawDeref => (
            format!(
                "`{}` dereferences a raw pointer inside `unsafe` and is reachable from Python",
                entry.symbol
            ),
            "`#[pyclass]` requires `Sync`, but `unsafe impl Sync` asserts it rather than \
             establishing it. Concurrent calls from Python threads share this pointer.",
        ),
    };

    Some(Finding {
        rule: "FT001".to_string(),
        message,
        confidence: Confidence::Certain,
        producer: Producer::Lint,
        symbol: entry.symbol.clone(),
        primary: location_of(span, path),
        stacks: vec![],
        justification: Some(detail.to_string()),
    })
}
