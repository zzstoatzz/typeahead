use std::{collections::HashMap, env, fs::File, io::{self, Read}, path::Path, sync::Arc};
use sha2::{Digest, Sha256};
use typeahead_tantivy::{
    Error, Result,
    engine::{Engine, import, resume_import},
    server,
};
fn arguments() -> Result<(String, HashMap<String, String>)> {
    let mut args = env::args().skip(1);
    let command = args
        .next()
        .ok_or_else(|| Error::Invalid("use import,serve,query,status".into()))?;
    let mut map = HashMap::new();
    while let Some(key) = args.next() {
        if !key.starts_with("--") {
            return Err(Error::Invalid("expected --option value".into()));
        }
        let value = args
            .next()
            .ok_or_else(|| Error::Invalid(format!("missing {key} value")))?;
        map.insert(key[2..].into(), value);
    }
    Ok((command, map))
}
fn required<'a>(args: &'a HashMap<String, String>, name: &str) -> Result<&'a str> {
    args.get(name)
        .map(String::as_str)
        .ok_or_else(|| Error::Invalid(format!("missing --{name}")))
}
#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("{}", serde_json::json!({"error":error.to_string()}));
        std::process::exit(1);
    }
}
async fn run() -> Result<()> {
    let (command, args) = arguments()?;
    let data = Path::new(required(&args, "data")?);
    let memory = args
        .get("writer-memory-mb")
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(128)
        .clamp(32, 1024)
        * 1024
        * 1024;
    match command.as_str() {
        "resume-import" => {
            let mut bytes = Vec::new();
            File::open(required(&args, "receipt")?)?.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
            if bytes.len() > 1024 * 1024
                || format!("{:x}", Sha256::digest(&bytes)) != required(&args, "receipt-sha256")?
            {
                return Err(Error::Invalid("stream receipt size or checksum differs".into()));
            }
            let receipt = serde_json::from_slice(&bytes)?;
            let metadata = resume_import(data, &receipt, memory)?;
            println!("{}", serde_json::to_string(&metadata)?);
        }
        "import" => {
            let input = required(&args, "input")?;
            let batch = args
                .get("batch-size")
                .and_then(|s| s.parse().ok())
                .unwrap_or(50000);
            let build = args.get("build-id").map(String::as_str);
            let metadata = if input == "-" {
                import(
                    data,
                    io::stdin().lock(),
                    build,
                    memory,
                    batch,
                    args.get("allow-partial").is_some_and(|s| s == "true"),
                )?
            } else {
                import(
                    data,
                    File::open(input)?,
                    build,
                    memory,
                    batch,
                    args.get("allow-partial").is_some_and(|s| s == "true"),
                )?
            };
            println!("{}", serde_json::to_string(&metadata)?);
        }
        "serve" => {
            let mut engine = Engine::open(data, memory)?;
            engine.allow_partial = args.get("allow-partial").is_some_and(|s| s == "true");
            engine.allow_stale = args.get("allow-stale").is_some_and(|s| s == "true");
            engine.retrieval = match args
                .get("retrieval")
                .map(String::as_str)
                .unwrap_or("per-segment")
            {
                "per-segment" => typeahead_tantivy::engine::RetrievalStrategy::PerSegment,
                "global-merge" => typeahead_tantivy::engine::RetrievalStrategy::GlobalMerge,
                _ => {
                    return Err(Error::Invalid(
                        "retrieval must be per-segment or global-merge".into(),
                    ));
                }
            };
            engine.max_sync_lag_s = args
                .get("max-sync-lag-s")
                .and_then(|s| s.parse().ok())
                .unwrap_or(1800)
                .clamp(1, 86400);
            engine.max_candidates = args
                .get("max-candidates")
                .and_then(|s| s.parse().ok())
                .unwrap_or(10000)
                .clamp(100, 100000);
            let listen = args
                .get("listen")
                .map(String::as_str)
                .unwrap_or("0.0.0.0:8080")
                .parse()
                .map_err(|_| Error::Invalid("invalid listen address".into()))?;
            let admin = Some(
                args.get("admin-listen")
                    .map(String::as_str)
                    .unwrap_or("127.0.0.1:8081")
                    .parse()
                    .map_err(|_| Error::Invalid("invalid admin address".into()))?,
            );
            let concurrency = args
                .get("concurrency")
                .and_then(|s| s.parse().ok())
                .unwrap_or(4);
            server::serve(Arc::new(engine), listen, admin, concurrency).await?;
        }
        "query" => {
            let engine = Engine::open(data, memory)?;
            let limit = args.get("limit").and_then(|s| s.parse().ok()).unwrap_or(10);
            let result = engine.search(
                required(&args, "q")?,
                limit,
                args.get("pds").map(String::as_str),
                args.get("prefer-pds").map(String::as_str),
                args.get("preference").is_some_and(|s| s == "pds_first"),
                args.get("boost").and_then(|s| s.parse().ok()),
            )?;
            println!(
                "{}",
                serde_json::json!({"actors":result.actors.iter().map(|a|a.public()).collect::<Vec<_>>(),"visited":result.visited,"build_id":result.metadata.build_id})
            );
        }
        "status" => {
            let engine = Engine::open(data, memory)?;
            println!("{}", serde_json::to_string(&engine.snapshot().metadata)?);
        }
        _ => return Err(Error::Invalid("unknown command".into())),
    }
    Ok(())
}
