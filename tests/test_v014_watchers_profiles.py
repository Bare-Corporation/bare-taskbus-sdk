"""Tests for 0.1.4: list-preserving Row.watchers and get_execution_profile().

Wire shapes here were measured against the live gateway on 2026-11-20, not guessed:

  * a row carries watchers_stakeholder_representative and it comes back as a LIST
    (an empty row returns []). Nothing about the field is scalar, so the client must
    not flatten it.
  * GET /v1/taskbus/rows/{id}/execution-profile returns 200 for a row that ran under a
    profile, with top keys row_id, profile_id, slug, version, config_hash, profile_hash,
    resolved_at, integrity, reviewer_authority, role and snapshot.
  * the same route answers 404 {"error": "no_execution_profile"} for a row that never
    ran under one - a different 404 from row_not_found, which is also what the gateway
    returns for another tenant's row.

The write-path cases below drive a stub at the HTTP boundary (the same DummyClient the
other suites use). That proves the client puts the right bytes on the wire and maps the
response correctly; it does not re-prove the gateway contract, which is why the measured
facts above are recorded here rather than re-asserted as if a stub had demonstrated them.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bare_taskbus import (Claim, ExecutionProfile, Row, TaskbusClient,  # noqa: E402
                          TaskbusError, TaskbusForbidden)
from bare_taskbus.client import _ids  # noqa: E402

WATCHERS = "watchers_stakeholder_representative"
WATCHER_ID = 271
PARENT_ID = 5


class DummyResponse:
    def __init__(self, status_code=200, payload=None, content=b"{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content
        self.text = str(payload)

    def json(self):
        return self._payload


class DummyClient:
    """Records every request; replays one response per call (the last one repeats)."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [DummyResponse()])

    def request(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "body": json})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def make_client(responses=None):
    return TaskbusClient(api_key="bck-test", base_url="https://example.invalid",
                         client=DummyClient(responses))


def last_call(client):
    return client._client.calls[-1]


# ------------------------------------------------------------------ _ids / Row.watchers
def test_ids_preserves_a_single_element_list():
    assert _ids([WATCHER_ID]) == [WATCHER_ID]


def test_ids_accepts_every_shape():
    assert _ids(None) == []
    assert _ids([]) == []
    assert _ids(WATCHER_ID) == [WATCHER_ID]
    assert _ids("271") == [WATCHER_ID]
    assert _ids([{"id": WATCHER_ID}]) == [WATCHER_ID]
    assert _ids([{"id": WATCHER_ID, "value": "bare-ai-developer @host"}]) == [WATCHER_ID]
    assert _ids([{"id": 1}, {"id": 2}]) == [1, 2]


def test_ids_skips_unusable_entries_instead_of_raising():
    assert _ids([{"value": "no id"}, WATCHER_ID, "not-a-number", None]) == [WATCHER_ID]


def test_row_watchers_with_one_watcher_is_a_list_not_a_scalar():
    row = Row({WATCHERS: [{"id": WATCHER_ID, "value": "bare-ai-developer @host"}]})
    assert row.watchers == [WATCHER_ID]
    # The whole point of _ids: _val() would collapse this to the bare integer.
    assert not isinstance(row.watchers, int)


def test_row_watchers_empty_and_absent_are_empty_lists():
    assert Row({WATCHERS: []}).watchers == []
    assert Row({}).watchers == []


def test_row_watchers_works_through_a_nested_fields_envelope():
    assert Row({"fields": {WATCHERS: [{"id": WATCHER_ID}]}}).watchers == [WATCHER_ID]


def test_row_watchers_and_parent_task_do_not_share_a_field():
    row = Row({WATCHERS: [{"id": WATCHER_ID}], "parent_task": [{"id": PARENT_ID}]})
    assert row.watchers == [WATCHER_ID]
    assert row.parent_task == [PARENT_ID]


# ------------------------------------------------------------------------ create_task
def test_create_with_one_watcher_emits_a_link_list():
    client = make_client()
    client.create_task("t", "p", watchers=[WATCHER_ID])
    call = last_call(client)
    assert call["method"] == "POST"
    assert call["body"][WATCHERS] == [WATCHER_ID]


def test_create_with_several_watchers_keeps_order():
    client = make_client()
    client.create_task("t", "p", watchers=[271, 84])
    assert last_call(client)["body"][WATCHERS] == [271, 84]


def test_create_with_empty_watcher_list_sends_the_field():
    client = make_client()
    client.create_task("t", "p", watchers=[])
    assert last_call(client)["body"][WATCHERS] == []


def test_create_without_watchers_omits_the_field():
    client = make_client()
    client.create_task("t", "p")
    assert WATCHERS not in last_call(client)["body"]


def test_create_parent_task_accepts_a_scalar_or_a_list():
    client = make_client()
    client.create_task("t", "p", parent_task=PARENT_ID)
    assert last_call(client)["body"]["parent_task"] == [PARENT_ID]

    client = make_client()
    client.create_task("t", "p", parent_task=[9, 10])
    assert last_call(client)["body"]["parent_task"] == [9, 10]

    client = make_client()
    client.create_task("t", "p")
    assert "parent_task" not in last_call(client)["body"]


def test_create_echo_with_a_single_watcher_reads_back_as_a_list():
    echo = {"id": 700, WATCHERS: [{"id": WATCHER_ID, "value": "watcher"}]}
    client = make_client([DummyResponse(200, echo)])
    row = client.create_task("t", "p", watchers=[WATCHER_ID])
    assert row.watchers == [WATCHER_ID]


# ------------------------------------------------------------------------ update_task
def test_update_sets_watchers():
    claim = Claim(row_id=700, nonce="nonce-abc")
    client = make_client()
    client.update_task(claim, watchers=[WATCHER_ID])
    call = last_call(client)
    assert call["method"] == "PATCH"
    assert call["body"][WATCHERS] == [WATCHER_ID]
    assert call["headers"]["X-Claim-Nonce"] == "nonce-abc"


def test_update_clears_watchers_with_an_empty_list():
    claim = Claim(row_id=700, nonce="nonce-abc")
    client = make_client()
    client.update_task(claim, watchers=[])
    body = last_call(client)["body"]
    # Present AND empty: an omitted key would leave the watchers in place and the caller
    # would believe the change landed.
    assert WATCHERS in body
    assert body[WATCHERS] == []


def test_update_without_watchers_does_not_touch_the_field():
    claim = Claim(row_id=700, nonce="nonce-abc")
    client = make_client()
    client.update_task(claim, machines_response="progress")
    assert WATCHERS not in last_call(client)["body"]


def test_update_still_requires_a_nonce_for_a_watcher_change():
    client = make_client()
    try:
        client.update_task(Claim(row_id=700, nonce=None), watchers=[WATCHER_ID])
    except TaskbusError as exc:
        assert "nonce" in str(exc)
    else:
        raise AssertionError("a watcher PATCH without a nonce must be refused locally")
    assert client._client.calls == []


def test_cross_tenant_watcher_write_raises_forbidden_and_is_not_retried():
    rejection = DummyResponse(403, {"error": "tenant_scope_violation",
                                    "message": "watchers_stakeholder_representative "
                                               "must belong to your tenant"})
    claim = Claim(row_id=700, nonce="nonce-abc")
    client = make_client([rejection])
    try:
        client.update_task(claim, watchers=[999])
    except TaskbusForbidden as exc:
        assert exc.status == 403
        assert exc.code == "tenant_scope_violation"
    else:
        raise AssertionError("a cross-tenant watcher must raise, not be dropped silently")
    assert len(client._client.calls) == 1


# --------------------------------------------------------------- get_execution_profile
PROFILE_PAYLOAD = {
    "row_id": 523,
    "profile_id": 2,
    "slug": "security-audit-frontier",
    "version": 1,
    "config_hash": "ad03d1d82186e18e",
    "profile_hash": "478836062f106723",
    "resolved_at": "2026-11-08T19:48:42Z",
    "integrity": "verified",
    "reviewer_authority": False,
    "role": {"alias": "security_auditor", "content_sha256": "abc123"},
    "snapshot": {
        "snapshot_version": 1,
        "profile": {"id": 2, "slug": "security-audit-frontier"},
        "role": {"alias": "security_auditor"},
        "resolved_at": "2026-11-08T19:48:42Z",
        "execution_config": {
            "pinned_model": "claude-sonnet-4-6|anthropic-us",
            "exclude_providers": ["deepseek", "openai"],
            "routing_strategy": "pinned",
            "max_attempts": 3,
            "require_human_review": True,
        },
    },
}


def test_get_execution_profile_parses_the_snapshot():
    client = make_client([DummyResponse(200, PROFILE_PAYLOAD)])
    profile = client.get_execution_profile(523)
    assert isinstance(profile, ExecutionProfile)
    assert last_call(client)["url"].endswith("/v1/taskbus/rows/523/execution-profile")
    assert profile.row_id == 523
    assert profile.profile_id == 2
    assert profile.slug == "security-audit-frontier"
    assert profile.config_hash == "ad03d1d82186e18e"
    assert profile.role_alias == "security_auditor"
    assert profile.integrity == "verified"
    assert profile.pinned_model == "claude-sonnet-4-6|anthropic-us"
    assert profile.exclude_providers == ["deepseek", "openai"]
    assert profile.routing_strategy == "pinned"


def test_get_execution_profile_tolerates_a_snapshot_without_pinned_model():
    legacy = {
        "row_id": 523,
        "profile_id": 2,
        "slug": "security-audit-frontier",
        "config_hash": "ad03d1d82186e18e",
        "snapshot": {"execution_config": {"exclude_providers": [],
                                          "routing_strategy": "cheapest_frontier"}},
    }
    client = make_client([DummyResponse(200, legacy)])
    profile = client.get_execution_profile(523)
    # Absent pin means "this run was not pinned", not "unknown" - and it must not raise.
    assert profile.pinned_model is None
    assert profile.exclude_providers == []
    assert profile.routing_strategy == "cheapest_frontier"


def test_get_execution_profile_returns_none_when_the_row_has_no_profile():
    no_profile = DummyResponse(404, {"error": "no_execution_profile",
                                     "message": "this row was not executed under an "
                                                "execution profile"})
    client = make_client([no_profile])
    assert client.get_execution_profile(1) is None


def test_get_execution_profile_raises_for_a_missing_or_foreign_row():
    missing = DummyResponse(404, {"error": "row_not_found", "message": "row not found"})
    client = make_client([missing])
    try:
        client.get_execution_profile(999999)
    except TaskbusError as exc:
        assert exc.status == 404
        assert exc.code == "row_not_found"
    else:
        raise AssertionError("an absent row must not read as a row without a profile")


def test_get_execution_profile_raises_when_the_gateway_is_unavailable():
    down = DummyResponse(503, {"error": "auth_unavailable", "message": "try later"})
    client = make_client([down])
    try:
        client.get_execution_profile(523)
    except TaskbusError:
        pass
    else:
        raise AssertionError("a 503 must raise, never be reported as 'no profile'")


def test_spawn_depth_reports_a_read_failure_instead_of_raising_name_error():
    # Regression: client.py caught TaskbusError in spawn_depth() without importing it, so
    # this path raised NameError rather than returning a problem string.
    missing = DummyResponse(404, {"error": "row_not_found", "message": "row not found"})
    client = make_client([missing])
    depth, problem = client.spawn_depth(999)
    assert depth == 0
    assert "could not read row" in problem
