use pyo3::prelude::*;

mod metadata;
mod trends;
mod usage;
mod quota;
mod quota_series;
mod index_scan;
mod analysis;

#[pymodule]
fn _cpu_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("ABI_VERSION", 1)?;
    metadata::register(module)?;
    trends::register(module)?;
    usage::register(module)?;
    quota::register(module)?;
    quota_series::register(module)?;
    index_scan::register(module)?;
    analysis::register(module)?;
    Ok(())
}
