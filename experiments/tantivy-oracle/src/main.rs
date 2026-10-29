mod ffi;
mod matching;
mod queries;
mod wide;
use matching::{Case, Dispatch, Heap, Prepared, Row};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    fs::{self, File, OpenOptions},
    io::{self, BufRead, BufReader, BufWriter, Read, Write},
    path::{Path, PathBuf},
    time::Instant,
};

const SOURCE_KIND: &str = "frozen-materialized-hidden";
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
fn fail(message: &str) -> Box<dyn std::error::Error> {
    message.into()
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn file_digest(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hash = Sha256::new();
    let mut bytes = [0u8; 65536];
    loop {
        let n = file.read(&mut bytes)?;
        if n == 0 {
            break;
        }
        hash.update(&bytes[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn read_json<T: serde::de::DeserializeOwned>(path: &Path, limit: u64) -> Result<(T, String)> {
    if path.metadata()?.len() > limit {
        return Err(fail("input metadata exceeds byte budget"));
    }
    let bytes = fs::read(path)?;
    Ok((serde_json::from_slice(&bytes)?, digest(&bytes)))
}
fn line(reader: &mut impl BufRead) -> Result<Option<Vec<u8>>> {
    let mut out = Vec::new();
    loop {
        let part = reader.fill_buf()?;
        if part.is_empty() {
            return Ok(if out.is_empty() { None } else { Some(out) });
        }
        let take = part
            .iter()
            .position(|b| *b == b'\n')
            .map_or(part.len(), |n| n + 1);
        if out.len() + take > 4 * 1024 * 1024 {
            return Err(fail("raw JSONL record exceeds4MiB"));
        }
        out.extend_from_slice(&part[..take]);
        let done = out.last() == Some(&b'\n');
        reader.consume(take);
        if done {
            return Ok(Some(out));
        }
    }
}
fn reader(path: &str) -> Result<Box<dyn BufRead>> {
    Ok(if path == "-" {
        Box::new(BufReader::new(io::stdin()))
    } else {
        Box::new(BufReader::new(File::open(path)?))
    })
}
fn rss_mb() -> u64 {
    unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) != 0 {
            return u64::MAX;
        }
        let bytes = if cfg!(target_os = "macos") {
            usage.ru_maxrss as u64
        } else {
            usage.ru_maxrss as u64 * 1024
        };
        bytes.div_ceil(1024 * 1024)
    }
}
fn check(start: Instant, seconds: u64, memory: u64) -> Result<()> {
    if start.elapsed().as_secs() > seconds {
        return Err(fail("oracle wall-time budget exceeded"));
    }
    if rss_mb() > memory {
        return Err(fail("oracle resident-memory budget exceeded"));
    }
    Ok(())
}
#[derive(Deserialize, Serialize)]
struct QuerySet {
    schema_version: u32,
    source_kind: String,
    cases: Vec<Case>,
    #[serde(default)]
    sample: Value,
}
#[derive(Deserialize)]
struct Batch {
    batch_id: String,
    cursor: u64,
    expected_cursor: u64,
    actors: Vec<Row>,
}
#[derive(Deserialize)]
struct Feed {
    schema_version: u32,
    source_kind: String,
    build_id: String,
    source_watermark: u64,
    baseline_actors: Vec<Row>,
    batches: Vec<Batch>,
}
fn relevant(row: &Row) -> Value {
    json!([
        row.did,
        row.handle,
        row.display_name,
        row.quality_score.to_bits(),
        row.pds,
        row.hidden,
        row.deleted,
        row.version
    ])
}
fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(value)?;
    if bytes.len() > 16 * 1024 * 1024 {
        return Err(fail("output metadata exceeds16MiB"));
    }
    let mut out = OpenOptions::new().write(true).create_new(true).open(path)?;
    out.write_all(&bytes)?;
    out.write_all(b"\n")?;
    out.sync_all()?;
    Ok(())
}
fn options() -> Result<(String, HashMap<String, String>)> {
    let mut args = std::env::args().skip(1);
    let mode = args.next().ok_or("use freeze,run,project,score")?;
    let mut values = HashMap::new();
    while let Some(key) = args.next() {
        if !key.starts_with("--") {
            return Err(fail("expected --option value"));
        }
        let value = args.next().ok_or("missing option value")?;
        if values.insert(key[2..].into(), value).is_some() {
            return Err(fail("duplicate option"));
        }
    }
    Ok((mode, values))
}
fn required<'a>(args: &'a HashMap<String, String>, key: &str) -> Result<&'a str> {
    args.get(key)
        .map(String::as_str)
        .ok_or_else(|| fail(&format!("missing --{key}")))
}
fn number(args: &HashMap<String, String>, key: &str, default: u64) -> Result<u64> {
    Ok(args
        .get(key)
        .map(|v| v.parse())
        .transpose()?
        .unwrap_or(default))
}

fn run(args: &HashMap<String, String>) -> Result<Value> {
    let started = Instant::now();
    let seconds = number(args, "max-seconds", 1800)?;
    let memory = number(args, "max-rss-mb", 256)?;
    let byte_budget = number(args, "max-source-bytes", 64 * 1024 * 1024 * 1024)?;
    if seconds == 0 || seconds > 3600 || !(32..=768).contains(&memory) {
        return Err(fail("unsupported oracle resource budgets"));
    }
    if required(args, "source-kind")? != SOURCE_KIND {
        return Err(fail(
            "only explicit frozen materialized-hidden source supported",
        ));
    }
    let input = required(args, "input")?;
    let output = PathBuf::from(required(args, "output")?);
    if output.exists() {
        return Err(fail("new oracle output path required"));
    }
    let (query_set, query_hash): (QuerySet, _) =
        read_json(Path::new(required(args, "queries")?), 4 * 1024 * 1024)?;
    if query_set.schema_version != 1
        || query_set.source_kind != SOURCE_KIND
        || query_set.cases.is_empty()
        || query_set.cases.len() > 512
    {
        return Err(fail("invalid frozen query set"));
    }
    if query_set
        .cases
        .iter()
        .map(|q| &q.id)
        .collect::<HashSet<_>>()
        .len()
        != query_set.cases.len()
    {
        return Err(fail("query IDs must be unique"));
    }
    let queries = query_set
        .cases
        .into_iter()
        .map(Prepared::new)
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let dispatch = Dispatch::new(&queries);
    let mut heaps = vec![Heap::new(); queries.len()];
    let (quarantine, quarantine_hash): (Vec<String>, _) =
        read_json(Path::new(required(args, "identity-policy")?), 65536)?;
    let feed_with_hash = if let Some(path) = args.get("updates") {
        Some(read_json::<Feed>(Path::new(path), 16 * 1024 * 1024)?)
    } else {
        None
    };
    if !query_set.sample["updates_sha256"].is_null()
        && query_set.sample["updates_sha256"].as_str()
            != feed_with_hash.as_ref().map(|(_, hash)| hash.as_str())
    {
        return Err(fail("frozen case feed hash differs from checkpoint feed"));
    }
    let mut affected = HashSet::new();
    let mut expected_baseline = HashMap::new();
    if let Some((feed, _)) = &feed_with_hash {
        if feed.schema_version != 1
            || feed.source_kind != SOURCE_KIND
            || feed.batches.len() > 64
            || feed.baseline_actors.len() > 256
        {
            return Err(fail("bounded update feed contract violated"));
        }
        for row in &feed.baseline_actors {
            if expected_baseline
                .insert(row.did.clone(), relevant(row))
                .is_some()
            {
                return Err(fail("duplicate baseline actor"));
            }
            affected.insert(row.did.clone());
        }
        let mut cursor = 0;
        for batch in &feed.batches {
            if batch.actors.len() > 500
                || batch.expected_cursor != cursor
                || batch.cursor != cursor + 1
                || batch.batch_id.is_empty()
            {
                return Err(fail("update feed cursor/size contract violated"));
            }
            cursor = batch.cursor;
            let mut ids = HashSet::new();
            for row in &batch.actors {
                if !ids.insert(&row.did) {
                    return Err(fail("duplicate actor in update batch"));
                }
                affected.insert(row.did.clone());
            }
        }
        if affected.len() > 256 {
            return Err(fail("too many affected identities"));
        }
    }
    let mut source = reader(input)?;
    let header_raw = line(&mut source)?.ok_or("raw source manifest missing")?;
    let header: Value = serde_json::from_slice(&header_raw)?;
    let expected_count = header["actor_count"]
        .as_u64()
        .ok_or("raw actor_count missing")?;
    let floor = header["source_watermark"]
        .as_u64()
        .ok_or("raw source watermark missing")?;
    let build = header["build_id"].as_str().ok_or("raw build_id missing")?;
    if header["type"] != "manifest"
        || header["partial"] != false
        || header["source_actor_count"].as_u64() != Some(expected_count)
        || header["normalize_version"].as_str() != Some(&ffi::versions().0)
    {
        return Err(fail("full frozen source manifest/normalization mismatch"));
    }
    let sampled_count = if query_set.sample.is_null() {
        None
    } else {
        let n = query_set.sample["raw_actors_read"]
            .as_u64()
            .ok_or("sample count missing")?;
        if !(1..=50000).contains(&n)
            || n > expected_count
            || query_set.sample["source_build_id"] != build
            || query_set.sample["source_watermark"].as_u64() != Some(floor)
            || query_set.sample["source_declared_actors"].as_u64() != Some(expected_count)
        {
            return Err(fail("query sample belongs to another source"));
        }
        Some(n)
    };
    if let Some((feed, _)) = &feed_with_hash {
        if feed.build_id != build || feed.source_watermark != floor {
            return Err(fail("update feed belongs to another frozen source"));
        }
    }
    let mut source_hash = Sha256::new();
    source_hash.update(&header_raw);
    let mut actor_hash = Sha256::new();
    let mut bytes = header_raw.len() as u64;
    let mut count = 0;
    let mut eligible = 0;
    let mut matches = 0;
    let mut missing_pds = 0;
    let mut invalid_pds = 0;
    let mut previous = String::new();
    let mut baseline = HashMap::new();
    let mut footer = false;
    while let Some(raw) = line(&mut source)? {
        bytes += raw.len() as u64;
        if bytes > byte_budget {
            return Err(fail("raw source byte budget exceeded"));
        }
        source_hash.update(&raw);
        let value: Value = serde_json::from_slice(&raw)?;
        if value["type"] == "end" {
            let actual = format!("{:x}", actor_hash.clone().finalize());
            if value["build_id"] != build
                || value["actor_count"].as_u64() != Some(count)
                || count != expected_count
                || value["actors_sha256"].as_str() != Some(&actual)
            {
                return Err(fail("raw footer/count/hash verification failed"));
            }
            footer = true;
            break;
        }
        actor_hash.update(&raw);
        let row: Row = serde_json::from_value(value)?;
        if row.did <= previous
            || !row.did.starts_with("did:")
            || row.did.len() > 2048
            || row.did.bytes().any(|b| b <= 32 || b == b'|')
            || row.version != floor
        {
            return Err(fail(
                "raw actor ordering/identity/version contract violated",
            ));
        }
        previous = row.did.clone();
        count += 1;
        if sampled_count == Some(count) {
            let hash = format!("{:x}", source_hash.clone().finalize());
            if query_set.sample["sample_prefix_sha256"].as_str() != Some(&hash)
                || query_set.sample["sample_bytes"].as_u64() != Some(bytes)
            {
                return Err(fail(
                    "query raw-sample prefix hash/bytes differ from source",
                ));
            }
        }
        let projection = ffi::Projection::new(&row.handle, &row.display_name, &row.pds)?;
        if row.pds.is_empty() {
            missing_pds += 1
        } else if projection.host().is_empty() {
            invalid_pds += 1
        }
        if !row.hidden && !row.deleted {
            eligible += 1
        }
        if affected.contains(&row.did) {
            if expected_baseline
                .get(&row.did)
                .is_none_or(|expected| *expected != relevant(&row))
            {
                return Err(fail("affected baseline does not match raw source"));
            }
            baseline.insert(row.did.clone(), row);
        } else {
            matches += matching::evaluate_projected(
                &row,
                &projection,
                &queries,
                &dispatch,
                &mut heaps,
                &quarantine,
            )?;
        }
        if count % 4096 == 0 {
            check(started, seconds, memory)?;
        }
    }
    if !footer || line(&mut source)?.is_some() {
        return Err(fail("raw stream is incomplete or has trailing data"));
    }
    if expected_baseline
        .keys()
        .any(|id| !baseline.contains_key(id))
    {
        return Err(fail("required baseline actor missing from raw source"));
    }
    check(started, seconds, memory)?;
    let source_hash = format!("{:x}", source_hash.finalize());
    let actor_hash = format!("{:x}", actor_hash.finalize());
    let receipt = json!({"schema_version":1,"oracle_binary_sha256":file_digest(&std::env::current_exe()?)?,"source_kind":SOURCE_KIND,"source_build_id":build,"source_watermark":floor,"source_actor_count":expected_count,"actors_checked":count,"raw_source_bytes":bytes,"raw_source_sha256":source_hash,"actor_stream_sha256":actor_hash,"queries_sha256":query_hash,"updates_sha256":feed_with_hash.as_ref().map(|(_,hash)|hash),"identity_policy_sha256":quarantine_hash,"normalize_version":ffi::versions().0,"plan_version":ffi::versions().1,"source_missing_pds":missing_pds,"source_unscopable_pds":invalid_pds,"materialized_visible_actors_before_identity_exclusions":eligible,"candidate_query_matches":matches,"affected_identities":affected.len(),"live_policy_or_freshness_qualification":false,"expected_ids_derived_from_engine_responses":false,"ranking":"exact signed320bit scaled2^-149 sums of inputf32values; exact priority, PDS policy, DID ties","retrieval":"raw normalized handle prefixes/domain suffixes + independent display run matching; query-term dispatch only"});
    let temporary = output.with_extension(format!("partial-{}", std::process::id()));
    let result = (|| -> Result<Value> {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        let mut out = BufWriter::new(BoundedOutput { file, bytes: 0 });
        out.write_all(b"{\"schema_version\":1,\"receipt\":")?;
        serde_json::to_writer(&mut out, &receipt)?;
        out.write_all(b",\"checkpoints\":[")?;
        let mut emit = |cursor: u64, rows: &HashMap<String, Row>, comma: bool| -> Result<()> {
            let mut phase = heaps.clone();
            for row in rows.values() {
                matching::evaluate(row, &queries, &dispatch, &mut phase, &quarantine)?;
            }
            if comma {
                out.write_all(b",")?
            }
            out.write_all(b"{\"cursor\":")?;
            serde_json::to_writer(&mut out, &cursor)?;
            out.write_all(b",\"queries\":")?;
            serde_json::to_writer(&mut out, &matching::answers(&queries, &phase))?;
            out.write_all(b"}")?;
            Ok(())
        };
        emit(0, &baseline, false)?;
        let mut checkpoints = 1;
        if let Some((feed, _)) = &feed_with_hash {
            for batch in &feed.batches {
                for row in &batch.actors {
                    if !row.quality_score.is_finite() {
                        return Err(fail("update score is not finitef32"));
                    }
                    if let Some(old) = baseline.get(&row.did) {
                        if row.version < old.version {
                            continue;
                        }
                        if row.version == old.version {
                            if relevant(row) != relevant(old) {
                                return Err(fail(
                                    "equal-version conflicting update in frozen feed",
                                ));
                            }
                            continue;
                        }
                    } else if row.version <= floor {
                        continue;
                    }
                    baseline.insert(row.did.clone(), row.clone());
                }
                emit(batch.cursor, &baseline, true)?;
                checkpoints += 1;
                check(started, seconds, memory)?;
            }
        }
        out.write_all(b"]}")?;
        out.flush()?;
        out.get_ref().file.sync_all()?;
        if temporary.metadata()?.len() > 64 * 1024 * 1024 {
            return Err(fail("oracle output exceeds64MiB"));
        }
        fs::hard_link(&temporary, &output)?;
        Ok(
            json!({"passed":true,"scope":"frozen-raw-profile-oracle","source_sha256":source_hash,"queries_sha256":query_hash,"checkpoints":checkpoints,"cases":queries.len(),"actors_checked":count,"wall_seconds":started.elapsed().as_secs_f64(),"peak_rss_mb":rss_mb(),"output_bytes":output.metadata()?.len(),"output_sha256":file_digest(&output)?,"full_live_policy_qualification":false}),
        )
    })();
    let _ = fs::remove_file(temporary);
    result
}

struct BoundedOutput {
    file: File,
    bytes: u64,
}
impl Write for BoundedOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.bytes + bytes.len() as u64 > 64 * 1024 * 1024 {
            return Err(io::Error::other("oracle output exceeds64MiB"));
        }
        let count = self.file.write(bytes)?;
        self.bytes += count as u64;
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}
fn main() {
    let result = (|| -> Result<Value> {
        let (mode, args) = options()?;
        match mode.as_str() {
            "freeze" => queries::freeze(&args),
            "run" => run(&args),
            "canonical" => {
                let mut source = BufReader::new(io::stdin());
                while let Some(raw) = line(&mut source)? {
                    let value: Value = serde_json::from_slice(&raw)?;
                    println!("{}", ffi::plan(value)?);
                }
                Ok(json!({"passed":true}))
            }
            "project" => {
                let mut source = BufReader::new(io::stdin());
                while let Some(raw) = line(&mut source)? {
                    let value: Value = serde_json::from_slice(&raw)?;
                    let h = value["handle"].as_str().unwrap_or("");
                    let d = value["display"].as_str().unwrap_or("");
                    let p = value["pds"].as_str().unwrap_or("");
                    let result = match ffi::Projection::new(h, d, p) {
                        Ok(out) => {
                            json!({"status":"ok","normalized_handle":out.handle(),"normalized_display":out.display(),"pds":out.host()})
                        }
                        Err(reason) => json!({"status":"error","reason":reason}),
                    };
                    println!("{}", result);
                }
                Ok(json!({"passed":true}))
            }
            "score" => {
                let a: u32 = required(&args, "a")?.parse()?;
                let b: u32 = required(&args, "b")?.parse()?;
                let c: u32 = required(&args, "c")?.parse()?;
                let d: u32 = required(&args, "d")?.parse()?;
                let values = [a, b, c, d].map(f32::from_bits);
                if values.iter().any(|v| !v.is_finite()) {
                    return Err(fail("score comparison requires finitef32"));
                }
                let ordering = wide::Exact::from_f32(values[0])
                    .add(wide::Exact::from_f32(values[1]))
                    .cmp(&wide::Exact::from_f32(values[2]).add(wide::Exact::from_f32(values[3])));
                Ok(json!({"cmp":if ordering.is_lt(){-1}else if ordering.is_gt(){1}else{0}}))
            }
            _ => Err(fail("unknown oracle operation")),
        }
    })();
    match result {
        Ok(value) => eprintln!("{}", value),
        Err(error) => {
            eprintln!("{}", json!({"passed":false,"error":error.to_string()}));
            std::process::exit(1)
        }
    }
}
