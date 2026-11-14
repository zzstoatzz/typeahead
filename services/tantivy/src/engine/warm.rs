//! Reads a new segment's files into the page cache so that the first
//! searches to reach them do not wait on the disk.
use std::{
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    thread,
};

/// Files smaller than this were written moments ago by a commit or a small
/// merge and are still in the page cache.
pub(super) const MIN_FILE_BYTES: u64 = 16 * 1024 * 1024;

/// The files of `segment` (its UUID without dashes) to read: those with one
/// of `extensions` that are at least `MIN_FILE_BYTES` long.
pub(super) fn files(index: &Path, segment: &str, extensions: &[String]) -> Vec<PathBuf> {
    extensions
        .iter()
        .map(|extension| index.join(format!("{segment}.{extension}")))
        .filter(|path| path.metadata().is_ok_and(|m| m.len() >= MIN_FILE_BYTES))
        .collect()
}

/// Read each file from start to end, twice: Linux keeps a page that has been
/// read twice ahead of pages read once, and other processes on the machine
/// read large files once (measured on typeahead-slot-a, 2026-11-11: after
/// one pass, 0.9 GB of 2.8 GB was still cached 90 s later, the store
/// process's start having read its own files meanwhile). A file a merge
/// has removed meanwhile is skipped. Returns the bytes read.
pub(super) fn read(files: &[PathBuf]) -> u64 {
    let mut buffer = vec![0u8; 1024 * 1024];
    let mut total = 0;
    for _ in 0..2 {
        for path in files {
            let Ok(mut file) = File::open(path) else {
                continue;
            };
            while let Ok(count) = file.read(&mut buffer) {
                if count == 0 {
                    break;
                }
                total += count as u64;
            }
        }
    }
    total
}

/// Read `files` on a thread of their own at the lowest scheduling priority.
pub(super) fn spawn(files: Vec<PathBuf>) {
    if files.is_empty() {
        return;
    }
    let started = thread::Builder::new()
        .name("page_warmer".into())
        .spawn(move || {
            // SAFETY: setpriority takes plain integers and touches no memory.
            // On Linux `who` 0 is the calling thread.
            #[cfg(target_os = "linux")]
            unsafe {
                libc::setpriority(libc::PRIO_PROCESS, 0, 19);
            }
            let begun = std::time::Instant::now();
            let bytes = read(&files);
            eprintln!(
                "{}",
                serde_json::json!({"event":"segment_files_warmed","files":files.len(),"bytes":bytes,
                    "elapsed_s":begun.elapsed().as_secs_f64()})
            );
        });
    if started.is_err() {
        eprintln!("{}", serde_json::json!({"event":"segment_warm_thread_failed"}));
    }
}
