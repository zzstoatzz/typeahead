use crate::{
    Error,
    actor::Actor,
    engine::{Engine, SourceReconcile, epoch},
    normalizer,
};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Query, State},
    http::{HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    io::Read,
    net::SocketAddr,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::Semaphore;
#[derive(Clone)]
struct App {
    engine: Arc<Engine>,
    admission: Arc<Semaphore>,
    admin_secret: Option<Arc<String>>,
    binary_sha256: Arc<String>,
    configured_concurrency: usize,
    machine_id: Option<Arc<String>>,
    instance_id: Option<Arc<String>>,
}
fn response(status: StatusCode, value: Value) -> Response {
    let mut response = (status, Json(value)).into_response();
    response
        .headers_mut()
        .insert("access-control-allow-origin", HeaderValue::from_static("*"));
    response
        .headers_mut()
        .insert("cache-control", HeaderValue::from_static("no-store"));
    response
}
fn with_identity(
    mut response: Response,
    build_id: &str,
    format: &str,
    app: &App,
    strategy: &str,
) -> Response {
    response
        .headers_mut()
        .insert("x-typeahead-engine", HeaderValue::from_static("tantivy"));
    response.headers_mut().insert(
        "x-typeahead-build",
        HeaderValue::from_str(build_id).unwrap_or_else(|_| HeaderValue::from_static("invalid")),
    );
    response.headers_mut().insert(
        "x-typeahead-format",
        HeaderValue::from_str(format).unwrap_or_else(|_| HeaderValue::from_static("invalid")),
    );
    response.headers_mut().insert(
        "x-typeahead-binary-sha256",
        HeaderValue::from_str(&app.binary_sha256).unwrap(),
    );
    response.headers_mut().insert(
        "x-typeahead-retrieval-strategy",
        HeaderValue::from_str(strategy).unwrap(),
    );
    if let Some(id) = &app.machine_id {
        response
            .headers_mut()
            .insert("x-typeahead-machine-id", HeaderValue::from_str(id).unwrap());
    }
    if let Some(version) = &app.instance_id {
        response.headers_mut().insert(
            "x-typeahead-machine-version",
            HeaderValue::from_str(version).unwrap(),
        );
    }
    response
}
fn failure(error: Error) -> Response {
    match error {
        Error::Invalid(message) => response(StatusCode::BAD_REQUEST, json!({"error":message})),
        Error::Conflict(_) => response(
            StatusCode::CONFLICT,
            json!({"error":"SourceVersionConflict"}),
        ),
        Error::Budget => response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"error":"SearchBudgetExceeded"}),
        ),
        Error::Busy => response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"error":"search admission exhausted"}),
        ),
        Error::NotReady => response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"error":"not ready"}),
        ),
        Error::Storage(_) => response(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({"error":"search storage failure"}),
        ),
    }
}
fn params(values: &HashMap<String, String>, name: &str) -> Option<String> {
    values.get(name).filter(|s| !s.is_empty()).cloned()
}
async fn search(State(app): State<App>, Query(values): Query<HashMap<String, String>>) -> Response {
    search_impl(app, values, false).await
}
async fn debug_search(
    State(app): State<App>,
    Query(values): Query<HashMap<String, String>>,
) -> Response {
    search_impl(app, values, true).await
}
async fn search_impl(app: App, values: HashMap<String, String>, debug: bool) -> Response {
    let q = match values.get("q").or_else(|| values.get("term")) {
        Some(q) if !q.trim().is_empty() => q.clone(),
        _ => return failure(Error::Invalid("missing or empty q parameter".into())),
    };
    if q.len() > 512 {
        return failure(Error::Invalid("query exceeds 512 bytes".into()));
    }
    let limit = match values.get("limit") {
        None => {
            if debug {
                25
            } else {
                10
            }
        }
        Some(raw) if !raw.is_empty() && raw.bytes().all(|b| b.is_ascii_digit()) => {
            match raw.parse::<usize>() {
                Ok(value) if (1..=100).contains(&value) => value,
                _ => return failure(Error::Invalid("limit must be 1..100".into())),
            }
        }
        Some(_) => return failure(Error::Invalid("invalid limit".into())),
    };
    if ["pds", "prefer_pds"]
        .iter()
        .any(|key| values.get(*key).is_some_and(|value| value.is_empty()))
    {
        return failure(Error::Invalid("present PDS scope cannot be empty".into()));
    }
    let pds = params(&values, "pds");
    let preferred = match params(&values, "prefer_pds")
        .map(|s| normalizer::host(&s))
        .transpose()
    {
        Ok(host) => host,
        Err(error) => return failure(error),
    };
    let boost = match values.get("boost") {
        Some(raw) => match raw.parse::<f32>() {
            Ok(v) if v.is_finite() && v >= 0.0 => Some(if v == 0.0 { 0.0 } else { v }),
            _ => return failure(Error::Invalid("invalid boost".into())),
        },
        None => None,
    };
    let mode = match values.get("preference").map(String::as_str) {
        Some("pds_first") if boost.is_none() && preferred.is_some() => Some("pds_first"),
        Some("boost") if boost.is_some() && preferred.is_some() => Some("boost"),
        Some(_) => return failure(Error::Invalid("invalid preference combination".into())),
        None if preferred.is_some() => Some(if boost.is_some() {
            "boost"
        } else {
            "pds_first"
        }),
        None if boost.is_none() => None,
        None => return failure(Error::Invalid("boost requires prefer_pds".into())),
    };
    let pds_first = mode == Some("pds_first");
    let preferred_ack = preferred.clone();
    let permit = match app.admission.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => return failure(Error::Busy),
    };
    let engine = app.engine.clone();
    let debug_q = q.clone();
    let start = Instant::now();
    let job = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        engine.search(
            &q,
            limit,
            pds.as_deref(),
            preferred.as_deref(),
            pds_first,
            boost,
        )
    });
    let result = match tokio::time::timeout(Duration::from_millis(2500), job).await {
        Ok(Ok(Ok(result))) => result,
        Ok(Ok(Err(error))) => return failure(error),
        Ok(Err(_)) => return failure(Error::Storage("search task failed".into())),
        Err(_) => {
            return response(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"error":"SearchDeadlineExceeded"}),
            );
        }
    };
    let body = if debug {
        json!({"engine":"tantivy","build_id":result.metadata.build_id,"q":debug_q,"normalized":result.plan["normalized"],"mode":result.plan["mode"],"hydrate_cap":null,"query_cut":false,"full_query_dropped":0,"exact_hits":null,"merged":result.actors.len(),"visited_candidates":result.visited,"retrieval_strategy":result.retrieval_strategy,"scorers_opened":result.scorers_opened,"rank_head_reads":result.rank_head_reads,"segments_visited":result.segments_visited,"segments_hydrated":result.segments_hydrated,"hydrated_candidates":result.hydrated_candidates,"hydration_ms":result.hydration_us as f64 / 1000.0,"moderation_ms":result.moderation_us as f64 / 1000.0,"segments":result.segments,"candidates":result.actors.iter().map(|a|json!({"did":a.did,"handle":a.handle,"display_name":a.display_name,"score":a.quality_score as f64})).collect::<Vec<_>>()})
    } else {
        json!({"actors":result.actors.iter().map(Actor::public).collect::<Vec<_>>()})
    };
    let mut response = response(StatusCode::OK, body);
    response.headers_mut().insert(
        "x-typeahead-pds",
        HeaderValue::from_str(result.pds.as_deref().unwrap_or("")).unwrap(),
    );
    response.headers_mut().insert(
        "x-typeahead-build",
        HeaderValue::from_str(&result.metadata.build_id)
            .unwrap_or_else(|_| HeaderValue::from_static("invalid")),
    );
    if let Some(host) = preferred_ack {
        response.headers_mut().insert(
            "x-typeahead-prefer-pds",
            HeaderValue::from_str(&host).unwrap(),
        );
        response.headers_mut().insert(
            "x-typeahead-preference",
            HeaderValue::from_static(mode.unwrap()),
        );
        if let Some(value) = boost {
            response.headers_mut().insert(
                "x-typeahead-boost",
                HeaderValue::from_str(&format!("{:08x}", value.to_bits())).unwrap(),
            );
        }
    }
    response.headers_mut().insert(
        "server-timing",
        HeaderValue::from_str(&format!(
            "search;dur={:.3}",
            start.elapsed().as_secs_f64() * 1000.0
        ))
        .unwrap(),
    );
    with_identity(
        response,
        &result.metadata.build_id,
        &result.metadata.format,
        &app,
        result.retrieval_strategy,
    )
}
async fn health(State(app): State<App>) -> Response {
    let g = app.engine.snapshot();
    let ready = app.engine.generation_ready(&g);
    with_identity(
        response(
            StatusCode::OK,
            json!({"status":if ready{"ok"}else{"not_ready"},"ready":ready,"engine":"tantivy","actors":g.metadata.actor_count,"sync_lag_s":epoch().saturating_sub(g.metadata.watermark),"build_id":g.metadata.build_id,"normalize_version":g.metadata.normalize_version,"plan_version":g.metadata.plan_version}),
        ),
        &g.metadata.build_id,
        &g.metadata.format,
        &app,
        app.engine.retrieval.name(),
    )
}
async fn ready(State(app): State<App>) -> Response {
    let g = app.engine.snapshot();
    let ready = app.engine.generation_ready(&g);
    let response = response(
        if ready {
            StatusCode::OK
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        },
        json!({"ready":ready}),
    );
    if ready {
        with_identity(
            response,
            &g.metadata.build_id,
            &g.metadata.format,
            &app,
            app.engine.retrieval.name(),
        )
    } else {
        response
    }
}
async fn status(State(app): State<App>) -> Response {
    let g = app.engine.snapshot();
    let mut value = serde_json::to_value(&g.metadata).unwrap();
    value["engine"] = json!("tantivy");
    value["machine_id"] = json!(app.machine_id.as_ref().map(|value| value.as_str()));
    value["instance_id"] = json!(app.instance_id.as_ref().map(|value| value.as_str()));
    value["binary_sha256"] = json!(app.binary_sha256.as_str());
    value["retrieval_strategy"] = json!(app.engine.retrieval.name());
    value["query_config"] = json!({"retrieval_strategy":app.engine.retrieval.name(),"max_candidates":app.engine.max_candidates,"max_segments":app.engine.max_segments,"allow_partial":app.engine.allow_partial,"allow_stale":app.engine.allow_stale,"max_sync_lag_s":app.engine.max_sync_lag_s,"concurrency":app.configured_concurrency});
    value["ready"] = json!(app.engine.generation_ready(&g));
    value["segments"] = json!(g.searcher.segment_readers().len());
    value["pds_scope_version"] = json!(1);
    value["pds_scope_policy"] = json!("all-recorded-canonical-hosts");
    value["pds_unknown_actor_count"] =
        json!(g.metadata.pds_missing_actor_count + g.metadata.pds_invalid_actor_count);
    value["prefix_count"] = Value::Null;
    value["expiry_coverage_ready"] = json!(g.metadata.expiry_coverage.as_ref().is_some_and(|coverage| coverage.ready(g.metadata.input_actors, g.metadata.actor_count, g.metadata.full_source)));
    value["expiry_coverage_state"] = json!(match g.metadata.expiry_coverage.as_ref() {
        None => "unknown",
        Some(coverage) if !coverage.import_complete => "incomplete",
        Some(coverage) if coverage.ready(g.metadata.input_actors, g.metadata.actor_count, g.metadata.full_source) => "complete",
        Some(_) => "blocked",
    });
    value.as_object_mut().unwrap().remove("last_source_batch");
    value.as_object_mut().unwrap().remove("policy_snapshot");
    let now = epoch();
    let policy = g.policy.as_ref();
    let observed_at = policy.map(|p| p.source_observed_at());
    value["moderation"] = json!({
        "enabled": policy.is_some(),
        "sha256": g.policy_sha256,
        "source_observed_at": observed_at,
        "age_s": observed_at.map(|t| now.saturating_sub(t)),
        "max_age_s": 300,
        "fresh": policy.is_some() && g.policy_fresh(now),
        "complete": policy.is_some(),
        "override_count": policy.map(|p| p.snapshot()["overrides"].as_array().map_or(0, Vec::len)),
        "blocked_domain_count": policy.map(|p| p.snapshot()["blocked_domains"].as_array().map_or(0, Vec::len)),
        "canonical_rules_source_sha256": crate::moderation::ModerationPolicy::rules_source_sha256().ok(),
    });
    let last_round = g
        .metadata
        .source_cursor
        .pointer("/reconciliation/last_completed_at")
        .and_then(Value::as_u64);
    let completed_start = g
        .metadata
        .source_cursor
        .pointer("/reconciliation/last_completed_started_at")
        .and_then(Value::as_u64);
    let round_duration = completed_start
        .zip(last_round)
        .and_then(|(start, end)| end.checked_sub(start));
    value["reconciliation"] = json!({
        "last_completed_at": last_round,
        "last_completed_started_at": completed_start,
        "last_completed_duration_s": round_duration,
        "age_s": last_round.map(|t| now.saturating_sub(t)),
        "polling_freshness_is_full_coverage": false,
    });
    value["cutover_source_ready"] = json!(
        app.engine.generation_ready(&g)
            && g.metadata.full_source
            && !app.engine.allow_partial
            && !app.engine.allow_stale
            && g.metadata.source_observation_revision > 0
            && g.metadata.source_cursor["source_policy"] == "turso-authoritative-observations-v2"
            && g.metadata.source_cursor["schema_version"] == 1
            && g.metadata.source_cursor["cycle"].is_null()
            && g.metadata.source_cursor["through"].as_u64() == Some(g.metadata.watermark)
            && g.metadata.source_cursor["build_id"].as_str() == Some(g.metadata.build_id.as_str())
            && g.metadata.source_cursor["snapshot_watermark"].as_u64()
                == Some(g.metadata.snapshot_watermark)
            && g.metadata.format == "tantivy-0.27-f32-did-expiry-v4"
            && now.saturating_sub(g.metadata.watermark) <= 120
            && policy.is_some()
            && g.policy_fresh(now)
            && last_round.is_some_and(|t| t <= now && now - t <= 86400)
            && round_duration.is_some_and(|duration| duration <= 86400)
    );
    value["cutover_source_slo"] = json!({"poll_max_age_s":120,"policy_max_age_s":300,"round_max_age_s":86400,"round_max_duration_s":86400,"qualified":false});
    value["source_snapshot_watermark"] = json!(g.metadata.snapshot_watermark);
    value["sync_lag_s"] = json!(epoch().saturating_sub(g.metadata.watermark));
    with_identity(
        response(StatusCode::OK, value),
        &g.metadata.build_id,
        &g.metadata.format,
        &app,
        app.engine.retrieval.name(),
    )
}
#[derive(Deserialize)]
struct Apply {
    actors: Vec<Actor>,
    watermark: Option<u64>,
    source_cursor: Option<Value>,
    expected_source_cursor: Option<Value>,
}
#[derive(Deserialize)]
struct SourceState {
    dids: Vec<String>,
}
#[derive(Deserialize)]
struct SourcePage {
    after_did: String,
    limit: usize,
}
fn authorized(app: &App, headers: &HeaderMap) -> bool {
    let supplied = headers
        .get("authorization")
        .and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer "))
        .unwrap_or("");
    app.admin_secret
        .as_ref()
        .is_some_and(|secret| constant_equal(supplied.as_bytes(), secret.as_bytes()))
}
async fn source_state(
    State(app): State<App>,
    headers: HeaderMap,
    Json(body): Json<SourceState>,
) -> Response {
    if !authorized(&app, &headers) {
        return response(StatusCode::UNAUTHORIZED, json!({"error":"unauthorized"}));
    }
    let engine = app.engine.clone();
    match tokio::task::spawn_blocking(move || engine.source_state(body.dids)).await {
        Ok(Ok(value)) => response(StatusCode::OK, value),
        Ok(Err(error)) => failure(error),
        Err(_) => failure(Error::Storage("source state task failed".into())),
    }
}
async fn source_page(
    State(app): State<App>,
    headers: HeaderMap,
    Json(body): Json<SourcePage>,
) -> Response {
    if !authorized(&app, &headers) {
        return response(StatusCode::UNAUTHORIZED, json!({"error":"unauthorized"}));
    }
    let engine = app.engine.clone();
    match tokio::task::spawn_blocking(move || engine.source_page(&body.after_did, body.limit)).await
    {
        Ok(Ok(value)) => response(StatusCode::OK, value),
        Ok(Err(error)) => failure(error),
        Err(_) => failure(Error::Storage("source page task failed".into())),
    }
}
async fn source_reconcile(
    State(app): State<App>,
    headers: HeaderMap,
    Json(body): Json<SourceReconcile>,
) -> Response {
    if !authorized(&app, &headers) {
        return response(StatusCode::UNAUTHORIZED, json!({"error":"unauthorized"}));
    }
    let engine = app.engine.clone();
    match tokio::task::spawn_blocking(move || engine.reconcile_source(body)).await {
        Ok(Ok(value)) => response(StatusCode::OK, serde_json::to_value(value).unwrap()),
        Ok(Err(error)) => failure(error),
        Err(_) => failure(Error::Storage("source reconcile task failed".into())),
    }
}
async fn apply(State(app): State<App>, headers: HeaderMap, Json(body): Json<Apply>) -> Response {
    let Some(secret) = app.admin_secret.as_ref() else {
        return response(StatusCode::NOT_FOUND, json!({"error":"not found"}));
    };
    let supplied = headers
        .get("authorization")
        .and_then(|s| s.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "))
        .unwrap_or("");
    if !constant_equal(supplied.as_bytes(), secret.as_bytes()) {
        return response(StatusCode::UNAUTHORIZED, json!({"error":"unauthorized"}));
    }
    let engine = app.engine.clone();
    match tokio::task::spawn_blocking(move || {
        engine.apply(
            body.actors,
            body.watermark,
            body.source_cursor,
            body.expected_source_cursor,
        )
    })
    .await
    {
        Ok(Ok(receipt)) => response(StatusCode::OK, serde_json::to_value(receipt).unwrap()),
        Ok(Err(error)) => failure(error),
        Err(_) => failure(Error::Storage("apply task failed".into())),
    }
}
fn constant_equal(a: &[u8], b: &[u8]) -> bool {
    let mut different = a.len() ^ b.len();
    for i in 0..b.len() {
        different |= (a.get(i).copied().unwrap_or(0) ^ b[i]) as usize;
    }
    different == 0
}
async fn missing() -> Response {
    response(StatusCode::NOT_FOUND, json!({"error":"not found"}))
}
fn runtime_identity(name: &str) -> crate::Result<Option<Arc<String>>> {
    match std::env::var(name) {
        Err(std::env::VarError::NotPresent) => Ok(None),
        Ok(value)
            if !value.is_empty()
                && value.len() <= 128
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_') =>
        {
            Ok(Some(Arc::new(value)))
        }
        _ => Err(Error::Invalid("invalid provider runtime identity".into())),
    }
}
fn executable_sha256() -> crate::Result<String> {
    #[cfg(target_os = "linux")]
    let path = std::path::PathBuf::from("/proc/self/exe");
    #[cfg(not(target_os = "linux"))]
    let path = std::env::current_exe()?;
    let mut file = std::fs::File::open(path)?;
    let mut buffer = vec![0u8; 1024 * 1024];
    let mut digest = Sha256::new();
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        digest.update(&buffer[..size]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
pub async fn serve(
    engine: Arc<Engine>,
    listen: SocketAddr,
    admin: Option<SocketAddr>,
    concurrency: usize,
) -> crate::Result<()> {
    if concurrency == 0 || concurrency > 32 {
        return Err(Error::Invalid("search concurrency must be1..32".into()));
    }
    if admin.is_some_and(|a| !a.ip().is_loopback()) {
        return Err(Error::Invalid("admin listener must bind loopback".into()));
    }
    let secret = std::env::var("ADMIN_SECRET")
        .ok()
        .filter(|s| !s.is_empty())
        .map(Arc::new);
    let app = App {
        engine,
        admission: Arc::new(Semaphore::new(concurrency)),
        admin_secret: secret,
        binary_sha256: Arc::new(executable_sha256()?),
        configured_concurrency: concurrency,
        machine_id: runtime_identity("FLY_MACHINE_ID")?,
        instance_id: runtime_identity("FLY_MACHINE_VERSION")?,
    };
    let router = Router::new()
        .route("/search", get(search))
        .route("/debug/search", get(debug_search))
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/index/status", get(status))
        .route("/index-status", get(status))
        .route("/xrpc/tech.waow.typeahead.searchActors", get(search))
        .route("/xrpc/app.bsky.actor.searchActorsTypeahead", get(search))
        .fallback(missing)
        .layer(DefaultBodyLimit::max(16384))
        .with_state(app.clone());
    let listener = tokio::net::TcpListener::bind(listen).await?;
    eprintln!(
        "{}",
        json!({"event":"listening","address":listen,"ready":app.engine.ready()})
    );
    if let Some(admin) = admin {
        if app.admin_secret.is_some() {
            let router = Router::new()
                .route("/admin/apply", post(apply))
                .route("/admin/source-state", post(source_state))
                .route("/admin/source-page", post(source_page))
                .route("/admin/source-reconcile", post(source_reconcile))
                .route("/index/status", get(status))
                .route("/health", get(health))
                .fallback(missing)
                .layer(DefaultBodyLimit::max(16 * 1024 * 1024))
                .with_state(app);
            let listener = tokio::net::TcpListener::bind(admin).await?;
            tokio::spawn(async move {
                if let Err(_) = axum::serve(listener, router).await {
                    eprintln!("admin server stopped");
                }
            });
        }
    }
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            #[cfg(unix)]
            {
                if let Ok(mut terminate) =
                    tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                {
                    tokio::select! {_ = tokio::signal::ctrl_c()=>{},_ = terminate.recv()=>{}}
                } else {
                    let _ = tokio::signal::ctrl_c().await;
                }
            }
            #[cfg(not(unix))]
            {
                let _ = tokio::signal::ctrl_c().await;
            }
        })
        .await
        .map_err(Error::from)
}
