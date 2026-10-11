//! Numeric quota chart preparation. Returned indices retain exact source rows.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyList};
use std::collections::{BTreeSet, HashMap};

struct Row {
    at: f64, remaining: f64, reported: f64, cost: Option<f64>, period_cost: Option<f64>,
    value: Option<f64>, completed: Option<Option<f64>>, connect: bool, tracking: bool,
    reset: bool, boundary: bool,
}

fn number(row: &Bound<'_, PyDict>, key: &str, integer_budget: &mut f64) -> PyResult<Option<f64>> {
    match row.get_item(key)? {
        Some(value) if !value.is_none() => {
            let integer = value.is_instance_of::<PyInt>();
            let value: f64 = value.extract()?;
            if integer {
                *integer_budget += value.abs();
                if *integer_budget > 9_007_199_254_740_991.0 {
                    return Err(pyo3::exceptions::PyOverflowError::new_err("Exact integer series requires Python"));
                }
            }
            Ok(Some(value))
        }
        _ => Ok(None),
    }
}

fn required(row: &Bound<'_, PyDict>, key: &str, integer_budget: &mut f64) -> PyResult<f64> {
    number(row, key, integer_budget)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_owned()))
}

fn flag(row: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    row.get_item(key)?.map_or(Ok(false), |value| value.is_truthy())
}

fn equal_field(py: Python<'_>, a: &Bound<'_, PyDict>, b: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    let a = a.get_item(key)?.unwrap_or_else(|| py.None().into_bound(py));
    let b = b.get_item(key)?.unwrap_or_else(|| py.None().into_bound(py));
    a.eq(&b)
}

fn same_cost(a: Option<f64>, b: Option<f64>) -> bool {
    matches!((a,b),(Some(a),Some(b)) if a == b || (a-b).abs() <= 1e-9)
}

struct Series {
    times: Vec<f64>, breaks: Vec<usize>, missing: [Vec<usize>; 3], samples: HashMap<usize, Vec<usize>>,
    maximum: f64, low: f64, high: f64, active_times: Vec<f64>, gap_indices: Vec<usize>, gap_seconds: Vec<f64>,
    cost_minimum: Option<f64>, completed_minimum: Option<f64>, cost_maximum: f64,
    value_minimum: Option<f64>, value_maximum: f64, completed_costs: Vec<Option<f64>>, completed_maximum: f64,
}

fn calculate_completed(rows: &[Row]) -> Vec<Option<f64>> {
    let mut completed = vec![None; rows.len()];
    let mut start = 0;
    let mut valid = false;
    for (index, row) in rows.iter().enumerate() {
        let cost = row.period_cost;
        let known = cost.is_some_and(|value| value.is_finite() && value >= 0.0);
        if row.boundary {
            start = index;
            valid = row.reported == 100.0 && known && cost == Some(0.0);
        } else {
            let previous = &rows[index - 1];
            if !row.connect && !(row.tracking && known && row.reported == previous.reported && same_cost(cost, previous.period_cost)) {
                start = index; valid = false;
            } else {
                valid = valid && known && previous.period_cost.is_some() &&
                    (cost >= previous.period_cost || same_cost(cost, previous.period_cost));
                if row.reported != previous.reported {
                    if valid && previous.reported - row.reported == 1.0 && previous.reported.fract() == 0.0 {
                        let amount = (cost.unwrap() - rows[start].period_cost.unwrap()).max(0.0);
                        completed[start..index].fill(Some(amount));
                    }
                    start = index;
                    valid = row.reported < previous.reported && row.reported.fract() == 0.0 && known;
                }
            }
        }
    }
    completed
}

fn calculate(rows: &[Row]) -> Series {
    let mut completed = if rows.iter().all(|row| row.completed.is_some()) {
        rows.iter().map(|row| row.completed.unwrap()).collect()
    } else { calculate_completed(rows) };
    for (index, row) in rows.iter().enumerate() { if let Some(value) = row.completed { completed[index] = value; } }
    let mut series = Series {
        times: Vec::with_capacity(rows.len()), breaks: Vec::with_capacity(rows.len()),
        missing: std::array::from_fn(|_| Vec::with_capacity(rows.len())), samples: HashMap::new(),
        maximum: 0.0, low: f64::INFINITY, high: 0.0, active_times: Vec::with_capacity(rows.len()),
        gap_indices: Vec::new(), gap_seconds: vec![0.0], cost_minimum: None,
        completed_minimum: completed.iter().flatten().copied().reduce(f64::min), cost_maximum: 0.0,
        value_minimum: None, value_maximum: 0.0,
        completed_maximum: completed.iter().flatten().copied().reduce(f64::max).unwrap_or(0.0), completed_costs: completed,
    };
    let mut active = 0.0;
    let mut gaps = 0;
    let mut missing = [0; 3];
    for (index, row) in rows.iter().enumerate() {
        series.times.push(row.at);
        if index > 0 {
            let elapsed = (row.at - rows[index - 1].at).max(0.0);
            if row.connect { active += elapsed; } else {
                series.gap_indices.push(index);
                series.gap_seconds.push(series.gap_seconds.last().copied().unwrap() + elapsed);
            }
        }
        series.active_times.push(active);
        gaps += usize::from(!row.connect); series.breaks.push(gaps);
        series.low = series.low.min(row.remaining); series.high = series.high.max(row.remaining);
        series.maximum = series.maximum.max(row.cost.unwrap_or(0.0)).max(row.value.unwrap_or(0.0));
        series.cost_maximum = series.cost_maximum.max(row.cost.unwrap_or(0.0));
        series.value_maximum = series.value_maximum.max(row.value.unwrap_or(0.0));
        if let Some(value) = row.value { series.value_minimum = Some(series.value_minimum.map_or(value, |old| old.min(value))); }
        if let Some(value) = row.cost { series.cost_minimum = Some(series.cost_minimum.map_or(value, |old| old.min(value))); }
        for (field, value) in [row.cost, row.value, series.completed_costs[index]].into_iter().enumerate() {
            missing[field] += usize::from(value.is_none());
            if field == 2 && index > 0 { missing[field] += usize::from(row.boundary); }
            series.missing[field].push(missing[field]);
        }
    }
    struct Bucket { first: usize, last: usize, extrema: [Option<(usize, usize)>; 4] }
    let value_at = |index: usize, field: usize| match field {
        0 => Some(rows[index].remaining), 1 => rows[index].cost, 2 => rows[index].value, _ => series.completed_costs[index],
    };
    for budget in [256, 768] {
        if rows.len() <= budget * 2 { series.samples.insert(budget, (0..rows.len()).collect()); continue; }
        let mut selected = BTreeSet::new();
        let mut buckets: Vec<Option<Bucket>> = (0..budget).map(|_| None).collect();
        for (index, row) in rows.iter().enumerate() {
            let bucket = ((series.active_times[index] / active.max(1.0) * budget as f64) as usize).min(budget - 1);
            let state = buckets[bucket].get_or_insert(Bucket { first: index, last: index, extrema: [None; 4] });
            state.last = index;
            for field in 0..4 {
                if let Some(value) = value_at(index, field) {
                    let pair = state.extrema[field].get_or_insert((index, index));
                    if value < value_at(pair.0, field).unwrap() { pair.0 = index; }
                    if value > value_at(pair.1, field).unwrap() { pair.1 = index; }
                }
            }
            if row.reset { selected.insert(index); }
        }
        if series.gap_indices.len() <= budget {
            for &index in &series.gap_indices { selected.insert(index - 1); selected.insert(index); }
        }
        for bucket in buckets.into_iter().flatten() {
            selected.insert(bucket.first); selected.insert(bucket.last);
            for (low, high) in bucket.extrema.into_iter().flatten() { selected.insert(low); selected.insert(high); }
        }
        series.samples.insert(budget, selected.into_iter().collect());
    }
    series
}

fn parse_rows(py: Python<'_>, rows: &Bound<'_, PyList>, series: bool) -> PyResult<Vec<Row>> {
    let mut prepared = Vec::with_capacity(rows.len());
    let mut previous: Option<Bound<'_, PyDict>> = None;
    let mut integer_budget = 0.0;
    for item in rows.iter() {
        let row = item.cast::<PyDict>()?;
        let reset = flag(row, "reset_kind")?;
        let boundary = match &previous {
            None => true,
            Some(old) => reset || !equal_field(py, row, old, "cycle_start")? || !equal_field(py, row, old, "account")?,
        };
        let remaining = required(row, "remaining", &mut integer_budget)?;
        let cost = number(row, "cycle_cost", &mut integer_budget)?;
        prepared.push(Row { at: if series { required(row, "at", &mut integer_budget)? } else { 0.0 }, remaining,
            reported: if row.contains("reported_remaining")? { required(row, "reported_remaining", &mut integer_budget)? } else { remaining },
            cost, period_cost: if row.contains("period_cost")? { number(row, "period_cost", &mut integer_budget)? } else { cost },
            value: if series { number(row, "cycle_value", &mut integer_budget)? } else { None },
            completed: if series && row.contains("completed_cost")? { Some(number(row, "completed_cost", &mut integer_budget)?) } else { None },
            connect: flag(row, "connect")?, tracking: flag(row, "tracking_continuous")?, reset, boundary });
        previous = Some(row.clone());
    }
    Ok(prepared)
}

#[pyfunction]
fn completed_percent_costs(py: Python<'_>, rows: &Bound<'_, PyList>) -> PyResult<Vec<Option<f64>>> {
    let prepared = parse_rows(py, rows, false)?;
    Ok(py.detach(move || calculate_completed(&prepared)))
}

#[pyfunction]
fn quota_series(py: Python<'_>, rows: &Bound<'_, PyList>) -> PyResult<Py<PyDict>> {
    let prepared = parse_rows(py, rows, true)?;
    let series = py.detach(move || calculate(&prepared));
    let output = PyDict::new(py);
    output.set_item("times", series.times)?; output.set_item("breaks", series.breaks)?;
    let missing = PyDict::new(py);
    for (field, values) in ["cycle_cost", "cycle_value", "completed_cost"].into_iter().zip(series.missing) { missing.set_item(field, values)?; }
    output.set_item("missing", missing)?; output.set_item("samples", series.samples)?;
    output.set_item("maximum", series.maximum)?; output.set_item("low", series.low)?; output.set_item("high", series.high)?;
    output.set_item("active_times", series.active_times)?; output.set_item("gap_indices", series.gap_indices)?; output.set_item("gap_seconds", series.gap_seconds)?;
    output.set_item("cost_minimum", series.cost_minimum)?; output.set_item("completed_minimum", series.completed_minimum)?;
    output.set_item("cost_maximum", series.cost_maximum)?; output.set_item("value_minimum", series.value_minimum)?;
    output.set_item("value_maximum", series.value_maximum)?; output.set_item("completed_costs", series.completed_costs)?;
    output.set_item("completed_maximum", series.completed_maximum)?;
    Ok(output.unbind())
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(quota_series, module)?)?;
    module.add_function(wrap_pyfunction!(completed_percent_costs, module)?)?;
    Ok(())
}
