use crate::{Error, Result};

#[cfg(target_os = "linux")]
pub(super) fn renice(nice: i32) -> Result<usize> {
    use std::{fs, thread, time::Duration};
    // Tantivy starts its merge pool with the writer, and a thread names
    // itself as it starts: wait for the names rather than miss a thread.
    for _ in 0..200 {
        let mut merge = Vec::new();
        for task in fs::read_dir("/proc/self/task")? {
            let task = task?;
            let Ok(name) = fs::read_to_string(task.path().join("comm")) else {
                continue;
            };
            if name.starts_with("merge_thread_") {
                merge.push(
                    task.file_name()
                        .to_string_lossy()
                        .parse::<libc::id_t>()
                        .map_err(|_| Error::Storage("unreadable thread id".into()))?,
                );
            }
        }
        if merge.is_empty() {
            thread::sleep(Duration::from_millis(5));
            continue;
        }
        for thread in &merge {
            // SAFETY: setpriority takes plain integers and touches no memory.
            if unsafe { libc::setpriority(libc::PRIO_PROCESS, *thread, nice) } != 0 {
                return Err(Error::Storage(format!(
                    "setting merge thread niceness failed: {}",
                    std::io::Error::last_os_error()
                )));
            }
        }
        return Ok(merge.len());
    }
    Err(Error::Storage("no merge threads found".into()))
}

#[cfg(not(target_os = "linux"))]
pub(super) fn renice(_nice: i32) -> Result<usize> {
    Err(Error::Invalid(
        "merge niceness is per-thread and needs Linux".into(),
    ))
}
