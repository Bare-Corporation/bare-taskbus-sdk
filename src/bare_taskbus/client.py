"""TaskbusClient - one method per gateway route, no hidden behaviour.

Deliberate design notes, each of which cost a real attempt to learn on the fleet:
  * the claim nonce travels in the X-Claim-Nonce HEADER. A nonce in the JSON body is ignored and
    every write then answers 409 "Stale or missing claim nonce", which reads like an expired lease.
  * the list endpoint paginates on page and page_size. The alias "size" is accepted by some builds
    and silently ignored by others, so this client only ever sends page_size.
  * an executor may move a row to InProgress, HumanReviewNeeded or Blocked. Completed is refused to
    an executor, so the client blocks it locally instead of spending a call on a 403.
  * the lease is 900 seconds and a heartbeat EXTENDS it. POST /v1/taskbus/rows/{id}/heartbeat
    with the claim nonce pushes lease_expires_at forward, so a long task no longer has to fit
    inside one 900-second window. See heartbeat() and HEARTBEAT_HORIZON_SECONDS: renewal is
    bounded, not infinite.
  * a heartbeat that answers 409 does not revive anything. 409 means the claim is no longer live -
    the row left InProgress, or the lease already lapsed and the nonce is stale - and the gateway
    refuses every later write with that nonce too. The only recovery is to re-claim, which mints
    a NEW nonce and spends another attempt. Never treat 409 as a retryable blip.
  * a PATCH still does not extend the lease, and reclamation is LAZY (no sweeper): a row becomes
    claimable at expiry but is only reclaimed when somebody next claims it.
  * POST /rows/{id}/review is tenant-gated. If the caller stakeholder is not on the row
    stakeholder_account_master, the route answers 404 row_not_found - deliberately a 404 and not
    a 403, because a 403 would confirm the row exists in another tenant. That gate runs BEFORE the
    idempotent branch, so a foreign row recorded verdict is never echoed back.
  * watchers_stakeholder_representative is a LINK LIST, and the row's own shape is the
    authority: a row watching exactly one representative still arrives as a one-element
    list. Row.watchers therefore goes through _ids(), never _val(), because _val collapses
    a one-element list to its element and [271] would silently become the integer 271
    instead of representative 271. A watcher is a tenant-scoped notification annotation:
    it grants NO claim and NO write on the watched row, it is not an assignment, and a
    watcher outside the caller tenant is a 403 tenant_scope_violation rather than a
    silent drop.
  * the execution profile lives ON the row and is read back through its own route
    (GET /v1/taskbus/rows/{id}/execution-profile). It carries the pinned_model and
    exclude_providers / routing_strategy decisions that were resolved when the row was
    dispatched, so it - not the current profile record - is the history. A row that never
    ran under a profile answers 404 no_execution_profile, which get_execution_profile()
    reports as None: an absent profile is a normal state, not a fault. Every other 404
    RAISES, because row_not_found is also how the gateway answers for another tenant row.
"""
import json
import os
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .errors import TaskbusError, TaskbusForbidden, TaskbusValidation, error_for

DEFAULT_BASE_URL = "https://api.bare-ai.net"
DEFAULT_UA = "bare-taskbus-sdk/0.2.0"
EXECUTOR_STATUSES = ("InProgress", "HumanReviewNeeded", "Blocked")
# A claim holds the row for this long before it must be renewed or lapse.
LEASE_SECONDS = 900
# Renewal is bounded: heartbeats push lease_expires_at out, but only within this
# horizon measured from the claim. Past it the claim lapses and the row must be
# re-claimed (new nonce, another attempt spent).
HEARTBEAT_HORIZON_SECONDS = 7200
# A row whose parent_task chain is this deep is refused rather than executed:
# recursion is bounded by the chain, not by a counter a worker could omit.
MAX_SPAWN_DEPTH = 3
PARENT_CHAIN_HARD_CAP = 32


@dataclass
class Row:
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self):
        return self.raw.get("id")

    def _field(self, name):
        """The named field, unwrapped, tolerating every shape the gateway sends.

        Reads the key by name; if it is absent, looks inside a nested "fields"
        envelope (the create echo is untyped, so it may nest the row). Returns None
        only when the field is genuinely not present.
        """
        if not isinstance(self.raw, dict):
            return None
        value = self.raw.get(name)
        if value is None:
            fields = self.raw.get("fields")
            if isinstance(fields, dict):
                value = fields.get(name)
        return _val(value)

    def get(self, name, default=None):
        """Field by name with a default, so callers never need .raw for the basics."""
        value = self._field(name)
        return default if value is None else value

    @property
    def status(self):
        return self._field("status")

    @property
    def priority(self):
        return self._field("priority")

    @property
    def short_description(self):
        return self._field("short_description")

    @property
    def success_criterion(self):
        return self._field("success_criterion")

    @property
    def assigned(self):
        return self._field("assigned_to_stakeholder_representative")

    def _ids_field(self, name):
        """The named link-list field as list[int], tolerating every row shape.

        Same key-then-nested-envelope lookup as _field(), but the value goes through
        _ids() so a one-element list stays a list instead of collapsing to a scalar.
        """
        if not isinstance(self.raw, dict):
            return []
        value = self.raw.get(name)
        if value is None:
            fields = self.raw.get("fields")
            if isinstance(fields, dict):
                value = fields.get(name)
        return _ids(value)

    @property
    def watchers(self):
        """Ids of the representatives watching this row. Always a list, never a scalar.

        A watcher is a notification annotation and the gateway scopes it to the caller
        tenant: it does not grant claim or write rights on the row, and it is not an
        assignment. Use `assigned` when you need to know who owns the work.
        """
        return self._ids_field("watchers_stakeholder_representative")

    @property
    def parent_task(self):
        """Ids of this row's parents. Accepts [1], [{"id": 1}] or 1, always as a list."""
        return self._ids_field("parent_task")

    def __repr__(self):
        return "Row(id=%r, status=%r, priority=%r)" % (self.id, self.status, self.priority)


@dataclass
class Claim:
    row_id: int
    nonce: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def lease_expires_at(self):
        return self.raw.get("lease_expires_at")


@dataclass
class ExecutionProfile:
    """The execution-profile snapshot a row was dispatched under (Execution Profiles v1.1).

    Read-only history: the gateway stores the snapshot ON the row, so this does not change
    when the profile record or the role behind it is later edited or retired. The shape
    below was measured against the live gateway on 2026-11-20:

        row_id, profile_id, slug, version, config_hash, profile_hash, resolved_at,
        integrity, reviewer_authority, role {alias, content_sha256},
        snapshot {snapshot_version, profile, role, execution_config, resolved_at}

    execution_config carries the routing decisions a caller cares about: pinned_model,
    exclude_providers, routing_strategy, plus role_alias, role_content_sha256,
    routing_ceiling_eur, max_attempts and require_human_review.

    pinned_model is read as OPTIONAL on purpose. Snapshots written before the 2026-11-20
    profile upgrade do not carry it, and an absent pin means "this run was not pinned",
    not "the pin is unknown". A caller that requires a pin must check for None itself.
    """

    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def row_id(self):
        return self.raw.get("row_id")

    @property
    def profile_id(self):
        return self.raw.get("profile_id")

    @property
    def slug(self):
        return self.raw.get("slug")

    @property
    def version(self):
        return self.raw.get("version")

    @property
    def config_hash(self):
        return self.raw.get("config_hash")

    @property
    def profile_hash(self):
        return self.raw.get("profile_hash")

    @property
    def resolved_at(self):
        return self.raw.get("resolved_at")

    @property
    def integrity(self):
        return self.raw.get("integrity")

    @property
    def reviewer_authority(self):
        return self.raw.get("reviewer_authority")

    @property
    def role_alias(self):
        role = self.raw.get("role")
        return role.get("alias") if isinstance(role, dict) else None

    @property
    def snapshot(self):
        snap = self.raw.get("snapshot")
        return snap if isinstance(snap, dict) else {}

    @property
    def execution_config(self):
        config = self.snapshot.get("execution_config")
        return config if isinstance(config, dict) else {}

    @property
    def pinned_model(self):
        return self.execution_config.get("pinned_model")

    @property
    def exclude_providers(self):
        """Providers this run was told to avoid. [] when the snapshot carries none."""
        value = self.execution_config.get("exclude_providers")
        return list(value) if isinstance(value, (list, tuple)) else []

    @property
    def routing_strategy(self):
        return self.execution_config.get("routing_strategy")

    def __repr__(self):
        return "ExecutionProfile(row_id=%r, slug=%r, pinned_model=%r)" % (
            self.row_id, self.slug, self.pinned_model)


def _val(value):
    """Unwrap a Taskbus field into a plain Python value.

    The same field arrives in several shapes, and the client must tolerate all of
    them because the create response is untyped (the OpenAPI spec says its
    field-level shape "is stable in practice but not yet promoted to a typed
    model"):

      * single-select: {"id": 8262, "value": "New", "color": "blue"}
      * link row:      {"id": 84, "order": 1, "value": "bare-sales@host"}
      * link list:     [{"id": 84, "value": "..."}] or a bare [84]
      * plain scalar:  "New" / 3 / None

    A dict with no usable "value" falls back to its id, and a nested {"fields":
    {...}} envelope is unwrapped, so a caller never has to reach into .raw.
    """
    if isinstance(value, dict):
        if value.get("value") is not None:
            return value["value"]
        fields = value.get("fields")
        if isinstance(fields, dict):
            return fields
        if value.get("id") is not None:
            return value["id"]
        return None
    if isinstance(value, list):
        out = [_val(v) for v in value]
        return out[0] if len(out) == 1 else out
    return value


def _ids(value):
    """A link-list field as list[int], PRESERVING the list shape.

    Why this exists next to _val(): _val deliberately collapses a one-element list to its
    element, which is right for a single-select and wrong for a link list. A row watching
    exactly one representative must still report [271]; reporting 271 instead loses the
    fact that it is a collection and breaks every caller that iterates row.watchers or
    compares against a list.

    Accepts [271], [{"id": 271}], [{"id": 271, "value": "..."}], a bare 271, "271", None.
    An unusable entry is skipped rather than raising, so one malformed link cannot make
    the whole row unreadable - the same tolerance _val() applies to its own shapes.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("id")
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


class TaskbusClient:
    def __init__(self, api_key=None, base_url=None, user_agent=None, timeout=30.0, client=None):
        self.api_key = api_key or os.environ.get("TASKBUS_API_KEY", "")
        if not self.api_key:
            raise TaskbusValidation("no API key: pass api_key= or set TASKBUS_API_KEY")
        self.base_url = (base_url or os.environ.get("TASKBUS_GATEWAY_BASE")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.user_agent = user_agent or os.environ.get("TASKBUS_USER_AGENT") or DEFAULT_UA
        self._client = client or httpx.Client(timeout=timeout)

    # ---- transport -------------------------------------------------------
    def _headers(self, nonce=None):
        h = {"X-API-Key": self.api_key, "Accept": "application/json",
             "User-Agent": self.user_agent}
        if nonce:
            h["X-Claim-Nonce"] = nonce
        return h

    def _request(self, method, path, body=None, nonce=None):
        resp = self._client.request(method, self.base_url + path, headers=self._headers(nonce),
                                    json=body if body is not None else None)
        if resp.status_code >= 400:
            payload = {}
            try:
                payload = resp.json()
            except Exception:
                pass
            code = payload.get("error") if isinstance(payload, dict) else None
            message = payload.get("message") or payload.get("error") or resp.text[:200] \
                if isinstance(payload, dict) else resp.text[:200]
            raise error_for(resp.status_code, code, message)
        if not resp.content:
            return {}
        return resp.json()

    # ---- read ------------------------------------------------------------
    def list_tasks(self, status=("new", "in_progress"), page=1, page_size=100) -> List[Row]:
        """List rows, filtered by status.

        status is a REPEATABLE query parameter, so it is sent once per value:
        ?status=new&status=in_progress. A comma-joined string is a 400 from the
        gateway (measured: "Unknown status value: 'new,in_progress'"), so a caller
        who passes "new,in_progress" or a list gets it split and repeated here.

        Accepts a single string, an iterable of strings, or None. None omits the
        parameter, which the gateway documents as "only New and InProgress".
        """
        parts = []
        if status is not None:
            values = status.split(",") if isinstance(status, str) else list(status)
            for value in values:
                value = str(value).strip()
                if value:
                    parts.append("status=" + urllib.parse.quote(value, safe=""))
        query = "&".join(parts + ["page=%d" % page, "page_size=%d" % page_size])
        data = self._request("GET", "/v1/taskbus/rows?" + query)
        return [Row(r) for r in (data.get("results") or data.get("rows") or [])]

    def get_task(self, row_id) -> Row:
        return Row(self._request("GET", "/v1/taskbus/rows/%d" % int(row_id)))

    def get_execution_profile(self, row_id):
        """The ExecutionProfile snapshot for a row, or None when it has none.

        GET /v1/taskbus/rows/{row_id}/execution-profile reads the snapshot stored ON the
        row and never re-resolves the current profile, so this is history: it survives
        the profile record being retired or its role being edited afterwards.

        Measured against the live gateway on 2026-11-20, a row that was never dispatched
        under a profile answers 404 {"error": "no_execution_profile"}. That is a normal
        state and is reported as None. Every other failure RAISES - notably a 404
        row_not_found, which is also how the gateway answers for another tenant's row, so
        a missing row must never be mistaken for a row without a profile.
        """
        try:
            return ExecutionProfile(self._request(
                "GET", "/v1/taskbus/rows/%d/execution-profile" % int(row_id)))
        except TaskbusError as exc:
            if exc.status == 404 and exc.code == "no_execution_profile":
                return None
            raise

    # ---- write -----------------------------------------------------------
    def create_task(self, short_description, ai_prompt, success_criterion=None,
                    priority="medium", max_attempts=3, assigned_rep_id=None,
                    depends_on=None, watchers=None, parent_task=None) -> Row:
        body = {"short_description": short_description, "ai_prompt": ai_prompt,
                "priority": priority, "max_attempts": max_attempts}
        if success_criterion:
            body["success_criterion"] = success_criterion
        if assigned_rep_id:
            body["assigned_to_stakeholder_representative"] = [int(assigned_rep_id)]
        if depends_on:
            body["dependant_on_task"] = [int(x) for x in depends_on]
        if watchers is not None:
            # A link list goes on the wire as [int]; the row reads it back through _ids().
            # An explicit [] is sent rather than omitted so the caller's intent is on the
            # request, not inferred from a missing key.
            body["watchers_stakeholder_representative"] = [int(x) for x in watchers]
        if parent_task is not None:
            parents = parent_task if isinstance(parent_task, (list, tuple)) else [parent_task]
            body["parent_task"] = [int(x) for x in parents]
        return Row(self._request("POST", "/v1/taskbus/rows", body))

    def claim_task(self, row_id) -> Claim:
        data = self._request("POST", "/v1/taskbus/rows/%d/claim" % int(row_id), {})
        return Claim(row_id=int(row_id), nonce=data.get("nonce"), raw=data)

    def update_task(self, claim: Claim, status=None, machines_response=None,
                    reason=None, watchers=None, **extra) -> Row:
        if status is not None and status not in EXECUTOR_STATUSES:
            raise TaskbusForbidden(
                "an executor may only set %s; %r belongs to the review route"
                % (", ".join(EXECUTOR_STATUSES), status))
        body = dict(extra)
        if status:
            body["status"] = status
        if machines_response is not None:
            body["machines_response"] = machines_response
        if reason is not None:
            body["re_opened_or_retry_reason"] = reason
        if watchers is not None:
            # An EMPTY list is meaningful here: it CLEARS the watchers. Never collapse this
            # with "if watchers", or the row keeps its watchers while the caller believes
            # the change landed - the same silent-no-op class as a misspelled field name.
            body["watchers_stakeholder_representative"] = [int(x) for x in watchers]
        if status in ("HumanReviewNeeded", "Blocked") and "re_opened_or_retry_reason" not in body:
            body["re_opened_or_retry_reason"] = "closed by bare-taskbus-sdk"
        if not claim.nonce:
            raise TaskbusValidation("claim has no nonce: the gateway will refuse this write")
        return Row(self._request("PATCH", "/v1/taskbus/rows/%d" % claim.row_id,
                                 body, nonce=claim.nonce))

    def release_task(self, claim: Claim, reason) -> Row:
        return self.update_task(claim, status="Blocked", reason=reason)

    def reopen_task(self, row_id, reason) -> Row:
        return Row(self._request("POST", "/v1/taskbus/rows/%d/reopen" % int(row_id),
                                 {"reason": reason}))

    def review_task(self, row_id, verdict, status, comments, verifier_rep_uuid=None) -> Dict[str, Any]:
        if verdict not in ("Passed", "Failed"):
            raise TaskbusValidation("verdict must be Passed or Failed")
        expected = "Completed" if verdict == "Passed" else "Blocked"
        if status != expected:
            raise TaskbusValidation(
                "%s pairs with %s, not %s (the gateway answers 422 verdict_status_conflict)"
                % (verdict, expected, status))
        body = {"verdict": verdict, "status": status, "comments": comments}
        if verifier_rep_uuid:
            body["verifier_rep_uuid"] = verifier_rep_uuid
        return self._request("POST", "/v1/taskbus/rows/%d/review" % int(row_id), body)

    # ---- lease -----------------------------------------------------------
    def heartbeat(self, claim: Claim) -> Dict[str, Any]:
        """Extend a claim's lease. Returns the gateway payload.

        The nonce travels in X-Claim-Nonce, exactly like every other write. A 409
        means the claim is no longer live and is NOT revived by retrying: it raises
        TaskbusConflict, which the caller must treat as "stop working on this row".
        """
        if not claim or not claim.nonce:
            raise TaskbusValidation("heartbeat needs a claim with a nonce")
        return self._request("POST", "/v1/taskbus/rows/%d/heartbeat" % int(claim.row_id),
                             {}, nonce=claim.nonce)

    # ---- recursion -------------------------------------------------------
    def spawn_depth(self, row_id):
        """(depth, problem) for a row, walking parent_task upward.

        The chain is the authority, not a counter a caller could omit. The walk is
        cycle-safe and capped independently of MAX_SPAWN_DEPTH, and a malformed
        chain reports a problem instead of looping.
        """
        depth = 0
        seen = set()
        try:
            current = int(row_id)
        except (TypeError, ValueError):
            return 0, "row id %r is not an integer" % (row_id,)
        while True:
            if current in seen:
                return depth, "parent chain cycles at row %s" % current
            seen.add(current)
            if len(seen) > PARENT_CHAIN_HARD_CAP:
                return depth, "parent chain exceeded %d rows" % PARENT_CHAIN_HARD_CAP
            try:
                row = self.get_task(current)
            except TaskbusError as exc:
                return depth, "could not read row %s: %s" % (current, exc)
            parents = row.parent_task
            if not parents:
                return depth, ""
            if len(parents) > 1:
                return depth, ("row %s has %d parents; the chain must be linear"
                               % (current, len(parents)))
            current = parents[0]
            depth += 1
            if depth >= MAX_SPAWN_DEPTH:
                # Deep enough to refuse; the rest of the chain cannot change that.
                return depth, ""

    # ---- credentials -----------------------------------------------------
    def exchange_key(self, ttl_minutes=15, scopes=None) -> Dict[str, Any]:
        """Mint a short-lived child key. TTL floor is 15 minutes; below that is a 422."""
        body = {"ttl_minutes": int(ttl_minutes)}
        if scopes:
            body["scopes"] = list(scopes)
        return self._request("POST", "/v1/keys/exchange", body)

    @classmethod
    def enroll(cls, enrollment_token, node_name=None, identifier=None, base_url=None):
        """Redeem a single-use enrollment token for a machine key. Shown once."""
        url = (base_url or os.environ.get("TASKBUS_GATEWAY_BASE") or DEFAULT_BASE_URL).rstrip("/")
        body = {"enrollment_token": enrollment_token}
        if node_name:
            body["node_name"] = node_name
        if identifier:
            body["identifier"] = identifier
        resp = httpx.post(url + "/v1/registry/machines/onboard", json=body,
                          headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
                          timeout=60.0)
        if resp.status_code >= 400:
            payload = resp.json() if resp.content else {}
            raise error_for(resp.status_code, payload.get("error"),
                            payload.get("message") or resp.text[:200])
        return resp.json()

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
