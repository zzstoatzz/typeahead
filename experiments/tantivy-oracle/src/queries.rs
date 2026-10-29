use super::*;
pub fn freeze(args: &HashMap<String, String>) -> Result<Value> {
    let maximum = number(args, "sample-actors", 10000)?;
    let target = number(args, "cases", 512)? as usize;
    let seed = number(args, "seed", 20261029)?;
    if !(1..=50000).contains(&maximum) || !(32..=512).contains(&target) {
        return Err(fail("bounded sample/query count required"));
    }
    let (quarantine, _): (Vec<String>, _) =
        read_json(Path::new(required(args, "identity-policy")?), 65536)?;
    let mut source = reader(required(args, "input")?)?;
    let header = line(&mut source)?.ok_or("sample header missing")?;
    let manifest: Value = serde_json::from_slice(&header)?;
    if manifest["type"] != "manifest" || manifest["partial"] != false {
        return Err(fail("frozen full-source sample header required"));
    }
    let mut supported_scopes = HashSet::new();
    let scopes_hash = if let Some(path) = args.get("native-scopes") {
        let (roster, hash): (Value, _) = read_json(Path::new(path), 32 * 1024 * 1024)?;
        if roster["schema_version"] != 1
            || roster["build_id"] != manifest["build_id"]
            || roster["source_watermark"] != manifest["source_watermark"]
        {
            return Err(fail("native scope roster belongs to another frozen source"));
        }
        let hosts = roster["hosts"]
            .as_array()
            .ok_or("native roster hosts missing")?;
        if hosts.len() > 100000 {
            return Err(fail("native scope roster count budget exceeded"));
        }
        for raw in hosts {
            let host = raw.as_str().ok_or("invalid native roster host")?;
            if ffi::host(host)? != host || !supported_scopes.insert(host.to_owned()) {
                return Err(fail("native roster must contain unique canonical hosts"));
            }
        }
        Some(hash)
    } else {
        None
    };
    let frozen_feed = if let Some(path) = args.get("updates") {
        let (feed, hash): (Feed, _) = read_json(Path::new(path), 8 * 1024 * 1024)?;
        if feed.schema_version != 1
            || feed.source_kind != SOURCE_KIND
            || feed.build_id != manifest["build_id"]
            || Some(feed.source_watermark) != manifest["source_watermark"].as_u64()
            || feed.baseline_actors.len() > 256
            || feed.batches.len() > 64
        {
            return Err(fail("bounded query feed belongs to another frozen source"));
        }
        Some((feed, hash))
    } else {
        None
    };
    let mut prefixes: HashMap<String, [u8; 32]> = HashMap::new();
    let mut curated = vec![];
    let mut seen = HashSet::new();
    let mut read = 0;
    let mut bytes = header.len() as u64;
    let mut hash = Sha256::new();
    hash.update(&header);
    let mut pairs = 0;
    let mut witness = false;
    let mut push = |q: String,
                    category: &str,
                    pds: Option<String>,
                    prefer_pds: Option<String>,
                    preference: Option<String>,
                    boost: Option<f32>,
                    limit: usize| {
        let key = json!([
            q,
            pds,
            prefer_pds,
            preference,
            boost.map(f32::to_bits),
            limit
        ])
        .to_string();
        if !seen.insert(key) {
            return;
        }
        let limitations = if pds.is_some() {
            vec![
                "native small-host scope support may reject a valid scope".into(),
                "native capped candidate selection may miss true scoped topK".into(),
            ]
        } else if prefer_pds.is_some() {
            vec!["native contextual PDS preference may be unsupported".into()]
        } else if q.contains(' ') {
            vec![
                "native capped per-token candidate conjunction may drop complete multiword matches"
                    .into(),
            ]
        } else {
            vec![
                "native capped prefix/hydration lists can miss true topK, especially limit100"
                    .into(),
            ]
        };
        curated.push(Case {
            id: format!("curated-{:03}", curated.len()),
            native_shared_features: prefer_pds.is_none()
                && pds
                    .as_ref()
                    .is_none_or(|host| supported_scopes.contains(host)),
            q,
            limit,
            pds,
            prefer_pds,
            preference,
            boost,
            category: category.into(),
            native_limitations: limitations,
        });
    };
    for q in [
        "zzstoatzz.io",
        "pfrazee.com",
        "jay.bsky.team",
        "Justin Fa",
        "Fa Justin",
        "東京",
        "日本太",
        "éric",
        "a",
        "music",
    ] {
        push(q.into(), "curated", None, None, None, None, 10)
    }
    if let Some((feed, _)) = &frozen_feed {
        for actors in std::iter::once(&feed.baseline_actors)
            .chain(feed.batches.iter().map(|batch| &batch.actors))
        {
            if actors.len() > 500 {
                return Err(fail("query feed batch actor budget exceeded"));
            }
            for actor in actors.iter().take(2) {
                let projected =
                    ffi::Projection::new(&actor.handle, &actor.display_name, &actor.pds)?;
                if !projected.handle().is_empty() {
                    push(
                        projected.handle().into(),
                        "feed-exact",
                        None,
                        None,
                        None,
                        None,
                        10,
                    );
                }
                let runs = matching::runs(projected.display());
                let latin = runs
                    .iter()
                    .filter(|(cjk, _)| !cjk)
                    .map(|(_, text)| text.chars().take(8).collect::<String>())
                    .collect::<Vec<_>>();
                if latin.len() >= 2 {
                    let q = format!("{} {}", latin[0], latin[1]);
                    push(q.clone(), "feed-conjunction", None, None, None, None, 100);
                    push(
                        format!("{} {}", latin[1], latin[0]),
                        "feed-reversed",
                        None,
                        None,
                        None,
                        None,
                        100,
                    );
                    if !projected.host().is_empty() {
                        push(
                            q,
                            "feed-hard-PDS",
                            Some(projected.host().into()),
                            None,
                            None,
                            None,
                            100,
                        );
                    }
                }
            }
        }
    }
    while read < maximum {
        let Some(raw) = line(&mut source)? else { break };
        let value: Value = serde_json::from_slice(&raw)?;
        if value["type"] == "end" {
            break;
        }
        bytes += raw.len() as u64;
        if bytes > 128 * 1024 * 1024 {
            return Err(fail("raw sample exceeds128MiB"));
        }
        hash.update(&raw);
        let row: Row = serde_json::from_value(value)?;
        read += 1;
        let projection = ffi::Projection::new(&row.handle, &row.display_name, &row.pds)?;
        if row.hidden
            || row.deleted
            || row.handle.to_ascii_lowercase().ends_with(".invalid")
            || quarantine.iter().any(|h| h == projection.handle())
        {
            continue;
        }
        let handle = projection.handle();
        if handle.is_ascii() {
            for n in [2, 3, 4, 5, 6, 8, 10, 12] {
                if handle.len() > n {
                    let prefix = &handle[..n];
                    let mut hasher = Sha256::new();
                    hasher.update(seed.to_le_bytes());
                    hasher.update(prefix.as_bytes());
                    prefixes
                        .entry(prefix.into())
                        .or_insert_with(|| hasher.finalize().into());
                }
            }
        }
        let parts = matching::runs(projection.display());
        let latin = parts
            .iter()
            .filter(|(cjk, text)| !cjk && text.len() >= 2)
            .map(|(_, text)| *text)
            .collect::<Vec<_>>();
        if latin.len() >= 2 && pairs < 5 {
            let a = latin[0].chars().take(6).collect::<String>();
            let b = latin[1].chars().take(4).collect::<String>();
            push(
                format!("{a} {b}"),
                "raw-sample-multiword",
                None,
                None,
                None,
                None,
                10,
            );
            push(
                format!("{b} {a}"),
                "raw-sample-reversed",
                None,
                None,
                None,
                None,
                10,
            );
            pairs += 1;
        }
        if !witness && !projection.host().is_empty() && !handle.is_empty() {
            let host = projection.host().to_owned();
            push(
                handle.into(),
                "raw-sample-exact",
                None,
                None,
                None,
                None,
                10,
            );
            push(
                handle.into(),
                "raw-sample-PDS-positive",
                Some(host.clone()),
                None,
                None,
                None,
                10,
            );
            push(
                handle.into(),
                "raw-sample-PDS-exclusion",
                Some("outside.invalid.test".into()),
                None,
                None,
                None,
                10,
            );
            push(
                "a".into(),
                "PDS-first",
                None,
                Some(host.clone()),
                Some("pds_first".into()),
                None,
                10,
            );
            for boost in [0.1_f32, f32::from_bits(1), 1e30, f32::MAX] {
                push(
                    "a".into(),
                    "exact-integer-PDS-boost",
                    None,
                    Some(host.clone()),
                    Some("boost".into()),
                    Some(boost),
                    10,
                )
            }
            push("a".into(), "broad-limit100", None, None, None, None, 100);
            witness = true;
        }
    }
    drop(push);
    let mut strata = HashMap::<usize, Vec<(String, [u8; 32])>>::new();
    for (value, rank) in prefixes {
        strata.entry(value.len()).or_default().push((value, rank));
    }
    for rows in strata.values_mut() {
        rows.sort_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)))
    }
    let mut offset = 0;
    while curated.len() < target {
        let mut added = false;
        for length in [2, 3, 4, 5, 6, 8, 10, 12] {
            if curated.len() == target {
                break;
            }
            if let Some((q, _)) = strata.get(&length).and_then(|rows| rows.get(offset)) {
                let key = json!([q, null, null, null, null, 10]).to_string();
                if seen.insert(key) {
                    curated.push(Case {
                        id: format!("prefix-{:03}", curated.len()),
                        native_shared_features: true,
                        q: q.clone(),
                        limit: 10,
                        pds: None,
                        prefer_pds: None,
                        preference: None,
                        boost: None,
                        category: format!("raw-handle-prefix-{length}"),
                        native_limitations: vec![
                            "native capped candidates may miss independently ranked topK".into(),
                        ],
                    });
                    added = true
                }
            }
        }
        if !added && strata.values().all(|rows| rows.len() <= offset + 1) {
            return Err(fail(
                "raw bounded sample has insufficient distinct query prefixes",
            ));
        }
        offset += 1;
    }
    if curated.len() > target {
        return Err(fail("case budget too small for required witness cases"));
    }
    let result = QuerySet {
        schema_version: 1,
        source_kind: SOURCE_KIND.into(),
        cases: curated,
        sample: json!({"method":"firstN raw actors in sourceDID order; deterministic hash-ranked prefix-length strata; never engine-derived","limitations":["bounded source-prefix sample is not a uniform full-corpus sample","curated positives/empties determined only by later complete raw reference pass"],"raw_actors_read":read,"sample_bytes":bytes,"sample_prefix_sha256":format!("{:x}",hash.finalize()),"source_build_id":manifest["build_id"],"source_watermark":manifest["source_watermark"],"source_declared_actors":manifest["actor_count"],"full_source_footer_verified":false,"seed":seed,"PDS_witness_in_sample":witness,"native_scopes_sha256":scopes_hash,"native_supported_scope_count":supported_scopes.len(),"updates_sha256":frozen_feed.as_ref().map(|(_,hash)|hash)}),
    };
    let output = Path::new(required(args, "output")?);
    write_json(output, &result)?;
    Ok(
        json!({"passed":true,"cases":result.cases.len(),"sample_actors":read,"queries_sha256":file_digest(output)?,"source_kind":SOURCE_KIND,"full_source_pass_ran":false,"derived_from_engine_responses":false}),
    )
}
