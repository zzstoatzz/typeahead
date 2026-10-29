use super::Engine;
use crate::{Error, Result};
use serde_json::{Value, json};
use std::collections::BTreeSet;

impl Engine {
    pub fn source_page(&self, after_did: &str, limit: usize) -> Result<Value> {
        if !(1..=500).contains(&limit) || after_did.len() > 2048 {
            return Err(Error::Invalid(
                "source page requires limit 1..500 and bounded cursor".into(),
            ));
        }
        let generation = self.snapshot();
        if generation.searcher.segment_readers().len() > self.max_segments {
            return Err(Error::Budget);
        }
        let mut dids = BTreeSet::new();
        for segment in generation.searcher.segment_readers() {
            let inverted = segment.inverted_index(self.fields.did)?;
            let mut terms = inverted
                .terms()
                .range()
                .gt(after_did.as_bytes())
                .into_stream()?;
            for _ in 0..limit {
                if !terms.advance() {
                    break;
                }
                let did = std::str::from_utf8(terms.key())
                    .map_err(|_| Error::Storage("invalid UTF-8 DID term".into()))?;
                dids.insert(did.to_owned());
                if dids.len() > limit {
                    dids.pop_last();
                }
            }
        }
        Ok(json!({
            "dids": dids.into_iter().collect::<Vec<_>>(),
            "source_cursor": generation.metadata.source_cursor,
            "source_observation_revision": generation.metadata.source_observation_revision,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::{Fields, Generation, Metadata, Writer};
    use std::sync::{Arc, Mutex, RwLock, atomic::AtomicBool};
    use tantivy::{Index, ReloadPolicy, Term, doc, merge_policy::NoMergePolicy};

    fn fixture() -> Engine {
        let (schema, fields) = Fields::schema();
        let index = Index::create_in_ram(schema);
        let mut writer = index.writer_with_num_threads(1, 50_000_000).unwrap();
        writer.set_merge_policy(Box::new(NoMergePolicy));
        for range in [300..650, 0..350] {
            for n in range.rev() {
                writer
                    .add_document(doc!(fields.did => format!("did:fixture:{n:04}")))
                    .unwrap();
            }
            writer.commit().unwrap();
        }
        writer.delete_term(Term::from_field_text(fields.did, "did:fixture:0001"));
        writer.commit().unwrap();
        let reader = index
            .reader_builder()
            .reload_policy(ReloadPolicy::Manual)
            .try_into()
            .unwrap();
        let mut metadata = Metadata::empty();
        metadata.source_cursor = json!({"through": 123});
        metadata.source_observation_revision = 7;
        let generation = Arc::new(Generation {
            searcher: reader.searcher(),
            metadata,
            policy: None,
            policy_sha256: None,
        });
        Engine {
            fields,
            writer: Mutex::new(Writer { writer, reader }),
            generation: RwLock::new(generation),
            max_candidates: 10000,
            retrieval: crate::engine::RetrievalStrategy::PerSegment,
            max_segments: 256,
            allow_partial: true,
            allow_stale: true,
            max_sync_lag_s: 1800,
            healthy: AtomicBool::new(true),
        }
    }

    #[test]
    fn lexical_pagination_covers_current_and_deleted_term_superset() {
        let engine = fixture();
        let mut all = Vec::new();
        let mut after = String::new();
        loop {
            let page = engine.source_page(&after, 73).unwrap();
            assert_eq!(page["source_cursor"], json!({"through":123}));
            assert_eq!(page["source_observation_revision"], 7);
            let ids: Vec<String> = serde_json::from_value(page["dids"].clone()).unwrap();
            assert!(ids.len() <= 73);
            assert!(ids.windows(2).all(|w| w[0] < w[1]));
            assert!(ids.iter().all(|did| did > &after));
            let Some(last) = ids.last() else { break };
            after = last.clone();
            all.extend(ids);
        }
        for n in 0..650 {
            if n != 1 {
                assert!(all.binary_search(&format!("did:fixture:{n:04}")).is_ok());
            }
        }
        assert_eq!(all.len(), all.iter().collect::<BTreeSet<_>>().len());
        assert!(
            engine.source_page("did:fixture:9999", 500).unwrap()["dids"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn caps_and_strict_lower_bound() {
        let mut engine = fixture();
        assert!(engine.source_page("", 0).is_err());
        assert!(engine.source_page("", 501).is_err());
        assert!(engine.source_page(&"x".repeat(2049), 1).is_err());
        let page = engine.source_page("did:fixture:0325", 500).unwrap();
        assert_eq!(page["dids"][0], "did:fixture:0326");
        assert_eq!(page["dids"].as_array().unwrap().len(), 324);
        engine.max_segments = 0;
        assert!(matches!(engine.source_page("", 1), Err(Error::Budget)));
    }
}
