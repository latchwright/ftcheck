//! Correct: `frozen` makes the type immutable after construction, so sharing it
//! across threads shares nothing that can change.

use pyo3::prelude::*;

#[pyclass(frozen)]
struct Label {
    text: String,
}

#[pymethods]
impl Label {
    #[new]
    fn new(text: String) -> Self {
        Label { text }
    }

    fn text(&self) -> &str {
        &self.text
    }
}

#[pymodule]
fn clean_frozen(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Label>()
}
