// SPDX-License-Identifier: MIT OR Apache-2.0
use crate::finding::Finding;
use std::fmt::Write as _;

fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(c),
        }
    }
    out
}

/// Emit JUnit XML, so findings appear in any CI that renders test results.
///
/// One finding becomes one failing test case. A clean run emits a suite with
/// zero failures rather than an empty document, so "ran and found nothing" is
/// distinguishable from "did not run".
pub fn to_junit(findings: &[Finding]) -> String {
    let mut body = String::new();
    for f in findings {
        let name = format!("{} {}:{}", f.rule, f.primary.file.display(), f.primary.line);
        let _ = write!(
            body,
            concat!(
                "    <testcase classname=\"ftcheck.{}\" name=\"{}\">\n",
                "      <failure type=\"{}\" message=\"{}\">{}</failure>\n",
                "    </testcase>\n"
            ),
            escape(&f.rule),
            escape(&name),
            escape(&f.rule),
            escape(&f.message),
            escape(&format!(
                "{} at {}:{}:{} in {} [{}]",
                f.message,
                f.primary.file.display(),
                f.primary.line,
                f.primary.column,
                f.symbol,
                f.confidence.as_str(),
            )),
        );
    }

    format!(
        concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
            "<testsuites>\n",
            "  <testsuite name=\"ftcheck\" tests=\"{n}\" failures=\"{n}\" errors=\"0\">\n",
            "{body}",
            "  </testsuite>\n",
            "</testsuites>\n"
        ),
        n = findings.len(),
        body = body,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::finding::{Confidence, Location, Producer};

    fn f(rule: &str, message: &str) -> Finding {
        Finding {
            rule: rule.to_string(),
            message: message.to_string(),
            confidence: Confidence::Certain,
            producer: Producer::Lint,
            symbol: "bump".to_string(),
            primary: Location::new("src/lib.rs", 10, 5),
            stacks: vec![],
            justification: None,
        }
    }

    #[test]
    fn a_clean_run_emits_a_suite_with_zero_failures() {
        let xml = to_junit(&[]);
        assert!(xml.contains(r#"tests="0""#));
        assert!(xml.contains(r#"failures="0""#));
        assert!(
            xml.contains("<testsuite"),
            "a clean run still reports that it ran"
        );
    }

    #[test]
    fn each_finding_becomes_one_failing_testcase_named_by_rule_and_location() {
        let xml = to_junit(&[f("FT001", "static mut")]);
        assert!(xml.contains(r#"failures="1""#));
        assert!(xml.contains("FT001"));
        assert!(xml.contains("src/lib.rs:10"));
    }

    #[test]
    fn xml_special_characters_are_escaped() {
        let xml = to_junit(&[f("FT001", r#"a < b && c > d "quoted""#)]);
        assert!(xml.contains("&lt;"));
        assert!(xml.contains("&amp;"));
        assert!(xml.contains("&quot;"));
        assert!(
            !xml.contains("a < b"),
            "raw markup would corrupt the document"
        );
    }

    #[test]
    fn the_confidence_tier_survives_into_the_failure_body() {
        let mut finding = f("FT003", "two locks");
        finding.confidence = Confidence::Likely;
        assert!(to_junit(&[finding]).contains("[likely]"));
    }
}
