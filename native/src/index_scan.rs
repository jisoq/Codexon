//! Find complete candidate event lines without constructing Python line objects.
use memchr::{memchr_iter, memmem};
use pyo3::prelude::*;

const MARKERS: [&[u8]; 4] = [
    b"\"turn_context\"",
    b"\"token_usage_record\"",
    b"\"event_msg\"",
    b"\"compacted\"",
];

#[pyfunction]
fn scan_event_lines(py: Python<'_>, data: &[u8]) -> (usize, Vec<(usize, usize)>) {
    py.detach(|| {
        let mut consumed = 0;
        let mut selected = Vec::new();
        let finders: Vec<_> = MARKERS.iter().map(|marker| memmem::Finder::new(marker)).collect();
        for newline in memchr_iter(b'\n', data) {
            let end = newline + 1;
            let prefix = &data[consumed..end.min(consumed + 250)];
            if finders.iter().any(|finder| finder.find(prefix).is_some()) {
                selected.push((consumed, end));
            }
            consumed = end;
        }
        (consumed, selected)
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(scan_event_lines, m)?)?;
    Ok(())
}
