import unittest

import httpx

import arbiter
import corpus
import refresh
import views
from fakes import NOW_MS, Network, policy, recorded, visibility

DID = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"
MOD = "did:plc:ar7c4by46qjdydhdevvrndac"
CREATED = "2024-01-01T00:00:00.000Z"
COUNTS = {"followersCount": 1234, "followsCount": 321, "postsCount": 4567, "createdAt": CREATED}
SCORE = refresh.authority_score(1234, 321, 4567, refresh.date_time_ms(CREATED), NOW_MS)


def view(handle="a.test", **changes):
    actor = {
        "did": DID, "handle": handle, "labels": [], "pds": "https://pds.test", "pds_host": "pds.test", "quality_score": SCORE,
        "display_name": "Ada", "avatar_url": "bafkavatar", "associated": {}, "created_at": CREATED,
    }
    actor.update(changes)
    return views.from_actor(actor, sha="s")


def entry(production, shadow, using=None, refreshed=True):
    return corpus.difference(DID, production or corpus.ABSENT, shadow or corpus.ABSENT, using or visibility(), refreshed)


def hiding(value="spam", **extra):
    return {"src": MOD, "uri": DID, "val": value, "cts": "2026-01-01T00:00:00Z", **extra}


class Judged(unittest.TestCase):
    def label(self, difference, network, name, using=None):
        label, reason = network.authority(using).judge(difference, name)
        self.assertTrue(reason)
        return label


class HandleTests(Judged):
    def test_handle_is_the_one_the_did_document_claims_and_that_resolves_back(self):
        network = Network()
        network.account(DID, "new.test")
        cases = [
            (view("old.test"), view("new.test"), arbiter.SHADOW_RIGHT),
            (view("new.test"), view("old.test"), arbiter.PRODUCTION_RIGHT),
            (view("old.test"), view("older.test"), arbiter.BOTH_STALE),
            (view("handle.invalid"), view("NEW.test"), arbiter.SHADOW_RIGHT),
        ]
        for production, shadow, expected in cases:
            self.assertEqual(self.label(entry(production, shadow), network, corpus.HANDLE_DIFFERS), expected)

    def test_the_appview_is_not_asked_about_a_handle(self):
        network = Network()
        network.account(DID, "real.test", profile={"handle": "handle.invalid"})
        self.assertEqual(self.label(entry(view("real.test"), view("handle.invalid")), network, corpus.HANDLE_DIFFERS), arbiter.PRODUCTION_RIGHT)
        self.assertNotIn("appview.test", [host for host, _, _ in network.requests])

    def test_a_claimed_handle_that_does_not_resolve_back_makes_handle_invalid_right(self):
        network = Network()
        network.document(DID, "lapsed.test")
        self.assertEqual(self.label(entry(view("lapsed.test"), view("handle.invalid")), network, corpus.HANDLE_DIFFERS), arbiter.SHADOW_RIGHT)
        self.assertEqual(self.label(entry(view("handle.invalid"), view("lapsed.test")), network, corpus.HANDLE_DIFFERS), arbiter.PRODUCTION_RIGHT)
        self.assertEqual(self.label(entry(view("lapsed.test"), view("other.test")), network, corpus.HANDLE_DIFFERS), arbiter.BOTH_STALE)

    def test_a_handle_that_could_not_be_resolved_is_undetermined(self):
        network = Network()
        network.document(DID, "down.test")
        network.well_known["down.test"] = httpx.ConnectTimeout("timed out")
        self.assertEqual(self.label(entry(view("down.test"), view("handle.invalid")), network, corpus.HANDLE_DIFFERS), arbiter.UNDETERMINED)
        self.assertEqual(self.label(entry(view("a.test"), view("b.test")), Network(), corpus.HANDLE_DIFFERS), arbiter.UNDETERMINED)


class PdsAndPresenceTests(Judged):
    def test_pds_is_judged_against_the_did_document_service(self):
        network = Network()
        network.account(DID, "a.test", "two.test")
        self.assertEqual(self.label(entry(view(pds_host="one.test"), view(pds_host="two.test")), network, corpus.PDS_DIFFERS), arbiter.SHADOW_RIGHT)
        self.assertEqual(self.label(entry(view(pds_host="two.test"), view(pds_host="one.test")), network, corpus.PDS_DIFFERS), arbiter.PRODUCTION_RIGHT)
        self.assertEqual(self.label(entry(view(pds_host="one.test"), view(pds_host="three.test")), network, corpus.PDS_DIFFERS), arbiter.BOTH_STALE)
        self.assertEqual(self.label(entry(view(pds_host="one.test"), view(pds_host="two.test")), Network(), corpus.PDS_DIFFERS), arbiter.UNDETERMINED)

    def test_presence_follows_what_the_pds_says_of_the_repo(self):
        active, inactive, gone = Network(), Network(), Network()
        active.account(DID, "a.test")
        inactive.account(DID, "a.test", active=False)
        only_production, only_shadow = entry(view(), None), entry(None, view())
        self.assertEqual(self.label(only_production, active, corpus.ONLY_IN_PRODUCTION), arbiter.PRODUCTION_RIGHT)
        self.assertEqual(self.label(only_shadow, active, corpus.ONLY_IN_SHADOW), arbiter.SHADOW_RIGHT)
        for network in (inactive, gone):
            self.assertEqual(self.label(only_production, network, corpus.ONLY_IN_PRODUCTION), arbiter.SHADOW_RIGHT)
            self.assertEqual(self.label(only_shadow, network, corpus.ONLY_IN_SHADOW), arbiter.PRODUCTION_RIGHT)

    def test_a_tombstone_in_the_directory_means_the_account_is_gone(self):
        network = Network()
        network.documents[DID] = (410, )
        self.assertEqual(self.label(entry(view(), None), network, corpus.ONLY_IN_PRODUCTION), arbiter.SHADOW_RIGHT)

    def test_a_pds_that_cannot_be_asked_falls_back_to_whether_the_appview_serves_the_account(self):
        network = Network()
        network.document(DID, "a.test")
        network.repos[("pds.test", DID)] = (503, {"error": "Unavailable"})
        self.assertEqual(self.label(entry(view(), None), network, corpus.ONLY_IN_PRODUCTION), arbiter.UNDETERMINED)
        network.profiles[DID] = {"handle": "a.test"}
        self.assertEqual(self.label(entry(view(), None), network, corpus.ONLY_IN_PRODUCTION), arbiter.PRODUCTION_RIGHT)
        network.fail_profiles = True
        self.assertEqual(self.label(entry(view(), None), network, corpus.ONLY_IN_PRODUCTION), arbiter.UNDETERMINED)


class VisibilityTests(Judged):
    def test_what_a_search_returns_is_the_rule_applied_to_the_verified_handle_and_the_appviews_labels(self):
        network = Network()
        network.account(DID, "a.test", profile={"labels": [hiding()]})
        self.assertEqual(self.label(entry(view(), view(labels=[hiding()])), network, corpus.VISIBILITY_DIFFERS), arbiter.SHADOW_RIGHT)
        network.profiles[DID]["labels"] = [hiding(neg=True)]
        self.assertEqual(self.label(entry(view(), view(labels=[hiding()])), network, corpus.VISIBILITY_DIFFERS), arbiter.PRODUCTION_RIGHT)

    def test_a_handle_that_no_longer_resolves_is_not_returned(self):
        network = Network()
        network.document(DID, "a.test")
        network.profiles[DID] = {"labels": []}
        self.assertEqual(self.label(entry(view("a.test"), view("handle.invalid")), network, corpus.VISIBILITY_DIFFERS), arbiter.SHADOW_RIGHT)

    def test_the_policy_decides_as_it_did_in_the_walk(self):
        network = Network()
        network.account(DID, "a.blocked.test", profile={"labels": []})
        blocked = policy(domains=["blocked.test"], source="snapshot")
        difference = entry(view("a.blocked.test"), view("a.elsewhere.test"), visibility(blocked))
        self.assertEqual(difference["returned"], {"production": False, "shadow": True})
        self.assertEqual(self.label(difference, network, corpus.VISIBILITY_DIFFERS, blocked), arbiter.PRODUCTION_RIGHT)

    def test_without_labels_or_a_handle_visibility_is_undetermined(self):
        difference = entry(view(), view(labels=[hiding()]))
        unserved, unresolved = Network(), Network()
        unserved.account(DID, "a.test")
        unresolved.profiles[DID] = {"labels": []}
        for network in (unserved, unresolved):
            self.assertEqual(self.label(difference, network, corpus.VISIBILITY_DIFFERS), arbiter.UNDETERMINED)


class RefreshTests(Judged):
    def network(self, **profile):
        network = Network()
        network.account(DID, "a.test", profile={**COUNTS, "displayName": "Ada", "avatar": f"https://cdn.bsky.app/img/avatar/plain/{DID}/bafkavatar@jpeg", **profile})
        return network

    def test_the_score_is_recomputed_from_the_appviews_counts(self):
        network = self.network()
        cases = [
            (view(quality_score=0.0), view(), arbiter.SHADOW_RIGHT),
            (view(), view(quality_score=0.0), arbiter.PRODUCTION_RIGHT),
            (view(quality_score=SCORE - 0.4), view(quality_score=SCORE + 0.9), arbiter.BOTH_STALE),
            (view(quality_score=SCORE - 1.5), view(quality_score=SCORE + 0.9), arbiter.SHADOW_RIGHT),
            (view(quality_score=SCORE - 40), view(quality_score=SCORE + 40), arbiter.BOTH_STALE),
        ]
        for production, shadow, expected in cases:
            self.assertEqual(self.label(entry(production, shadow), network, corpus.QUALITY_SCORE_DIFFERS), expected)

    def test_a_profile_without_counts_scores_nothing_and_faults_nobody(self):
        network = self.network(followersCount=None, followsCount=None, postsCount=None)
        self.assertEqual(self.label(entry(view(quality_score=0.0), view()), network, corpus.QUALITY_SCORE_DIFFERS), arbiter.UNDETERMINED)

    def test_labels_are_compared_by_labeler_and_value(self):
        self_label = {"src": DID, "uri": "at://x", "val": "!no-unauthenticated", "cts": "c", "cid": "bafy"}
        network = self.network(labels=[dict(self_label, cts="later", cid="other"), hiding(neg=True)])
        self.assertEqual(self.label(entry(view(), view(labels=[self_label])), network, corpus.LABELS_DIFFER), arbiter.SHADOW_RIGHT)
        self.assertEqual(self.label(entry(view(labels=[self_label]), view(labels=[hiding()])), network, corpus.LABELS_DIFFER), arbiter.PRODUCTION_RIGHT)
        self.assertEqual(self.label(entry(view(labels=[hiding()]), view()), network, corpus.LABELS_DIFFER), arbiter.BOTH_STALE)

    def test_profile_fields_are_the_values_a_refresh_would_store(self):
        network = self.network(associated={"lists": 0, "chat": {"allowIncoming": "all"}, "labeler": False})
        cases = [
            (corpus.DISPLAY_NAME_DIFFERS, view(display_name="Old"), view()),
            (corpus.AVATAR_DIFFERS, view(avatar_url="bafkold"), view(avatar_url=f"https://cdn.bsky.app/img/avatar/plain/{DID}/bafkavatar@jpeg")),
            (corpus.ASSOCIATED_DIFFERS, view(), view(associated={"chat": {"allowIncoming": "all"}})),
            (corpus.CREATED_AT_DIFFERS, view(created_at=""), view()),
        ]
        for name, stale, current in cases:
            self.assertEqual(self.label(entry(stale, current), network, name), arbiter.SHADOW_RIGHT, name)
            self.assertEqual(self.label(entry(current, stale), network, name), arbiter.PRODUCTION_RIGHT, name)

    def test_an_empty_answer_faults_neither_side(self):
        network = self.network(displayName="", avatar="", associated={"lists": 0})
        cases = [
            (corpus.DISPLAY_NAME_DIFFERS, view(display_name="Kept"), view(display_name="")),
            (corpus.AVATAR_DIFFERS, view(avatar_url="bafkkept"), view(avatar_url="")),
            (corpus.ASSOCIATED_DIFFERS, view(associated={"labeler": True}), view()),
        ]
        for name, production, shadow in cases:
            self.assertEqual(self.label(entry(production, shadow), network, name), arbiter.UNDETERMINED, name)

    def test_an_account_the_appview_does_not_serve_is_undetermined(self):
        network = Network()
        network.account(DID, "a.test")
        self.assertEqual(self.label(entry(view(quality_score=0.0), view()), network, corpus.QUALITY_SCORE_DIFFERS), arbiter.UNDETERMINED)

    def test_a_profile_hash_cannot_be_judged(self):
        hashed = views.Doc("s", {k: v for k, v in view(display_name="Other").fields.items() if k not in views.ITEMISED})
        self.assertEqual(self.label(entry(view(), hashed), self.network(), corpus.PROFILE_DIFFERS), arbiter.UNDETERMINED)


class SampleTests(unittest.TestCase):
    def test_samples_are_labelled_and_tallied_per_class_and_stratum(self):
        network = Network()
        entries = []
        for n in range(12):
            did = f"did:plc:{n:024}"
            network.account(did, f"h{n}.test", profile=COUNTS)
            stale, current = view(f"h{n}.test", quality_score=0.0), view(f"h{n}.test")
            production, shadow = (current, stale) if n < 3 else (stale, current)
            entries.append(corpus.difference(did, production, shadow, visibility(), refreshed=n % 2 == 0))
        summary = recorded(entries, sample_size=10)
        authority = network.authority()
        arbiter.label_samples(summary, authority)
        scores = summary["classes"][corpus.QUALITY_SCORE_DIFFERS]
        self.assertEqual(scores["arbiter_labels"], {arbiter.SHADOW_RIGHT: 9, arbiter.PRODUCTION_RIGHT: 3})
        self.assertEqual(scores["strata"][corpus.REFRESHED]["arbiter_labels"], {arbiter.PRODUCTION_RIGHT: 2, arbiter.SHADOW_RIGHT: 4})
        self.assertEqual(scores["strata"][corpus.LAGGING]["arbiter_labels"], {arbiter.PRODUCTION_RIGHT: 1, arbiter.SHADOW_RIGHT: 5})
        self.assertEqual(scores["strata"][corpus.LAGGING]["estimated_production_right"], 1)
        self.assertEqual(scores["sample"][0]["arbiter"][corpus.QUALITY_SCORE_DIFFERS]["label"], arbiter.SHADOW_RIGHT)
        self.assertEqual((summary["arbiter_ran"], summary["arbiter_failures"]), (True, []))
        self.assertEqual(sum(host == "appview.test" for host, _, _ in network.requests), 1)
        self.assertEqual(sum(host == "plc.test" for host, _, _ in network.requests), 0)

    def test_an_estimate_scales_the_sample_share_to_the_stratum(self):
        network = Network()
        entries = []
        for n in range(40):
            did = f"did:plc:{n:024}"
            network.account(did, f"h{n}.test", profile=COUNTS)
            entries.append(corpus.difference(did, view(f"h{n}.test"), view(f"h{n}.test", quality_score=0.0), visibility(), refreshed=False))
        summary = recorded(entries, sample_size=4)
        arbiter.label_samples(summary, network.authority())
        lagging = summary["classes"][corpus.QUALITY_SCORE_DIFFERS]["strata"][corpus.LAGGING]
        self.assertEqual((lagging["count"], lagging["sampled"], lagging["estimated_production_right"]), (40, 4, 40))

    def test_an_appview_that_cannot_be_read_is_reported_and_judges_nothing(self):
        network = Network()
        network.account(DID, "a.test", profile=COUNTS)
        network.fail_profiles = True
        summary = recorded([entry(view(quality_score=0.0), view())])
        arbiter.label_samples(summary, network.authority())
        self.assertEqual(summary["classes"][corpus.QUALITY_SCORE_DIFFERS]["arbiter_labels"], {arbiter.UNDETERMINED: 1})
        self.assertEqual(len(summary["arbiter_failures"]), 1)

    def test_profiles_are_read_25_to_a_request(self):
        network = Network()
        dids = [f"did:plc:{n:024}" for n in range(60)]
        for did in dids:
            network.profiles[did] = {}
        authority = network.authority()
        authority.load(dids + dids[:5])
        self.assertEqual(sum(host == "appview.test" for host, _, _ in network.requests), 3)
        self.assertEqual(authority.profile(dids[59])[0], "served")
        self.assertEqual(authority.profile("did:plc:unknown"), ("absent", None))


if __name__ == "__main__":
    unittest.main()
