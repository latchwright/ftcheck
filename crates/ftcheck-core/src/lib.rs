// SPDX-License-Identifier: MIT OR Apache-2.0
//! Finding model and report emitters shared by every ftcheck producer.
//!
//! The lint produces findings statically and the sanitizer pipeline produces
//! them dynamically. Both land in one report with one schema, so a consumer
//! reading SARIF cannot tell which producer a finding came from except by its
//! rule id. That is why this crate owns the type and the emitters, and the
//! rules do not.

mod dedupe;
mod finding;
mod junit;
mod sarif;

pub use dedupe::dedupe;
pub use finding::{Confidence, Finding, Frame, Location, Producer, Stack};
pub use junit::to_junit;
pub use sarif::to_sarif;
