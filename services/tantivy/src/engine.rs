use crate::{Error, Result, actor::Actor, moderation::ModerationPolicy, normalizer};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs,
    io::{BufRead, BufReader, Read},
    path::Path,
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, Ordering},
    },
    time::{Instant, SystemTime, UNIX_EPOCH},
};
use tantivy::collector::TopDocs;
use tantivy::columnar::StrColumn;
use tantivy::merge_policy::{LogMergePolicy, NoMergePolicy};
use tantivy::query::{BooleanQuery, EmptyQuery, EnableScoring, Occur, Query, TermQuery};
use tantivy::schema::{
    FAST, Field, INDEXED, IndexRecordOption, STORED, STRING, Schema, Value as DocumentValue,
};
use tantivy::{
    DocAddress, Index, IndexReader, IndexSettings, IndexSortByField, IndexWriter, Order,
    ReloadPolicy, Searcher, TERMINATED, TantivyDocument, Term,
};
const FORMAT: &str = "tantivy-0.27-f32-did-v1";
const SOURCE_FORMAT: &str = "tantivy-0.27-f32-did-source-v2";
const POLICY_FORMAT: &str = "tantivy-0.27-f32-did-policy-v3";
const EXPIRY_FORMAT: &str = "tantivy-0.27-f32-did-expiry-v4";
mod expiry_coverage;
static QUARANTINED_HANDLES: std::sync::LazyLock<Vec<String>> = std::sync::LazyLock::new(|| {
    serde_json::from_str(include_str!(concat!(
        env!("OUT_DIR"),
        "/identity-quarantine.json"
    )))
    .expect("valid committed identity quarantine")
});
mod boost;
mod merge_threads;
mod rank_merge;
mod source;
mod source_page;
pub use source::{SourceBatchReceipt, SourceReconcile};
pub fn epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Metadata {
    pub format: String,
    pub build_id: String,
    pub source_build_id: String,
    pub normalize_version: String,
    pub plan_version: String,
    pub snapshot_watermark: u64,
    pub watermark: u64,
    pub actor_count: u64,
    pub searchable_actor_count: u64,
    pub tombstone_count: u64,
    pub indexed_memberships: u64,
    pub input_actors: u64,
    pub import_complete: bool,
    #[serde(default)]
    pub full_source: bool,
    pub last_commit_epoch: u64,
    pub source_manifest: Value,
    #[serde(default)]
    pub source_cursor: Value,
    #[serde(default)]
    pub source_observation_revision: u64,
    #[serde(default)]
    pub last_source_batch: Option<SourceBatchReceipt>,
    #[serde(default)]
    pub pds_missing_actor_count: u64,
    #[serde(default)]
    pub pds_invalid_actor_count: u64,
    #[serde(default)]
    pub pds_invalid_reason_counts: BTreeMap<String, u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub policy_snapshot: Option<Value>,
    #[serde(default)]
    pub expiry_coverage: Option<expiry_coverage::Evidence>,
}
impl Metadata {
    fn empty() -> Self {
        Self {
            format: FORMAT.into(),
            build_id: "uninitialized".into(),
            source_build_id: String::new(),
            normalize_version: normalizer::version(),
            plan_version: normalizer::plan_version(),
            snapshot_watermark: 0,
            watermark: 0,
            actor_count: 0,
            searchable_actor_count: 0,
            tombstone_count: 0,
            indexed_memberships: 0,
            input_actors: 0,
            import_complete: false,
            full_source: false,
            last_commit_epoch: epoch(),
            source_manifest: Value::Null,
            source_cursor: Value::Null,
            source_observation_revision: 0,
            last_source_batch: None,
            pds_missing_actor_count: 0,
            pds_invalid_actor_count: 0,
            pds_invalid_reason_counts: BTreeMap::new(),
            policy_snapshot: None,
            expiry_coverage: None,
        }
    }
}
#[derive(Clone, Copy)]
struct Fields {
    key: Field,
    did: Field,
    rank: Field,
    pds: Field,
    visible: Field,
    actor: Field,
}
impl Fields {
    fn schema() -> (Schema, Self) {
        let mut b = Schema::builder();
        let key = b.add_text_field("key", STRING);
        let did = b.add_text_field("did", STRING);
        let rank = b.add_text_field("rank", FAST);
        let pds = b.add_text_field("pds", STRING);
        let visible = b.add_u64_field("visible", INDEXED);
        let actor = b.add_text_field("actor", STORED);
        (
            b.build(),
            Self {
                key,
                did,
                rank,
                pds,
                visible,
                actor,
            },
        )
    }
    fn from_index(index: &Index) -> Result<Self> {
        let s = index.schema();
        Ok(Self {
            key: s.get_field("key")?,
            did: s.get_field("did")?,
            rank: s.get_field("rank")?,
            pds: s.get_field("pds")?,
            visible: s.get_field("visible")?,
            actor: s.get_field("actor")?,
        })
    }
    fn document(&self, actor: &Actor) -> Result<(TantivyDocument, usize)> {
        let mut doc = TantivyDocument::default();
        doc.add_text(self.did, &actor.did);
        doc.add_text(self.rank, actor.rank());
        doc.add_u64(self.visible, u64::from(actor.visible()));
        if !actor.pds_host.is_empty() {
            doc.add_text(self.pds, &actor.pds_host);
        }
        let keys = if actor.deleted {
            vec![]
        } else {
            normalizer::actor_keys(&actor.handle, &actor.display_name)?
        };
        for key in &keys {
            doc.add_text(self.key, key);
        }
        doc.add_text(self.actor, serde_json::to_string(actor)?);
        Ok((doc, keys.len()))
    }
    fn hydrate(&self, searcher: &Searcher, address: DocAddress) -> Result<Actor> {
        let doc = searcher.doc::<TantivyDocument>(address)?;
        let raw = doc
            .get_first(self.actor)
            .and_then(|v| v.as_str())
            .ok_or_else(|| Error::Storage("committed profile missing".into()))?;
        serde_json::from_str(raw).map_err(Error::from)
    }
    fn find(&self, searcher: &Searcher, did: &str) -> Result<Option<Actor>> {
        let q = TermQuery::new(
            Term::from_field_text(self.did, did),
            IndexRecordOption::Basic,
        );
        let rows = searcher.search(&q, &TopDocs::with_limit(2).order_by_score())?;
        if rows.len() > 1 {
            return Err(Error::Storage("multiple live versions of actor".into()));
        }
        rows.first()
            .map(|(_, a)| self.hydrate(searcher, *a))
            .transpose()
    }
}
pub struct Generation {
    pub searcher: Searcher,
    pub metadata: Metadata,
    pub policy: Option<Arc<ModerationPolicy>>,
    pub policy_sha256: Option<String>,
    /// Each segment's rank column, in `segment_readers()` order. Opening one
    /// decodes a header per 512 documents of the segment, so it is done once
    /// per segment when a generation is built, never per query. `None` is a
    /// segment without the column, which a query reports as it always has.
    ranks: Vec<Option<StrColumn>>,
}
impl Generation {
    /// A segment's files never change, so a column the previous generation
    /// opened for the same segment is shared rather than opened again.
    fn new(
        searcher: Searcher,
        metadata: Metadata,
        policy: Option<Arc<ModerationPolicy>>,
        policy_sha256: Option<String>,
        previous: Option<&Generation>,
    ) -> Result<Self> {
        let mut ranks = Vec::with_capacity(searcher.segment_readers().len());
        for segment in searcher.segment_readers() {
            let shared = previous.and_then(|old| {
                old.searcher
                    .segment_readers()
                    .iter()
                    .position(|reader| reader.segment_id() == segment.segment_id())
                    .map(|ordinal| old.ranks[ordinal].clone())
            });
            ranks.push(match shared {
                Some(column) => column,
                None => segment.fast_fields().str("rank")?,
            });
        }
        Ok(Self {
            searcher,
            metadata,
            policy,
            policy_sha256,
            ranks,
        })
    }
    fn load(searcher: Searcher, metadata: Metadata) -> Result<Self> {
        let policy = metadata
            .policy_snapshot
            .clone()
            .map(ModerationPolicy::validate)
            .transpose()?
            .map(Arc::new);
        if policy
            .as_ref()
            .is_some_and(|p| p.source_observed_at() > epoch().saturating_add(300))
        {
            return Err(Error::Storage(
                "committed moderation source clock is in the future".into(),
            ));
        }
        let policy_sha256 = policy
            .as_ref()
            .map(|p| policy_hash(p.snapshot()))
            .transpose()?;
        Self::new(searcher, metadata, policy, policy_sha256, None)
    }
    pub fn policy_fresh(&self, now: u64) -> bool {
        self.policy.as_ref().is_none_or(|p| {
            p.source_observed_at() <= now.saturating_add(300)
                && now.saturating_sub(p.source_observed_at()) <= 300
        })
    }
}
struct Writer {
    writer: IndexWriter,
    reader: IndexReader,
}
#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub enum RetrievalStrategy {
    #[default]
    PerSegment,
    GlobalMerge,
}
impl RetrievalStrategy {
    pub fn name(self) -> &'static str {
        match self {
            Self::PerSegment => "per-segment",
            Self::GlobalMerge => "global-merge",
        }
    }
}
/// When the serving writer merges segments. The defaults are Tantivy's: a
/// level merges at eight segments, levels are a factor of 2^0.75 apart,
/// every segment under 10,000 documents counts as that size, deleted
/// documents never cause a merge, and a segment with more than ten million
/// live documents is never merged again.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct MergeSettings {
    /// Segments in one size level that start a merge of that level.
    pub min_segments: usize,
    /// log2 of the size ratio between one level and the next.
    pub level_log_size: f64,
    /// Segments smaller than this are all placed as if they held this many
    /// documents, so the smallest level is one segment that absorbs each
    /// new commit until it is `2^level_log_size` times this size.
    pub min_layer_size: u32,
    /// A level holding a segment with more than this share of its documents
    /// deleted is merged, whatever its segment count; 1.0 turns that off.
    pub delete_ratio: f64,
    /// A segment with more live documents than this is left alone.
    pub max_docs: usize,
}
impl Default for MergeSettings {
    fn default() -> Self {
        Self {
            min_segments: 8,
            level_log_size: 0.75,
            min_layer_size: 10_000,
            delete_ratio: 1.0,
            max_docs: 10_000_000,
        }
    }
}
impl MergeSettings {
    fn policy(self) -> Result<LogMergePolicy> {
        if !(2..=64).contains(&self.min_segments)
            || !(0.25..=8.0).contains(&self.level_log_size)
            || self.min_layer_size == 0
            || !(self.delete_ratio > 0.0 && self.delete_ratio <= 1.0)
            || self.max_docs == 0
        {
            return Err(Error::Invalid("invalid merge settings".into()));
        }
        let mut policy = LogMergePolicy::default();
        policy.set_min_num_segments(self.min_segments);
        policy.set_level_log_size(self.level_log_size);
        policy.set_min_layer_size(self.min_layer_size);
        policy.set_del_docs_ratio_before_merge(self.delete_ratio as f32);
        policy.set_max_docs_before_merge(self.max_docs);
        Ok(policy)
    }
}
pub struct Engine {
    fields: Fields,
    writer: Mutex<Writer>,
    generation: RwLock<Arc<Generation>>,
    pub max_candidates: usize,
    pub retrieval: RetrievalStrategy,
    pub max_segments: usize,
    pub allow_partial: bool,
    pub allow_stale: bool,
    pub max_sync_lag_s: u64,
    merge: MergeSettings,
    healthy: AtomicBool,
}
#[derive(Default, Debug, Clone, Serialize, Deserialize)]
pub struct ApplyReceipt {
    pub applied: u64,
    pub replayed: u64,
    pub stale: u64,
    pub watermark: u64,
    pub opstamp: u64,
    pub source_cursor: Value,
    #[serde(default)]
    pub source_observation_revision: u64,
}
impl Engine {
    pub fn open(data: &Path, writer_memory: usize) -> Result<Self> {
        let index = Index::open_in_dir(data.join("index"))?;
        check_format(&index)?;
        let fields = Fields::from_index(&index)?;
        let metadata = load_metadata(&index)?;
        let reader = index
            .reader_builder()
            .reload_policy(ReloadPolicy::Manual)
            .try_into()?;
        let writer = index.writer_with_num_threads(1, writer_memory)?;
        let generation = Arc::new(Generation::load(reader.searcher(), metadata)?);
        Ok(Self {
            fields,
            writer: Mutex::new(Writer { writer, reader }),
            generation: RwLock::new(generation),
            max_candidates: 10000,
            retrieval: RetrievalStrategy::PerSegment,
            max_segments: 256,
            allow_partial: false,
            allow_stale: false,
            max_sync_lag_s: 1800,
            merge: MergeSettings::default(),
            healthy: AtomicBool::new(true),
        })
    }
    pub fn merge_settings(&self) -> MergeSettings {
        self.merge
    }
    /// Takes effect at the next commit, `start_pending_merges` included.
    pub fn set_merge_settings(&mut self, settings: MergeSettings) -> Result<()> {
        let policy = settings.policy()?;
        self.writer
            .lock()
            .map_err(|_| Error::Storage("writer lock poisoned".into()))?
            .writer
            .set_merge_policy(Box::new(policy));
        self.merge = settings;
        Ok(())
    }
    /// Give the writer's merge threads scheduling niceness `nice`, so a merge
    /// yields the CPU to a search. Linux applies niceness per thread; the
    /// threads are found by the name Tantivy gives them. Returns how many
    /// were changed.
    pub fn set_merge_niceness(&self, nice: i32) -> Result<usize> {
        if !(0..=19).contains(&nice) {
            return Err(Error::Invalid("merge niceness must be 0..19".into()));
        }
        let _writer = self
            .writer
            .lock()
            .map_err(|_| Error::Storage("writer lock poisoned".into()))?;
        merge_threads::renice(nice)
    }
    /// Start the merges the previous process left pending, before any write
    /// is accepted. The commit carries the committed payload unchanged.
    pub fn start_pending_merges(&self) -> Result<()> {
        let mut writer = self
            .writer
            .lock()
            .map_err(|_| Error::Storage("writer lock poisoned".into()))?;
        let payload = writer.writer.index().load_metas()?.payload;
        let mut prepared = writer.writer.prepare_commit()?;
        if let Some(payload) = &payload {
            prepared.set_payload(payload);
        }
        prepared.commit()?;
        Ok(())
    }
    pub fn snapshot(&self) -> Arc<Generation> {
        self.generation.read().unwrap().clone()
    }
    pub fn ready(&self) -> bool {
        self.generation_ready(&self.snapshot())
    }
    pub(crate) fn generation_ready(&self, g: &Generation) -> bool {
        self.healthy.load(Ordering::Acquire)
            && g.policy_fresh(epoch())
            && g.metadata.expiry_coverage.as_ref().is_some_and(|value| value.ready(g.metadata.input_actors, g.metadata.actor_count, g.metadata.full_source))
            && g.metadata.import_complete
            && (g.metadata.full_source || self.allow_partial)
            && (self.allow_stale
                || (g.metadata.watermark <= epoch().saturating_add(300)
                    && epoch().saturating_sub(g.metadata.watermark) <= self.max_sync_lag_s))
            && g.metadata.normalize_version == normalizer::version()
            && g.metadata.plan_version == normalizer::plan_version()
            && g.searcher.segment_readers().len() <= self.max_segments
    }
    pub fn apply(
        &self,
        actors: Vec<Actor>,
        watermark: Option<u64>,
        source_cursor: Option<Value>,
        expected_cursor: Option<Value>,
    ) -> Result<ApplyReceipt> {
        self.apply_batch(actors, watermark, source_cursor, expected_cursor, None)
    }
    fn apply_batch(
        &self,
        actors: Vec<Actor>,
        watermark: Option<u64>,
        source_cursor: Option<Value>,
        expected_cursor: Option<Value>,
        authority: Option<source::Authority>,
    ) -> Result<ApplyReceipt> {
        if actors.len() > 1000 {
            return Err(Error::Invalid("batch exceeds 1000 actors".into()));
        }
        let mut writer = self
            .writer
            .lock()
            .map_err(|_| Error::Storage("writer lock poisoned".into()))?;
        let generation = self.snapshot();
        if !self.healthy.load(Ordering::Acquire) || !generation.metadata.import_complete {
            return Err(Error::NotReady);
        }
        let mut metadata = generation.metadata.clone();
        let mut policy = generation.policy.clone();
        let mut policy_sha256 = generation.policy_sha256.clone();
        if let Some(authority) = &authority {
            if let Some(receipt) = self.check_source_authority(&generation, authority)? {
                return Ok(receipt);
            }
        } else if metadata.source_observation_revision > 0 {
            return Err(Error::Conflict(
                "source-owned reconciliation required".into(),
            ));
        }
        if let Some(incoming) = authority.as_ref().and_then(|a| a.policy.as_ref()) {
            if incoming.source_observed_at() > epoch().saturating_add(300)
                || policy
                    .as_ref()
                    .is_some_and(|old| incoming.source_observed_at() < old.source_observed_at())
            {
                return Err(Error::Invalid(
                    "moderation source clock cannot move backward or into the future".into(),
                ));
            }
            metadata.policy_snapshot = Some(incoming.snapshot().clone());
            policy_sha256 = Some(policy_hash(incoming.snapshot())?);
            policy = Some(incoming.clone());
        }
        if let Some(cursor) = &source_cursor {
            if !cursor.is_object() || serde_json::to_vec(cursor)?.len() > 16384 {
                return Err(Error::Invalid("invalid source cursor".into()));
            }
            if expected_cursor.as_ref().unwrap_or(&Value::Null) != &metadata.source_cursor {
                eprintln!(
                    "{}",
                    json!({
                        "event":"source_cursor_conflict",
                        "expected_cursor_sha256":json_hash(expected_cursor.as_ref().unwrap_or(&Value::Null))?,
                        "committed_cursor_sha256":json_hash(&metadata.source_cursor)?,
                        "incoming_cursor_sha256":json_hash(cursor)?,
                    })
                );
                return Err(Error::Conflict(
                    "source cursor compare-and-set failed".into(),
                ));
            }
        }
        if watermark.is_some_and(|w| w < metadata.watermark) {
            return Err(Error::Invalid("watermark cannot move backward".into()));
        }
        let mut pending = BTreeMap::<String, Actor>::new();
        let mut receipt = ApplyReceipt::default();
        for raw in actors {
            let actor = raw.validate()?;
            let old = if let Some(a) = pending.get(&actor.did) {
                Some(a.clone())
            } else {
                self.fields.find(&generation.searcher, &actor.did)?
            };
            if let Some(old) = &old {
                if authority.is_some() {
                    let mut projection = actor.clone();
                    projection.version = old.version;
                    if &projection == old {
                        receipt.replayed += 1;
                        continue;
                    }
                }
                if authority.is_none() && actor.version < old.version {
                    receipt.stale += 1;
                    continue;
                }
                if actor.version == old.version {
                    if old == &actor {
                        receipt.replayed += 1;
                        continue;
                    }
                    if authority.is_none() {
                        eprintln!(
                            "{}",
                            json!({
                                "event":"actor_equal_version_conflict",
                                "did_sha256":format!("{:x}",Sha256::digest(actor.did.as_bytes())),
                                "stored_version":old.version,
                                "incoming_version":actor.version,
                                "old_deleted":old.deleted,
                                "incoming_deleted":actor.deleted,
                                "differing_fields":old.differing_fields(&actor),
                            })
                        );
                        return Err(Error::Conflict("equal-version actor conflict".into()));
                    }
                }
            }
            if authority.is_none() && actor.version <= metadata.snapshot_watermark {
                receipt.stale += 1;
                continue;
            }
            pending.insert(actor.did.clone(), actor);
            receipt.applied += 1;
        }
        // Validate and construct every document before mutating the writer.
        let mut documents = Vec::with_capacity(pending.len());
        for actor in pending.into_values() {
            let old = self.fields.find(&generation.searcher, &actor.did)?;
            let (doc, keys) = self.fields.document(&actor)?;
            if let Some(coverage) = metadata.expiry_coverage.as_mut() {
                coverage.replace_actor(old.as_ref(), &actor)?;
            }
            if let Some(old) = old {
                if !old.deleted {
                    if old.pds.is_empty() {
                        metadata.pds_missing_actor_count =
                            metadata.pds_missing_actor_count.saturating_sub(1);
                    } else if let Some(reason) = old.invalid_pds_reason() {
                        metadata.pds_invalid_actor_count =
                            metadata.pds_invalid_actor_count.saturating_sub(1);
                        if let Some(count) = metadata.pds_invalid_reason_counts.get_mut(reason) {
                            *count = count.saturating_sub(1);
                        }
                    }
                    metadata.actor_count = metadata.actor_count.saturating_sub(1);
                } else {
                    metadata.tombstone_count = metadata.tombstone_count.saturating_sub(1);
                }
                if old.visible() {
                    metadata.searchable_actor_count =
                        metadata.searchable_actor_count.saturating_sub(1);
                }
            }
            if actor.deleted {
                metadata.tombstone_count += 1;
            } else {
                metadata.actor_count += 1;
                if actor.pds.is_empty() {
                    metadata.pds_missing_actor_count += 1;
                } else if let Some(reason) = actor.invalid_pds_reason() {
                    metadata.pds_invalid_actor_count += 1;
                    *metadata
                        .pds_invalid_reason_counts
                        .entry(reason.into())
                        .or_default() += 1;
                }
            }
            if actor.visible() {
                metadata.searchable_actor_count += 1;
            }
            metadata.indexed_memberships += keys as u64;
            documents.push((actor.did, doc));
        }
        for (did, doc) in documents {
            writer
                .writer
                .delete_term(Term::from_field_text(self.fields.did, &did));
            if let Err(error) = writer.writer.add_document(doc) {
                if writer.writer.rollback().is_err() {
                    self.healthy.store(false, Ordering::Release);
                }
                return Err(error.into());
            }
        }
        if let Some(value) = watermark {
            metadata.watermark = value;
        }
        if let Some(cursor) = source_cursor {
            metadata.source_cursor = cursor;
        }
        metadata.last_commit_epoch = epoch();
        if let Some(authority) = authority {
            metadata.source_observation_revision = metadata
                .source_observation_revision
                .checked_add(1)
                .ok_or_else(|| Error::Storage("source observation revision exhausted".into()))?;
            metadata.format = if metadata.expiry_coverage.is_some() {
                EXPIRY_FORMAT
            } else if policy.is_some() {
                POLICY_FORMAT
            } else {
                SOURCE_FORMAT
            }
            .into();
            receipt.watermark = metadata.watermark;
            receipt.source_cursor = metadata.source_cursor.clone();
            receipt.source_observation_revision = metadata.source_observation_revision;
            metadata.last_source_batch = Some(SourceBatchReceipt {
                batch_id: authority.batch_id,
                request_sha256: authority.request_sha256,
                receipt: receipt.clone(),
            });
        }
        failpoint("before_commit");
        receipt.opstamp = match commit(&mut writer.writer, &mut metadata) {
            Ok(value) => value,
            Err(error) => {
                self.healthy.store(false, Ordering::Release);
                let _ = writer.writer.rollback();
                return Err(error);
            }
        };
        failpoint("after_commit");
        if let Err(error) = writer.reader.reload() {
            self.healthy.store(false, Ordering::Release);
            return Err(error.into());
        }
        let next = match Generation::new(
            writer.reader.searcher(),
            metadata.clone(),
            policy,
            policy_sha256,
            Some(&generation),
        ) {
            Ok(next) => Arc::new(next),
            Err(error) => {
                self.healthy.store(false, Ordering::Release);
                return Err(error);
            }
        };
        *self.generation.write().unwrap() = next;
        receipt.watermark = metadata.watermark;
        receipt.source_cursor = metadata.source_cursor;
        Ok(receipt)
    }
    pub fn search(
        &self,
        q: &str,
        limit: usize,
        pds: Option<&str>,
        preference: Option<&str>,
        pds_first: bool,
        boost: Option<f32>,
    ) -> Result<SearchResult> {
        let generation = self.snapshot();
        if !self.generation_ready(&generation) {
            return Err(Error::NotReady);
        }
        if generation.searcher.segment_readers().len() > self.max_segments {
            return Err(Error::Budget);
        }
        if q.is_empty() || q.len() > 512 || !(1..=100).contains(&limit) {
            return Err(Error::Invalid(
                "query/limit outside supported bounds".into(),
            ));
        }
        if boost.is_some_and(|s| !s.is_finite()) {
            return Err(Error::Invalid("boost must be finite".into()));
        }
        let query_epoch_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX);
        let plan = normalizer::query(q)?;
        let pds = pds.map(normalizer::host).transpose()?;
        let preference = preference.map(normalizer::host).transpose()?;
        if plan["mode"] == "empty" {
            return Ok(SearchResult {
                actors: vec![],
                plan,
                pds,
                metadata: QueryMetadata {
                    build_id: generation.metadata.build_id.clone(),
                    format: generation.metadata.format.clone(),
                },
                visited: 0,
                retrieval_strategy: self.retrieval.name(),
                scorers_opened: 0,
                rank_head_reads: 0,
                segments_visited: 0,
                segments_hydrated: 0,
                hydrated_candidates: 0,
                hydration_us: 0,
                moderation_us: 0,
                segments: generation.searcher.segment_readers().len(),
            });
        }
        let mut budget = Budget {
            remaining: self.max_candidates,
            visited: 0,
        };
        let mut scorers_opened = 0;
        let mut rank_head_reads = 0;
        let mut segments_visited = HashSet::new();
        let mut segments_hydrated = HashSet::new();
        let mut hydrated_candidates = 0;
        let mut hydration_ns = 0;
        let mut moderation_ns = 0;
        let mut hydrate = |address| {
            let start = Instant::now();
            let actor = self.fields.hydrate(&generation.searcher, address)?;
            hydration_ns += start.elapsed().as_nanos();
            hydrated_candidates += 1;
            segments_hydrated.insert(address.segment_ord);
            Ok::<Actor, Error>(actor)
        };
        let mut found = HashMap::<String, Candidate>::new();
        let buckets = if preference.is_some() {
            vec![Some(true), Some(false)]
        } else {
            vec![None]
        };
        for exact in [true, false] {
            for bucket in &buckets {
                let query = make_query(
                    self.fields,
                    &plan,
                    exact,
                    pds.as_deref(),
                    preference.as_deref(),
                    *bucket,
                    generation.policy.is_some(),
                )?;
                let weight =
                    query.weight(EnableScoring::disabled_from_searcher(&generation.searcher))?;
                let mut consider = |key: String, address: DocAddress| -> Result<bool> {
                    let (_, did) = key
                        .split_once('|')
                        .ok_or_else(|| Error::Storage("invalid rank encoding".into()))?;
                    let hydrated = if generation.policy.is_some()
                        || plan["needs_verification"].as_bool().unwrap_or(false)
                    {
                        Some(hydrate(address)?)
                    } else {
                        None
                    };
                    let eligible =
                        if let (Some(policy), Some(actor)) = (&generation.policy, &hydrated) {
                            if actor.deleted
                                || actor.handle.is_empty()
                                || actor.handle.to_ascii_lowercase().ends_with(".invalid")
                            {
                                false
                            } else {
                                let start = Instant::now();
                                let hidden = policy
                                    .hidden_at_ms(actor, query_epoch_ms)
                                    .map_err(|_| Error::NotReady)?;
                                moderation_ns += start.elapsed().as_nanos();
                                !hidden
                            }
                        } else {
                            true
                        };
                    let verified = eligible
                        && if plan["needs_verification"].as_bool().unwrap_or(false) {
                            let actor = hydrated.as_ref().unwrap();
                            normalizer::matches(&actor.handle, &actor.display_name, q)?
                        } else {
                            true
                        };
                    if verified {
                        found
                            .entry(did.to_owned())
                            .and_modify(|c| c.exact |= exact)
                            .or_insert(Candidate {
                                rank: key,
                                address,
                                exact,
                                local: *bucket == Some(true),
                                actor: hydrated,
                            });
                        return Ok(true);
                    }
                    Ok(false)
                };
                match self.retrieval {
                    RetrievalStrategy::PerSegment => {
                        for (ordinal, segment) in
                            generation.searcher.segment_readers().iter().enumerate()
                        {
                            let rank = generation.ranks[ordinal]
                                .as_ref()
                                .ok_or_else(|| Error::Storage("rank column missing".into()))?;
                            let mut scorer = weight.scorer(segment, 1.0)?;
                            scorers_opened += 1;
                            let mut accepted = 0;
                            while scorer.doc() != TERMINATED && accepted < limit {
                                let doc = scorer.doc();
                                budget.visit()?;
                                segments_visited.insert(ordinal);
                                if !segment.is_deleted(doc) {
                                    let key = rank_merge::rank_key(rank, doc)?;
                                    rank_head_reads += 1;
                                    if consider(key, DocAddress::new(ordinal as u32, doc))? {
                                        accepted += 1;
                                    }
                                }
                                scorer.advance();
                            }
                        }
                    }
                    RetrievalStrategy::GlobalMerge => {
                        let mut merge =
                            rank_merge::RankMerge::new(&generation.searcher, &generation.ranks, weight.as_ref())?;
                        scorers_opened += generation.searcher.segment_readers().len();
                        let mut accepted = 0;
                        while accepted < limit {
                            let Some((key, address)) = merge.next()? else {
                                break;
                            };
                            budget.visit()?;
                            segments_visited.insert(address.segment_ord as usize);
                            if !generation.searcher.segment_readers()[address.segment_ord as usize]
                                .is_deleted(address.doc_id)
                                && consider(key, address)?
                            {
                                accepted += 1;
                            }
                        }
                        rank_head_reads += merge.head_reads;
                    }
                }
            }
        }
        let mut candidates = found.into_values().collect::<Vec<_>>();
        candidates.sort_by(|a, b| {
            let a_score = rank_score(&a.rank);
            let b_score = rank_score(&b.rank);
            let a_bonus = if a.local { boost.unwrap_or(0.0) } else { 0.0 };
            let b_bonus = if b.local { boost.unwrap_or(0.0) } else { 0.0 };
            (!a.exact, !(pds_first && a.local))
                .cmp(&(!b.exact, !(pds_first && b.local)))
                .then_with(|| boost::cmp_effective(b_score, b_bonus, a_score, a_bonus))
                .then_with(|| a.rank[9..].cmp(&b.rank[9..]))
        });
        candidates.truncate(limit);
        let mut actors = Vec::with_capacity(candidates.len());
        for candidate in candidates {
            actors.push(match candidate.actor {
                Some(actor) => actor,
                None => hydrate(candidate.address)?,
            });
        }
        Ok(SearchResult {
            actors,
            plan,
            pds,
            metadata: QueryMetadata {
                build_id: generation.metadata.build_id.clone(),
                format: generation.metadata.format.clone(),
            },
            visited: budget.visited,
            retrieval_strategy: self.retrieval.name(),
            scorers_opened,
            rank_head_reads,
            segments_visited: segments_visited.len(),
            segments_hydrated: segments_hydrated.len(),
            hydrated_candidates,
            hydration_us: (hydration_ns / 1000) as u64,
            moderation_us: (moderation_ns / 1000) as u64,
            segments: generation.searcher.segment_readers().len(),
        })
    }
}
#[derive(Clone)]
struct Candidate {
    rank: String,
    address: DocAddress,
    exact: bool,
    local: bool,
    actor: Option<Actor>,
}
struct Budget {
    remaining: usize,
    visited: usize,
}
impl Budget {
    fn visit(&mut self) -> Result<()> {
        if self.remaining == 0 {
            return Err(Error::Budget);
        }
        self.remaining -= 1;
        self.visited += 1;
        Ok(())
    }
}
pub struct QueryMetadata {
    pub build_id: String,
    pub format: String,
}
pub struct SearchResult {
    pub actors: Vec<Actor>,
    pub plan: Value,
    pub pds: Option<String>,
    pub metadata: QueryMetadata,
    pub visited: usize,
    pub retrieval_strategy: &'static str,
    pub scorers_opened: usize,
    pub rank_head_reads: usize,
    pub segments_visited: usize,
    pub segments_hydrated: usize,
    pub hydrated_candidates: usize,
    pub hydration_us: u64,
    pub moderation_us: u64,
    pub segments: usize,
}
fn rank_score(rank: &str) -> f32 {
    let descending = u32::from_str_radix(&rank[..8], 16).unwrap();
    let ordered = !descending;
    let bits = if ordered & 0x8000_0000 != 0 {
        ordered ^ 0x8000_0000
    } else {
        !ordered
    };
    f32::from_bits(bits)
}
fn key_term(field: Field, value: &str) -> Box<dyn Query> {
    Box::new(TermQuery::new(
        Term::from_field_text(field, value),
        IndexRecordOption::Basic,
    ))
}
fn all(queries: Vec<Box<dyn Query>>) -> Box<dyn Query> {
    if queries.is_empty() {
        Box::new(EmptyQuery)
    } else {
        Box::new(BooleanQuery::new(
            queries.into_iter().map(|q| (Occur::Must, q)).collect(),
        ))
    }
}
fn lexical(fields: Fields, plan: &Value, exact: bool) -> Result<Box<dyn Query>> {
    if exact {
        return Ok(plan["exact"]
            .as_str()
            .map(|s| key_term(fields.key, s))
            .unwrap_or(Box::new(EmptyQuery)));
    }
    let mut display = vec![];
    if let Some(groups) = plan["groups"].as_array() {
        for group in groups {
            let keys = group["keys"]
                .as_array()
                .ok_or_else(|| Error::Invalid("normalizer query groups missing keys".into()))?;
            display.push(all(keys
                .iter()
                .map(|key| key_term(fields.key, key.as_str().unwrap()))
                .collect()));
        }
    }
    let display = all(display);
    if plan["mode"] == "and" {
        return Ok(display);
    }
    let mut alternatives = vec![(Occur::Should, display)];
    for field in ["handle_key", "domain_key"] {
        if let Some(value) = plan[field].as_str() {
            alternatives.push((Occur::Should, key_term(fields.key, value)));
        }
    }
    Ok(Box::new(BooleanQuery::new(alternatives)))
}
fn make_query(
    fields: Fields,
    plan: &Value,
    exact: bool,
    pds: Option<&str>,
    preferred: Option<&str>,
    bucket: Option<bool>,
    policy_enabled: bool,
) -> Result<Box<dyn Query>> {
    let mut clauses = vec![(Occur::Must, lexical(fields, plan, exact)?)];
    if !policy_enabled {
        clauses.push((
            Occur::Must,
            Box::new(TermQuery::new(
                Term::from_field_u64(fields.visible, 1),
                IndexRecordOption::Basic,
            )) as Box<dyn Query>,
        ));
    }
    if !exact {
        if let Some(value) = plan["exact"].as_str() {
            clauses.push((Occur::MustNot, key_term(fields.key, value)));
        }
    }
    clauses.push((Occur::MustNot, key_term(fields.key, "e:handle.invalid")));
    for handle in QUARANTINED_HANDLES.iter() {
        clauses.push((Occur::MustNot, key_term(fields.key, &format!("e:{handle}"))));
    }
    if let Some(pds) = pds {
        clauses.push((Occur::Must, key_term(fields.pds, pds)));
    }
    if let (Some(local), Some(host)) = (bucket, preferred) {
        clauses.push((
            if local { Occur::Must } else { Occur::MustNot },
            key_term(fields.pds, host),
        ));
    }
    Ok(Box::new(BooleanQuery::new(clauses)))
}
fn policy_hash(value: &Value) -> Result<String> {
    let mut value = value.clone();
    value
        .as_object_mut()
        .ok_or_else(|| Error::Invalid("moderation snapshot must be an object".into()))?
        .remove("source_observed_at");
    json_hash(&value)
}
fn json_hash(value: &Value) -> Result<String> {
    Ok(format!("{:x}", Sha256::digest(serde_json::to_vec(value)?)))
}
fn check_format(index: &Index) -> Result<()> {
    let settings = index.settings();
    if !settings
        .sort_by_field
        .as_ref()
        .is_some_and(|s| s.field == "rank" && s.order.is_asc())
    {
        return Err(Error::Storage("rank-sorted index required".into()));
    }
    Ok(())
}
fn load_metadata(index: &Index) -> Result<Metadata> {
    let metas = index.load_metas()?;
    let metadata: Metadata = serde_json::from_str(
        metas
            .payload
            .as_deref()
            .ok_or_else(|| Error::Storage("index commit metadata missing".into()))?,
    )?;
    if metadata.format != FORMAT
        && metadata.format != SOURCE_FORMAT
        && metadata.format != POLICY_FORMAT
        && metadata.format != EXPIRY_FORMAT
    {
        return Err(Error::Storage("unsupported backend format".into()));
    }
    if (metadata.format == POLICY_FORMAT && metadata.policy_snapshot.is_none())
        || (metadata.policy_snapshot.is_some() && metadata.format != POLICY_FORMAT && metadata.format != EXPIRY_FORMAT)
        || (metadata.expiry_coverage.is_some() != (metadata.format == EXPIRY_FORMAT)) {
        return Err(Error::Storage(
            "moderation policy format is inconsistent".into(),
        ));
    }
    if metadata.source_observation_revision > 0 {
        let last = metadata
            .last_source_batch
            .as_ref()
            .ok_or_else(|| Error::Storage("source commit receipt missing".into()))?;
        if (metadata.format != SOURCE_FORMAT && metadata.format != POLICY_FORMAT && metadata.format != EXPIRY_FORMAT)
            || last.receipt.source_observation_revision != metadata.source_observation_revision
            || last.receipt.source_cursor != metadata.source_cursor
            || last.receipt.watermark != metadata.watermark
        {
            return Err(Error::Storage(
                "source commit metadata is inconsistent".into(),
            ));
        }
    } else if metadata.format == SOURCE_FORMAT
        || metadata.format == POLICY_FORMAT
        || metadata.last_source_batch.is_some()
        || metadata.policy_snapshot.is_some()
    {
        return Err(Error::Storage("invalid source observation metadata".into()));
    }
    Ok(metadata)
}
fn commit(writer: &mut IndexWriter, metadata: &mut Metadata) -> Result<u64> {
    let mut prepared = writer.prepare_commit()?;
    if let Some(batch) = metadata.last_source_batch.as_mut() {
        batch.receipt.opstamp = prepared.opstamp();
    }
    prepared.set_payload(&serde_json::to_string(metadata)?);
    prepared.commit().map_err(Error::from)
}
fn failpoint(name: &str) {
    if std::env::var("TYPEAHEAD_TEST_CRASH").ok().as_deref() == Some(name) {
        std::process::abort();
    }
}
fn line<R: BufRead>(reader: &mut R) -> Result<Option<Vec<u8>>> {
    let mut output = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Ok(if output.is_empty() {
                None
            } else {
                Some(output)
            });
        }
        let take = available
            .iter()
            .position(|b| *b == b'\n')
            .map_or(available.len(), |i| i + 1);
        if output.len() + take > 4 * 1024 * 1024 {
            return Err(Error::Invalid("input line exceeds 4MiB".into()));
        }
        output.extend_from_slice(&available[..take]);
        let done = output.last() == Some(&b'\n');
        reader.consume(take);
        if done {
            return Ok(Some(output));
        }
    }
}
#[derive(Clone, Copy, Debug)]
pub struct ImportOptions {
    pub writer_memory: usize,
    pub batch_size: usize,
    pub indexing_threads: usize,
    pub final_segments: usize,
}
impl Default for ImportOptions {
    fn default() -> Self {
        Self {
            writer_memory: 128 * 1024 * 1024,
            batch_size: 50000,
            indexing_threads: 1,
            final_segments: 1,
        }
    }
}
impl ImportOptions {
    fn validate(self) -> Result<()> {
        if !(1..=8).contains(&self.indexing_threads)
            || !(1..=32).contains(&self.final_segments)
            || !(1..=1_000_000).contains(&self.batch_size)
            || self.writer_memory / self.indexing_threads < 15 * 1024 * 1024
            || self.writer_memory > 4 * 1024 * 1024 * 1024
        {
            return Err(Error::Invalid("invalid bounded bulk import settings".into()));
        }
        Ok(())
    }
}

pub fn import<R: Read>(
    data: &Path,
    input: R,
    override_build: Option<&str>,
    options: ImportOptions,
    allow_partial: bool,
) -> Result<Metadata> {
    options.validate()?;
    if data.join("index/meta.json").exists() {
        return Err(Error::Invalid("target already contains an index".into()));
    }
    fs::create_dir_all(data.join("index"))?;
    let (schema, fields) = Fields::schema();
    let mut settings = IndexSettings::default();
    settings.sort_by_field = Some(IndexSortByField {
        field: "rank".into(),
        order: Order::Asc,
    });
    let index = Index::builder()
        .schema(schema)
        .settings(settings)
        .create_in_dir(data.join("index"))?;
    let mut writer = index.writer_with_num_threads(options.indexing_threads, options.writer_memory)?;
    writer.set_merge_policy(Box::new(NoMergePolicy));
    let mut metadata = Metadata::empty();
    commit(&mut writer, &mut metadata)?;
    let mut reader = BufReader::new(input);
    let header = line(&mut reader)?.ok_or_else(|| Error::Invalid("manifest missing".into()))?;
    let manifest: Value = serde_json::from_slice(&header)?;
    if manifest["type"] != "manifest" {
        return Err(Error::Invalid("manifest must be first record".into()));
    }
    metadata.source_build_id = manifest["build_id"]
        .as_str()
        .ok_or_else(|| Error::Invalid("manifest build_id missing".into()))?
        .into();
    metadata.build_id = override_build.unwrap_or(&metadata.source_build_id).into();
    metadata.source_manifest = manifest.clone();
    metadata.snapshot_watermark = manifest["source_watermark"]
        .as_u64()
        .ok_or_else(|| Error::Invalid("source_watermark missing".into()))?;
    metadata.watermark = metadata.snapshot_watermark;
    let expected_count = manifest["actor_count"]
        .as_u64()
        .ok_or_else(|| Error::Invalid("manifest actor_count missing".into()))?;
    metadata.full_source = manifest["partial"] == false
        && manifest["source_actor_count"].as_u64() == Some(expected_count);
    if !metadata.full_source && !allow_partial {
        return Err(Error::Invalid(
            "full-source manifest required; fixtures need explicit --allow-partial true".into(),
        ));
    }
    metadata.expiry_coverage = Some(expiry_coverage::Evidence::new(expected_count, manifest["source_actor_count"].as_u64(), metadata.full_source)?);
    metadata.format = EXPIRY_FORMAT.into();
    commit(&mut writer, &mut metadata)?;
    let mut digest = Sha256::new();
    let mut previous = String::new();
    let started = Instant::now();
    let mut footer_seen = false;
    while let Some(raw) = line(&mut reader)? {
        let value: Value = serde_json::from_slice(&raw)?;
        if value["type"] == "end" {
            let hash = format!("{:x}", digest.finalize());
            if value["actor_count"].as_u64() != Some(metadata.input_actors)
                || metadata.input_actors != expected_count
                || value["build_id"].as_str() != Some(&metadata.source_build_id)
                || value["actors_sha256"].as_str() != Some(&hash)
            {
                return Err(Error::Invalid("input footer/count/hash mismatch".into()));
            }
            metadata.expiry_coverage.as_mut().unwrap().verified_input_actors_sha256 = Some(hash);
            footer_seen = true;
            break;
        }
        digest.update(&raw);
        let actor: Actor = serde_json::from_value(value)?;
        let actor = actor.validate()?;
        if !previous.is_empty() && actor.did <= previous {
            return Err(Error::Invalid(
                "initial actors must have strictly ascending unique DIDs".into(),
            ));
        }
        previous = actor.did.clone();
        if actor.version != metadata.snapshot_watermark {
            return Err(Error::Invalid(
                "initial actor version differs from snapshot watermark".into(),
            ));
        }
        let (document, keys) = fields.document(&actor).map_err(|error|{
            eprintln!("{}",json!({"event":"import_actor_rejected","accepted_actors":metadata.input_actors,"error_class":error.to_string(),"handle_bytes":actor.handle.len(),"display_bytes":actor.display_name.len(),"avatar_bytes":actor.avatar_url.len(),"pds_bytes":actor.pds.len()}));
            error
        })?;
        metadata.expiry_coverage.as_mut().unwrap().import_actor(&actor)?;
        writer.add_document(document)?;
        metadata.input_actors += 1;
        metadata.indexed_memberships += keys as u64;
        if actor.deleted {
            metadata.tombstone_count += 1;
        } else {
            metadata.actor_count += 1;
            if actor.pds.is_empty() {
                metadata.pds_missing_actor_count += 1;
            } else if let Some(reason) = actor.invalid_pds_reason() {
                metadata.pds_invalid_actor_count += 1;
                *metadata
                    .pds_invalid_reason_counts
                    .entry(reason.into())
                    .or_default() += 1;
            }
        }
        if actor.visible() {
            metadata.searchable_actor_count += 1;
        }
        if metadata.input_actors % options.batch_size as u64 == 0 {
            metadata.last_commit_epoch = epoch();
            commit(&mut writer, &mut metadata)?;
            eprintln!(
                "{}",
                json!({"event":"import_progress","actors":metadata.input_actors,"elapsed_s":started.elapsed().as_secs(),"invalid_source_pds":metadata.pds_invalid_actor_count,"missing_source_pds":metadata.pds_missing_actor_count,"invalid_source_pds_reasons":metadata.pds_invalid_reason_counts,"expiry_actors_checked":metadata.expiry_coverage.as_ref().map(|value|value.input_actors_checked),"unsupported_expiry_actors":metadata.expiry_coverage.as_ref().map(|value|value.current.actors_with_unsupported_expiry),"malformed_label_actors":metadata.expiry_coverage.as_ref().map(|value|value.current.actors_with_malformed_labels)})
            );
        }
    }
    if !footer_seen {
        return Err(Error::Invalid("input ended without verified footer".into()));
    }
    if line(&mut reader)?.is_some() {
        return Err(Error::Invalid("input data follows footer".into()));
    }
    commit(&mut writer, &mut metadata)?;
    failpoint("import_after_footer");
    eprintln!("{}", json!({"event":"import_indexed", "actors":metadata.input_actors,
        "elapsed_s":started.elapsed().as_secs_f64(), "indexing_threads":options.indexing_threads,
        "writer_memory_bytes":options.writer_memory, "batch_size":options.batch_size}));
    finish_import(index, writer, metadata, options.final_segments)
}

pub fn resume_import(data: &Path, receipt: &Value, options: ImportOptions) -> Result<Metadata> {
    options.validate()?;
    let index = Index::open_in_dir(data.join("index"))?;
    check_format(&index)?;
    let mut writer = index.writer_with_num_threads(options.indexing_threads, options.writer_memory)?;
    writer.set_merge_policy(Box::new(NoMergePolicy));
    let metadata = load_metadata(&index)?;
    let evidence = metadata.expiry_coverage.as_ref()
        .ok_or_else(|| Error::Invalid("verified import footer required".into()))?;
    let expected_hash = receipt["actors_sha256"].as_str()
        .ok_or_else(|| Error::Invalid("stream receipt hash required".into()))?;
    let reader: IndexReader = index.reader()?;
    if !metadata.full_source
        || metadata.source_manifest != receipt["source_manifest"]
        || receipt["input_documents"].as_u64() != Some(metadata.input_actors)
        || metadata.source_manifest["actor_count"].as_u64() != Some(metadata.input_actors)
        || metadata.build_id != metadata.source_build_id
        || metadata.watermark != metadata.snapshot_watermark
        || !metadata.source_cursor.is_null()
        || metadata.source_observation_revision != 0
        || metadata.last_source_batch.is_some()
        || evidence.verified_input_actors_sha256.as_deref() != Some(expected_hash)
        || evidence.input_actors_checked != metadata.input_actors
        || expected_hash.len() != 64
        || !expected_hash.bytes().all(|b| b.is_ascii_hexdigit())
        || metadata.actor_count.checked_add(metadata.tombstone_count) != Some(metadata.input_actors)
        || reader.searcher().num_docs() != metadata.input_actors
    {
        return Err(Error::Invalid("saved import differs from verified stream receipt".into()));
    }
    if metadata.import_complete {
        return Ok(metadata);
    }
    finish_import(index, writer, metadata, options.final_segments)
}

fn finish_import(index: Index, mut writer: IndexWriter, mut metadata: Metadata, final_segments: usize) -> Result<Metadata> {
    let started = Instant::now();
    let segments = index.searchable_segment_ids()?;
    let before = segments.len();
    if before > final_segments {
        eprintln!("{}", json!({"event":"import_merge", "segments":before, "target_segments":final_segments}));
        let group_size = before.div_ceil(final_segments);
        let merges = segments.chunks(group_size)
            .filter(|group| group.len() > 1)
            .map(|group| writer.merge(group))
            .collect::<Vec<_>>();
        for merge in merges {
            merge.wait()?;
        }
    }
    let after = index.searchable_segment_ids()?.len();
    if after > final_segments {
        return Err(Error::Invalid("bulk import segment bound was not reached".into()));
    }
    eprintln!("{}", json!({"event":"import_merge_finished", "segments_before":before,
        "segments_after":after, "elapsed_s":started.elapsed().as_secs_f64()}));
    metadata.expiry_coverage.as_mut().unwrap().import_complete = true;
    metadata.import_complete = true;
    metadata.last_commit_epoch = epoch();
    commit(&mut writer, &mut metadata)?;
    writer.garbage_collect_files().wait()?;
    writer.wait_merging_threads()?;
    Ok(metadata)
}

#[cfg(test)]
mod rank_merge_tests;
