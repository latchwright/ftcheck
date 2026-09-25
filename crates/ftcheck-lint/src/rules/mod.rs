// SPDX-License-Identifier: MIT OR Apache-2.0
pub mod ft001;
pub mod ft002;
pub mod ft003;

use ftcheck_core::Location;
use std::path::Path;

/// Turn a span into a source location.
///
/// Requires `proc-macro2`'s `span-locations` feature. Without it every line is
/// zero and no finding can be mapped back to source, which is why the workspace
/// manifest pins that feature with a comment saying so.
pub(crate) fn location_of(span: proc_macro2::Span, file: &Path) -> Location {
    let start = span.start();
    // proc-macro2 columns are 0-based; SARIF and every editor are 1-based.
    Location::new(
        file.to_path_buf(),
        start.line as u32,
        start.column as u32 + 1,
    )
}
