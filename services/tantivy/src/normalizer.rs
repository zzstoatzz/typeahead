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
/// The index key every document with this handle carries, or `None` for a
/// handle that normalizes to nothing.
pub fn exact_handle_key(handle: &str) -> Result<Option<String>> {
    Ok(actor_keys(handle, "")?
        .into_iter()
        .find(|key| key.starts_with("e:")))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn results_remain_independently_owned_during_concurrent_allocation_and_free() {
        std::thread::scope(|scope| {
            for worker in 0..8 {
                scope.spawn(move || {
                    for iteration in 0..256 {
                        let request = json!({"op":"actor", "handle":format!("actor{worker}.example"),
                            "display":format!("名前日本太郎 {iteration}")});
                        let bytes = serde_json::to_vec(&request).unwrap();
                        let mut result = NativeResult {data:ptr::null_mut(),len:0,owner:ptr::null_mut()};
                        assert_eq!(unsafe {ta_plan(bytes.as_ptr(),bytes.len(),&mut result)},0);
                        let saved = unsafe {std::slice::from_raw_parts(result.data,result.len)}.to_vec();
                        assert_eq!(serde_json::from_slice::<Value>(&saved).unwrap(),plan(request).unwrap());
                        assert_eq!(unsafe {std::slice::from_raw_parts(result.data,result.len)},saved);
                        unsafe {ta_result_free(&mut result)};
                        assert!(result.data.is_null() && result.owner.is_null() && result.len==0);
                    }
                });
            }
        });
    }
}
