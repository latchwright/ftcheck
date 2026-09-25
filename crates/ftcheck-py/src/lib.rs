// SPDX-License-Identifier: MIT OR Apache-2.0
//! Python bindings for ftcheck.
//!
//! This crate is also ftcheck's dogfood: it is itself a PyO3 extension, so the
//! tool can be run against its own bindings. `gil_used = false` declares the
//! module free-threading-ready, which is the claim ftcheck exists to check.
//!
//! The boundary speaks JSON. Findings already serialise for SARIF and JUnit, so
//! reusing that representation avoids a third hand-written conversion that
//! could disagree with the other two.

use ftcheck_core::Confidence;
use ftcheck_lint::{lint_crate, LintOptions};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::path::Path;

fn parse_confidence(value: &str) -> PyResult<Confidence> {
    Confidence::parse(value).ok_or_else(|| {
        PyValueError::new_err(format!(
            "min_confidence must be 'certain' or 'likely', got {value:?}"
        ))
    })
}

/// Lint a crate. Returns a JSON document, parsed by `ftcheck.lint`.
#[pyfunction]
#[pyo3(signature = (path, pyo3_version = None, min_confidence = "certain"))]
fn lint_crate_json(
    path: &str,
    pyo3_version: Option<&str>,
    min_confidence: &str,
) -> PyResult<String> {
    let min_confidence = parse_confidence(min_confidence)?;

    let version = match pyo3_version {
        Some(v) => Some(
            semver::Version::parse(v)
                .map_err(|e| PyValueError::new_err(format!("bad pyo3_version {v:?}: {e}")))?,
        ),
        None => None,
    };

    let report = lint_crate(
        Path::new(path),
        &LintOptions {
            pyo3_version: version,
            min_confidence,
            ..Default::default()
        },
    )
    .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;

    let value = serde_json::json!({
        "findings": report.findings,
        "files_scanned": report.files_scanned,
        "entry_points_seen": report.entry_points_seen,
        "unparseable": report
            .unparseable
            .iter()
            .map(|(p, e)| (p.display().to_string(), e.clone()))
            .collect::<Vec<_>>(),
        "pyo3_version": report.pyo3_version,
    });
    serde_json::to_string(&value).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

fn parse_findings(findings_json: &str) -> PyResult<Vec<ftcheck_core::Finding>> {
    serde_json::from_str(findings_json)
        .map_err(|e| PyValueError::new_err(format!("bad findings JSON: {e}")))
}

/// Render findings as SARIF 2.1.0.
#[pyfunction]
fn to_sarif(findings_json: &str, tool_version: &str) -> PyResult<String> {
    let findings = parse_findings(findings_json)?;
    let value = ftcheck_core::to_sarif(&findings, tool_version);
    serde_json::to_string_pretty(&value).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Render findings as JUnit XML.
#[pyfunction]
fn to_junit(findings_json: &str) -> PyResult<String> {
    Ok(ftcheck_core::to_junit(&parse_findings(findings_json)?))
}

/// The PyO3 release in which `freelist` became thread-safe (PR #4902).
#[pyfunction]
fn freelist_fixed_in() -> &'static str {
    ftcheck_lint::rules::ft002::FIXED_IN
}

#[pymodule(gil_used = false)]
fn _ftcheck(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(lint_crate_json, m)?)?;
    m.add_function(wrap_pyfunction!(to_sarif, m)?)?;
    m.add_function(wrap_pyfunction!(to_junit, m)?)?;
    m.add_function(wrap_pyfunction!(freelist_fixed_in, m)?)?;
    Ok(())
}
