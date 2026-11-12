use super::*;
use tantivy::collector::Count;

/// Most actors one request may name.
pub const MAX_ACTORS: usize = 25;
/// Most documents read for one handle: two accounts can claim the same one.
const MAX_PER_HANDLE: usize = 8;

/// One actor as the index stores it, with the moderation decision a search
/// would make about it now.
#[derive(Debug)]
pub struct StoredActor {
    pub actor: Actor,
    pub hidden: bool,
}

impl StoredActor {
    /// The stored fields under the names the document has, not the public
    /// search shape: a reader builds its own views from these.
    pub fn row(&self) -> Value {
        let a = &self.actor;
        json!({
            "did": a.did,
            "handle": a.handle,
            "display_name": a.display_name,
            "avatar_url": a.avatar_url,
            "labels": a.labels,
            "created_at": a.created_at,
            "associated": a.associated,
            "pds": a.pds,
            "description": a.description,
            "banner_url": a.banner_url,
            "followers_count": a.followers_count,
            "follows_count": a.follows_count,
            "posts_count": a.posts_count,
            "quality_score": a.quality_score as f64,
            "updated_at": a.updated_at,
            "profile_checked": a.profile_checked,
            "hidden": self.hidden,
            "hidden_stored": a.hidden,
        })
    }
}

impl Engine {
    /// The stored actors named by DID or by handle, in the order asked for,
    /// each once. A name the index has no live document for is left out.
    /// Hidden actors are returned with `hidden` set: the reader decides what
    /// to show, as it does with the rows of any other store.
    pub fn actors(&self, wanted: &[String]) -> Result<Vec<StoredActor>> {
        let generation = self.snapshot();
        if !self.generation_ready(&generation) {
            return Err(Error::NotReady);
        }
        if wanted.is_empty() || wanted.len() > MAX_ACTORS {
            return Err(Error::Invalid(format!("1..{MAX_ACTORS} actors required")));
        }
        if wanted.iter().any(|name| name.is_empty() || name.len() > 2048) {
            return Err(Error::Invalid("actor outside supported bounds".into()));
        }
        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX);
        let mut seen = HashSet::new();
        let mut found = Vec::new();
        for name in wanted {
            let matches = if name.starts_with("did:") {
                self.fields
                    .find(&generation.searcher, name)?
                    .into_iter()
                    .collect()
            } else {
                self.by_handle(&generation.searcher, name)?
            };
            for actor in matches {
                if actor.deleted || !seen.insert(actor.did.clone()) {
                    continue;
                }
                let hidden = match &generation.policy {
                    Some(policy) => policy
                        .hidden_at_ms(&actor, now_ms)
                        .map_err(|_| Error::NotReady)?,
                    None => actor.hidden,
                };
                found.push(StoredActor { actor, hidden });
            }
        }
        Ok(found)
    }

    fn by_handle(&self, searcher: &Searcher, handle: &str) -> Result<Vec<Actor>> {
        let Some(key) = normalizer::exact_handle_key(handle)? else {
            return Ok(vec![]);
        };
        let rows = searcher.search(
            &key_term(self.fields.key, &key),
            &TopDocs::with_limit(MAX_PER_HANDLE).order_by_score(),
        )?;
        let mut actors = Vec::with_capacity(rows.len());
        for (_, address) in rows {
            let actor = self.fields.hydrate(searcher, address)?;
            if actor.handle.eq_ignore_ascii_case(handle) {
                actors.push(actor);
            }
        }
        actors.sort_by(|a, b| a.did.cmp(&b.did));
        Ok(actors)
    }

    /// What the index holds, counted from its own bookkeeping and from the
    /// postings of the handles search always excludes.
    pub fn corpus(&self) -> Result<Value> {
        let generation = self.snapshot();
        if !self.generation_ready(&generation) {
            return Err(Error::NotReady);
        }
        let count = |handle: &str| -> Result<u64> {
            let query = key_term(self.fields.key, &format!("e:{handle}"));
            Ok(generation.searcher.search(&query, &Count)? as u64)
        };
        let mut quarantined = 0;
        for handle in QUARANTINED_HANDLES.iter() {
            quarantined += count(handle)?;
        }
        Ok(json!({
            "actors": generation.metadata.actor_count,
            "searchable": generation.metadata.searchable_actor_count,
            "handle_invalid": count("handle.invalid")?,
            "quarantined": quarantined,
            "watermark": generation.metadata.watermark,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn actor(n: usize, handle: &str, extra: Value) -> Actor {
        let mut value = json!({
            "did": format!("did:fixture:{n:04}"),
            "handle": handle,
            "display_name": format!("Reader {n}"),
            "quality_score": n as f32,
            "pds": "https://local.test",
            "version": 100,
        });
        value
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        serde_json::from_value(value).unwrap()
    }

    fn reconcile(engine: &Engine, rows: Vec<Actor>, policy: Option<Value>) {
        let dids = rows.iter().map(|a| a.did.clone()).collect::<Vec<_>>();
        let state = engine.source_state(dids, false).unwrap();
        let revision = state["source_observation_revision"].as_u64().unwrap();
        engine
            .reconcile_source(SourceReconcile {
                actors: rows,
                expected_documents: serde_json::from_value(state["actors"].clone()).unwrap(),
                expected_source_cursor: state["source_cursor"].clone(),
                source_cursor: json!({"round": revision + 1}),
                expected_observation_revision: revision,
                batch_id: format!("{revision:064x}"),
                watermark: None,
                policy_snapshot: policy,
            })
            .unwrap();
    }

    fn fixture() -> (tempfile::TempDir, Engine) {
        let directory = tempfile::tempdir().unwrap();
        let empty_hash = format!("{:x}", Sha256::digest(b""));
        let input = format!(
            "{}\n{}\n",
            json!({"type":"manifest","build_id":"fixture-read","source_watermark":100,"actor_count":0,"source_actor_count":0,"partial":true}),
            json!({"type":"end","build_id":"fixture-read","actor_count":0,"actors_sha256":empty_hash})
        );
        import(
            directory.path(),
            Cursor::new(input),
            None,
            ImportOptions {
                writer_memory: 32 * 1024 * 1024,
                ..ImportOptions::default()
            },
            true,
        )
        .unwrap();
        let mut engine = Engine::open(directory.path(), 32 * 1024 * 1024).unwrap();
        engine.allow_partial = true;
        engine.allow_stale = true;
        (directory, engine)
    }

    fn names(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    fn dids(found: &[StoredActor]) -> Vec<&str> {
        found.iter().map(|a| a.actor.did.as_str()).collect()
    }

    #[test]
    fn reads_stored_fields_by_did_and_by_handle_in_any_case() {
        let (_directory, engine) = fixture();
        reconcile(
            &engine,
            vec![
                actor(
                    1,
                    "Alice.Example",
                    json!({"description":"line one\nline two","banner_url":"bafkbanner","updated_at":1_794_000_000u64,
                        "profile_checked":true,"followers_count":7,"avatar_url":"bafkavatar"}),
                ),
                actor(2, "alice.example.long", json!({})),
                actor(3, "bob.example", json!({"display_name":"alice.example"})),
            ],
            None,
        );
        let by_did = engine.actors(&names(&["did:fixture:0001"])).unwrap();
        let row = by_did[0].row();
        assert_eq!(row["handle"], "Alice.Example");
        assert_eq!(row["description"], "line one\nline two");
        assert_eq!(row["banner_url"], "bafkbanner");
        assert_eq!(row["updated_at"], 1_794_000_000u64);
        assert_eq!(row["profile_checked"], true);
        assert_eq!(row["followers_count"], 7);
        assert_eq!(row["hidden"], false);

        let by_handle = engine.actors(&names(&["alice.example"])).unwrap();
        assert_eq!(dids(&by_handle), ["did:fixture:0001"]);
        let upper = engine.actors(&names(&["ALICE.EXAMPLE"])).unwrap();
        assert_eq!(dids(&upper), ["did:fixture:0001"]);
        assert!(engine.actors(&names(&["alice"])).unwrap().is_empty());
        assert!(engine.actors(&names(&["nobody.example"])).unwrap().is_empty());
    }

    #[test]
    fn answers_in_request_order_once_each_and_leaves_out_the_unknown_and_deleted() {
        let (_directory, engine) = fixture();
        reconcile(
            &engine,
            vec![
                actor(1, "one.example", json!({})),
                actor(2, "two.example", json!({})),
                actor(3, "", json!({"deleted":true})),
            ],
            None,
        );
        let found = engine
            .actors(&names(&[
                "two.example",
                "did:fixture:0009",
                "did:fixture:0003",
                "did:fixture:0001",
                "did:fixture:0002",
                "ONE.example",
            ]))
            .unwrap();
        assert_eq!(dids(&found), ["did:fixture:0002", "did:fixture:0001"]);
    }

    #[test]
    fn two_accounts_claiming_one_handle_are_both_returned() {
        let (_directory, engine) = fixture();
        reconcile(
            &engine,
            vec![
                actor(2, "shared.example", json!({})),
                actor(1, "shared.example", json!({})),
            ],
            None,
        );
        let found = engine.actors(&names(&["shared.example"])).unwrap();
        assert_eq!(dids(&found), ["did:fixture:0001", "did:fixture:0002"]);
    }

    #[test]
    fn hidden_is_the_stored_flag_without_a_policy_and_the_policy_with_one() {
        let (_directory, engine) = fixture();
        let rows = vec![
            actor(1, "flagged.example", json!({"hidden":true})),
            actor(2, "overridden.example", json!({"hidden":true})),
            actor(3, "member.blocked.example", json!({})),
        ];
        reconcile(&engine, rows.clone(), None);
        let all = names(&["did:fixture:0001", "did:fixture:0002", "did:fixture:0003"]);
        let stored = engine.actors(&all).unwrap();
        assert_eq!(
            stored.iter().map(|a| a.hidden).collect::<Vec<_>>(),
            [true, true, false]
        );

        let policy = json!({"schema_version":1,"source_observed_at":epoch(),"complete":true,
            "blocked_domains":["blocked.example"],
            "overrides":[{"did":"did:fixture:0002","action":"show"}]});
        reconcile(&engine, vec![], Some(policy));
        let decided = engine.actors(&all).unwrap();
        assert_eq!(
            decided.iter().map(|a| a.hidden).collect::<Vec<_>>(),
            [false, false, true]
        );
        assert_eq!(decided[0].row()["hidden_stored"], true);
    }

    #[test]
    fn refuses_no_actors_too_many_and_an_index_that_is_not_ready() {
        let (_directory, mut engine) = fixture();
        assert!(matches!(engine.actors(&[]), Err(Error::Invalid(_))));
        let many = (0..=MAX_ACTORS)
            .map(|n| format!("did:fixture:{n:04}"))
            .collect::<Vec<_>>();
        assert!(matches!(engine.actors(&many), Err(Error::Invalid(_))));
        assert!(matches!(
            engine.actors(&names(&[""])),
            Err(Error::Invalid(_))
        ));
        engine.allow_stale = false;
        engine.max_sync_lag_s = 0;
        assert!(matches!(
            engine.actors(&names(&["did:fixture:0001"])),
            Err(Error::NotReady)
        ));
        assert!(matches!(engine.corpus(), Err(Error::NotReady)));
    }

    #[test]
    fn documents_without_the_optional_fields_hash_as_they_did_before_them() {
        let plain = actor(1, "plain.example", json!({}));
        let text = serde_json::to_string(&plain).unwrap();
        for field in ["description", "banner_url", "updated_at", "profile_checked"] {
            assert!(!text.contains(field), "{field} written while unset");
        }
        let full = actor(
            1,
            "plain.example",
            json!({"description":"d","banner_url":"b","updated_at":1,"profile_checked":true}),
        );
        assert_eq!(
            plain.differing_fields(&full),
            ["description", "banner_url", "updated_at", "profile_checked"]
        );
    }

    #[test]
    fn corpus_counts_actors_searchable_actors_and_excluded_identities() {
        let (_directory, engine) = fixture();
        let quarantined = QUARANTINED_HANDLES.first().unwrap().clone();
        reconcile(
            &engine,
            vec![
                actor(1, "one.example", json!({})),
                actor(2, "two.example", json!({"hidden":true})),
                actor(3, "handle.invalid", json!({})),
                actor(4, "HANDLE.INVALID", json!({})),
                actor(5, &quarantined, json!({})),
                actor(6, "", json!({"deleted":true})),
            ],
            None,
        );
        let corpus = engine.corpus().unwrap();
        assert_eq!(corpus["actors"], 5);
        // the bookkeeping's `visible()` knows the stored flag and a lowercase
        // `.invalid` suffix only; the two identity counts cover the rest
        assert_eq!(corpus["searchable"], 3);
        assert_eq!(corpus["handle_invalid"], 2);
        assert_eq!(corpus["quarantined"], 1);
    }
}
