use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::exceptions::PyValueError;

fn exact_time(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    let number = value.extract::<f64>()?;
    if !number.is_finite() || number.abs() > 9_007_199_254_740_991.0 {
        return Err(PyValueError::new_err("Timestamp requires Python precision"));
    }
    Ok(number)
}

#[pyclass]
struct QuotaClipIndex {
    endpoints: Vec<Py<PyAny>>,
    endpoint_times: Vec<f64>,
    ordered_endpoints: Vec<(f64, usize)>,
    rows: Vec<Py<PyAny>>,
    ordered_rows: Vec<(f64, usize)>,
}

fn selected(values: &[(f64, usize)], start: f64, end: f64, left_open: bool) -> Vec<usize> {
    let lo = values.partition_point(|(time, _)| if left_open { *time <= start } else { *time < start });
    let hi = values.partition_point(|(time, _)| *time <= end);
    let mut indices: Vec<_> = values[lo.min(hi)..hi].iter().map(|(_, index)| *index).collect();
    indices.sort_unstable(); // Keep original stable record order, including duplicate times.
    indices
}

#[pymethods]
impl QuotaClipIndex {
    #[new]
    fn new(endpoints: &Bound<'_, PyList>, rows: &Bound<'_, PyList>) -> PyResult<Self> {
        let mut result = Self { endpoints: Vec::new(), endpoint_times: Vec::new(), ordered_endpoints: Vec::new(), rows: Vec::new(), ordered_rows: Vec::new() };
        for (index, row) in endpoints.iter().enumerate() {
            let time = exact_time(&row.get_item(0)?)?;
            result.endpoint_times.push(time);
            result.ordered_endpoints.push((time, index));
            result.endpoints.push(row.unbind());
        }
        for (index, row) in rows.iter().enumerate() {
            let time = exact_time(&row.cast::<PyDict>()?.get_item("ts")?.ok_or_else(|| PyValueError::new_err("Missing timestamp"))?)?;
            result.ordered_rows.push((time, index));
            result.rows.push(row.unbind());
        }
        result.ordered_endpoints.sort_by(|a,b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
        result.ordered_rows.sort_by(|a,b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
        Ok(result)
    }

    fn select<'py>(&self, py: Python<'py>, start: f64, end: f64) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
        let (points, rows) = py.detach(|| {
            let points = selected(&self.ordered_endpoints, start, end, false);
            let rows = if points.len() >= 2 {
                selected(&self.ordered_rows, self.endpoint_times[points[0]], self.endpoint_times[*points.last().unwrap()], true)
            } else { Vec::new() };
            (points, rows)
        });
        Ok((PyList::new(py, points.iter().map(|i| self.endpoints[*i].bind(py)))?,
            PyList::new(py, rows.iter().map(|i| self.rows[*i].bind(py)))?))
    }
}

#[pyclass]
struct RequestWindows {
    rows: Vec<Py<PyAny>>,
    starts: Vec<(f64, f64, usize)>,
    ends: Vec<(f64, f64, usize)>,
}

#[pymethods]
impl RequestWindows {
    #[new]
    fn new(spans: &Bound<'_, PyList>) -> PyResult<Self> {
        let mut rows = Vec::new();
        let mut starts = Vec::new();
        for (index, span) in spans.iter().enumerate() {
            let span = span.cast::<PyTuple>()?;
            let start = exact_time(&span.get_item(0)?)?;
            let end = span.get_item(1)?.extract::<f64>()?;
            if end.is_nan() || end == f64::NEG_INFINITY || end.is_finite() && end.abs() > 9_007_199_254_740_991.0 {
                return Err(PyValueError::new_err("Timestamp requires Python precision"));
            }
            starts.push((start, end, index));
            rows.push(span.get_item(2)?.unbind());
        }
        let mut ends = starts.clone();
        // Python treats signed zeros as equal and keeps their input order.
        // Construction rejects NaN; an open end may still be positive infinity.
        starts.sort_by(|a,b| a.0.partial_cmp(&b.0).expect("validated start time"));
        ends.sort_by(|a,b| a.1.partial_cmp(&b.1).expect("validated end time"));
        Ok(Self { rows, starts, ends })
    }

    fn overlapping<'py>(&self, py: Python<'py>, start: f64, end: f64) -> PyResult<Bound<'py, PyList>> {
        let indices = py.detach(|| {
            let hi = self.starts.partition_point(|row| row.0 <= end);
            let lo = self.ends.partition_point(|row| row.1 < start);
            if hi <= self.ends.len() - lo {
                self.starts[..hi].iter().filter(|row| row.1 >= start).map(|row| row.2).collect::<Vec<_>>()
            } else {
                self.ends[lo..].iter().filter(|row| row.0 <= end).map(|row| row.2).collect()
            }
        });
        PyList::new(py, indices.iter().map(|index| self.rows[*index].bind(py)))
    }
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<QuotaClipIndex>()?;
    module.add_class::<RequestWindows>()
}
