use serde_json::{Value, json};
use typeahead_tantivy::{
    actor::Actor,
    moderation::{ModerationPolicy, expiry_coverage, has_explicit_slur, should_hide_at_ms},
};

fn assert_expected(
    id: &str,
    component: &str,
    actual: typeahead_tantivy::Result<bool>,
    expected: &Value,
    unsupported: bool,
) {
    if unsupported && actual.is_err() {
        return;
    }
    if expected["ok"] == true {
        assert_eq!(
            actual.unwrap_or_else(|e| panic!("{id}/{component}: {e}")),
            expected["value"].as_bool().unwrap(),
            "{id}/{component}"
        );
    } else {
        assert!(
            actual.is_err(),
            "{id}/{component}: expected canonical error"
        );
    }
}
#[test]
fn actual_bun_policy_differential() {
    let fixture: Value = serde_json::from_str(include_str!("moderation/cases.json")).unwrap();
    assert_eq!(
        ModerationPolicy::rules_source_sha256().unwrap(),
        fixture["canonical_source_sha256"].as_str().unwrap()
    );
    let mut unsupported = 0;
    for case in fixture["cases"].as_array().unwrap() {
        let id = case["id"].as_str().unwrap();
        let actor: Actor = serde_json::from_value(case["actor"].clone()).unwrap();
        let policy = ModerationPolicy::validate(case["snapshot"].clone()).unwrap();
        let now = case["now_millis"].as_u64().unwrap();
        let exp = &case["expected"];
        let is_unsupported = case["expiry_unsupported"] == true;
        let label = should_hide_at_ms(&actor.labels, now);
        let coverage = expiry_coverage(&actor.labels);
        if is_unsupported {
            assert!(coverage.unwrap().unsupported_expiry > 0);
        }
        if label.is_err() && is_unsupported {
            unsupported += 1;
        }
        assert_expected(
            id,
            "slur",
            has_explicit_slur(&actor.handle),
            &exp["slur"],
            false,
        );
        assert_expected(
            id,
            "domain",
            Ok(policy.domain_blocked(&actor.handle)),
            &exp["domain"],
            false,
        );
        assert_expected(id, "labels", label, &exp["label_hide"], is_unsupported);
        assert_expected(
            id,
            "hidden",
            policy.hidden_at_ms(&actor, now),
            &exp["hidden"],
            is_unsupported,
        );
    }
    assert!(unsupported > 0);
    println!(
        "MODERATION_DIFFERENTIAL {}",
        json!({"cases":fixture["cases"].as_array().unwrap().len(),"unsupported_expiry_cases":unsupported,"canonical_source_sha256":fixture["canonical_source_sha256"]})
    );
}
#[test]
fn invalid_or_incomplete_snapshots_fail_closed() {
    let base = json!({"schema_version":1,"source_observed_at":1800000000u64,"complete":true,"overrides":[],"blocked_domains":[]});
    for patch in [
        json!({"complete":false}),
        json!({"schema_version":2}),
        json!({"source_observed_at":0}),
        json!({"overrides":[{"did":"did:fixture:a","action":"invalid"}]}),
        json!({"overrides":[{"did":"did:fixture:a","action":"show"},{"did":"did:fixture:a","action":"hide"}]}),
        json!({"blocked_domains":vec!["domain.test";10001]}),
    ] {
        let mut value = base.clone();
        for (key, item) in patch.as_object().unwrap() {
            value[key] = item.clone();
        }
        assert!(ModerationPolicy::validate(value).is_err());
    }
}
