// SPDX-License-Identifier: MIT OR Apache-2.0
use crate::finding::{Confidence, Finding};
use serde_json::{json, Value};
use std::collections::BTreeMap;

const SCHEMA: &str =
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json";

/// One-line descriptions for the rules ftcheck knows about.
fn rule_description(rule: &str) -> &'static str {
    match rule {
        "FT001" => "Unsynchronised `static mut` or raw-pointer access reachable from Python",
        "FT002" => "`#[pyclass(freelist = ...)]` on a PyO3 version where it is thread-unsafe",
        "FT003" => "Two locks acquired in a non-atomic sequence guarding one invariant",
        "FT004" => "`OnceCell`/`LazyLock` initialiser with an observable side effect",
        "FT005" => "Unsynchronised access to a C dependency documented as not thread-safe",
        "tsan/data-race" => "Data race observed by ThreadSanitizer",
        "tsan/heap-use-after-free" => "Heap use-after-free observed by ThreadSanitizer",
        "tsan/lock-order-inversion-potential-deadlock" => {
            "Lock-order inversion (potential deadlock) observed by ThreadSanitizer"
        }
        "tsan/signal-unsafe-call-inside-of-a-signal" => {
            "Signal-unsafe call inside a signal handler observed by ThreadSanitizer"
        }
        r if r.starts_with("tsan/") => "Reported by ThreadSanitizer",
        "stress/hang" => "Calls stopped making progress under concurrency (possible deadlock)",
        "stress/panic" => "A Rust panic raised only under concurrent calls",
        _ => "Free-threading hazard",
    }
}

fn level(confidence: Confidence) -> &'static str {
    match confidence {
        Confidence::Certain => "error",
        Confidence::Likely => "warning",
    }
}

fn physical_location(file: &str, line: u32, column: u32) -> Value {
    json!({
        "physicalLocation": {
            "artifactLocation": { "uri": file },
            "region": { "startLine": line.max(1), "startColumn": column.max(1) }
        }
    })
}

/// Emit SARIF 2.1.0 for GitHub code scanning.
///
/// Every rule referenced by a result is declared in the driver, because a
/// consumer that cannot resolve `ruleId` shows the finding without its name.
pub fn to_sarif(findings: &[Finding], tool_version: &str) -> Value {
    let mut rules: BTreeMap<&str, Value> = BTreeMap::new();
    for f in findings {
        rules.entry(&f.rule).or_insert_with(|| {
            json!({
                "id": f.rule,
                "name": f.rule,
                "shortDescription": { "text": rule_description(&f.rule) },
                "defaultConfiguration": { "level": level(f.confidence) },
            })
        });
    }

    let results: Vec<Value> = findings
        .iter()
        .map(|f| {
            let file = f.primary.file.display().to_string();
            let related: Vec<Value> = f
                .stacks
                .iter()
                .enumerate()
                .map(|(i, stack)| {
                    let frame = stack.frames.first();
                    let loc = frame.and_then(|fr| fr.location.as_ref());
                    let mut v = physical_location(
                        &loc.map(|l| l.file.display().to_string())
                            .unwrap_or_else(|| file.clone()),
                        loc.map(|l| l.line).unwrap_or(1),
                        loc.map(|l| l.column).unwrap_or(1),
                    );
                    v["message"] = json!({
                        "text": format!(
                            "thread {} — {}",
                            i + 1,
                            frame.map(|fr| fr.symbol.as_str()).unwrap_or("<unknown>")
                        )
                    });
                    v
                })
                .collect();

            let mut result = json!({
                "ruleId": f.rule,
                "level": level(f.confidence),
                "message": { "text": f.message },
                "locations": [physical_location(&file, f.primary.line, f.primary.column)],
                "properties": {
                    "confidence": f.confidence.as_str(),
                    "producer": f.producer,
                    "symbol": f.symbol,
                },
            });
            if !related.is_empty() {
                result["relatedLocations"] = json!(related);
            }
            if let Some(j) = &f.justification {
                result["properties"]["justification"] = json!(j);
            }
            result
        })
        .collect();

    json!({
        "$schema": SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "ftcheck",
                    "version": tool_version,
                    "informationUri": "https://github.com/latchwright/ftcheck",
                    "rules": rules.into_values().collect::<Vec<_>>(),
                }
            },
            "results": results,
        }]
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::finding::{Frame, Location, Producer, Stack};

    fn lint(rule: &str, confidence: Confidence) -> Finding {
        Finding {
            rule: rule.to_string(),
            message: format!("{rule} fired"),
            confidence,
            producer: Producer::Lint,
            symbol: "bump".to_string(),
            primary: Location::new("src/lib.rs", 10, 5),
            stacks: vec![],
            justification: None,
        }
    }

    fn stack(symbol: &str, line: u32) -> Stack {
        Stack {
            frames: vec![Frame {
                symbol: symbol.to_string(),
                location: Some(Location::new("src/lib.rs", line, 1)),
            }],
        }
    }

    #[test]
    fn emits_sarif_210_with_one_result_per_finding() {
        let v = to_sarif(
            &[
                lint("FT001", Confidence::Certain),
                lint("FT002", Confidence::Certain),
            ],
            "0.0.0",
        );
        assert_eq!(v["version"], "2.1.0");
        assert_eq!(v["runs"][0]["results"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn every_rule_referenced_by_a_result_is_declared_in_the_driver() {
        let v = to_sarif(&[lint("FT001", Confidence::Certain)], "0.0.0");
        let declared: Vec<String> = v["runs"][0]["tool"]["driver"]["rules"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["id"].as_str().unwrap().to_string())
            .collect();
        assert!(declared.contains(&"FT001".to_string()));
    }

    #[test]
    fn a_rule_is_declared_once_however_often_it_fires() {
        let v = to_sarif(
            &[
                lint("FT001", Confidence::Certain),
                lint("FT001", Confidence::Certain),
            ],
            "0.0.0",
        );
        assert_eq!(
            v["runs"][0]["tool"]["driver"]["rules"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn likely_findings_carry_a_lower_level_than_certain() {
        let certain = to_sarif(&[lint("FT001", Confidence::Certain)], "0.0.0");
        let likely = to_sarif(&[lint("FT003", Confidence::Likely)], "0.0.0");
        assert_eq!(certain["runs"][0]["results"][0]["level"], "error");
        assert_eq!(likely["runs"][0]["results"][0]["level"], "warning");
    }

    #[test]
    fn a_race_carries_both_stacks_as_related_locations() {
        let mut f = lint("FT001", Confidence::Certain);
        f.producer = Producer::Tsan;
        f.stacks = vec![stack("thread_one", 10), stack("thread_two", 42)];
        let v = to_sarif(&[f], "0.0.0");
        let related = v["runs"][0]["results"][0]["relatedLocations"]
            .as_array()
            .unwrap();
        assert_eq!(
            related.len(),
            2,
            "both stacks of a race must survive into SARIF"
        );
    }

    #[test]
    fn a_lint_finding_has_no_related_locations_key() {
        let v = to_sarif(&[lint("FT001", Confidence::Certain)], "0.0.0");
        assert!(v["runs"][0]["results"][0].get("relatedLocations").is_none());
    }

    #[test]
    fn line_and_column_are_never_zero() {
        let mut f = lint("FT001", Confidence::Certain);
        f.primary = Location::new("src/lib.rs", 0, 0);
        let v = to_sarif(&[f], "0.0.0");
        let region = &v["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"];
        assert_eq!(region["startLine"], 1, "SARIF regions are 1-based");
        assert_eq!(region["startColumn"], 1);
    }

    #[test]
    fn sanitizer_rules_are_described_not_left_generic() {
        assert_eq!(
            rule_description("tsan/data-race"),
            "Data race observed by ThreadSanitizer"
        );
        assert_eq!(
            rule_description("tsan/some-future-kind"),
            "Reported by ThreadSanitizer"
        );
    }

    #[test]
    fn an_empty_run_is_still_a_valid_document() {
        let v = to_sarif(&[], "0.0.0");
        assert_eq!(v["runs"][0]["results"].as_array().unwrap().len(), 0);
        assert_eq!(v["runs"][0]["tool"]["driver"]["name"], "ftcheck");
    }
}
