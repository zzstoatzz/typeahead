use super::*;
use std::{io::Cursor, thread};

fn row(n: usize) -> Actor {
    let local = n % 3 == 0;
    serde_json::from_value(json!({
        "did":format!("did:fixture:{n:06}"),
        "handle":if n % 64 < 2 { "needle.fixture.test".to_owned() } else if n%64==2 { "needle.fixture.test.long".to_owned() } else { format!("needle{n}.fixture.test") },
        "display_name":if n<70 {"日本 東京 本太"} else if n==70 {"名前日本太郎"} else {"Rank Needle"},
        "quality_score":if n<70 {10000.0+n as f32} else if n==70 {-100.0} else if n%64==5 || n%64==6 {16777216.0} else {((n*37)%81) as f32-40.0},
        "pds":if local {"https://local.test"} else {"https://remote.test"},
        "hidden":n%11==0,"version":100,
        "labels":if n>=70 && n%13==0 {json!([{"src":"did:plc:ar7c4by46qjdydhdevvrndac","val":"!hide"}])} else {json!([])},
    })).unwrap()
}
fn apply(engine: &Engine, rows: Vec<Actor>, policy: Option<Value>) {
    let dids = rows.iter().map(|a| a.did.clone()).collect::<Vec<_>>();
    let state = engine.source_state(dids).unwrap();
    let revision = state["source_observation_revision"].as_u64().unwrap();
    let request = SourceReconcile {
        actors: rows,
        expected_documents: serde_json::from_value(state["actors"].clone()).unwrap(),
        expected_source_cursor: state["source_cursor"].clone(),
        source_cursor: json!({"round":revision+1}),
        expected_observation_revision: revision,
        batch_id: format!("{revision:064x}"),
        watermark: None,
        policy_snapshot: policy,
    };
    engine.reconcile_source(request).unwrap();
}
fn fixture(segments: usize) -> (tempfile::TempDir, Engine, Vec<Actor>) {
    let directory = tempfile::tempdir().unwrap();
    let empty_hash = format!("{:x}", Sha256::digest(b""));
    let input = format!(
        "{}\n{}\n",
        json!({"type":"manifest","build_id":"fixture-rank-merge","source_watermark":100,"actor_count":0,"source_actor_count":0,"partial":true}),
        json!({"type":"end","build_id":"fixture-rank-merge","actor_count":0,"actors_sha256":empty_hash})
    );
    import(
        directory.path(),
        Cursor::new(input),
        None,
        32 * 1024 * 1024,
        64,
        true,
    )
    .unwrap();
    let mut engine = Engine::open(directory.path(), 32 * 1024 * 1024).unwrap();
    engine.allow_partial = true;
    engine.allow_stale = true;
    engine
        .writer
        .lock()
        .unwrap()
        .writer
        .set_merge_policy(Box::new(NoMergePolicy));
    let rows = (0..segments * 64).map(row).collect::<Vec<_>>();
    let policy = json!({"schema_version":1,"source_observed_at":epoch(),"complete":true,"blocked_domains":[],"overrides":rows.iter().filter(|a|a.did[12..].parse::<usize>().unwrap()%19==0).map(|a|json!({"did":a.did,"action":"show"})).collect::<Vec<_>>()});
    for batch in rows.chunks(64) {
        apply(&engine, batch.to_vec(), Some(policy.clone()));
    }
    assert_eq!(engine.snapshot().searcher.segment_readers().len(), segments);
    (directory, engine, rows)
}
fn expected(
    rows: &[Actor],
    q: &str,
    limit: usize,
    pds: Option<&str>,
    preference: Option<&str>,
    first: bool,
    bonus: f32,
) -> Vec<String> {
    let tokens = q
        .to_ascii_lowercase()
        .split_whitespace()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let mut candidates = rows
        .iter()
        .filter(|a| {
            let n = a.did[12..].parse::<usize>().unwrap();
            let hidden = if n % 19 == 0 {
                false
            } else {
                n >= 70 && n % 13 == 0
            };
            if hidden || a.deleted || pds.is_some_and(|host| a.pds != format!("https://{host}")) {
                return false;
            }
            if q == "日本太" {
                return a.display_name.contains(q);
            }
            let display = a.display_name.to_ascii_lowercase();
            let display_match = tokens
                .iter()
                .all(|t| display.split_whitespace().any(|w| w.starts_with(t)));
            display_match || (tokens.len() == 1 && a.handle.starts_with(q))
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|a, b| {
        let local = |a: &Actor| preference.is_some_and(|h| a.pds == format!("https://{h}"));
        let score =
            |a: &Actor| f64::from(a.quality_score) + if local(a) { f64::from(bonus) } else { 0.0 };
        (a.handle != q, !(first && local(a)))
            .cmp(&(b.handle != q, !(first && local(b))))
            .then_with(|| score(b).total_cmp(&score(a)))
            .then_with(|| a.did.cmp(&b.did))
    });
    candidates
        .into_iter()
        .take(limit)
        .map(|a| a.did.clone())
        .collect()
}

#[test]
fn independent_segment_oracle_and_ranked_hydration_work() {
    let mut evidence = Vec::new();
    for segment_count in [8, 32] {
        let (_directory, mut engine, mut rows) = fixture(segment_count);
        let mut comparisons = 0;
        for q in [
            "needle",
            "Rank Need",
            "needle.fixture.test",
            "日本太",
            "absent",
        ] {
            for limit in [1, 10, 50, 100] {
                for (scope, preferred, first, bonus) in [
                    (None, None, false, 0.0),
                    (Some("local.test"), None, false, 0.0),
                    (None, Some("local.test"), true, 0.0),
                    (None, Some("local.test"), false, 8.0),
                    (None, Some("remote.test"), false, 1000.0),
                ] {
                    let oracle = expected(&rows, q, limit, scope, preferred, first, bonus);
                    let mut previous = None;
                    for strategy in [
                        RetrievalStrategy::PerSegment,
                        RetrievalStrategy::GlobalMerge,
                    ] {
                        engine.retrieval = strategy;
                        let result = engine
                            .search(q, limit, scope, preferred, first, Some(bonus))
                            .unwrap();
                        let ids = result
                            .actors
                            .iter()
                            .map(|a| a.did.clone())
                            .collect::<Vec<_>>();
                        assert_eq!(
                            ids,
                            oracle,
                            "segments={segment_count},q={q},limit={limit},strategy={}",
                            strategy.name()
                        );
                        if let Some(old) = &previous {
                            assert_eq!(&ids, old);
                        }
                        previous = Some(ids);
                        comparisons += 1;
                    }
                }
            }
        }
        engine.retrieval = RetrievalStrategy::PerSegment;
        let baseline = engine
            .search("needle", 10, None, None, false, None)
            .unwrap();
        engine.retrieval = RetrievalStrategy::GlobalMerge;
        let merged = engine
            .search("needle", 10, None, None, false, None)
            .unwrap();
        assert!(merged.hydrated_candidates < baseline.hydrated_candidates);
        evidence.push(json!({"segments":segment_count,"actors":rows.len(),"independent_oracle_comparisons":comparisons,
            "baseline":{"visited":baseline.visited,"hydrated":baseline.hydrated_candidates,"rank_head_reads":baseline.rank_head_reads,"scorers_opened":baseline.scorers_opened},
            "global_merge":{"visited":merged.visited,"hydrated":merged.hydrated_candidates,"rank_head_reads":merged.rank_head_reads,"scorers_opened":merged.scorers_opened,"segments_visited":merged.segments_visited,"segments_hydrated":merged.segments_hydrated}}));
        // A newer document removes the matching old segment row; deleted scorers never count as accepted.
        rows[100].handle = "renamed.fixture.test".into();
        rows[100].display_name = "Renamed".into();
        rows[101].deleted = true;
        rows[101].handle = String::new();
        apply(&engine, vec![rows[100].clone(), rows[101].clone()], None);
        for strategy in [
            RetrievalStrategy::PerSegment,
            RetrievalStrategy::GlobalMerge,
        ] {
            engine.retrieval = strategy;
            assert_eq!(
                engine
                    .search("needle", 100, None, None, false, None)
                    .unwrap()
                    .actors
                    .iter()
                    .map(|a| a.did.clone())
                    .collect::<Vec<_>>(),
                expected(&rows, "needle", 100, None, None, false, 0.0)
            );
            engine.max_candidates = 5;
            assert!(matches!(
                engine.search("日本太", 10, None, None, false, None),
                Err(Error::Budget)
            ));
            engine.max_candidates = 10000;
        }
        let mut bad = row(segment_count * 64 + 1);
        bad.handle = "unsupported.fixture.test".into();
        bad.labels =
            json!([{"src":"did:plc:ar7c4by46qjdydhdevvrndac","val":"!hide","exp":"not-a-date"}]);
        apply(&engine, vec![bad], None);
        for strategy in [
            RetrievalStrategy::PerSegment,
            RetrievalStrategy::GlobalMerge,
        ] {
            engine.retrieval = strategy;
            assert!(matches!(
                engine.search("unsupported", 10, None, None, false, None),
                Err(Error::NotReady)
            ));
        }
    }
    println!(
        "RANK_MERGE_WORK={}",
        json!({"synthetic":true,"timing_claim":false,"fixtures":evidence})
    );
    if let Ok(path) = std::env::var("TA_RANK_MERGE_RECEIPT") {
        fs::write(
            path,
            serde_json::to_vec_pretty(
                &json!({"passed":true,"synthetic":true,"timing_claim":false,"fixtures":evidence}),
            )
            .unwrap(),
        )
        .unwrap();
    }
}

#[test]
fn global_merge_captured_generation_survives_concurrent_commits() {
    let (_directory, mut engine, _rows) = fixture(8);
    engine.retrieval = RetrievalStrategy::GlobalMerge;
    let mut atomic = row(10000);
    atomic.handle = "atomic.fixture.test".into();
    atomic.display_name = "Atomic A".into();
    apply(&engine, vec![atomic.clone()], None);
    let engine = Arc::new(engine);
    let reader = engine.clone();
    let task = thread::spawn(move || {
        for _ in 0..100 {
            let result = reader
                .search("atomic.fixture.test", 1, None, None, false, None)
                .unwrap();
            assert_eq!(result.actors.len(), 1);
            assert!(result.actors[0].display_name.starts_with("Atomic "));
        }
    });
    for n in 0..12 {
        atomic.display_name = format!("Atomic {n}");
        apply(&engine, vec![atomic.clone()], None);
    }
    task.join().unwrap();
}
