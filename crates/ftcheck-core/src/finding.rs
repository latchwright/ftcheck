// SPDX-License-Identifier: MIT OR Apache-2.0
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// How much the tool is willing to assert.
///
/// `Certain` findings are shown by default and fail CI. `Likely` findings are
/// hidden unless asked for, because a false `CRITICAL` on a trivial case costs
/// more trust than a missed `likely` ever recovers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Confidence {
    // Ordering matters: `Certain > Likely`.
    Likely,
    Certain,
}

impl Confidence {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "certain" => Some(Confidence::Certain),
            "likely" => Some(Confidence::Likely),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Confidence::Certain => "certain",
            Confidence::Likely => "likely",
        }
    }
}

/// Which half of ftcheck produced a finding.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Producer {
    Lint,
    Tsan,
    Asan,
    /// Observed by the stress driver itself — a hang under concurrent calls.
    Stress,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Location {
    pub file: PathBuf,
    pub line: u32,
    pub column: u32,
}

impl Location {
    pub fn new(file: impl Into<PathBuf>, line: u32, column: u32) -> Self {
        Location {
            file: file.into(),
            line,
            column,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Frame {
    pub symbol: String,
    pub location: Option<Location>,
}

/// One thread's stack in a sanitizer report. A race has two.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Stack {
    pub frames: Vec<Frame>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Finding {
    pub rule: String,
    pub message: String,
    pub confidence: Confidence,
    pub producer: Producer,
    /// The enclosing symbol — `bump`, `Buffer::bump`. Part of the dedup key.
    pub symbol: String,
    pub primary: Location,
    #[serde(default)]
    pub stacks: Vec<Stack>,
    #[serde(default)]
    pub justification: Option<String>,
}

impl Finding {
    /// A stable identity for this finding, used to deduplicate.
    ///
    /// Deliberately keyed on rule, file and symbol — **not** on line number.
    /// Reformatting a file must not resurrect a finding a team has already
    /// triaged, and the same race reported from twenty interleavings is one
    /// problem, not twenty.
    pub fn signature(&self) -> String {
        let symbol = self
            .stacks
            .first()
            .and_then(|s| s.frames.first())
            .map(|f| f.symbol.as_str())
            .unwrap_or(self.symbol.as_str());
        format!("{}|{}|{}", self.rule, self.primary.file.display(), symbol)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lint(rule: &str, file: &str, line: u32, symbol: &str) -> Finding {
        Finding {
            rule: rule.to_string(),
            message: "x".to_string(),
            confidence: Confidence::Certain,
            producer: Producer::Lint,
            symbol: symbol.to_string(),
            primary: Location::new(file, line, 1),
            stacks: vec![],
            justification: None,
        }
    }

    #[test]
    fn signature_ignores_line_drift_within_the_same_symbol() {
        let a = lint("FT001", "src/lib.rs", 10, "bump");
        let b = lint("FT001", "src/lib.rs", 14, "bump");
        assert_eq!(
            a.signature(),
            b.signature(),
            "reformatting must not resurrect a triaged finding"
        );
    }

    #[test]
    fn signature_distinguishes_different_rules() {
        assert_ne!(
            lint("FT001", "src/lib.rs", 10, "bump").signature(),
            lint("FT003", "src/lib.rs", 10, "bump").signature()
        );
    }

    #[test]
    fn signature_distinguishes_different_symbols() {
        assert_ne!(
            lint("FT001", "src/lib.rs", 10, "bump").signature(),
            lint("FT001", "src/lib.rs", 10, "reset").signature()
        );
    }

    #[test]
    fn signature_distinguishes_different_files() {
        assert_ne!(
            lint("FT001", "src/a.rs", 10, "bump").signature(),
            lint("FT001", "src/b.rs", 10, "bump").signature()
        );
    }

    #[test]
    fn a_stack_frame_symbol_wins_over_the_recorded_symbol() {
        let mut f = lint("FT001", "src/lib.rs", 10, "outer");
        f.stacks = vec![Stack {
            frames: vec![Frame {
                symbol: "inner".to_string(),
                location: None,
            }],
        }];
        assert!(f.signature().ends_with("|inner"));
    }

    #[test]
    fn certain_sorts_above_likely() {
        assert!(Confidence::Certain > Confidence::Likely);
    }

    #[test]
    fn confidence_round_trips_through_its_string_form() {
        for c in [Confidence::Certain, Confidence::Likely] {
            assert_eq!(Confidence::parse(c.as_str()), Some(c));
        }
        assert_eq!(Confidence::parse("extremely"), None);
    }
}
