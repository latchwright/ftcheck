// SPDX-License-Identifier: MIT OR Apache-2.0
//! FT003 — locks acquired in a non-atomic sequence.
//!
//! Confidence: **likely**, and hidden by default.
//!
//! This is not a data race. Every individual access is synchronised, so
//! ThreadSanitizer sees nothing wrong and never will. The hazard is a *logic*
//! race: state read under one acquisition is relied on — or written back —
//! under a later one, and another thread can act in between.
//!
//! ## What counts as a sequence
//!
//! Only acquisitions that happen while **no guard is held**. The public-project
//! triage that shaped this rule found 23 of 31 wrong calls were nested locks:
//! the first guard was still alive when the second was taken, which is one
//! critical section, not two. So guards are tracked:
//!
//! - `let g = m.lock()` whose value is the guard itself — followed only by
//!   `unwrap`, `expect`, `map_err` or `?`, or behind `&` — is **held** until
//!   the end of the block or `drop(g)`.
//! - Anything else (`m.read().clone()`, `*m.lock().unwrap()`, a guard used
//!   inside a larger expression) is a **temporary**, released at the end of its
//!   statement.
//!
//! Also from the triage: only zero-argument `lock`/`read`/`write` calls are
//! locks (`ptr.write(v)` and `reader.read(buf)` are not), and bodies of `async`
//! blocks are skipped, because they run later, elsewhere.
//!
//! When the same receiver is acquired twice the message says so: that is the
//! shape of every true positive the triage found — a snapshot taken, the lock
//! released for slow work, and the stale snapshot written back.
//!
//! It stays `likely` because a sequence can still be correct: the locks may
//! guard unrelated state. The AST cannot tell, so the tool does not insist.

use crate::pyo3_surface::EntryPoint;
use crate::rules::location_of;
use ftcheck_core::{Confidence, Finding, Producer};
use quote::ToTokens;
use std::collections::HashSet;
use std::path::Path;
use syn::visit::Visit;

const LOCKING_METHODS: [&str; 5] = ["lock", "read", "write", "lock_mut", "borrow_mut"];

/// Calls that pass a guard through unchanged, so the binding holds the guard.
const GUARD_PASSTHROUGH: [&str; 4] = ["unwrap", "expect", "map_err", "unwrap_or_else"];

fn is_lock_call(call: &syn::ExprMethodCall) -> bool {
    call.args.is_empty() && LOCKING_METHODS.contains(&call.method.to_string().as_str())
}

/// The first lock acquisition in a statement, outside `async` blocks.
struct LockFinder {
    found: Option<(proc_macro2::Span, String)>,
}

impl<'ast> Visit<'ast> for LockFinder {
    fn visit_expr_method_call(&mut self, node: &'ast syn::ExprMethodCall) {
        if self.found.is_none() && is_lock_call(node) {
            let receiver = node.receiver.to_token_stream().to_string();
            self.found = Some((node.method.span(), receiver));
        }
        syn::visit::visit_expr_method_call(self, node);
    }

    fn visit_expr_async(&mut self, _node: &'ast syn::ExprAsync) {
        // Runs later, in another task: not part of this sequence.
    }
}

/// True when `expr` evaluates to a lock guard (or a reference to one).
fn yields_guard(expr: &syn::Expr) -> bool {
    match expr {
        syn::Expr::Reference(r) => yields_guard(&r.expr) || derefs_guard(&r.expr),
        syn::Expr::Try(t) => yields_guard(&t.expr),
        syn::Expr::Paren(p) => yields_guard(&p.expr),
        syn::Expr::MethodCall(call) if is_lock_call(call) => true,
        syn::Expr::MethodCall(call)
            if GUARD_PASSTHROUGH.contains(&call.method.to_string().as_str()) =>
        {
            yields_guard(&call.receiver)
        }
        _ => false,
    }
}

/// `*guard` — only meaningful under `&`, where it borrows through the guard.
fn derefs_guard(expr: &syn::Expr) -> bool {
    matches!(expr, syn::Expr::Unary(u)
        if matches!(u.op, syn::UnOp::Deref(_)) && yields_guard(&u.expr))
}

/// The binding a `let` introduces, when it holds a guard. `Some(None)` for a
/// guard bound to a pattern with no single name (held until the block ends).
fn held_guard(stmt: &syn::Stmt) -> Option<Option<String>> {
    let syn::Stmt::Local(local) = stmt else {
        return None;
    };
    let init = local.init.as_ref()?;
    if !yields_guard(&init.expr) {
        return None;
    }
    let name = match &local.pat {
        syn::Pat::Ident(p) => Some(p.ident.to_string()),
        syn::Pat::Type(t) => match &*t.pat {
            syn::Pat::Ident(p) => Some(p.ident.to_string()),
            _ => None,
        },
        _ => None,
    };
    Some(name)
}

/// The name in a `drop(name);` statement.
fn dropped(stmt: &syn::Stmt) -> Option<String> {
    let syn::Stmt::Expr(syn::Expr::Call(call), _) = stmt else {
        return None;
    };
    let syn::Expr::Path(func) = &*call.func else {
        return None;
    };
    if !func.path.is_ident("drop") || call.args.len() != 1 {
        return None;
    }
    match call.args.first()? {
        syn::Expr::Path(p) => p.path.get_ident().map(|i| i.to_string()),
        _ => None,
    }
}

/// For `if cond { ...; return ... }` with no `else`, the condition. Locks taken
/// inside such a branch end with it: the path that reaches later statements
/// never took them (a same-object fast path, in the triage).
fn early_return_branch(stmt: &syn::Stmt) -> Option<&syn::Expr> {
    let syn::Stmt::Expr(syn::Expr::If(branch), _) = stmt else {
        return None;
    };
    if branch.else_branch.is_some() {
        return None;
    }
    let returns = match branch.then_branch.stmts.last()? {
        syn::Stmt::Expr(syn::Expr::Return(_), _) => true,
        syn::Stmt::Expr(syn::Expr::Macro(m), _) => m.mac.path.is_ident("panic"),
        syn::Stmt::Macro(m) => m.mac.path.is_ident("panic"),
        _ => false,
    };
    returns.then_some(&*branch.cond)
}

/// Acquisitions made while no guard was held, in statement order.
fn sequential_acquisitions(block: &syn::Block) -> Vec<(proc_macro2::Span, String)> {
    let mut live: HashSet<Option<String>> = HashSet::new();
    let mut out = Vec::new();
    for stmt in &block.stmts {
        if let Some(name) = dropped(stmt) {
            live.remove(&Some(name));
            continue;
        }
        let mut finder = LockFinder { found: None };
        match early_return_branch(stmt) {
            // Only the condition runs on the path that continues.
            Some(cond) => finder.visit_expr(cond),
            None => finder.visit_stmt(stmt),
        }
        if let Some(acquisition) = finder.found {
            if live.is_empty() {
                out.push(acquisition);
            }
        }
        if let Some(name) = held_guard(stmt) {
            live.insert(name);
        }
    }
    out
}

pub fn check(entry: &EntryPoint<'_>, path: &Path) -> Option<Finding> {
    let acquisitions = sequential_acquisitions(entry.block);
    if acquisitions.len() < 2 {
        return None;
    }

    let mut seen = HashSet::new();
    let repeated = acquisitions
        .iter()
        .find(|(_, receiver)| !seen.insert(receiver.clone()))
        .map(|(_, receiver)| receiver.replace(' ', ""));

    let message = match &repeated {
        Some(receiver) => format!(
            "`{}` acquires `{}` more than once, releasing it in between; state read under the \
             first acquisition may be stale by the next",
            entry.symbol, receiver
        ),
        None => format!(
            "`{}` acquires locks in {} separate steps, holding none across them; any \
             invariant spanning them is not protected",
            entry.symbol,
            acquisitions.len()
        ),
    };

    Some(Finding {
        rule: "FT003".to_string(),
        message,
        confidence: Confidence::Likely,
        producer: Producer::Lint,
        symbol: entry.symbol.clone(),
        primary: location_of(acquisitions[0].0, path),
        stacks: vec![],
        justification: Some(
            "Each acquisition is individually correct, so ThreadSanitizer will not report \
             this. Another thread can act between them: a value copied under the first can \
             be overwritten by writing it back under the second (a lost update), and two \
             values read under separate acquisitions need not agree. Hold one guard across \
             the whole operation. If the state is unrelated, this finding is noise — which is \
             why it is `likely` and hidden by default."
                .to_string(),
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pyo3_surface::entry_points;

    fn fires(body: &str) -> Option<String> {
        let src = format!("#[pymethods] impl T {{ fn m(&self) {{ {body} }} }}");
        let file: syn::File = syn::parse_str(&src).unwrap();
        let entry = entry_points(&file).into_iter().next().unwrap();
        check(&entry, Path::new("src/lib.rs")).map(|f| f.message)
    }

    #[test]
    fn two_temporary_acquisitions_are_a_sequence() {
        assert!(fires("self.a.lock().unwrap().push(1); *self.b.lock().unwrap() += 1;").is_some());
    }

    #[test]
    fn a_held_guard_makes_the_second_lock_nested() {
        assert!(fires("let a = self.a.lock().unwrap(); let b = self.b.lock().unwrap();").is_none());
    }

    #[test]
    fn a_guard_bound_through_question_mark_or_reference_is_held() {
        assert!(fires("let a = self.a.lock()?; self.b.lock().unwrap().push(1);").is_none());
        assert!(fires("let a = &*self.a.read().unwrap(); self.b.write().unwrap();").is_none());
    }

    #[test]
    fn a_value_copied_out_of_a_guard_releases_it() {
        // `*guard` and `.clone()` produce values; the guard is a temporary.
        assert!(fires("let n = *self.a.lock().unwrap(); *self.b.lock().unwrap() = n;").is_some());
        assert!(fires("let v = self.a.read().clone(); *self.a.write() = v;").is_some());
    }

    #[test]
    fn dropping_the_guard_ends_the_critical_section() {
        assert!(
            fires("let a = self.a.lock().unwrap(); drop(a); let b = self.b.lock().unwrap();")
                .is_some()
        );
    }

    #[test]
    fn reacquiring_one_lock_is_named_as_such() {
        let msg = fires("let v = self.df.read().clone(); *self.df.write() = v;").unwrap();
        assert!(msg.contains("`self.df` more than once"), "{msg}");
    }

    #[test]
    fn a_lock_in_a_branch_that_returns_does_not_sequence_with_what_follows() {
        assert!(fires(
            "if same { let g = self.a.read().unwrap(); return g.len(); } \
             let a = self.a.read().unwrap(); let b = other.b.read().unwrap();"
        )
        .is_none());
    }

    #[test]
    fn a_lock_in_a_branch_that_falls_through_still_counts() {
        assert!(
            fires("if x { self.a.lock().unwrap().push(1); } self.b.lock().unwrap().push(2);")
                .is_some()
        );
    }

    #[test]
    fn calls_with_arguments_are_not_locks() {
        assert!(fires("ptr.write(v); reader.read(buf); self.a.lock().unwrap();").is_none());
    }

    #[test]
    fn locks_inside_an_async_block_are_not_part_of_the_sequence() {
        assert!(
            fires("self.a.lock().unwrap(); spawn(async move { self.b.lock().unwrap(); });")
                .is_none()
        );
    }

    #[test]
    fn locks_inside_a_synchronous_closure_still_count() {
        assert!(
            fires("let v = self.a.read().clone(); run(|| { *self.a.write() = v; });").is_some()
        );
    }
}
