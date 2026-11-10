"""Sample readiness and sync position on both backends over a window."""

import time

import corpus
import sources

MUST_MATCH = ("normalize_version", "plan_version", "format", "moderation_rules_sha256", "moderation_policy_sha256")


def sample(client):
    ready = client.get("/ready")
    status = client.get_json("/index/status")
    moderation = status.get("moderation") or {}
    reconciliation = status.get("reconciliation") or {}
    return {
        "observed_at": int(time.time()),
        "ready": ready.status == 200 and bool(status.get("ready")),
        "sync_lag_s": status.get("sync_lag_s"),
        "watermark": status.get("watermark"),
        "build_id": status.get("build_id"),
        "source_observation_revision": status.get("source_observation_revision"),
        "actor_count": status.get("actor_count"),
        "searchable_actor_count": status.get("searchable_actor_count"),
        "tombstone_count": status.get("tombstone_count"),
        "segments": status.get("segments"),
        "expiry_coverage_state": status.get("expiry_coverage_state"),
        "cutover_source_ready": status.get("cutover_source_ready"),
        "moderation_fresh": bool(moderation.get("fresh")),
        "moderation_age_s": moderation.get("age_s"),
        "reconciliation_age_s": reconciliation.get("age_s"),
        "normalize_version": status.get("normalize_version"),
        "plan_version": status.get("plan_version"),
        "format": status.get("format"),
        "retrieval_strategy": status.get("retrieval_strategy"),
        "moderation_rules_sha256": moderation.get("canonical_rules_source_sha256"),
        "moderation_policy_sha256": moderation.get("sha256"),
    }


def lag_summary(samples):
    lags = [s["sync_lag_s"] for s in samples if s["sync_lag_s"] is not None]
    return {
        "min_sync_lag_s": min(lags) if lags else None,
        "max_sync_lag_s": max(lags) if lags else None,
        "watermark_advance_s": samples[-1]["watermark"] - samples[0]["watermark"]
        if samples and samples[0]["watermark"] is not None and samples[-1]["watermark"] is not None
        else None,
        "builds": sorted({s["build_id"] for s in samples if s["build_id"]}),
    }


def watch(production, shadow, *, samples, interval, sleep=time.sleep, changed_dids=(), admins=None, want_fields=True):
    clients = {"production": production, "shadow": shadow}
    taken = {side: [] for side in clients}
    started = time.monotonic()
    changed_dids = sorted(set(changed_dids))
    converged = {did: None for did in changed_dids}
    last = {}
    for tick in range(samples):
        if tick:
            sleep(interval)
        for side, client in clients.items():
            taken[side].append(sample(client))
        if changed_dids and admins:
            a = sources.fetch_states(admins["production"], changed_dids, want_fields)
            b = sources.fetch_states(admins["shadow"], changed_dids, want_fields)
            for did in changed_dids:
                entry = corpus.difference(did, a[did], b[did])
                last[did] = entry
                if entry is None and converged[did] is None:
                    converged[did] = round(time.monotonic() - started, 3)
    agreement = {}
    for name in MUST_MATCH:
        pairs = list(zip(taken["production"], taken["shadow"]))
        agreement[name] = {
            "matched": any(a[name] is not None and a[name] == b[name] for a, b in pairs),
            "production": taken["production"][-1][name] if pairs else None,
            "shadow": taken["shadow"][-1][name] if pairs else None,
        }
    result = {
        "samples": taken,
        "interval_s": interval,
        "window_s": round(time.monotonic() - started, 3),
        "summary": {side: lag_summary(values) for side, values in taken.items()},
        "must_match": list(MUST_MATCH),
        "agreement": agreement,
    }
    if changed_dids:
        result["changed_dids"] = {
            "count": len(changed_dids),
            "agreed_within_window": sum(value is not None for value in converged.values()),
            "first_agreement_s": converged,
            "still_differing": {did: entry["classes"] for did, entry in last.items() if entry is not None},
        }
    return result
