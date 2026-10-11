//! Batch object-preserving ingest bookkeeping; pricing remains in Python.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

#[pyfunction]
fn row_signatures<'py>(
    py: Python<'py>,
    rows: &Bound<'py, PyList>,
    fields: &Bound<'py, PyTuple>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let keys: Vec<_> = fields.iter().collect();
    let mut result = Vec::with_capacity(rows.len());
    for row in rows.iter() {
        if !row.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        let row = row.cast::<PyDict>()?;
        let mut values = Vec::with_capacity(keys.len());
        for key in &keys {
            values.push(row.get_item(key)?.unwrap_or_else(|| py.None().into_bound(py)));
        }
        result.push(PyTuple::new(py, values)?);
    }
    Ok(Some(PyTuple::new(py, result)?))
}

fn identity<'py>(
    py: Python<'py>,
    row: &Bound<'py, PyAny>,
    home: &Bound<'py, PyAny>,
    sid: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    if !row.is_exact_instance_of::<PyDict>() {
        return Ok(None);
    }
    let row = row.cast::<PyDict>()?;
    let key = row.get_item("key")?.unwrap_or_else(|| py.None().into_bound(py));
    let local = if key.is_none() {
        false
    } else if key.is_exact_instance_of::<PyString>() {
        let text = key.cast::<PyString>()?;
        let Ok(text) = text.to_str() else { return Ok(None) };
        text.starts_with("record:") || text.starts_with("legacy:")
    } else {
        // call_identity uses str(key) while retaining the original key. Let
        // Python handle custom strings, numbers and custom hash behavior.
        return Ok(None);
    };
    Ok(Some(if local {
        PyTuple::new(py, [home.clone(), sid.clone(), key])?
    } else {
        PyTuple::new(py, [home.clone(), key])?
    }))
}

#[pyfunction]
fn owned_histories<'py>(
    py: Python<'py>,
    sessions: &Bound<'py, PyList>,
    order: Vec<usize>,
) -> PyResult<Option<Bound<'py, PyList>>> {
    let mut sources = Vec::with_capacity(sessions.len());
    for session in sessions.iter() {
        if !session.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        let session = session.cast::<PyDict>()?;
        let Some(home) = session.get_item("home")? else { return Ok(None) };
        let Some(sid) = session.get_item("id")? else { return Ok(None) };
        let Some(history) = session.get_item("history")? else { return Ok(None) };
        if !history.is_exact_instance_of::<PyList>() {
            return Ok(None);
        }
        sources.push((home, sid, history.cast_into::<PyList>()?));
    }
    let owners = PyDict::new(py);
    for index in order {
        let Some((home, sid, history)) = sources.get(index) else { return Ok(None) };
        let session_key = PyTuple::new(py, [home, sid])?;
        for row in history.iter() {
            let Some(key) = identity(py, &row, home, sid)? else { return Ok(None) };
            if !owners.contains(&key)? {
                owners.set_item(key, &session_key)?;
            }
        }
    }
    let result = PyList::empty(py);
    for (home, sid, history) in sources {
        let session_key = PyTuple::new(py, [&home, &sid])?;
        let owned = PyList::empty(py);
        for row in history.iter() {
            let Some(key) = identity(py, &row, &home, &sid)? else { return Ok(None) };
            let Some(owner) = owners.get_item(key)? else { return Ok(None) };
            if owner.eq(&session_key)? {
                owned.append(row)?;
            }
        }
        result.append(owned)?;
    }
    Ok(Some(result))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(row_signatures, m)?)?;
    m.add_function(wrap_pyfunction!(owned_histories, m)?)?;
    Ok(())
}
