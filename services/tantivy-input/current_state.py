"""Bounded read-only current-source observations; never use as timestamp replay writes."""

import json
import re
from contextlib import nullcontext


def valid_did(value):
    if (
        not isinstance(value, str)
        or not value.startswith("did:")
        or len(value.encode()) > 2048
    ):
        raise ValueError("invalid bounded actor identity")
    return value


def compact_array(raw, replay, label):
    def reject_constant(_):raise ValueError()
    try:value=json.loads(raw,parse_constant=reject_constant)
    except (TypeError,ValueError):raise replay.SafeError('invalid compact '+label) from None
    if not isinstance(value,list):raise replay.SafeError('invalid compact '+label+' array')
    return value


def compact_profile(values, expected, replay):
    if not isinstance(values,list) or len(values)!=1+len(replay.ACTOR_COLUMNS):
        raise replay.SafeError('compact actor row width differs')
    if values[0]!=expected:raise replay.SafeError('compact actor identity order differs')
    row=dict(zip(replay.ACTOR_COLUMNS,values[1:]))
    text_columns={'did','handle','display_name','avatar_url','labels','created_at','associated','pds'}
    for key,value in row.items():
        if value is None:continue
        if key in text_columns and not isinstance(value,str):raise replay.SafeError('compact actor text type differs')
        if key not in text_columns and key!='quality_score' and type(value) is not int:raise replay.SafeError('compact actor integer type differs')
        if key=='quality_score':
            if not isinstance(value,str) or len(value)>64 or re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?',value) is None:raise replay.SafeError('compact actor score type differs')
            row[key]=float(value)
    if row['did'] is None and any(value is not None for value in row.values()):raise replay.SafeError('compact absent actor has payload')
    return row


class CurrentStateSource:
    def __init__(self, source, replay):
        self.source = source
        self.replay = replay

    def observe(self, dids):
        if not isinstance(dids, list) or not 1 <= len(dids) <= 500:
            raise self.replay.SafeError(
                "current-state identity batch must contain 1..500 identities"
            )
        try:
            ordered = sorted({valid_did(did) for did in dids})
        except ValueError as exc:
            raise self.replay.SafeError(str(exc)) from None
        if len(ordered) != len(dids):
            raise self.replay.SafeError(
                "current-state identity batch contains duplicates"
            )
        rows = self.source.select(
            self.replay.CURRENT_SQL, [json.dumps(ordered, separators=(",", ":"))]
        )
        if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict) or set(rows[0])!={'observations_json'}:
            raise self.replay.SafeError('current-state compact envelope differs')
        profiles=compact_array(rows[0]['observations_json'],self.replay,'observation')
        if len(profiles)!=len(ordered):
            raise self.replay.SafeError('current-state observation is incomplete')
        observations=[];source_now=None
        for expected,values in zip(ordered,profiles):
            if not isinstance(values,list) or len(values)!=2+len(self.replay.ACTOR_COLUMNS):
                raise self.replay.SafeError('compact observation row width differs')
            observed_at=self.replay.number(values[1],'source observation clock')
            if source_now is not None and source_now!=observed_at:
                raise self.replay.SafeError('current-state source clock differs within one statement')
            source_now=observed_at
            row=compact_profile([values[0],*values[2:]],expected,self.replay)
            if row['did'] is None:
                actor=self.replay.deletion(expected,0);source_updated_at=None
            else:
                if row['did']!=expected:raise self.replay.SafeError('current-state joined identity differs')
                actor=self.replay.actor_record(row);source_updated_at=actor['version']
            observations.append({'actor':actor,'source_updated_at':source_updated_at})
        return {"source_now": source_now, "observations": observations}

    def source_page(self, after_did="", limit=500):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise self.replay.SafeError(
                "current-state source page must contain at most 500 identities"
            )
        try:
            if after_did:
                valid_did(after_did)
        except ValueError as exc:
            raise self.replay.SafeError(str(exc)) from None
        rows = self.source.select(self.replay.SOURCE_WALK_SQL, [after_did, limit])
        dids = []
        for row in rows:
            try:
                did = valid_did(row.get("did"))
            except ValueError as exc:
                raise self.replay.SafeError(str(exc)) from None
            if did <= (dids[-1] if dids else after_did):
                raise self.replay.SafeError(
                    "current-state source page is not strictly ordered"
                )
            dids.append(did)
        return dids


class ObservationAdmin:
    def __init__(self, admin, replay):
        self.admin = admin
        self.replay = replay

    def state(self, dids):
        result = self.admin.http.request(
            self.admin.url + "/admin/source-state",
            self.admin.secret,
            {"dids": dids},
            max_bytes=256 * 1024,
        )[0]
        revision = self.replay.number(
            result.get("source_observation_revision"), "observation revision"
        )
        expected = result.get("actors")
        if not isinstance(expected, list) or len(expected) != len(dids):
            raise self.replay.SafeError("backend document state is incomplete")
        found = set()
        for item in expected:
            did = item.get("did")
            digest = item.get("document_sha256")
            if (
                did not in dids
                or did in found
                or (
                    digest is not None
                    and (
                        not isinstance(digest, str)
                        or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)
                    )
                )
            ):
                raise self.replay.SafeError("backend document state differs")
            found.add(did)
        return result, revision

    def page(self, after_did, limit):
        result = self.admin.http.request(
            self.admin.url + "/admin/source-page",
            self.admin.secret,
            {"after_did": after_did, "limit": limit},
            max_bytes=2 * 1024 * 1024,
        )[0]
        dids = result.get("dids")
        if not isinstance(dids, list) or len(dids) > limit:
            raise self.replay.SafeError("backend identity page differs")
        last = after_did
        for did in dids:
            try:
                valid_did(did)
            except ValueError as exc:
                raise self.replay.SafeError(str(exc)) from None
            if did <= last:
                raise self.replay.SafeError(
                    "backend identity page is not strictly ordered"
                )
            last = did
        return dids

    def reconcile(
        self,
        actors,
        expected,
        revision,
        previous,
        next_cursor,
        watermark=None,
        policy_snapshot=None,
    ):
        import hashlib

        body = {
            "actors": actors,
            "expected_documents": expected,
            "expected_observation_revision": revision,
            "source_cursor": next_cursor,
            "expected_source_cursor": previous,
        }
        if watermark is not None:
            body["watermark"] = watermark
        if policy_snapshot is not None:
            body["policy_snapshot"] = policy_snapshot
        body["batch_id"] = hashlib.sha256(self.replay.json_bytes(body)).hexdigest()
        return self.admin.http.request(
            self.admin.url + "/admin/source-reconcile",
            self.admin.secret,
            body,
            max_bytes=256 * 1024,
        )[0]


SOURCE_POLICY = "turso-authoritative-observations-v2"


class ObservationWriter:
    def __init__(
        self, source, admin, replay, retries=3, backoff=0.5, policy_mode="off"
    ):
        if policy_mode not in ["off", "snapshot"]:
            raise replay.SafeError("unknown moderation policy mode")
        self.policy_mode = policy_mode
        self.source = (
            CurrentPolicySource(source, replay)
            if policy_mode == "snapshot"
            else CurrentStateSource(source, replay)
        )
        self.admin = ObservationAdmin(admin, replay)
        self.replay = replay
        self.retries = retries
        self.backoff = backoff

    def commit(self, dids, previous, next_cursor, watermark=None, only_missing=False):
        import time

        ordered = sorted(dids)
        state, revision = self.admin.state(ordered)
        if state.get("source_cursor") != previous:
            raise self.replay.CursorConflict(
                "source cursor changed before source observation"
            )
        expected = state["actors"]
        if only_missing:
            expected = [item for item in expected if item["document_sha256"] is None]
            ordered = sorted(item["did"] for item in expected)
        policy_snapshot = None
        if only_missing and not ordered:
            observations = {"observations": []}
            if watermark is not None:
                raise self.replay.SafeError(
                    "cursor-only sweep cannot refresh polling watermark"
                )
        elif self.policy_mode == "snapshot":
            observations = self.source.observe_with_policy(ordered)
            policy_snapshot = {
                **observations["moderation"],
                "source_observed_at": observations["source_now"],
                "complete": True,
            }
        else:
            observations = (
                self.source.observe(ordered) if ordered else {"observations": []}
            )
        actors = [item["actor"] for item in observations["observations"]]
        for attempt in range(self.retries):
            try:
                reply = self.admin.reconcile(
                    actors,
                    expected,
                    revision,
                    previous,
                    next_cursor,
                    watermark,
                    policy_snapshot,
                )
                if (
                    reply.get("source_cursor") != next_cursor
                    or reply.get("source_observation_revision") != revision + 1
                ):
                    raise self.replay.SafeError(
                        "source observation acknowledgement differs"
                    )
                return reply
            except self.replay.Retryable:
                observed = self.admin.admin.status()
                if (
                    observed.get("source_cursor") == next_cursor
                    and observed.get("source_observation_revision") == revision + 1
                ):
                    return {
                        "source_cursor": next_cursor,
                        "source_observation_revision": revision + 1,
                        "ack_recovered": True,
                    }
                if (
                    observed.get("source_cursor") != previous
                    or observed.get("source_observation_revision") != revision
                ):
                    raise self.replay.CursorConflict(
                        "source observation changed during ambiguous commit"
                    )
                if attempt + 1 == self.retries:
                    raise
                time.sleep(min(4, self.backoff * 2**attempt))
        raise self.replay.SafeError("no source observation commit attempted")


class ObservedSynchronizer:
    def __init__(
        self,
        source,
        admin,
        replay,
        anti_entropy_pages=2,
        policy_mode="off",
        sweep_cooldown_seconds=3600,
        max_sweep_seconds=5,
        **settings,
    ):
        import copy

        self.copy = copy.deepcopy
        self.replay = replay
        self.legacy = replay.Synchronizer(source, admin, **settings)
        if (
            self.legacy.page_size > 500
            or type(anti_entropy_pages) is not int
            or not 0 <= anti_entropy_pages <= 1000
        ):
            raise replay.SafeError("invalid bounded observation settings")
        self.source = source
        self.admin = admin
        self.writer = ObservationWriter(
            source, admin, replay, self.legacy.retries, self.legacy.backoff, policy_mode
        )
        if (
            type(sweep_cooldown_seconds) is not int
            or not 0 <= sweep_cooldown_seconds <= 86400
            or type(max_sweep_seconds) not in (int, float)
            or not 0 < max_sweep_seconds <= 30
        ):
            raise replay.SafeError("invalid bounded sweep pacing")
        self.anti_entropy_pages = anti_entropy_pages
        self.sweep_cooldown_seconds = sweep_cooldown_seconds
        self.max_sweep_seconds = max_sweep_seconds

    def cursor(self, status):
        adapted = self.copy(status)
        current = adapted.get("source_cursor")
        if isinstance(current, dict) and current.get("source_policy") == SOURCE_POLICY:
            current["source_policy"] = self.replay.SOURCE_POLICY
        cursor = self.replay.initial_cursor(adapted, self.legacy.lookback_seconds)
        cursor["source_policy"] = SOURCE_POLICY
        sweep = cursor.get("reconciliation")
        if sweep is not None:
            if not isinstance(sweep, dict) or sweep.get("schema_version") != 1:
                raise self.replay.SafeError("invalid reconciliation cursor")
            for field in ["source_after_did", "backend_after_did"]:
                try:
                    if sweep.get(field):
                        valid_did(sweep[field])
                except ValueError:
                    raise self.replay.SafeError(
                        "invalid reconciliation identity cursor"
                    ) from None
                if not isinstance(sweep.get(field), str):
                    raise self.replay.SafeError(
                        "missing reconciliation identity cursor"
                    )
            for field in ["source_done", "backend_done"]:
                if type(sweep.get(field)) is not bool:
                    raise self.replay.SafeError("invalid reconciliation stream state")
            self.replay.number(sweep.get("round"), "reconciliation round")
            self.replay.number(sweep.get("round_started_at"), "reconciliation start")
            if sweep.get("last_completed_at") is not None:
                self.replay.number(
                    sweep["last_completed_at"], "reconciliation completion"
                )
        if sweep is not None and sweep.get("last_completed_started_at") is not None:
            started = self.replay.number(
                sweep["last_completed_started_at"], "completed reconciliation start"
            )
            if (
                sweep.get("last_completed_at") is None
                or started > sweep["last_completed_at"]
            ):
                raise self.replay.SafeError("invalid completed reconciliation duration")
        return cursor

    def run(self):
        import time

        started = time.monotonic()
        status = self.admin.status()
        previous = status.get("source_cursor")
        cursor = self.cursor(status)
        now = self.source.now()
        if now < cursor["through"]:
            raise self.replay.SafeError("source clock is behind durable watermark")
        if now - cursor["through"] > self.replay.RETENTION_SECONDS:
            raise self.replay.SafeError(
                "source gap exceeds tombstone retention; require complete reconciliation or fresh snapshot"
            )
        if cursor["cycle"] is None:
            upper = now - self.legacy.safety_lag
            if upper < cursor["through"]:
                return {
                    "phase": "waiting-for-safe-source-clock",
                    "caught_up": False,
                    "through": cursor["through"],
                }
            anchor = max(
                cursor["snapshot_watermark"],
                cursor["through"] - cursor["lookback_seconds"],
            )
            cursor["actors"] = {"updated_at": anchor, "did": ""}
            cursor["tombstones"] = {"deleted_at": anchor, "did": ""}
            cursor["cycle"] = {"upper_bound": upper, "stream": "actors"}
        pages = records = applied = replayed = ack_recoveries = 0
        while (
            pages < self.legacy.max_pages
            and time.monotonic() - started < self.legacy.max_seconds
        ):
            stream = cursor["cycle"]["stream"]
            column = "updated_at" if stream == "actors" else "deleted_at"
            position = cursor[stream]
            upper = cursor["cycle"]["upper_bound"]
            sql = (
                self.replay.ACTORS_SQL
                if stream == "actors"
                else self.replay.TOMBSTONES_SQL
            )
            rows = self.source.select(
                sql,
                [
                    cursor["snapshot_watermark"],
                    position[column],
                    position["did"],
                    upper,
                    self.legacy.page_size,
                ],
            )
            next_cursor = self.copy(cursor)
            last = (position[column], position["did"].encode())
            dids = []
            for row in rows:
                stamp = self.replay.number(
                    row.get(column), "source discovery timestamp"
                )
                did = self.replay.text(row, "did")
                try:
                    valid_did(did)
                except ValueError as exc:
                    raise self.replay.SafeError(str(exc)) from None
                key = (stamp, did.encode())
                if not cursor["snapshot_watermark"] < stamp <= upper or key <= last:
                    raise self.replay.SafeError(
                        "source discovery keyset violates cutoff"
                    )
                dids.append(did)
                last = key
            watermark = None
            if rows:
                next_cursor[stream] = {column: last[0], "did": last[1].decode()}
            else:
                next_cursor[stream] = {column: upper, "did": ""}
                if stream == "actors":
                    next_cursor["cycle"]["stream"] = "tombstones"
                else:
                    next_cursor.update(cycle=None, through=upper)
                    watermark = upper
            receipt = self.writer.commit(dids, previous, next_cursor, watermark)
            pages += 1
            records += len(rows)
            applied += receipt.get("applied", 0)
            replayed += receipt.get("replayed", 0)
            ack_recoveries += int(receipt.get("ack_recovered", False))
            cursor = next_cursor
            previous = self.copy(cursor)
            if cursor["cycle"] is None:
                break
        sweep_pages = 0
        sweep_started = time.monotonic()
        sweep_pause = None
        sweep = cursor.get("reconciliation") or {}
        idle_round = not any(
            sweep.get(key)
            for key in [
                "source_after_did",
                "backend_after_did",
                "source_done",
                "backend_done",
            ]
        )
        if (
            sweep.get("last_completed_at") is not None
            and idle_round
            and now - sweep["last_completed_at"] < self.sweep_cooldown_seconds
        ):
            sweep_pause = "cooldown"
        while (
            cursor["cycle"] is None
            and sweep_pause is None
            and time.monotonic() - sweep_started < self.max_sweep_seconds
            and sweep_pages < self.anti_entropy_pages
            and time.monotonic() - started < self.legacy.max_seconds
        ):
            next_cursor = self.copy(cursor)
            sweep = next_cursor.setdefault(
                "reconciliation",
                {
                    "schema_version": 1,
                    "round": 1,
                    "source_after_did": "",
                    "backend_after_did": "",
                    "source_done": False,
                    "backend_done": False,
                    "round_started_at": now,
                    "last_completed_at": None,
                    "last_completed_started_at": None,
                },
            )
            scope = (
                self.source.budget_scope("sweep")
                if hasattr(self.source, "budget_scope")
                else nullcontext()
            )
            try:
                with scope:
                    if idle_round and sweep_pages == 0:
                        sweep["round_started_at"] = now
                    stream = "backend" if sweep["source_done"] else "source"
                    after = sweep[stream + "_after_did"]
                    dids = (
                        self.writer.source.source_page(after, self.legacy.page_size)
                        if stream == "source"
                        else self.writer.admin.page(after, self.legacy.page_size)
                    )
                    if dids:
                        sweep[stream + "_after_did"] = dids[-1]
                    else:
                        sweep[stream + "_done"] = True
                    complete = sweep["source_done"] and sweep["backend_done"]
                    if complete:
                        completed = self.source.now()
                        if completed < sweep["round_started_at"]:
                            raise self.replay.SafeError(
                                "source clock is behind reconciliation start"
                            )
                        completed_start = sweep["round_started_at"]
                        sweep.update(
                            round=sweep["round"] + 1,
                            source_after_did="",
                            backend_after_did="",
                            source_done=False,
                            backend_done=False,
                            round_started_at=completed,
                            last_completed_at=completed,
                            last_completed_started_at=completed_start,
                        )
                    receipt = self.writer.commit(
                        dids, previous, next_cursor, only_missing=stream == "source"
                    )
            except self.replay.SweepBudgetExhausted:
                sweep_pause = "budget"
                break
            applied += receipt.get("applied", 0)
            replayed += receipt.get("replayed", 0)
            ack_recoveries += int(receipt.get("ack_recovered", False))
            sweep_pages += 1
            records += len(dids)
            cursor = next_cursor
            previous = self.copy(cursor)
            if complete:
                break
        if sweep_pause is None and sweep_pages < self.anti_entropy_pages:
            if cursor["cycle"] is not None:
                sweep_pause = "discovery"
            elif (
                time.monotonic() - sweep_started >= self.max_sweep_seconds
                or time.monotonic() - started >= self.legacy.max_seconds
            ):
                sweep_pause = "time-slice"
        result = {
            "sweep_pause": sweep_pause,
            "source_budget": self.source.accounting()
            if hasattr(self.source, "accounting")
            else None,
            "phase": "caught-up" if cursor["cycle"] is None else "bounded-pause",
            "caught_up": cursor["cycle"] is None,
            "through": cursor["through"],
            "source_policy": SOURCE_POLICY,
            "policy_mode": self.writer.policy_mode,
            "source_cursor": cursor,
            "pages": pages,
            "anti_entropy_pages": sweep_pages,
            "records": records,
            "applied": applied,
            "replayed": replayed,
            "ack_recoveries": ack_recoveries,
            "reconciliation": cursor.get("reconciliation"),
            "source_requests": self.source.requests,
            "source_bytes": self.source.bytes,
            "wall_seconds": time.monotonic() - started,
        }
        self.legacy.save(result)
        return result


class CurrentPolicySource(CurrentStateSource):
    """Observe profiles and complete bounded operator policy in one SQL snapshot."""

    def observe_with_policy(self, dids):
        import hashlib

        if not isinstance(dids, list) or len(dids) > 500:
            raise self.replay.SafeError(
                "policy identity batch must contain at most 500 identities"
            )
        try:
            ordered = sorted({valid_did(did) for did in dids})
        except ValueError as exc:
            raise self.replay.SafeError(str(exc)) from None
        if len(ordered) != len(dids):
            raise self.replay.SafeError("policy identity batch contains duplicates")
        rows = self.source.select(
            self.replay.CURRENT_POLICY_SQL, [json.dumps(ordered, separators=(",", ":"))]
        )
        if len(rows) != 1:
            raise self.replay.SafeError("policy observation envelope differs")
        row = rows[0]
        now = self.replay.number(row.get("source_now"), "policy source clock")
        try:
            profiles = compact_array(row["actors_json"], self.replay, "policy profiles")
            overrides = json.loads(row["overrides_json"])
            domains = json.loads(row["domains_json"])
        except (KeyError, TypeError, ValueError):
            raise self.replay.SafeError("invalid policy observation envelope") from None
        if not isinstance(profiles, list) or len(profiles) != len(ordered):
            raise self.replay.SafeError("policy actor observation is incomplete")
        if not isinstance(overrides, list) or not isinstance(domains, list):
            raise self.replay.SafeError("policy snapshot arrays differ")
        if len(overrides) > 10000 or len(domains) > 10000:
            raise self.replay.SafeError(
                "complete moderation policy exceeds bounded admission"
            )
        actions = {}
        for item in overrides:
            try:
                did = valid_did(item["did"])
                action = item["action"]
            except (TypeError, KeyError, ValueError):
                raise self.replay.SafeError("invalid moderation override") from None
            if did in actions or action not in ["show", "hide"]:
                raise self.replay.SafeError("ambiguous moderation override")
            actions[did] = action
        for domain in domains:
            if not isinstance(domain, str) or not domain or len(domain.encode()) > 2048:
                raise self.replay.SafeError("invalid moderation domain")
        policy = {
            "schema_version": 1,
            "overrides": [
                {"did": did, "action": actions[did]} for did in sorted(actions)
            ],
            "blocked_domains": sorted({domain.lower() for domain in domains}),
        }
        observations = []
        for did, values in zip(ordered, profiles):
            profile = compact_profile(values, did, self.replay)
            if profile.get("did") is None:
                actor = self.replay.deletion(did, 0)
                stamp = None
            else:
                if profile["did"] != did:
                    raise self.replay.SafeError("policy joined identity differs")
                actor = self.replay.actor_record(profile)
                stamp = actor["version"]
            observations.append({"actor": actor, "source_updated_at": stamp})
        return {
            "source_now": now,
            "observations": observations,
            "moderation": policy,
            "moderation_sha256": hashlib.sha256(
                self.replay.json_bytes(policy)
            ).hexdigest(),
        }
