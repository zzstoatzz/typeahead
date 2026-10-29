use crate::{Error, Result, actor::Actor};
use regex::RegexSet;
use serde::Deserialize;
use serde_json::Value;
use std::{collections::BTreeMap, sync::OnceLock};
use time::{Date, Month, OffsetDateTime, format_description::well_known::Rfc3339};

#[derive(Deserialize)]
struct RulesArtifact {
    schema_version: u64,
    canonical_source_sha256: String,
    explicit_slur_regexes: Vec<String>,
    hide_values: Vec<String>,
    labeler_did: String,
}
struct Rules {
    regexes: RegexSet,
    artifact: RulesArtifact,
}
static RULES: OnceLock<std::result::Result<Rules, String>> = OnceLock::new();

fn rules() -> Result<&'static Rules> {
    RULES
        .get_or_init(|| {
            let artifact: RulesArtifact = serde_json::from_str(include_str!(concat!(
                env!("OUT_DIR"),
                "/moderation-rules.json"
            )))
            .map_err(|_| "invalid canonical moderation artifact".to_owned())?;
            if artifact.schema_version != 1
                || artifact.canonical_source_sha256.len() != 64
                || artifact.explicit_slur_regexes.is_empty()
                || artifact.explicit_slur_regexes.len() > 64
                || artifact.hide_values.is_empty()
                || !artifact.labeler_did.starts_with("did:")
            {
                return Err("invalid canonical moderation artifact shape".into());
            }
            let patterns: Vec<_> = artifact
                .explicit_slur_regexes
                .iter()
                .map(|source| source.replace(r"\b", r"(?-u:\b)"))
                .collect();
            let regexes = RegexSet::new(patterns)
                .map_err(|_| "unsupported canonical moderation regex".to_owned())?;
            Ok(Rules { regexes, artifact })
        })
        .as_ref()
        .map_err(|message| Error::Storage(message.clone()))
}

#[derive(Clone, Deserialize)]
struct Snapshot {
    schema_version: u64,
    source_observed_at: u64,
    complete: bool,
    overrides: Vec<Override>,
    blocked_domains: Vec<String>,
}
#[derive(Clone, Deserialize)]
struct Override {
    did: String,
    action: String,
}
#[derive(Clone)]
pub struct ModerationPolicy {
    raw: Value,
    observed_at: u64,
    overrides: BTreeMap<String, bool>,
    domains: Vec<String>,
}
impl ModerationPolicy {
    pub fn validate(snapshot: Value) -> Result<Self> {
        rules()?;
        let parsed: Snapshot = serde_json::from_value(snapshot.clone())?;
        if parsed.schema_version != 1
            || !parsed.complete
            || parsed.source_observed_at == 0
            || parsed.overrides.len() > 10_000
            || parsed.blocked_domains.len() > 10_000
        {
            return Err(Error::Invalid(
                "incomplete or unsupported moderation snapshot".into(),
            ));
        }
        let mut overrides = BTreeMap::new();
        for entry in parsed.overrides {
            if !entry.did.starts_with("did:")
                || entry.did.len() > 2048
                || !matches!(entry.action.as_str(), "show" | "hide")
            {
                return Err(Error::Invalid("invalid moderation override".into()));
            }
            if overrides
                .insert(entry.did, entry.action == "hide")
                .is_some()
            {
                return Err(Error::Invalid("duplicate moderation override".into()));
            }
        }
        let mut domains = Vec::new();
        for domain in parsed.blocked_domains {
            if domain.len() > 65535 {
                return Err(Error::Invalid("moderation domain exceeds budget".into()));
            }
            domains.push(domain.to_lowercase());
        }
        domains.sort();
        domains.dedup();
        Ok(Self {
            raw: snapshot,
            observed_at: parsed.source_observed_at,
            overrides,
            domains,
        })
    }
    pub fn snapshot(&self) -> &Value {
        &self.raw
    }
    pub fn source_observed_at(&self) -> u64 {
        self.observed_at
    }
    pub fn rules_source_sha256() -> Result<&'static str> {
        Ok(&rules()?.artifact.canonical_source_sha256)
    }
    pub fn hidden(&self, actor: &Actor, now_epoch: u64) -> Result<bool> {
        self.hidden_at_ms(
            actor,
            now_epoch
                .checked_mul(1000)
                .ok_or_else(|| Error::Invalid("moderation clock overflow".into()))?,
        )
    }
    pub fn hidden_at_ms(&self, actor: &Actor, now_millis: u64) -> Result<bool> {
        let labels_hide = should_hide_at_ms(&actor.labels, now_millis)?;
        let mut hidden =
            labels_hide || has_explicit_slur(&actor.handle)? || self.domain_blocked(&actor.handle);
        if let Some(value) = self.overrides.get(&actor.did) {
            hidden = *value;
        }
        Ok(hidden)
    }
    pub fn domain_blocked(&self, handle: &str) -> bool {
        if handle.is_empty() {
            return false;
        }
        let handle = handle.to_lowercase();
        if self.domains.binary_search(&handle).is_ok() {
            return true;
        }
        handle.match_indices('.').any(|(offset, _)| {
            self.domains
                .binary_search_by(|domain| domain.as_str().cmp(&handle[offset + 1..]))
                .is_ok()
        })
    }
}

pub fn has_explicit_slur(handle: &str) -> Result<bool> {
    let rules = rules()?;
    if rules.regexes.is_match(handle) {
        return Ok(true);
    }
    let stripped: String = handle
        .chars()
        .filter(|c| !matches!(c, '.' | '-' | '_'))
        .collect();
    Ok(rules.regexes.is_match(&stripped))
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64().is_some_and(|n| n != 0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(_) | Value::Object(_) => true,
    }
}

pub fn should_hide(labels: &Value, now_epoch: u64) -> Result<bool> {
    should_hide_at_ms(
        labels,
        now_epoch
            .checked_mul(1000)
            .ok_or_else(|| Error::Invalid("moderation clock overflow".into()))?,
    )
}
pub fn should_hide_at_ms(labels: &Value, now_millis: u64) -> Result<bool> {
    let labels = labels
        .as_array()
        .ok_or_else(|| Error::Invalid("malformed moderation labels".into()))?;
    let rules = rules()?;
    let now_ms = i128::from(now_millis);
    if now_ms > 8_640_000_000_000_000_i128 {
        return Err(Error::Invalid(
            "moderation clock outside JS Date range".into(),
        ));
    }
    for label in labels {
        if label.is_null() {
            return Err(Error::Invalid("malformed moderation labels".into()));
        }
        if label.get("neg").is_some_and(truthy) {
            continue;
        }
        if let Some(expiry) = label.get("exp").filter(|v| truthy(v)) {
            if expiry_ms(expiry)? <= now_ms {
                continue;
            }
        }
        if label.get("src").and_then(Value::as_str) != Some(rules.artifact.labeler_did.as_str())
            || !label
                .get("val")
                .and_then(Value::as_str)
                .is_some_and(|v| rules.artifact.hide_values.iter().any(|hide| hide == v))
        {
            continue;
        }
        return Ok(true);
    }
    Ok(false)
}

fn expiry_ms(value: &Value) -> Result<i128> {
    let unsupported =
        || Error::Invalid("unsupported moderation expiry: JS Date coverage required".into());
    if let Some(number) = value.as_f64() {
        if number.abs() > 8_640_000_000_000_000_f64 {
            return Err(unsupported());
        }
        return Ok(number.trunc() as i128);
    }
    if let Value::Bool(boolean) = value {
        return Ok(i128::from(*boolean));
    }
    let value = value.as_str().ok_or_else(unsupported)?;
    let bytes = value.as_bytes();
    if bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(i, b)| i == 4 || i == 7 || b.is_ascii_digit())
    {
        let year = value[..4].parse::<i32>().map_err(|_| unsupported())?;
        let month = value[5..7].parse::<u8>().map_err(|_| unsupported())?;
        let day = value[8..].parse::<u8>().map_err(|_| unsupported())?;
        let date = Date::from_calendar_date(
            year,
            Month::try_from(month).map_err(|_| unsupported())?,
            day,
        )
        .map_err(|_| unsupported())?;
        return Ok(date
            .midnight()
            .assume_utc()
            .unix_timestamp_nanos()
            .div_euclid(1_000_000));
    }
    if bytes.len() < 20
        || !bytes[..4].iter().all(u8::is_ascii_digit)
        || bytes[10] != b'T' && bytes[10] != b't'
        || &bytes[17..19] == b"60"
    {
        return Err(unsupported());
    }
    let parsed = OffsetDateTime::parse(value, &Rfc3339).map_err(|_| unsupported())?;
    Ok(parsed.unix_timestamp_nanos().div_euclid(1_000_000))
}

#[derive(Default, serde::Serialize)]
pub struct ExpiryCoverage {
    pub nonnegative_labels: u64,
    pub candidate_hide_labels: u64,
    pub without_expiry: u64,
    pub supported_expiry: u64,
    pub unsupported_expiry: u64,
}
pub fn expiry_coverage(labels: &Value) -> Result<ExpiryCoverage> {
    let rules = rules()?;
    let labels = labels
        .as_array()
        .ok_or_else(|| Error::Invalid("malformed moderation labels".into()))?;
    let mut coverage = ExpiryCoverage::default();
    for label in labels {
        if label.is_null() {
            return Err(Error::Invalid("malformed moderation labels".into()));
        }
        if label.get("neg").is_some_and(truthy) {
            continue;
        }
        coverage.nonnegative_labels += 1;
        if label.get("src").and_then(Value::as_str) == Some(rules.artifact.labeler_did.as_str())
            && label
                .get("val")
                .and_then(Value::as_str)
                .is_some_and(|v| rules.artifact.hide_values.iter().any(|hide| hide == v))
        {
            coverage.candidate_hide_labels += 1;
        }
        match label.get("exp").filter(|v| truthy(v)) {
            None => coverage.without_expiry += 1,
            Some(value) => {
                if expiry_ms(value).is_ok() {
                    coverage.supported_expiry += 1;
                } else {
                    coverage.unsupported_expiry += 1;
                }
            }
        }
    }
    Ok(coverage)
}
