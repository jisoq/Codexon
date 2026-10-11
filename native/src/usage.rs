//! Batch usage aggregation and chronological transport matching.
//!
//! Unsupported Python objects and unusually large integers return None so the
//! caller can preserve Python's complete numeric and mapping semantics.
use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString};

const METRICS: [&str; 9] = [
    "input", "cached", "uncached", "written", "ordinary_input", "output",
    "reasoning", "non_reasoning", "total",
];

#[derive(Default)]
struct Summary {
    count: usize,
    sums: [u128; 9],
    known: [usize; 9],
    inputs: u128,
    cached: u128,
}

impl Summary {
    fn add(&mut self, values: [Option<u128>; 9]) -> Option<()> {
        self.count += 1;
        for (index, value) in values.iter().enumerate() {
            if let Some(value) = value {
                self.sums[index] = self.sums[index].checked_add(*value)?;
                self.known[index] += 1;
            }
        }
        if let (Some(input), Some(cached)) = (values[0], values[1]) {
            if cached <= input {
                self.inputs = self.inputs.checked_add(input)?;
                self.cached = self.cached.checked_add(cached)?;
            }
        }
        Some(())
    }

    fn output<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        // Keep Python's correctly rounded integer true division, including
        // values above f64's exact integer range.
        let Some(numerator) = self.cached.checked_mul(100) else {
            return Ok(None);
        };
        let result = PyDict::new(py);
        let missing = PyDict::new(py);
        result.set_item("count", self.count)?;
        for (index, key) in METRICS.iter().enumerate() {
            if self.known[index] > 0 || self.count == 0 {
                result.set_item(key, self.sums[index])?;
            } else {
                result.set_item(key, py.None())?;
            }
            missing.set_item(key, self.count - self.known[index])?;
        }
        result.set_item("missing", missing)?;
        if self.inputs > 0 {
            let rate = numerator.into_pyobject(py)?.call_method1("__truediv__", (self.inputs,))?;
            result.set_item("rate", rate)?;
        } else {
            result.set_item("rate", py.None())?;
        }
        Ok(Some(result))
    }
}

fn values(row: &Bound<'_, PyDict>, keys: &[Bound<'_, PyString>]) -> PyResult<Option<[Option<u128>; 9]>> {
    let mut result = [None; 9];
    for (index, key) in keys.iter().enumerate() {
        let Some(value) = row.get_item(key)? else { continue };
        // Python's token_number deliberately excludes bool and int subclasses.
        if !value.is_exact_instance_of::<PyInt>() {
            continue;
        }
        match value.extract::<u128>() {
            Ok(number) => result[index] = Some(number),
            Err(_) if value.lt(0)? => (),
            Err(_) => return Ok(None),
        }
    }
    Ok(Some(result))
}

#[pyfunction]
fn summarize_usage<'py>(py: Python<'py>, rows: &Bound<'py, PyList>) -> PyResult<Option<Bound<'py, PyDict>>> {
    let keys: Vec<_> = METRICS.iter().map(|key| PyString::intern(py, key)).collect();
    let mut summary = Summary::default();
    for row in rows.iter() {
        if !row.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        let row = row.cast::<PyDict>()?;
        let Some(values) = values(row, &keys)? else { return Ok(None) };
        if summary.add(values).is_none() {
            return Ok(None);
        }
    }
    summary.output(py)
}

fn label(row: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match row.get_item(key)? {
        None => Ok(Some("미확인".to_owned())),
        Some(value) if !value.is_truthy()? => Ok(Some("미확인".to_owned())),
        Some(value) if value.is_exact_instance_of::<PyString>() => Ok(Some(value.extract()?)),
        Some(_) => Ok(None),
    }
}

#[pyfunction]
fn group_usage<'py>(py: Python<'py>, rows: &Bound<'py, PyList>) -> PyResult<Option<Bound<'py, PyList>>> {
    let keys: Vec<_> = METRICS.iter().map(|key| PyString::intern(py, key)).collect();
    let mut positions = HashMap::new();
    let mut groups: Vec<(String, String, Summary)> = Vec::new();
    for row in rows.iter() {
        if !row.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        let row = row.cast::<PyDict>()?;
        let Some(model) = label(row, "model")? else { return Ok(None) };
        let Some(effort) = label(row, "effort")? else { return Ok(None) };
        let index = *positions.entry((model.clone(), effort.clone())).or_insert_with(|| {
            let index = groups.len();
            groups.push((model, effort, Summary::default()));
            index
        });
        let Some(values) = values(row, &keys)? else { return Ok(None) };
        if groups[index].2.add(values).is_none() {
            return Ok(None);
        }
    }
    // Stable sorting preserves the first-seen group order when totals tie.
    groups.sort_by(|a, b| b.2.sums[8].cmp(&a.2.sums[8]));
    let result = PyList::empty(py);
    for (model, effort, summary) in groups {
        let Some(group) = summary.output(py)? else { return Ok(None) };
        group.set_item("model", model)?;
        group.set_item("effort", effort)?;
        result.append(group)?;
    }
    Ok(Some(result))
}

struct TimedTurn {
    ts: f64,
    turn: String,
    index: usize,
}

fn timed_turns(items: &Bound<'_, PyList>) -> PyResult<Option<Vec<TimedTurn>>> {
    let mut result = Vec::with_capacity(items.len());
    for (index, item) in items.iter().enumerate() {
        let value = item.getattr("ts")?;
        if !value.is_exact_instance_of::<PyInt>() && !value.is_exact_instance_of::<PyFloat>() {
            return Ok(None);
        }
        let Ok(ts) = value.extract::<f64>() else { return Ok(None) };
        if value.is_exact_instance_of::<PyInt>() && !value.eq(ts)? {
            return Ok(None);
        }
        if !ts.is_finite() {
            return Ok(None);
        }
        let turn = item.getattr("turn")?;
        let turn = if !turn.is_truthy()? {
            String::new()
        } else if turn.is_exact_instance_of::<PyString>() {
            turn.extract()?
        } else {
            return Ok(None);
        };
        result.push(TimedTurn { ts, turn, index });
    }
    Ok(Some(result))
}

fn later(a: Option<(f64, usize)>, b: Option<(f64, usize)>) -> Option<(f64, usize)> {
    match (a, b) {
        (Some(a), Some(b)) => Some(if b.0 > a.0 || b.0 == a.0 && b.1 < a.1 { b } else { a }),
        (a, b) => a.or(b),
    }
}

#[pyfunction]
fn transport_matches(py: Python<'_>, requests: &Bound<'_, PyList>, transports: &Bound<'_, PyList>) -> PyResult<Option<Vec<Option<usize>>>> {
    let Some(mut requests) = timed_turns(requests)? else { return Ok(None) };
    let Some(mut transports) = timed_turns(transports)? else { return Ok(None) };
    Ok(Some(py.detach(move || {
        requests.sort_by(|a, b| a.ts.total_cmp(&b.ts));
        transports.sort_by(|a, b| a.ts.total_cmp(&b.ts));
        let mut result = vec![None; requests.len()];
        let mut by_turn: HashMap<String, (f64, usize)> = HashMap::new();
        let mut any = None;
        let mut blank = None;
        let mut offset = 0;
        for request in requests {
            while offset < transports.len() && transports[offset].ts <= request.ts {
                let transport = &transports[offset];
                let candidate = Some((transport.ts, transport.index));
                any = later(any, candidate);
                if transport.turn.is_empty() {
                    blank = later(blank, candidate);
                } else {
                    let best = later(by_turn.get(&transport.turn).copied(), candidate).unwrap();
                    by_turn.insert(transport.turn.clone(), best);
                }
                offset += 1;
            }
            let selected = if request.turn.is_empty() {
                any
            } else {
                later(blank, by_turn.get(&request.turn).copied())
            };
            result[request.index] = selected.map(|(_, index)| index);
        }
        result
    })))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(summarize_usage, m)?)?;
    m.add_function(wrap_pyfunction!(group_usage, m)?)?;
    m.add_function(wrap_pyfunction!(transport_matches, m)?)?;
    Ok(())
}
