use super::*;

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct CorpusCounts {
    pub with_handles: u64,
    pub with_avatars: u64,
    pub hidden: u64,
    pub handle_invalid: u64,
    pub quarantined: u64,
}

pub(super) fn deserialize_counts<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Option<CorpusCounts>, D::Error> {
    let value = Value::deserialize(deserializer)?;
    Ok(serde_json::from_value(value).ok())
}

impl CorpusCounts {
    pub(super) fn valid(self, actors: u64) -> bool {
        [
            self.with_handles,
            self.with_avatars,
            self.hidden,
            self.handle_invalid,
            self.quarantined,
        ]
        .iter()
        .all(|n| *n <= actors)
    }
    fn contribution(actor: &Actor) -> Self {
        if actor.deleted {
            return Self::default();
        }
        Self {
            with_handles: u64::from(!actor.handle.is_empty()),
            with_avatars: u64::from(!actor.avatar_url.is_empty()),
            hidden: u64::from(actor.hidden),
            handle_invalid: u64::from(actor.handle.eq_ignore_ascii_case("handle.invalid")),
            quarantined: u64::from(
                QUARANTINED_HANDLES
                    .iter()
                    .any(|h| h.eq_ignore_ascii_case(&actor.handle)),
            ),
        }
    }

    pub(super) fn replace(self, old: Option<&Actor>, new: &Actor) -> Option<Self> {
        let old = old.map(Self::contribution).unwrap_or_default();
        let new = Self::contribution(new);
        Some(Self {
            with_handles: self
                .with_handles
                .checked_sub(old.with_handles)?
                .checked_add(new.with_handles)?,
            with_avatars: self
                .with_avatars
                .checked_sub(old.with_avatars)?
                .checked_add(new.with_avatars)?,
            hidden: self
                .hidden
                .checked_sub(old.hidden)?
                .checked_add(new.hidden)?,
            handle_invalid: self
                .handle_invalid
                .checked_sub(old.handle_invalid)?
                .checked_add(new.handle_invalid)?,
            quarantined: self
                .quarantined
                .checked_sub(old.quarantined)?
                .checked_add(new.quarantined)?,
        })
    }
}

impl Engine {
    pub fn initialize_statistics_offline(&self) -> Result<()> {
        let mut writer = self
            .writer
            .lock()
            .map_err(|_| Error::Storage("writer lock poisoned".into()))?;
        writer
            .writer
            .set_merge_policy(Box::new(tantivy::merge_policy::NoMergePolicy));
        let generation = self.snapshot();
        let mut counts = CorpusCounts::default();
        let mut live = 0u64;
        for segment in generation.searcher.segment_readers() {
            let store = segment.get_store_reader(1)?;
            for document in store.iter::<TantivyDocument>(segment.alive_bitset()) {
                let document = document?;
                let raw = document
                    .get_first(self.fields.actor)
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| Error::Storage("committed profile missing".into()))?;
                let actor: Actor = serde_json::from_str(raw)?;
                live += u64::from(!actor.deleted);
                counts = counts
                    .replace(None, &actor)
                    .ok_or_else(|| Error::Invalid("statistics overflow".into()))?;
            }
        }
        if live != generation.metadata.actor_count {
            return Err(Error::Invalid(
                "statistics live count differs from index metadata".into(),
            ));
        }
        let mut metadata = generation.metadata.clone();
        metadata.corpus_counts = Some(counts);
        commit(&mut writer.writer, &mut metadata)?;
        writer.reader.reload()?;
        let next = Generation::new(
            writer.reader.searcher(),
            metadata,
            generation.policy.clone(),
            generation.policy_sha256.clone(),
            Some(&generation),
        )?;
        *self.generation.write().unwrap() = Arc::new(next);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn fixture() -> (tempfile::TempDir, Engine) {
        let dir = tempfile::tempdir().unwrap();
        let input = format!(
            "{}\n{}\n",
            json!({"type":"manifest","build_id":"fixture-statistics","source_watermark":100,"actor_count":0,"source_actor_count":0,"partial":true}),
            json!({"type":"end","build_id":"fixture-statistics","actor_count":0,"actors_sha256":format!("{:x}",Sha256::digest(b""))})
        );
        import(
            dir.path(),
            Cursor::new(input),
            None,
            ImportOptions {
                writer_memory: 32 * 1024 * 1024,
                ..ImportOptions::default()
            },
            true,
        )
        .unwrap();
        let engine = Engine::open(dir.path(), 32 * 1024 * 1024).unwrap();
        (dir, engine)
    }

    #[test]
    fn committed_counts_match_independent_histories_across_reopen() {
        let (dir, mut engine) = fixture();
        let mut model = HashMap::<String, Actor>::new();
        let mut seed = 20261116u64;
        for version in 101..=140 {
            let mut batch = Vec::new();
            for i in 0..4 {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                let n = (version + i) % 16;
                let handle = match seed % 7 {
                    0 => "empty.test".into(),
                    1 => "handle.invalid".into(),
                    2 => QUARANTINED_HANDLES[0].clone(),
                    _ => format!("actor{n}.test"),
                };
                let actor:Actor=serde_json::from_value(json!({"did":format!("did:fixture:{n}"),"handle":handle,"avatar_url":if seed&8==0{""}else{"avatar"},"hidden":seed&16!=0,"deleted":seed&32!=0,"quality_score":1.0,"version":version})).unwrap();
                model.insert(actor.did.clone(), actor.clone());
                batch.push(actor);
            }
            engine.apply(batch.clone(), None, None, None).unwrap();
            engine.apply(batch, None, None, None).unwrap();
            let live = model.values().filter(|a| !a.deleted).collect::<Vec<_>>();
            let actual = engine.snapshot().metadata.corpus_counts.unwrap();
            assert_eq!(
                actual.with_handles,
                live.iter().filter(|a| !a.handle.is_empty()).count() as u64
            );
            assert_eq!(
                actual.with_avatars,
                live.iter().filter(|a| !a.avatar_url.is_empty()).count() as u64
            );
            assert_eq!(
                actual.hidden,
                live.iter().filter(|a| a.hidden).count() as u64
            );
            assert_eq!(
                actual.handle_invalid,
                live.iter().filter(|a| a.handle == "handle.invalid").count() as u64
            );
            assert_eq!(
                actual.quarantined,
                live.iter()
                    .filter(|a| a.handle == QUARANTINED_HANDLES[0])
                    .count() as u64
            );
            if version % 10 == 0 {
                drop(engine);
                engine = Engine::open(dir.path(), 32 * 1024 * 1024).unwrap();
            }
        }
        let before = engine.snapshot().metadata.corpus_counts;
        let bad: Actor =
            serde_json::from_value(json!({"did":"not-a-did","quality_score":1.0,"version":999}))
                .unwrap();
        assert!(engine.apply(vec![bad], None, None, None).is_err());
        assert_eq!(before, engine.snapshot().metadata.corpus_counts);
        engine.initialize_statistics_offline().unwrap();
        assert_eq!(before, engine.snapshot().metadata.corpus_counts);
    }

    #[test]
    fn missing_and_malformed_statistics_never_recount_or_block_actor_work() {
        let (dir, engine) = fixture();
        assert!(Engine::open(dir.path(), 32 * 1024 * 1024).is_err());
        let mut value = serde_json::to_value(&engine.snapshot().metadata).unwrap();
        for bad in [Value::Null, json!("bad"), json!({"hidden":-1})] {
            value["corpus_counts"] = bad;
            let decoded: Metadata = serde_json::from_value(value.clone()).unwrap();
            assert!(decoded.corpus_counts.is_none());
        }
        let mut metadata = engine.snapshot().metadata.clone();
        metadata.corpus_counts = None;
        commit(&mut engine.writer.lock().unwrap().writer, &mut metadata).unwrap();
        drop(engine);
        let mut engine = Engine::open(dir.path(), 32 * 1024 * 1024).unwrap();
        engine.allow_partial = true;
        engine.allow_stale = true;
        assert!(engine.corpus().unwrap()["counts"].is_null());
        let actor: Actor = serde_json::from_value(
            json!({"did":"did:fixture:new","handle":"new.test","quality_score":1.0,"version":101}),
        )
        .unwrap();
        engine.apply(vec![actor], None, None, None).unwrap();
        assert!(engine.corpus().unwrap()["counts"].is_null());
        assert_eq!(engine.corpus().unwrap()["actors"], 1);
        engine.initialize_statistics_offline().unwrap();
        assert_eq!(engine.corpus().unwrap()["counts"]["with_handles"], 1);
    }

    #[test]
    fn underflow_invalidates_statistics_without_failing_actor_work() {
        let actor:Actor=serde_json::from_value(json!({"did":"did:fixture:old","handle":"old.test","avatar_url":"avatar","quality_score":1.0,"version":101})).unwrap();
        assert!(
            CorpusCounts::default()
                .replace(Some(&actor), &actor)
                .is_none()
        );
    }
}
