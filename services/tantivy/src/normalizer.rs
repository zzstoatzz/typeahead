use crate::{Error, Result};
use serde_json::{Value, json};
use std::{
    ffi::{CStr, c_char, c_void},
    ptr,
};
#[repr(C)]
struct NativeResult {
    data: *mut u8,
    len: usize,
    owner: *mut c_void,
}
unsafe extern "C" {
    fn ta_plan(input: *const u8, len: usize, out: *mut NativeResult) -> i32;
    fn ta_result_free(result: *mut NativeResult);
    fn ta_normalizer_version() -> *const c_char;
    fn ta_plan_version() -> *const c_char;
}
pub fn version() -> String {
    unsafe {
        CStr::from_ptr(ta_normalizer_version())
            .to_string_lossy()
            .into_owned()
    }
}
pub fn plan_version() -> String {
    unsafe {
        CStr::from_ptr(ta_plan_version())
            .to_string_lossy()
            .into_owned()
    }
}
pub fn plan(input: Value) -> Result<Value> {
    let bytes = serde_json::to_vec(&input)?;
    let mut output = NativeResult {
        data: ptr::null_mut(),
        len: 0,
        owner: ptr::null_mut(),
    };
    let status = unsafe { ta_plan(bytes.as_ptr(), bytes.len(), &mut output) };
    if status != 0 {
        unsafe { ta_result_free(&mut output) };
        return Err(Error::Invalid("normalizer rejected input".into()));
    }
    let result = if output.data.is_null() || output.len > 4 * 1024 * 1024 {
        Err(Error::Invalid("invalid normalizer result".into()))
    } else {
        serde_json::from_slice(unsafe { std::slice::from_raw_parts(output.data, output.len) })
            .map_err(Error::from)
    };
    unsafe { ta_result_free(&mut output) };
    result
}
pub fn host(value: &str) -> Result<String> {
    let output = plan(json!({"op":"pds","pds":value}))?;
    if output["status"] != "ok" {
        return Err(Error::Invalid("invalid pds hostname".into()));
    }
    output["pds"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| Error::Invalid("normalizer omitted pds".into()))
}
pub fn source_host(value: &str) -> Result<String> {
    if value.is_empty() || value.len() > 263 {
        return Ok(String::new());
    }
    let output = plan(json!({"op":"pds","pds":value}))?;
    if output["status"] == "invalid_input" && output["reason"] == "InvalidPds" {
        return Ok(String::new());
    }
    if output["status"] != "ok" {
        return Err(Error::Storage("source PDS normalizer failed".into()));
    }
    output["pds"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| Error::Storage("source PDS normalizer omitted host".into()))
}
pub fn actor_keys(handle: &str, display: &str) -> Result<Vec<String>> {
    let output = plan(json!({"op":"actor","handle":handle,"display":display}))?;
    if output["status"] != "ok" {
        return Err(Error::Invalid(format!(
            "actor_normalization_{}",
            output["status"].as_str().unwrap_or("invalid_response")
        )));
    }
    serde_json::from_value(output["keys"].clone()).map_err(Error::from)
}
pub fn query(q: &str) -> Result<Value> {
    let output = plan(json!({"op":"query","q":q}))?;
    match output["status"].as_str() {
        Some("ok" | "empty") => Ok(output),
        Some("budget_exceeded") => Err(Error::Budget),
        _ => Err(Error::Invalid("unsupported query".into())),
    }
}
pub fn matches(handle: &str, display: &str, q: &str) -> Result<bool> {
    let output = plan(json!({"op":"matches","handle":handle,"display":display,"q":q}))?;
    if output["status"] != "ok" {
        return Err(Error::Invalid("query verification failed".into()));
    }
    output["matches"]
        .as_bool()
        .ok_or_else(|| Error::Invalid("normalizer omitted match predicate".into()))
}
