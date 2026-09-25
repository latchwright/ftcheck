// SPDX-License-Identifier: MIT OR Apache-2.0
//! Static free-threading hazard rules for Rust/PyO3 extensions.
//!
//! Deliberately small. `rustc` already rejects most of what a naive rule set
//! would check: `#[pyclass]` must be `Sync`, so a `RefCell` field fails to
//! compile, and `GILProtected` is gated out of free-threaded builds. What is
//! left is the `unsafe`-mediated and logic-level remainder, where the compiler
//! has no opinion.
//!
//! This is a fast pre-flight, not the product.

pub mod pyo3_surface;
pub mod rules;

use ftcheck_core::{Confidence, Finding};
use std::path::Path;

/// How to run the lint.
#[derive(Debug, Clone)]
pub struct LintOptions {
    /// The PyO3 version the target resolves to. Some rules are only true below
    /// a given version, and guessing is worse than saying "unknown".
    pub pyo3_version: Option<semver::Version>,
    /// Findings below this confidence are not reported.
    pub min_confidence: Confidence,
    /// `#[pyclass(unsendable)]` types declared anywhere in the crate. A class
    /// may be declared in one file and given `#[pymethods]` in another, so
    /// `lint_crate` collects these before linting any file.
    pub unsendable: std::collections::HashSet<String>,
}

impl Default for LintOptions {
    fn default() -> Self {
        LintOptions {
            pyo3_version: None,
            // `likely` findings are hidden unless asked for.
            min_confidence: Confidence::Certain,
            unsendable: Default::default(),
        }
    }
}

/// Lint one parsed source file.
pub fn lint_file(path: &Path, src: &str, opts: &LintOptions) -> Result<Vec<Finding>, syn::Error> {
    let file = syn::parse_file(src)?;
    let statics = rules::ft001::static_mut_names(&file);
    let mut unsendable = pyo3_surface::unsendable_classes(&file);
    unsendable.extend(opts.unsendable.iter().cloned());

    let mut findings = Vec::new();
    for entry in pyo3_surface::entry_points(&file) {
        if let Some(f) = rules::ft001::check(&entry, &statics, &unsendable, path) {
            findings.push(f);
        }
        if let Some(f) = rules::ft003::check(&entry, path) {
            findings.push(f);
        }
    }

    findings.extend(rules::ft002::check(&file, opts.pyo3_version.as_ref(), path));

    findings.retain(|f| f.confidence >= opts.min_confidence);
    Ok(ftcheck_core::dedupe(findings))
}

/// What a crate-level run found, **and what it could not examine**.
///
/// Coverage is reported, not implied. A tool that says "no findings" without
/// saying what it looked at is asking to be trusted rather than checked.
#[derive(Debug, Clone, Default)]
pub struct CrateReport {
    pub findings: Vec<Finding>,
    pub files_scanned: usize,
    pub entry_points_seen: usize,
    /// Files `syn` could not parse, with the reason. Named, never skipped.
    pub unparseable: Vec<(std::path::PathBuf, String)>,
    /// The PyO3 version the run resolved, if any.
    pub pyo3_version: Option<String>,
}

/// Resolve the PyO3 version a crate actually builds against.
///
/// `Cargo.lock` is preferred because it records what was resolved; the manifest
/// records only what was requested. Returns `None` rather than guessing, and
/// rules that care degrade to `likely` when it does.
///
/// The path given is often a repository root rather than the extension crate:
/// maturin projects keep the crate in `src/_native/` or `bindings/python/`. So,
/// in order: the path itself; the crate named by `[tool.maturin] manifest-path`;
/// then every crate below the path that depends on PyO3 — and if those disagree
/// on the version, the answer is `None`, not the first one found.
pub fn resolve_pyo3_version(root: &Path) -> Option<semver::Version> {
    if let Some(v) = version_for_crate(root, root) {
        return Some(v);
    }
    if let Some(manifest) = maturin_manifest(root) {
        if let Some(dir) = manifest.parent() {
            if let Some(v) = version_for_crate(dir, root) {
                return Some(v);
            }
        }
    }
    let mut found: Vec<semver::Version> = Vec::new();
    for dir in pyo3_crates_below(root) {
        if let Some(v) = version_for_crate(&dir, root) {
            if !found.contains(&v) {
                found.push(v);
            }
        }
    }
    match found.len() {
        1 => found.pop(),
        _ => None,
    }
}

/// The version for one crate directory: its lockfile, or the nearest one above
/// it (a workspace member's lock lives at the workspace root, never above
/// `stop`), then its manifest's requirement.
fn version_for_crate(dir: &Path, stop: &Path) -> Option<semver::Version> {
    let mut cursor = Some(dir);
    while let Some(d) = cursor {
        if let Some(v) = version_from_lock(&d.join("Cargo.lock")) {
            return Some(v);
        }
        if d == stop {
            break;
        }
        cursor = d.parent().filter(|p| p.starts_with(stop));
    }
    version_from_manifest(&dir.join("Cargo.toml"))
}

fn version_from_lock(path: &Path) -> Option<semver::Version> {
    let lock = std::fs::read_to_string(path).ok()?;
    let parsed = lock.parse::<toml::Table>().ok()?;
    let packages = parsed.get("package")?.as_array()?;
    packages
        .iter()
        .filter(|pkg| pkg.get("name").and_then(|n| n.as_str()) == Some("pyo3"))
        .filter_map(|pkg| pkg.get("version")?.as_str())
        .filter_map(|v| semver::Version::parse(v).ok())
        .max()
}

fn pyo3_requirement(table: &toml::Table) -> Option<&toml::Value> {
    table
        .get("dependencies")
        .and_then(|d| d.get("pyo3"))
        .or_else(|| {
            table
                .get("workspace")
                .and_then(|w| w.get("dependencies"))
                .and_then(|d| d.get("pyo3"))
        })
}

fn version_from_manifest(path: &Path) -> Option<semver::Version> {
    let manifest = std::fs::read_to_string(path).ok()?;
    let table: toml::Table = manifest.parse().ok()?;
    let req = match pyo3_requirement(&table)? {
        toml::Value::String(s) => s.clone(),
        toml::Value::Table(t) => t.get("version")?.as_str()?.to_string(),
        _ => return None,
    };
    // Strip a leading `=`, `^` or `~`; a bare `0.29` is not valid semver, so
    // pad missing components.
    let cleaned = req.trim_start_matches(['=', '^', '~', ' ']).to_string();
    semver::Version::parse(&cleaned)
        .or_else(|_| semver::Version::parse(&format!("{cleaned}.0")))
        .or_else(|_| semver::Version::parse(&format!("{cleaned}.0.0")))
        .ok()
}

/// `[tool.maturin] manifest-path` from `pyproject.toml`, resolved against `root`.
fn maturin_manifest(root: &Path) -> Option<std::path::PathBuf> {
    let text = std::fs::read_to_string(root.join("pyproject.toml")).ok()?;
    let table: toml::Table = text.parse().ok()?;
    let rel = table
        .get("tool")?
        .get("maturin")?
        .get("manifest-path")?
        .as_str()?;
    Some(root.join(rel))
}

/// Directories below `root` holding a `Cargo.toml` that depends on PyO3.
fn pyo3_crates_below(root: &Path) -> Vec<std::path::PathBuf> {
    const SKIP: [&str; 5] = ["target", "node_modules", ".git", "fuzz", "benches"];
    walkdir::WalkDir::new(root)
        .max_depth(4)
        .sort_by_file_name()
        .into_iter()
        .filter_entry(|e| {
            !e.file_type().is_dir() || !SKIP.contains(&e.file_name().to_string_lossy().as_ref())
        })
        .filter_map(Result::ok)
        .filter(|e| e.file_name() == "Cargo.toml")
        .filter(|e| {
            std::fs::read_to_string(e.path())
                .ok()
                .and_then(|t| t.parse::<toml::Table>().ok())
                .is_some_and(|t| pyo3_requirement(&t).is_some())
        })
        .filter_map(|e| e.path().parent().map(Path::to_path_buf))
        .collect()
}

/// Lint every `.rs` file under a crate root.
pub fn lint_crate(root: &Path, opts: &LintOptions) -> anyhow::Result<CrateReport> {
    if !root.exists() {
        anyhow::bail!("path does not exist: {}", root.display());
    }

    let mut opts = opts.clone();
    if opts.pyo3_version.is_none() {
        opts.pyo3_version = resolve_pyo3_version(root);
    }

    let src = root.join("src");
    let scan_root = if src.is_dir() {
        src
    } else {
        root.to_path_buf()
    };

    let mut report = CrateReport {
        pyo3_version: opts.pyo3_version.as_ref().map(|v| v.to_string()),
        ..Default::default()
    };

    let files: Vec<_> = walkdir::WalkDir::new(&scan_root)
        .sort_by_file_name()
        .into_iter()
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_file())
        .filter(|e| e.path().extension().is_some_and(|x| x == "rs"))
        .collect();

    // Crate-wide facts a single file cannot establish on its own.
    for entry in &files {
        if let Ok(parsed) = std::fs::read_to_string(entry.path())
            .map_err(|_| ())
            .and_then(|t| syn::parse_file(&t).map_err(|_| ()))
        {
            opts.unsendable
                .extend(pyo3_surface::unsendable_classes(&parsed));
        }
    }

    for entry in files {
        let path = entry.path();
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) => {
                report.unparseable.push((path.to_path_buf(), e.to_string()));
                continue;
            }
        };
        report.files_scanned += 1;

        match syn::parse_file(&text) {
            Ok(parsed) => report.entry_points_seen += pyo3_surface::entry_points(&parsed).len(),
            Err(e) => {
                report.unparseable.push((path.to_path_buf(), e.to_string()));
                continue;
            }
        }

        match lint_file(path, &text, &opts) {
            Ok(found) => report.findings.extend(found),
            Err(e) => report.unparseable.push((path.to_path_buf(), e.to_string())),
        }
    }

    report.findings = ftcheck_core::dedupe(std::mem::take(&mut report.findings));
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lint(src: &str) -> Vec<Finding> {
        lint_file(Path::new("src/lib.rs"), src, &LintOptions::default()).unwrap()
    }

    #[test]
    fn flags_static_mut_access_from_a_pyfunction() {
        let src = r#"
            static mut COUNTER: u64 = 0;
            #[pyfunction] fn bump() -> u64 { unsafe { COUNTER += 1; COUNTER } }
        "#;
        let out = lint(src);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].rule, "FT001");
        assert_eq!(out[0].confidence, Confidence::Certain);
        assert_eq!(out[0].symbol, "bump");
    }

    #[test]
    fn an_unsendable_class_is_exempt_from_ft001() {
        let src = r#"
            #[pyclass(unsendable)] struct Cursor { ptr: *mut u64 }
            #[pymethods] impl Cursor {
                fn advance(&self) -> u64 { unsafe { *self.ptr += 1; *self.ptr } }
            }
        "#;
        assert!(
            lint(src).is_empty(),
            "PyO3 confines unsendable objects to one thread"
        );
    }

    #[test]
    fn an_unsendable_class_declared_elsewhere_in_the_crate_is_exempt() {
        let src = r#"
            #[pymethods] impl Cursor {
                fn advance(&self) -> u64 { unsafe { *self.ptr } }
            }
        "#;
        let opts = LintOptions {
            unsendable: ["Cursor".to_string()].into_iter().collect(),
            ..Default::default()
        };
        assert!(lint_file(Path::new("src/a.rs"), src, &opts)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn the_buffer_protocol_view_is_caller_owned() {
        let src = r#"
            #[pymethods] impl Frame {
                unsafe fn __getbuffer__(slf: Bound<'_, Self>, view: *mut ffi::Py_buffer, flags: c_int) -> PyResult<()> {
                    unsafe { let v = &mut *view; v.readonly = 1; }
                    Ok(())
                }
                unsafe fn __releasebuffer__(&self, view: *mut ffi::Py_buffer) {
                    unsafe { let v = &mut *view; }
                }
            }
        "#;
        assert!(
            lint(src).is_empty(),
            "CPython hands each request its own Py_buffer"
        );
    }

    #[test]
    fn a_shared_pointer_deref_in_a_buffer_slot_is_still_flagged() {
        let src = r#"
            #[pymethods] impl Frame {
                unsafe fn __getbuffer__(slf: Bound<'_, Self>, view: *mut ffi::Py_buffer, flags: c_int) -> PyResult<()> {
                    unsafe { *slf.get().counter += 1; }
                    Ok(())
                }
            }
        "#;
        assert_eq!(lint(src).len(), 1, "only the caller-owned view is exempt");
    }

    #[test]
    fn a_pointer_parameter_outside_buffer_slots_is_still_flagged() {
        let src = r#"
            #[pymethods] impl Frame {
                unsafe fn poke(&self, p: *mut u64) { unsafe { *p += 1; } }
            }
        "#;
        assert_eq!(lint(src).len(), 1);
    }

    #[test]
    fn flags_a_raw_pointer_deref_inside_unsafe_in_pymethods() {
        let src = r#"
            #[pymethods] impl Buffer {
                fn bump(&self, i: usize) -> u64 {
                    unsafe { let s = self.ptr.add(i); *s += 1; *s }
                }
            }
        "#;
        let out = lint(src);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].rule, "FT001");
        assert_eq!(out[0].symbol, "Buffer::bump");
    }

    #[test]
    fn reports_one_finding_per_entry_point_not_per_occurrence() {
        let src = r#"
            static mut COUNTER: u64 = 0;
            #[pyfunction]
            fn bump() -> u64 { unsafe { COUNTER += 1; COUNTER += 1; COUNTER } }
        "#;
        assert_eq!(lint(src).len(), 1, "one racy method is one bug, not three");
    }

    #[test]
    fn does_not_flag_a_static_mut_that_no_entry_point_touches() {
        let src = r#"
            static mut COUNTER: u64 = 0;
            fn internal() { unsafe { COUNTER += 1; } }
            #[pyfunction] fn safe_one() -> u64 { 0 }
        "#;
        assert!(
            lint(src).is_empty(),
            "v1 reachability is direct containment only; an indirect path is a \
             documented gap, not a finding"
        );
    }

    #[test]
    fn does_not_flag_a_safe_deref_outside_unsafe() {
        assert!(lint(r#"#[pyfunction] fn f(b: Box<u64>) -> u64 { *b }"#).is_empty());
    }

    #[test]
    fn does_not_flag_an_immutable_static() {
        let src = r#"
            static COUNTER: AtomicU64 = AtomicU64::new(0);
            #[pyfunction] fn bump() -> u64 { COUNTER.fetch_add(1, Ordering::SeqCst) }
        "#;
        assert!(lint(src).is_empty());
    }

    #[test]
    fn does_not_flag_a_pyfunction_with_no_hazard() {
        assert!(lint(r#"#[pyfunction] fn add(a: u64, b: u64) -> u64 { a + b }"#).is_empty());
    }

    #[test]
    fn locations_are_real_line_numbers_not_zero() {
        let src = "static mut C: u64 = 0;\n#[pyfunction]\nfn bump() { unsafe { C += 1; } }\n";
        let out = lint(src);
        assert_eq!(out.len(), 1);
        assert!(out[0].primary.line >= 3, "span-locations must be enabled");
    }

    #[test]
    fn a_parse_error_is_returned_not_swallowed() {
        assert!(lint_file(
            Path::new("x.rs"),
            "this is not rust",
            &LintOptions::default()
        )
        .is_err());
    }
}

#[cfg(test)]
mod ft002_tests {
    use super::*;

    const FREELIST: &str = r#"#[pyclass(freelist = 8)] struct Token { id: u64 }"#;

    fn lint_at(src: &str, version: Option<&str>) -> Vec<Finding> {
        let opts = LintOptions {
            pyo3_version: version.map(|v| semver::Version::parse(v).unwrap()),
            min_confidence: Confidence::Likely,
            ..Default::default()
        };
        lint_file(Path::new("src/lib.rs"), src, &opts).unwrap()
    }

    #[test]
    fn flags_freelist_on_pyo3_before_the_fix() {
        let out = lint_at(FREELIST, Some("0.23.4"));
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].rule, "FT002");
        assert_eq!(out[0].confidence, Confidence::Certain);
        assert!(out[0].justification.as_ref().unwrap().contains("4902"));
    }

    #[test]
    fn does_not_flag_freelist_on_the_release_that_fixed_it() {
        assert!(lint_at(FREELIST, Some("0.23.5")).is_empty());
    }

    #[test]
    fn does_not_flag_freelist_on_current_pyo3() {
        assert!(
            lint_at(FREELIST, Some("0.29.2")).is_empty(),
            "PyO3 >= 0.23.5 guards the freelist with a mutex; flagging it is a false positive"
        );
    }

    #[test]
    fn reports_likely_when_the_pyo3_version_cannot_be_resolved() {
        let out = lint_at(FREELIST, None);
        assert_eq!(out.len(), 1);
        assert_eq!(
            out[0].confidence,
            Confidence::Likely,
            "an unresolvable version is uncertainty — neither silence nor a shout"
        );
    }

    #[test]
    fn an_unresolved_freelist_is_hidden_at_default_confidence() {
        let out = lint_file(Path::new("src/lib.rs"), FREELIST, &LintOptions::default()).unwrap();
        assert!(
            out.is_empty(),
            "likely findings are hidden unless asked for"
        );
    }

    #[test]
    fn does_not_flag_a_pyclass_without_freelist() {
        assert!(lint_at(r#"#[pyclass(frozen)] struct Token;"#, Some("0.23.4")).is_empty());
    }

    #[test]
    fn does_not_flag_a_bare_pyclass() {
        assert!(lint_at(r#"#[pyclass] struct Token;"#, Some("0.23.4")).is_empty());
    }

    #[test]
    fn flags_a_freelist_enum_too() {
        let src = r#"#[pyclass(freelist = 4)] enum Kind { A, B }"#;
        assert_eq!(lint_at(src, Some("0.23.4")).len(), 1);
    }

    #[test]
    fn a_prerelease_below_the_fix_still_counts_as_below() {
        assert_eq!(lint_at(FREELIST, Some("0.23.0")).len(), 1);
    }
}

#[cfg(test)]
mod ft003_tests {
    use super::*;

    fn lint_likely(src: &str) -> Vec<Finding> {
        let opts = LintOptions {
            pyo3_version: None,
            min_confidence: Confidence::Likely,
            ..Default::default()
        };
        lint_file(Path::new("src/lib.rs"), src, &opts).unwrap()
    }

    fn lint_default(src: &str) -> Vec<Finding> {
        lint_file(Path::new("src/lib.rs"), src, &LintOptions::default()).unwrap()
    }

    const TWO_LOCKS: &str = r#"
        #[pymethods] impl Ledger {
            fn push(&self, v: u64) {
                self.items.lock().unwrap().push(v);
                *self.count.lock().unwrap() += 1;
            }
        }
    "#;

    #[test]
    fn flags_two_locks_taken_in_separate_statements() {
        let out = lint_likely(TWO_LOCKS);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].rule, "FT003");
        assert_eq!(out[0].confidence, Confidence::Likely);
    }

    #[test]
    fn ft003_is_hidden_at_default_confidence() {
        assert!(
            lint_default(TWO_LOCKS).is_empty(),
            "likely findings are hidden by default"
        );
    }

    #[test]
    fn does_not_flag_a_single_lock() {
        let src = r#"
            #[pymethods] impl L {
                fn push(&self, v: u64) { self.items.lock().unwrap().push(v); }
            }
        "#;
        assert!(lint_likely(src).is_empty());
    }

    #[test]
    fn does_not_flag_two_locks_inside_one_statement() {
        let src = r#"
            #[pymethods] impl L {
                fn cmp(&self) -> bool { self.a.lock().unwrap().len() == self.b.lock().unwrap().len() }
            }
        "#;
        assert!(
            lint_likely(src).is_empty(),
            "one expression cannot be interrupted between its own sub-expressions in a way \
             this rule is meant to catch"
        );
    }

    #[test]
    fn does_not_flag_a_function_with_no_locks() {
        let src = r#"#[pymethods] impl L { fn n(&self) -> u64 { 1 } }"#;
        assert!(lint_likely(src).is_empty());
    }
}

#[cfg(test)]
mod walker_tests {
    use super::*;

    fn fixture(name: &str) -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../fixtures")
            .join(name)
    }

    #[test]
    fn reports_coverage_alongside_findings() {
        let report =
            lint_crate(&fixture("racy/ft001-static-mut"), &LintOptions::default()).unwrap();
        assert_eq!(report.files_scanned, 1);
        assert_eq!(report.entry_points_seen, 1);
        assert!(report.unparseable.is_empty());
        assert_eq!(report.findings.len(), 1);
        assert_eq!(report.findings[0].rule, "FT001");
    }

    #[test]
    fn resolves_the_pinned_pyo3_version_from_the_lockfile() {
        let v = resolve_pyo3_version(&fixture("racy/ft002-freelist-old-pyo3")).unwrap();
        assert_eq!(v.to_string(), "0.23.4");
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("ftcheck-resolve-{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn lock_with(version: &str) -> String {
        format!("version = 4\n[[package]]\nname = \"pyo3\"\nversion = \"{version}\"\n")
    }

    #[test]
    fn resolves_a_crate_in_a_subdirectory_of_a_repository() {
        let root = scratch("subdir");
        let crate_dir = root.join("src/_native");
        std::fs::create_dir_all(&crate_dir).unwrap();
        std::fs::write(
            crate_dir.join("Cargo.toml"),
            "[dependencies]\npyo3 = \"0.29\"\n",
        )
        .unwrap();
        std::fs::write(crate_dir.join("Cargo.lock"), lock_with("0.29.2")).unwrap();
        assert_eq!(resolve_pyo3_version(&root).unwrap().to_string(), "0.29.2");
    }

    #[test]
    fn follows_maturin_manifest_path() {
        let root = scratch("maturin");
        std::fs::create_dir_all(root.join("bindings/python")).unwrap();
        std::fs::create_dir_all(root.join("other")).unwrap();
        std::fs::write(
            root.join("pyproject.toml"),
            "[tool.maturin]\nmanifest-path = \"bindings/python/Cargo.toml\"\n",
        )
        .unwrap();
        std::fs::write(
            root.join("bindings/python/Cargo.toml"),
            "[dependencies]\npyo3 = \"0.28\"\n",
        )
        .unwrap();
        std::fs::write(root.join("bindings/python/Cargo.lock"), lock_with("0.28.3")).unwrap();
        // A second PyO3 crate that disagrees must not matter when maturin names one.
        std::fs::write(
            root.join("other/Cargo.toml"),
            "[dependencies]\npyo3 = \"0.22\"\n",
        )
        .unwrap();
        assert_eq!(resolve_pyo3_version(&root).unwrap().to_string(), "0.28.3");
    }

    #[test]
    fn a_workspace_members_lock_is_found_at_the_workspace_root() {
        let root = scratch("workspace");
        std::fs::create_dir_all(root.join("py")).unwrap();
        std::fs::write(root.join("Cargo.toml"), "[workspace]\nmembers = [\"py\"]\n").unwrap();
        std::fs::write(root.join("Cargo.lock"), lock_with("0.27.1")).unwrap();
        std::fs::write(
            root.join("py/Cargo.toml"),
            "[dependencies]\npyo3 = \"0.27\"\n",
        )
        .unwrap();
        assert_eq!(resolve_pyo3_version(&root).unwrap().to_string(), "0.27.1");
    }

    #[test]
    fn crates_that_disagree_resolve_to_none_not_to_the_first_found() {
        let root = scratch("disagree");
        for (dir, v) in [("a", "0.23.4"), ("b", "0.29.2")] {
            std::fs::create_dir_all(root.join(dir)).unwrap();
            std::fs::write(
                root.join(dir).join("Cargo.toml"),
                "[dependencies]\npyo3 = \"0.2\"\n",
            )
            .unwrap();
            std::fs::write(root.join(dir).join("Cargo.lock"), lock_with(v)).unwrap();
        }
        assert_eq!(resolve_pyo3_version(&root), None);
    }

    #[test]
    fn resolves_the_current_pyo3_version_for_the_clean_twin() {
        let v = resolve_pyo3_version(&fixture("clean/clean-freelist-current-pyo3")).unwrap();
        assert!(v >= semver::Version::parse("0.23.5").unwrap());
    }

    #[test]
    fn the_freelist_pair_differs_only_by_version() {
        let old = lint_crate(
            &fixture("racy/ft002-freelist-old-pyo3"),
            &LintOptions::default(),
        )
        .unwrap();
        let new = lint_crate(
            &fixture("clean/clean-freelist-current-pyo3"),
            &LintOptions::default(),
        )
        .unwrap();
        assert_eq!(
            old.findings
                .iter()
                .map(|f| f.rule.as_str())
                .collect::<Vec<_>>(),
            vec!["FT002"]
        );
        assert!(
            new.findings.is_empty(),
            "flagging a fixed freelist is a false positive"
        );
    }

    #[test]
    fn clean_fixtures_produce_nothing_even_at_likely() {
        let opts = LintOptions {
            pyo3_version: None,
            min_confidence: Confidence::Likely,
            ..Default::default()
        };
        for name in [
            "clean/clean-frozen",
            "clean/clean-mutex",
            "clean/clean-atomics",
        ] {
            let report = lint_crate(&fixture(name), &opts).unwrap();
            assert!(
                report.findings.is_empty(),
                "{name} produced {:?}",
                report.findings
            );
        }
    }

    #[test]
    fn ft001_does_not_fire_on_the_c_dependency_fixture() {
        // The FFI string read there is not a raw-pointer write, and flagging
        // every FFI read would drown FT001 in noise.
        let opts = LintOptions {
            pyo3_version: None,
            min_confidence: Confidence::Likely,
            ..Default::default()
        };
        let report = lint_crate(&fixture("pending/ft005-unsync-c-dep"), &opts).unwrap();
        assert!(
            !report.findings.iter().any(|f| f.rule == "FT001"),
            "FT001 must not fire here: {:?}",
            report.findings
        );
    }

    #[test]
    fn an_unparseable_file_is_named_not_silently_skipped() {
        let dir = std::env::temp_dir().join("ftcheck-unparseable-test/src");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("broken.rs"), "this is not rust at all {{{").unwrap();
        let report = lint_crate(dir.parent().unwrap(), &LintOptions::default()).unwrap();
        assert_eq!(report.unparseable.len(), 1);
        assert!(report.unparseable[0].0.ends_with("broken.rs"));
        std::fs::remove_dir_all(dir.parent().unwrap()).ok();
    }

    #[test]
    fn a_missing_path_is_an_error_not_an_empty_clean_report() {
        assert!(lint_crate(Path::new("/does/not/exist"), &LintOptions::default()).is_err());
    }
}
