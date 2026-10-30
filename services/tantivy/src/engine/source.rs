use super::*;
use std::collections::BTreeSet;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceBatchReceipt {
    pub batch_id: String,
    pub request_sha256: String,
    pub receipt: ApplyReceipt,
}

#[derive(Serialize, Deserialize)]
pub struct ExpectedDocument {
    pub did: String,
    pub document_sha256: Option<String>,
}

#[derive(Serialize, Deserialize)]
pub struct SourceReconcile {
    pub actors: Vec<Actor>,
    pub expected_documents: Vec<ExpectedDocument>,
    pub source_cursor: Value,
    pub expected_source_cursor: Value,
    pub expected_observation_revision: u64,
    pub batch_id: String,
    pub watermark: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub policy_snapshot: Option<Value>,
}

pub(super) struct Authority {
    pub batch_id: String,
    pub request_sha256: String,
    expected_documents: Vec<ExpectedDocument>,
    expected_observation_revision: u64,
    pub policy: Option<Arc<crate::moderation::ModerationPolicy>>,
}

fn valid_did(did: &str) -> bool {
    did.starts_with("did:") && did.len() <= 2048 && !did.bytes().any(|b| b <= 0x20 || b == b'|')
}

fn digest(actor: &Actor) -> Result<String> {
    Ok(format!("{:x}", Sha256::digest(serde_json::to_vec(actor)?)))
}

impl Engine {
    pub fn source_state(&self, dids: Vec<String>) -> Result<Value> {
        if dids.len() > 500
            || dids.iter().any(|d| !valid_did(d))
            || dids.iter().collect::<BTreeSet<_>>().len() != dids.len()
        {
            return Err(Error::Invalid(
                "source state requires at most 500 unique DIDs".into(),
            ));
        }
        let generation = self.snapshot();
        let mut actors = Vec::with_capacity(dids.len());
        for did in dids {
            let old = self.fields.find(&generation.searcher, &did)?;
            let document_sha256 = old.as_ref().map(digest).transpose()?;
            actors.push(json!({"did":did,"document_sha256":document_sha256}));
        }
        Ok(json!({"source_cursor":generation.metadata.source_cursor,
            "source_observation_revision":generation.metadata.source_observation_revision,
            "actors":actors}))
    }

    pub fn reconcile_source(&self, request: SourceReconcile) -> Result<ApplyReceipt> {
        if request.actors.len() > 500
            || request.batch_id.len() != 64
            || !request
                .batch_id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(Error::Invalid("invalid source batch size or ID".into()));
        }
        let dids: BTreeSet<_> = request.actors.iter().map(|a| a.did.as_str()).collect();
        let expected: BTreeSet<_> = request
            .expected_documents
            .iter()
            .map(|a| a.did.as_str())
            .collect();
        if dids.len() != request.actors.len()
            || expected.len() != request.expected_documents.len()
            || dids != expected
            || !request.source_cursor.is_object()
        {
            return Err(Error::Invalid(
                "source expectations must cover each unique actor".into(),
            ));
        }
        let request_sha256 = format!("{:x}", Sha256::digest(serde_json::to_vec(&request)?));
        let policy = request
            .policy_snapshot
            .map(crate::moderation::ModerationPolicy::validate)
            .transpose()?
            .map(Arc::new);
        if policy
            .as_ref()
            .is_some_and(|p| p.source_observed_at() > epoch().saturating_add(300))
        {
            return Err(Error::Invalid(
                "moderation source clock is in the future".into(),
            ));
        }
        let authority = Authority {
            batch_id: request.batch_id,
            request_sha256,
            expected_documents: request.expected_documents,
            expected_observation_revision: request.expected_observation_revision,
            policy,
        };
        self.apply_batch(
            request.actors,
            request.watermark,
            Some(request.source_cursor),
            Some(request.expected_source_cursor),
            Some(authority),
        )
    }

    pub(super) fn check_source_authority(
        &self,
        generation: &Generation,
        authority: &Authority,
    ) -> Result<Option<ApplyReceipt>> {
        if let Some(last) = &generation.metadata.last_source_batch {
            if last.batch_id == authority.batch_id {
                if last.request_sha256 != authority.request_sha256 {
                    return Err(Error::Conflict(
                        "source batch ID reused with different content".into(),
                    ));
                }
                return Ok(Some(last.receipt.clone()));
            }
        }
        if authority.expected_observation_revision
            != generation.metadata.source_observation_revision
            || authority.expected_observation_revision == u64::MAX
        {
            return Err(Error::Conflict(
                "source observation compare-and-set failed".into(),
            ));
        }
        for expected in &authority.expected_documents {
            let actual = self.fields.find(&generation.searcher, &expected.did)?;
            if actual.as_ref().map(digest).transpose()? != expected.document_sha256 {
                return Err(Error::Conflict(
                    "source document compare-and-set failed".into(),
                ));
            }
        }
        Ok(None)
    }
}
