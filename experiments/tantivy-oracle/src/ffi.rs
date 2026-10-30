use serde_json::{Value, json};
use std::{
    ffi::{CStr, c_char, c_void},
    ptr,
};

#[repr(C)]
struct NativeResult {
    data: *const u8,
    len: usize,
    owner: *mut c_void,
}
#[repr(C)]
pub struct Projection {
    handle: [u8; 256],
    display: [u8; 256],
    host: [u8; 253],
    handle_len: usize,
    display_len: usize,
    host_len: usize,
}
unsafe extern "C" {
    fn oracle_project(
        h: *const u8,
        hl: usize,
        d: *const u8,
        dl: usize,
        p: *const u8,
        pl: usize,
        out: *mut Projection,
    ) -> i32;
    fn ta_plan(input: *const u8, len: usize, result: *mut NativeResult) -> i32;
    fn ta_result_free(result: *mut NativeResult);
    fn ta_normalizer_version() -> *const c_char;
    fn ta_plan_version() -> *const c_char;
}
impl Projection {
    pub fn new(handle: &str, display: &str, pds: &str) -> Result<Self, String> {
        let mut out = Self {
            handle: [0; 256],
            display: [0; 256],
            host: [0; 253],
            handle_len: 0,
            display_len: 0,
            host_len: 0,
        };
        let status = unsafe {
            oracle_project(
                handle.as_ptr(),
                handle.len(),
                display.as_ptr(),
                display.len(),
                pds.as_ptr(),
                pds.len(),
                &mut out,
            )
        };
        if status != 0 {
            return Err(format!("raw actor projection status={status}"));
        }
        Ok(out)
    }
    pub fn handle(&self) -> &str {
        std::str::from_utf8(&self.handle[..self.handle_len]).unwrap()
    }
    pub fn display(&self) -> &str {
        std::str::from_utf8(&self.display[..self.display_len]).unwrap()
    }
    pub fn host(&self) -> &str {
        std::str::from_utf8(&self.host[..self.host_len]).unwrap()
    }
}
pub fn plan(input: Value) -> Result<Value, String> {
    let raw = serde_json::to_vec(&input).map_err(|e| e.to_string())?;
    let mut result = NativeResult {
        data: ptr::null(),
        len: 0,
        owner: ptr::null_mut(),
    };
    let code = unsafe { ta_plan(raw.as_ptr(), raw.len(), &mut result) };
    if code != 0 {
        unsafe { ta_result_free(&mut result) };
        return Err(format!("canonical ABI status={code}"));
    }
    let parsed = if result.data.is_null() || result.len > 4 * 1024 * 1024 {
        Err("invalid canonical reply".into())
    } else {
        serde_json::from_slice(unsafe { std::slice::from_raw_parts(result.data, result.len) })
            .map_err(|e| e.to_string())
    };
    unsafe { ta_result_free(&mut result) };
    parsed
}
pub fn host(value: &str) -> Result<String, String> {
    let out = plan(json!({"op":"pds","pds":value}))?;
    if out["status"] != "ok" {
        return Err("invalid PDS scope".into());
    }
    out["pds"]
        .as_str()
        .map(str::to_owned)
        .ok_or("missing PDS".into())
}
pub fn versions() -> (String, String) {
    unsafe {
        (
            CStr::from_ptr(ta_normalizer_version())
                .to_string_lossy()
                .into_owned(),
            CStr::from_ptr(ta_plan_version())
                .to_string_lossy()
                .into_owned(),
        )
    }
}
