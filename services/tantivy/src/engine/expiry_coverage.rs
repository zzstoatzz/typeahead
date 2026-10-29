use crate::{Error, Result, actor::Actor, moderation};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::sync::LazyLock;

fn classifier_sha256() -> &'static str {
    static HASH: LazyLock<String> = LazyLock::new(|| {
        let mut hash = Sha256::new();
        hash.update(include_bytes!("../moderation.rs"));
        hash.update(include_bytes!("expiry_coverage.rs"));
        format!("{:x}", hash.finalize())
    });
    &HASH
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Counts {
    pub live_actors: u64,
    pub actors_with_unsupported_expiry: u64,
    pub actors_with_malformed_labels: u64,
    pub nonnegative_labels: u64,
    pub candidate_hide_labels: u64,
    pub without_expiry: u64,
    pub supported_expiry: u64,
    pub unsupported_expiry: u64,
}

impl Counts {
    pub fn actor(actor: &Actor) -> Self {
        if actor.deleted {
            return Self::default();
        }
        match moderation::expiry_coverage(&actor.labels) {
            Ok(value) => Self {
                live_actors: 1,
                actors_with_unsupported_expiry: u64::from(value.unsupported_expiry > 0),
                actors_with_malformed_labels: 0,
                nonnegative_labels: value.nonnegative_labels,
                candidate_hide_labels: value.candidate_hide_labels,
                without_expiry: value.without_expiry,
                supported_expiry: value.supported_expiry,
                unsupported_expiry: value.unsupported_expiry,
            },
            Err(_) => Self {
                live_actors: 1,
                actors_with_malformed_labels: 1,
                ..Self::default()
            },
        }
    }

    fn replace(&mut self, old: &Self, new: &Self) -> Result<()> {
        macro_rules! update {
            ($($field:ident),+) => {$(self.$field = self.$field.checked_sub(old.$field)
                .and_then(|value| value.checked_add(new.$field))
                .ok_or_else(|| Error::Storage("expiry coverage counter mismatch".into()))?;)+};
        }
        update!(
            live_actors,
            actors_with_unsupported_expiry,
            actors_with_malformed_labels,
            nonnegative_labels,
            candidate_hide_labels,
            without_expiry,
            supported_expiry,
            unsupported_expiry
        );
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Evidence {
    pub schema_version: u64,
    pub canonical_rules_source_sha256: String,
    pub classifier_sha256: String,
    pub expected_input_actors: u64,
    pub source_actor_count: Option<u64>,
    pub input_actors_checked: u64,
    pub full_source: bool,
    pub import_complete: bool,
    pub verified_input_actors_sha256: Option<String>,
    pub imported: Counts,
    pub current: Counts,
    pub source_document_changes: u64,
}

impl Evidence {
    pub fn new(
        expected_input_actors: u64,
        source_actor_count: Option<u64>,
        full_source: bool,
    ) -> Result<Self> {
        Ok(Self {
            schema_version: 1,
            canonical_rules_source_sha256: moderation::ModerationPolicy::rules_source_sha256()?
                .into(),
            classifier_sha256: classifier_sha256().into(),
            expected_input_actors,
            source_actor_count,
            input_actors_checked: 0,
            full_source,
            import_complete: false,
            verified_input_actors_sha256: None,
            imported: Counts::default(),
            current: Counts::default(),
            source_document_changes: 0,
        })
    }

    pub fn import_actor(&mut self, actor: &Actor) -> Result<()> {
        let counts = Counts::actor(actor);
        self.imported.replace(&Counts::default(), &counts)?;
        self.current.replace(&Counts::default(), &counts)?;
        self.input_actors_checked = self
            .input_actors_checked
            .checked_add(1)
            .ok_or_else(|| Error::Storage("expiry actor count overflow".into()))?;
        Ok(())
    }

    pub fn replace_actor(&mut self, old: Option<&Actor>, new: &Actor) -> Result<()> {
        if moderation::ModerationPolicy::rules_source_sha256()?
            != self.canonical_rules_source_sha256
            || self.classifier_sha256 != classifier_sha256()
        {
            return Err(Error::Conflict(
                "expiry coverage rules changed; bounded audit required".into(),
            ));
        }
        let previous = old.map(Counts::actor).unwrap_or_default();
        self.current.replace(&previous, &Counts::actor(new))?;
        self.source_document_changes = self
            .source_document_changes
            .checked_add(1)
            .ok_or_else(|| Error::Storage("expiry observation count overflow".into()))?;
        Ok(())
    }

    pub fn ready(&self, input_actors: u64, live_actors: u64, full_source: bool) -> bool {
        self.schema_version == 1
            && self.import_complete
            && self.classifier_sha256 == classifier_sha256()
            && moderation::ModerationPolicy::rules_source_sha256()
                .is_ok_and(|value| value == self.canonical_rules_source_sha256)
            && self.input_actors_checked == self.expected_input_actors
            && input_actors == self.input_actors_checked
            && self.full_source == full_source
            && (!full_source || self.source_actor_count == Some(self.expected_input_actors))
            && self
                .verified_input_actors_sha256
                .as_ref()
                .is_some_and(|value| {
                    value.len() == 64
                        && value
                            .bytes()
                            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                })
            && self.current.live_actors == live_actors
            && self.current.actors_with_unsupported_expiry == 0
            && self.current.actors_with_malformed_labels == 0
    }
}
