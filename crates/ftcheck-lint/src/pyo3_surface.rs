// SPDX-License-Identifier: MIT OR Apache-2.0
//! Finding the code Python can actually reach.
//!
//! Every rule in ftcheck is scoped to a PyO3 entry point. A `static mut` that
//! only internal code touches is somebody else's problem; the same `static mut`
//! reachable from a `#[pyfunction]` is reachable from N Python threads at once.

use syn::visit::Visit;

/// A function body reachable from Python.
pub struct EntryPoint<'ast> {
    /// `bump`, or `Buffer::bump` for a method.
    pub symbol: String,
    /// The `#[pymethods]` type, for a method.
    pub type_name: Option<String>,
    pub sig: &'ast syn::Signature,
    pub block: &'ast syn::Block,
}

/// True when an attribute is `#[name]`, `#[pyo3::name]`, or `#[name(..)]`.
///
/// Matching the last path segment rather than the whole path is what makes
/// `#[pyo3::pyfunction]` work, and users do write it that way.
fn attribute_is(attr: &syn::Attribute, name: &str) -> bool {
    attr.path().segments.last().is_some_and(|s| s.ident == name)
}

fn has_attribute(attrs: &[syn::Attribute], name: &str) -> bool {
    attrs.iter().any(|a| attribute_is(a, name))
}

fn impl_type_name(item: &syn::ItemImpl) -> String {
    match &*item.self_ty {
        syn::Type::Path(p) => p
            .path
            .segments
            .last()
            .map(|s| s.ident.to_string())
            .unwrap_or_else(|| "<impl>".to_string()),
        _ => "<impl>".to_string(),
    }
}

struct Collector<'ast> {
    entries: Vec<EntryPoint<'ast>>,
}

impl<'ast> Visit<'ast> for Collector<'ast> {
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        if has_attribute(&node.attrs, "pyfunction") {
            self.entries.push(EntryPoint {
                symbol: node.sig.ident.to_string(),
                type_name: None,
                sig: &node.sig,
                block: &node.block,
            });
        }
        syn::visit::visit_item_fn(self, node);
    }

    fn visit_item_impl(&mut self, node: &'ast syn::ItemImpl) {
        if has_attribute(&node.attrs, "pymethods") {
            let type_name = impl_type_name(node);
            for item in &node.items {
                if let syn::ImplItem::Fn(method) = item {
                    self.entries.push(EntryPoint {
                        symbol: format!("{}::{}", type_name, method.sig.ident),
                        type_name: Some(type_name.clone()),
                        sig: &method.sig,
                        block: &method.block,
                    });
                }
            }
        }
        syn::visit::visit_item_impl(self, node);
    }
}

/// Every `#[pyfunction]` and every method of every `#[pymethods]` impl.
pub fn entry_points(file: &syn::File) -> Vec<EntryPoint<'_>> {
    let mut collector = Collector {
        entries: Vec::new(),
    };
    collector.visit_file(file);
    collector.entries
}

/// True for `#[pyclass(.., unsendable, ..)]`.
fn is_unsendable_pyclass(attrs: &[syn::Attribute]) -> bool {
    attrs.iter().any(|a| {
        attribute_is(a, "pyclass")
            && matches!(&a.meta, syn::Meta::List(list)
            if list.tokens.clone().into_iter().any(|t| {
                matches!(t, proc_macro2::TokenTree::Ident(i) if i == "unsendable")
            }))
    })
}

/// Names of every `#[pyclass(unsendable)]` struct or enum in the file.
///
/// PyO3 checks on every access that an unsendable object is used only from the
/// thread that created it, and raises otherwise. Its state cannot be reached
/// from a second thread, so rules about shared state do not apply to it.
pub fn unsendable_classes(file: &syn::File) -> std::collections::HashSet<String> {
    struct Finder(std::collections::HashSet<String>);
    impl<'ast> Visit<'ast> for Finder {
        fn visit_item_struct(&mut self, node: &'ast syn::ItemStruct) {
            if is_unsendable_pyclass(&node.attrs) {
                self.0.insert(node.ident.to_string());
            }
            syn::visit::visit_item_struct(self, node);
        }
        fn visit_item_enum(&mut self, node: &'ast syn::ItemEnum) {
            if is_unsendable_pyclass(&node.attrs) {
                self.0.insert(node.ident.to_string());
            }
            syn::visit::visit_item_enum(self, node);
        }
    }
    let mut finder = Finder(Default::default());
    finder.visit_file(file);
    finder.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(src: &str) -> Vec<String> {
        let file: syn::File = syn::parse_str(src).unwrap();
        entry_points(&file).into_iter().map(|e| e.symbol).collect()
    }

    #[test]
    fn finds_a_pyfunction() {
        assert_eq!(names("#[pyfunction] fn bump() -> u64 { 0 }"), vec!["bump"]);
    }

    #[test]
    fn finds_methods_in_a_pymethods_impl() {
        let src = "#[pymethods] impl Buffer { fn a(&self) {} fn b(&self) {} }";
        assert_eq!(names(src), vec!["Buffer::a", "Buffer::b"]);
    }

    #[test]
    fn ignores_a_plain_function() {
        assert!(names("fn helper() {}").is_empty());
    }

    #[test]
    fn ignores_a_plain_impl_block() {
        assert!(names("impl Buffer { fn helper(&self) {} }").is_empty());
    }

    #[test]
    fn recognises_the_fully_qualified_attribute_path() {
        assert_eq!(names("#[pyo3::pyfunction] fn g() {}"), vec!["g"]);
    }

    #[test]
    fn recognises_an_attribute_with_arguments() {
        assert_eq!(
            names("#[pyfunction(signature = (a, b))] fn g(a: u8, b: u8) {}"),
            vec!["g"]
        );
    }

    #[test]
    fn finds_unsendable_pyclasses_and_only_those() {
        let file: syn::File = syn::parse_str(
            "#[pyclass(unsendable)] struct A; #[pyclass(frozen, unsendable)] struct B; \
             #[pyclass] struct C; #[pyclass(frozen)] struct D; struct E;",
        )
        .unwrap();
        let mut found: Vec<_> = unsendable_classes(&file).into_iter().collect();
        found.sort();
        assert_eq!(found, vec!["A", "B"]);
    }

    #[test]
    fn finds_entry_points_nested_in_a_module() {
        let src = "mod inner { #[pyfunction] fn deep() {} }";
        assert_eq!(names(src), vec!["deep"]);
    }
}
