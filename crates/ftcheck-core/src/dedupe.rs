// SPDX-License-Identifier: MIT OR Apache-2.0
use crate::finding::Finding;
use std::collections::HashSet;

/// Collapse findings that share a signature, preserving first-seen order.
///
/// Order preservation matters: findings arrive in source order, and a report
/// that reshuffles between runs is a report nobody can diff.
pub fn dedupe(findings: Vec<Finding>) -> Vec<Finding> {
    let mut seen = HashSet::new();
    findings
        .into_iter()
        .filter(|f| seen.insert(f.signature()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::finding::{Confidence, Location, Producer};

    fn f(rule: &str, symbol: &str) -> Finding {
        Finding {
            rule: rule.to_string(),
            message: "x".to_string(),
            confidence: Confidence::Certain,
            producer: Producer::Lint,
            symbol: symbol.to_string(),
            primary: Location::new("src/lib.rs", 1, 1),
            stacks: vec![],
            justification: None,
        }
    }

    #[test]
    fn collapses_identical_signatures_and_preserves_order() {
        let out = dedupe(vec![f("FT001", "a"), f("FT001", "a"), f("FT002", "b")]);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].rule, "FT001");
        assert_eq!(out[1].rule, "FT002");
    }

    #[test]
    fn keeps_findings_that_differ_only_by_symbol() {
        assert_eq!(dedupe(vec![f("FT001", "a"), f("FT001", "b")]).len(), 2);
    }

    #[test]
    fn an_empty_input_yields_an_empty_output() {
        assert!(dedupe(vec![]).is_empty());
    }
}
