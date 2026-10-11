//! Skip unobserved JSON in Rust; never materialize request/response content.
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString};
use serde::de::{self, DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};
use std::collections::BTreeMap;
use std::fmt;

enum Scalar { Text(String), Boolean(bool) }
type Fields = BTreeMap<String, Scalar>;

struct Selection<'a> { fields: &'a mut Fields, response: bool }
impl<'de> DeserializeSeed<'de> for Selection<'_> {
    type Value = ();
    fn deserialize<D: de::Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for Selection<'_> {
    type Value = ();
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result { f.write_str("JSON value") }
    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<(), M::Error> {
        while let Some(key) = map.next_key::<String>()? {
            if !self.response && key == "response" {
                map.next_value_seed(Selection { fields: self.fields, response: true })?;
                continue;
            }
            let name = if self.response { format!("response.{key}") } else { key };
            if matches!(name.as_str(), "model" | "stream_id" | "type" | "object" | "id" |
                "event_id" | "response_id" | "status" | "generate" | "stream" |
                "prompt_cache_retention" | "service_tier" | "response.id" |
                "response.model" | "response.status" | "response.service_tier") {
                map.next_value_seed(SelectedScalar { fields: self.fields, name })?;
            } else { map.next_value::<IgnoredAny>()?; }
        }
        Ok(())
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        while seq.next_element::<IgnoredAny>()?.is_some() {}
        Ok(())
    }
    fn visit_str<E: de::Error>(self, _: &str) -> Result<(), E> { Ok(()) }
    fn visit_bool<E: de::Error>(self, _: bool) -> Result<(), E> { Ok(()) }
    fn visit_i64<E: de::Error>(self, _: i64) -> Result<(), E> { Ok(()) }
    fn visit_u64<E: de::Error>(self, _: u64) -> Result<(), E> { Ok(()) }
    fn visit_f64<E: de::Error>(self, _: f64) -> Result<(), E> { Ok(()) }
    fn visit_unit<E: de::Error>(self) -> Result<(), E> { Ok(()) }
}

struct SelectedScalar<'a> { fields: &'a mut Fields, name: String }
impl<'de> DeserializeSeed<'de> for SelectedScalar<'_> {
    type Value = ();
    fn deserialize<D: de::Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for SelectedScalar<'_> {
    type Value = ();
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result { f.write_str("JSON scalar") }
    fn visit_str<E: de::Error>(self, value: &str) -> Result<(), E> {
        if value.chars().count() <= 256 {
            self.fields.insert(self.name, Scalar::Text(value.into()));
        }
        Ok(())
    }
    fn visit_bool<E: de::Error>(self, value: bool) -> Result<(), E> {
        self.fields.insert(self.name, Scalar::Boolean(value)); Ok(())
    }
    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<(), M::Error> {
        while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {} Ok(())
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        while seq.next_element::<IgnoredAny>()?.is_some() {} Ok(())
    }
    fn visit_i64<E: de::Error>(self, _: i64) -> Result<(), E> { Ok(()) }
    fn visit_u64<E: de::Error>(self, _: u64) -> Result<(), E> { Ok(()) }
    fn visit_f64<E: de::Error>(self, _: f64) -> Result<(), E> { Ok(()) }
    fn visit_unit<E: de::Error>(self) -> Result<(), E> { Ok(()) }
}

fn select(data: &[u8]) -> Option<Fields> {
    // IgnoredAny skips string allocation, and therefore does not validate UTF-8.
    // Validate all bytes so ignored private content cannot hide malformed JSON.
    std::str::from_utf8(data).ok()?;
    let mut fields = Fields::new();
    let mut decoder = serde_json::Deserializer::from_slice(data);
    Selection { fields: &mut fields, response: false }.deserialize(&mut decoder).ok()?;
    decoder.end().ok()?;
    Some(fields)
}

#[pyfunction]
fn message_metadata<'py>(py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let bytes = if let Ok(value) = data.cast::<PyBytes>() { value.as_bytes() }
        else { data.cast::<PyString>()?.to_str()?.as_bytes() };
    let output = PyDict::new(py);
    if bytes.len() > 128 * 1024 * 1024 { return Ok(output); }
    let fields = py.detach(|| select(bytes));
    // Returning None lets the mature streaming validator handle unusual JSON
    // (for example numeric representations outside serde_json's range).
    let Some(fields) = fields else { return Err(pyo3::exceptions::PyValueError::new_err("Use streaming JSON validator")); };
    for (key, value) in fields {
        let (target, name) = if let Some(name) = key.strip_prefix("response.") {
            let nested = match output.get_item("response")? {
                Some(value) => value.cast_into::<PyDict>()?,
                None => { let value = PyDict::new(py); output.set_item("response", &value)?; value }
            };
            (nested, name)
        } else { (output.clone(), key.as_str()) };
        match value {
            Scalar::Text(value) => target.set_item(name, value)?,
            Scalar::Boolean(value) => target.set_item(name, value)?,
        }
    }
    Ok(output)
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(message_metadata, module)?)
}
