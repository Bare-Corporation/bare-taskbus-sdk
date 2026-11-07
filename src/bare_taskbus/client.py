"""TaskbusClient - one method per gateway route, no hidden behaviour.

Deliberate design notes, each of which cost a real attempt to learn on the fleet:
  * the claim nonce travels in the X-Claim-Nonce HEADER. A nonce in the JSON body is ignored and
    every write then answers 409 "Stale or missing claim nonce", which reads like an expired lease.
  * the list endpoint paginates on page and page_size. The alias "size" is accepted by some builds
    and silently ignored by others, so this client only ever sends page_size.
  * an executor may move a row to InProgress, HumanReviewNeeded or Blocked. Completed is refused to
    an executor, so the client blocks it locally instead of spending a call on a 403.
  * the lease is 900 seconds and cannot be renewed. There is no renew route and a PATCH does not
    extend lease_expires_at: re-claiming after expiry is the only way to get more time, and it
    mints a new nonce. Reclamation is LAZY (no sweeper), so a row becomes claimable at expiry but
    is only reclaimed when somebody next claims it. 45 minutes is the cumulative leased execution
    time across the default 3 attempts, not a wall-clock ceiling.
  * POST /rows/{id}/review is tenant-gated. If the caller stakeholder is not on the row
    stakeholder_account_master, the route answers 404 row_not_found - deliberately a 404 and not
    a 403, because a 403 would confirm the row exists in another tenant. That gate runs BEFORE the
    idempotent branch, so a foreign row recorded verdict is never echoed back.
"""
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .errors import TaskbusForbidden, TaskbusValidation, error_for

DEFAULT_BASE_URL = "https://api.bare-ai.net"
DEFAULT_UA = "bare-taskbus-sdk/0.1.0"
EXECUTOR_STATUSES = ("InProgress", "HumanReviewNeeded", "Blocked")


@dataclass
class Row:
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self):
        return self.raw.get("id")

    @property
    def status(self):
        return _val(self.raw.get("status"))

    @property
    def priority(self):
        return _val(self.raw.get("priority"))

    @property
    def short_description(self):
        return self.raw.get("short_description")

    @property
    def success_criterion(self):
        return self.raw.get("success_criterion")

    @property
    def assigned(self):
        return _val(self.raw.get("assigned_to_stakeholder_representative"))

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


def _val(value):
    """Taskbus single-select and link fields arrive as objects or lists of objects."""
    if isinstance(value, dict):
        return value.get("value")
    if isinstance(value, list):
        out = [_val(v) for v in value]
        return out[0] if len(out) == 1 else out
    return value


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
    def list_tasks(self, status="new,in_progress", page=1, page_size=100) -> List[Row]:
        path = "/v1/taskbus/rows?status=%s&page=%d&page_size=%d" % (status, page, page_size)
        data = self._request("GET", path)
        return [Row(r) for r in (data.get("results") or data.get("rows") or [])]

    def get_task(self, row_id) -> Row:
        return Row(self._request("GET", "/v1/taskbus/rows/%d" % int(row_id)))

    # ---- write -----------------------------------------------------------
    def create_task(self, short_description, ai_prompt, success_criterion=None,
                    priority="medium", max_attempts=3, assigned_rep_id=None,
                    depends_on=None) -> Row:
        body = {"short_description": short_description, "ai_prompt": ai_prompt,
                "priority": priority, "max_attempts": max_attempts}
        if success_criterion:
            body["success_criterion"] = success_criterion
        if assigned_rep_id:
            body["assigned_to_stakeholder_representative"] = [int(assigned_rep_id)]
        if depends_on:
            body["dependant_on_task"] = [int(x) for x in depends_on]
        return Row(self._request("POST", "/v1/taskbus/rows", body))

    def claim_task(self, row_id) -> Claim:
        data = self._request("POST", "/v1/taskbus/rows/%d/claim" % int(row_id), {})
        return Claim(row_id=int(row_id), nonce=data.get("nonce"), raw=data)

    def update_task(self, claim: Claim, status=None, machines_response=None,
                    reason=None, **extra) -> Row:
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
