//! CPU-only trend projection kernels. Python owns labels, records and calendars.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList};
use std::collections::{HashMap, VecDeque};

fn number(row: &Bound<'_, PyDict>, key: &str, integer_budget: &mut f64) -> PyResult<Option<f64>> {
    let Some(value) = row.get_item(key)? else { return Ok(None) };
    if !value.is_exact_instance_of::<PyInt>() && !value.is_exact_instance_of::<PyFloat>() {
        return Ok(None);
    }
    let integer = value.is_exact_instance_of::<PyInt>();
    let value: f64 = value.extract()?;
    if integer {
        *integer_budget += value.abs();
        if *integer_budget > 9_007_199_254_740_991.0 {
            return Err(pyo3::exceptions::PyOverflowError::new_err("Exact integer population requires Python"));
        }
    }
    Ok((value.is_finite() && value >= 0.0).then_some(value))
}

fn text(row: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    match row.get_item(key)? {
        Some(value) if value.is_truthy()? => value.extract(),
        _ => Ok("미확인".to_owned()),
    }
}

fn flag(row: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    row.get_item(key)?.map_or(Ok(false), |value| value.is_truthy())
}

fn quantile(values: &[f64], q: f64) -> Option<f64> {
    if values.is_empty() { return None; }
    let position = (values.len() - 1) as f64 * q;
    let index = position as usize;
    Some(values[index] + (values[(index + 1).min(values.len() - 1)] - values[index]) * (position - index as f64))
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(f64::total_cmp);
    let index = values.len() / 2;
    if values.len() % 2 == 0 { (values[index - 1] + values[index]) / 2.0 } else { values[index] }
}

#[derive(Clone)]
struct Observation {
    row: usize,
    at: f64,
    value: f64,
    scope: (String, String, String),
}

#[derive(Clone)]
struct Sample {
    row: usize,
    value: f64,
    baseline: Option<f64>,
    delta: Option<f64>,
    previous: usize,
    direction: i32,
}

#[derive(Default)]
struct History {
    values: VecDeque<f64>,
    pending_at: Option<f64>,
    pending: Vec<f64>,
}

struct Cell {
    first: Sample,
    low: Sample,
    high: Sample,
    last: Sample,
    negative: Option<(Sample, usize)>,
    positive: Option<(Sample, usize)>,
}

type SampleOutput = (usize, Option<f64>, Option<f64>, usize, i32, Option<usize>);
type SampleBatch = (Vec<SampleOutput>, usize, usize, f64, f64);

#[pyfunction]
#[pyo3(signature = (rows, field, start, end, width, effort=None))]
fn trend_samples(
    py: Python<'_>, rows: &Bound<'_, PyList>, field: &str, start: f64,
    end: f64, width: usize, effort: Option<&str>,
) -> PyResult<SampleBatch> {
    if width == 0 || end <= start {
        return Err(pyo3::exceptions::PyValueError::new_err("Trend range and width must be positive"));
    }
    let mut observations = Vec::with_capacity(rows.len());
    let mut total = 0;
    let mut integer_budget = 0.0;
    for (index, item) in rows.iter().enumerate() {
        let row = item.cast::<PyDict>()?;
        let own_effort = text(row, "effort")?;
        if effort.is_some_and(|wanted| wanted != own_effort) { continue; }
        let at: f64 = row.get_item("ts")?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("ts"))?.extract()?;
        if start <= at && at <= end { total += 1; }
        if let Some(value) = number(row, field, &mut integer_budget)? {
            observations.push(Observation { row: index, at, value,
                scope: (text(row, "model")?, text(row, "service_tier")?, own_effort) });
        }
    }
    Ok(py.detach(move || {
        let mut histories: HashMap<(String, String, String), History> = HashMap::new();
        let mut positions: HashMap<(String, String, usize), usize> = HashMap::new();
        let mut cells: Vec<Cell> = Vec::new();
        let mut count = 0;
        let mut minimum = f64::INFINITY;
        let mut maximum = f64::NEG_INFINITY;
        for observation in observations {
            let history = histories.entry(observation.scope.clone()).or_default();
            if history.pending_at.is_some_and(|at| at < observation.at) {
                for value in history.pending.drain(..) {
                    if history.values.len() == 50 { history.values.pop_front(); }
                    history.values.push_back(value);
                }
                history.pending_at = None;
            }
            let previous = history.values.len();
            let (baseline, delta, direction) = if previous >= 10 {
                let mut values: Vec<f64> = history.values.iter().copied().collect();
                let center = median(&mut values);
                for value in &mut values { *value = (*value - center).abs(); }
                let deviation = median(&mut values);
                let delta = observation.value - center;
                let abnormal = delta.abs() > 3.5 * 1.4826 * deviation && delta.abs() > center.abs() * 0.2;
                (Some(center), Some(delta), if abnormal { if delta > 0.0 { 1 } else { -1 } } else { 0 })
            } else { (None, None, 0) };
            history.pending_at.get_or_insert(observation.at);
            history.pending.push(observation.value);
            if observation.at < start || observation.at > end { continue; }
            count += 1;
            minimum = minimum.min(observation.value);
            maximum = maximum.max(observation.value);
            let sample = Sample { row: observation.row, value: observation.value, baseline, delta, previous, direction };
            let pixel = (((observation.at - start) / (end - start) * width as f64) as usize).min(width - 1);
            let key = (observation.scope.0, observation.scope.1, pixel);
            let position = *positions.entry(key).or_insert_with(|| {
                cells.push(Cell { first: sample.clone(), low: sample.clone(), high: sample.clone(), last: sample.clone(), negative: None, positive: None });
                cells.len() - 1
            });
            let cell = &mut cells[position];
            cell.last = sample.clone();
            if sample.value < cell.low.value { cell.low = sample.clone(); }
            if sample.value > cell.high.value { cell.high = sample.clone(); }
            let anomaly = if direction < 0 { &mut cell.negative } else { &mut cell.positive };
            if direction != 0 {
                if let Some((_, count)) = anomaly { *count += 1; } else { *anomaly = Some((sample, 1)); }
            }
        }
        let output = |sample: Sample, count| (sample.row, sample.baseline, sample.delta, sample.previous, sample.direction, count);
        let mut samples = Vec::new();
        for cell in cells {
            let mut seen = Vec::with_capacity(4);
            for sample in [cell.first, cell.low, cell.high, cell.last] {
                if !seen.contains(&sample.row) { seen.push(sample.row); samples.push(output(sample, None)); }
            }
            for (sample, count) in [cell.negative, cell.positive].into_iter().flatten() { samples.push(output(sample, Some(count))); }
        }
        (samples, count, total, if count == 0 { 0.0 } else { minimum }, if count == 0 { 1.0 } else { maximum })
    }))
}

struct MetricRow {
    value: Option<f64>, input: Option<f64>, cached: Option<f64>, output: Option<f64>,
    seconds: Option<f64>, session: usize,
}

// Python 3.12 sums floats with compensation; avoid ordinary left-fold drift.
fn sum(values: impl Iterator<Item = f64>) -> f64 {
    let mut total = 0.0_f64;
    let mut correction = 0.0_f64;
    for value in values {
        let next = total + value;
        correction += if total.abs() >= value.abs() { (total - next) + value } else { (value - next) + total };
        total = next;
    }
    if correction.is_finite() { total + correction } else { total }
}

// (values, denominator, totals [sum/input/output, cached/seconds, valid_calls], sessions)
fn population(rows: &[MetricRow], metric: &str) -> (Vec<f64>, usize, [f64; 3], usize) {
    let mut indices = HashMap::new();
    let mut sessions = Vec::<[f64; 3]>::new();
    for row in rows {
        let index = *indices.entry(row.session).or_insert_with(|| { sessions.push([0.0; 3]); sessions.len() - 1 });
        if metric == "session_cache" {
            if let (Some(input), Some(cached)) = (row.input, row.cached) {
                sessions[index][0] += input; sessions[index][1] += cached; sessions[index][2] += 1.0;
            }
        }
    }
    let session_count = sessions.len();
    if metric == "count" { return (Vec::new(), rows.len(), [rows.len() as f64, 0.0, 0.0], session_count); }
    if metric == "session_cache" {
        let eligible: Vec<_> = sessions.iter().filter(|row| row[0] > 0.0).collect();
        return (eligible.iter().map(|row| 100.0 * row[1] / row[0]).collect(), session_count,
            [sum(eligible.iter().map(|row| row[0])), sum(eligible.iter().map(|row| row[1])), sum(eligible.iter().map(|row| row[2]))], session_count);
    }
    let values: Vec<_> = rows.iter().filter_map(|row| row.value).collect();
    let totals = if metric == "output_speed" {
        [sum(rows.iter().filter(|row| row.value.is_some()).filter_map(|row| row.output)),
         sum(rows.iter().filter(|row| row.value.is_some()).filter_map(|row| row.seconds)), 0.0]
    } else { [sum(values.iter().copied()), 0.0, 0.0] };
    (values, rows.len(), totals, session_count)
}

// Numeric fields: median, q1, q3, p10, p90, minimum, maximum.
type Distribution = (Option<f64>, Option<f64>, Option<f64>, Option<f64>, Option<f64>, Option<f64>, Option<f64>);
type PopulationOutput = (usize, usize, [f64; 3], Distribution, Option<f64>, bool, usize);
type RollingOutput = ((usize, usize, f64, f64), Vec<PopulationOutput>);

#[pyfunction]
fn trend_populations(py: Python<'_>, rows: &Bound<'_, PyList>, metric: &str,
    windows: Vec<(usize, usize, usize, usize)>) -> PyResult<RollingOutput> {
    let field = if metric.starts_with("reasoning:") { "reasoning" } else { metric };
    let mut prepared = Vec::with_capacity(rows.len());
    let mut sessions = HashMap::new();
    let mut integer_budget = 0.0;
    for item in rows.iter() {
        let row = item.cast::<PyDict>()?;
        let session_key: (String, String) = (
            row.get_item("home")?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("home"))?.extract()?,
            row.get_item("sid")?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("sid"))?.extract()?,
        );
        let next = sessions.len();
        let session = *sessions.entry(session_key).or_insert(next);
        let mut value = number(row, field, &mut integer_budget)?;
        let mut input = None; let mut cached = None; let mut output = None; let mut seconds = None;
        if metric == "session_cache" {
            input = number(row, "input", &mut integer_budget)?; cached = number(row, "cached", &mut integer_budget)?;
            if flag(row, "input_conflict")? || !matches!((input, cached), (Some(a), Some(b)) if b <= a) { input = None; cached = None; }
        } else if metric == "output_speed" {
            output = number(row, "output", &mut integer_budget)?;
            seconds = number(row, "completion_latency_ms", &mut integer_budget)?.filter(|value| *value > 0.0).map(|value| value / 1000.0);
            if output.is_none() || seconds.is_none() { value = None; }
        }
        prepared.push(MetricRow { value, input, cached, output, seconds, session });
    }
    if windows.iter().any(|&(a, b, c, d)| a > b || c > d || b > prepared.len() || d > prepared.len()) {
        return Err(pyo3::exceptions::PyValueError::new_err("Trend window is outside the population"));
    }
    Ok(py.detach(move || {
        let (values, total, _, _) = population(&prepared, metric);
        let eligible = if metric == "count" { prepared.len() } else { values.len() };
        let low = values.iter().copied().reduce(f64::min).unwrap_or(0.0);
        let high = values.iter().copied().reduce(f64::max).unwrap_or(1.0);
        let mut output = Vec::with_capacity(windows.len());
        for (start, end, support_start, support_end) in windows {
            let (mut values, total, totals, session_count) = population(&prepared[start..end], metric);
            let mean = if metric == "count" { Some(totals[0]) } else if metric == "output_speed" {
                (totals[1] != 0.0).then(|| totals[0] / totals[1])
            } else { (!values.is_empty()).then(|| sum(values.iter().copied()) / values.len() as f64) };
            values.sort_by(f64::total_cmp);
            let distribution = (quantile(&values, 0.5), quantile(&values, 0.25), quantile(&values, 0.75),
                (values.len() >= 30).then(|| quantile(&values, 0.1)).flatten(), (values.len() >= 30).then(|| quantile(&values, 0.9)).flatten(),
                values.first().copied(), values.last().copied());
            let support = metric == "count" || !population(&prepared[support_start..support_end], metric).0.is_empty();
            let eligible = if metric == "count" { end - start } else { values.len() };
            output.push((eligible, total, totals, distribution, mean, support, session_count));
        }
        ((eligible, total, low, high), output)
    }))
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(trend_samples, module)?)?;
    module.add_function(wrap_pyfunction!(trend_populations, module)?)?;
    Ok(())
}
