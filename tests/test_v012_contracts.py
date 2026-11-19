"""Offline tests for the v0.1.2 contracts: heartbeat, spawn depth, supervisor close.

No network: every gateway interaction goes through a stub that records the call.
The three behaviours worth pinning are the ones an out-of-date SDK gets wrong:

  * a claim is renewable by heartbeat, and renewal is bounded by the horizon;
  * a 409 from the heartbeat is terminal (the claim is gone, never revived);
  * recursion is bounded by the row chain, refused at MAX_SPAWN_DEPTH.

Plus the SUPERVISOR_RESULT contract, which may never close a row as Completed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bare_taskbus import (HEARTBEAT_HORIZON_SECONDS, LEASE_SECONDS, MAX_SPAWN_DEPTH,
                          SUPERVISOR_RESULT_PREFIX, __version__,
                          parse_supervisor_result, supervisor_close_body)
from bare_taskbus.client import DEFAULT_UA, Claim, Row, TaskbusClient
from bare_taskbus.errors import TaskbusConflict, TaskbusValidation
from bare_taskbus.supervisor import SUPERVISOR_TERMINAL_STATUSES
from bare_taskbus.worker import HandlerResult, TaskbusWorker


class StubResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}"
        self.text = str(self._payload)

    def json(self):
        return self._payload


class StubTransport:
    """Records every request; answers from a queue, or the same response always."""

    def __init__(self, responses=None, default=None):
        self.calls = []
        self.queue = list(responses or [])
        self.default = default or StubResponse()

    def request(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "body": json})
        return self.queue.pop(0) if self.queue else self.default


def client_with(transport):
    return TaskbusClient(api_key="bck-test", base_url="https://example.invalid",
                         client=transport)


# ---- heartbeat ------------------------------------------------------------
def test_heartbeat_posts_to_the_route_with_the_nonce():
    transport = StubTransport([StubResponse(200, {"lease_expires_at": "2026-01-01T00:15:00Z"})])
    client = client_with(transport)
    payload = client.heartbeat(Claim(row_id=697, nonce="abc123"))
    call = transport.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1/taskbus/rows/697/heartbeat")
    assert call["headers"].get("X-Claim-Nonce") == "abc123"
    assert payload["lease_expires_at"] == "2026-01-01T00:15:00Z"


def test_heartbeat_needs_a_nonce():
    transport = StubTransport()
    client = client_with(transport)
    try:
        client.heartbeat(Claim(row_id=1, nonce=None))
    except TaskbusValidation:
        pass
    else:
        raise AssertionError("a heartbeat without a nonce must be refused locally")
    assert transport.calls == []


def test_heartbeat_409_is_a_conflict_not_a_retryable_error():
    transport = StubTransport([StubResponse(409, {"error": "stale_claim_nonce",
                                                  "message": "Stale or missing claim nonce"})])
    client = client_with(transport)
    try:
        client.heartbeat(Claim(row_id=5, nonce="old"))
    except TaskbusConflict:
        pass
    else:
        raise AssertionError("409 must raise TaskbusConflict")


def test_horizon_is_bounded_and_above_the_lease():
    assert LEASE_SECONDS == 900
    assert HEARTBEAT_HORIZON_SECONDS == 7200
    assert HEARTBEAT_HORIZON_SECONDS > LEASE_SECONDS


# ---- spawn depth ----------------------------------------------------------
def _chain_client(rows, fail_on=None):
    """A client whose get_task reads a dict of rows, and which records the reads."""

    class ChainClient:
        def __init__(self):
            self.reads = []

        def get_task(self, row_id):
            self.reads.append(row_id)
            if fail_on is not None and int(row_id) == fail_on:
                from bare_taskbus.errors import TaskbusError
                raise TaskbusError("row %s not readable" % row_id)
            return Row(rows.get(int(row_id), {"id": row_id}))

    return ChainClient()


def test_spawn_depth_walks_the_parent_chain():
    client = _chain_client({10: {"id": 10, "parent_task": [{"id": 9}]},
                            9: {"id": 9, "parent_task": [{"id": 7}]},
                            7: {"id": 7, "parent_task": []}})
    depth, problem = TaskbusClient.spawn_depth(client, 10)
    assert (depth, problem) == (2, "")


def test_spawn_depth_stops_at_the_limit():
    client = _chain_client({5: {"id": 5, "parent_task": [4]},
                            4: {"id": 4, "parent_task": [3]},
                            3: {"id": 3, "parent_task": [2]},
                            2: {"id": 2, "parent_task": []}})
    depth, problem = TaskbusClient.spawn_depth(client, 5)
    assert depth >= MAX_SPAWN_DEPTH
    assert problem == ""
    assert client.reads == [5, 4, 3]          # stopped once refusal was certain


def test_spawn_depth_reports_a_cycle():
    client = _chain_client({1: {"id": 1, "parent_task": [2]},
                            2: {"id": 2, "parent_task": [1]}})
    depth, problem = TaskbusClient.spawn_depth(client, 1)
    assert "cycle" in problem


def test_spawn_depth_reports_multiple_parents():
    client = _chain_client({8: {"id": 8, "parent_task": [6, 7]}})
    depth, problem = TaskbusClient.spawn_depth(client, 8)
    assert "linear" in problem


def test_row_parent_task_accepts_every_shape():
    assert Row({"parent_task": [3]}).parent_task == [3]
    assert Row({"parent_task": [{"id": 4}]}).parent_task == [4]
    assert Row({"parent_task": 5}).parent_task == [5]
    assert Row({}).parent_task == []


# ---- worker ---------------------------------------------------------------
class WorkerStub:
    """The narrow client surface TaskbusWorker uses, with call recording."""

    def __init__(self, rows, depth=(0, ""), heartbeat_error=None):
        self.api_key = "bck-test"
        self.base_url = "https://example.invalid"
        self.rows = rows
        self.depth = depth
        self.heartbeat_error = heartbeat_error
        self.claimed = []
        self.written = []
        self.released = []
        self.beats = []

    def list_tasks(self, status=None, page_size=None):
        return self.rows

    def spawn_depth(self, row_id):
        return self.depth

    def claim_task(self, row_id):
        self.claimed.append(row_id)
        return Claim(row_id=row_id, nonce="nonce-%s" % row_id)

    def update_task(self, claim, status=None, machines_response=None, reason=None):
        self.written.append({"row": claim.row_id, "status": status,
                             "summary": machines_response, "reason": reason})
        return Row({"id": claim.row_id})

    def release_task(self, claim, reason):
        self.released.append({"row": claim.row_id, "reason": reason})
        return Row({"id": claim.row_id})

    def heartbeat(self, claim):
        self.beats.append(claim.row_id)
        if self.heartbeat_error:
            raise self.heartbeat_error
        return {"lease_expires_at": "renewed"}


def _worker(stub, handler):
    worker = TaskbusWorker(stub, handler=handler, refresh=False, max_concurrent=1)
    worker.heartbeat_enabled = False          # keep the loop deterministic
    # run_once() asks _with_child() for a real client built from the stub's
    # key/base_url; hand back the recording stub instead so the cycle logic is
    # exercised against it.
    worker._with_child = lambda: stub
    return worker


def test_depth_limit_refuses_before_the_handler_runs():
    called = []

    def handler(row, client):
        called.append(row.id)
        return HandlerResult()

    stub = WorkerStub([Row({"id": 42, "status": "New"})], depth=(MAX_SPAWN_DEPTH, ""))
    summary = _worker(stub, handler).run_once()
    assert called == []                       # the handler never ran
    assert stub.claimed == [42]               # claimed, so Blocked is writable
    assert stub.released and "limit" in stub.released[0]["reason"]
    assert summary["blocked"] == 1


def test_below_the_depth_limit_dispatches_normally():
    stub = WorkerStub([Row({"id": 11, "status": "New"})], depth=(1, ""))
    summary = _worker(stub, lambda row, client: HandlerResult()).run_once()
    assert stub.claimed == [11]
    assert stub.released == []
    assert summary["completed"] == 1


def test_heartbeat_409_marks_the_claim_lost():
    stub = WorkerStub([], heartbeat_error=TaskbusConflict("claim released", 409))
    worker = _worker(stub, lambda row, client: HandlerResult())
    worker.heartbeat_interval = 0             # one pass, no waiting
    worker._heartbeat_loop(Claim(row_id=7, nonce="n"), _stop_event())
    assert worker._claim_lost is True
    assert stub.beats == [7]


def test_a_worker_that_lost_its_claim_does_not_write_back():
    stub = WorkerStub([Row({"id": 9, "status": "New"})])
    worker = _worker(stub, lambda row, client: HandlerResult(status="HumanReviewNeeded"))

    def fake_run(row, client, claim=None):
        worker._claim_lost = True             # as the heartbeat thread would
        return HandlerResult(status="HumanReviewNeeded", summary="done")

    worker._run_handler = fake_run
    summary = worker.run_once()
    assert stub.written == []                 # nothing attempted with a dead nonce
    assert summary["lost"] == 1


class _stop_event:
    """A stop event that never fires, so a heartbeat loop runs exactly once."""

    def wait(self, timeout):
        return False


def test_supervisor_envelope_decides_the_close():
    envelope = SUPERVISOR_RESULT_PREFIX + (
        '{"outcome": "verified", "reason": "criteria met", "passes": 2, '
        '"state_path": "/tmp/s.md", "status_hint": "HumanReviewNeeded"}')
    stub = WorkerStub([Row({"id": 3, "status": "New"})])
    worker = _worker(stub, lambda row, client: HandlerResult(summary=envelope))
    worker.run_once()
    assert stub.written[0]["status"] == "HumanReviewNeeded"
    assert "supervisor outcome=verified" in stub.written[0]["summary"]
    assert "criteria met" in stub.written[0]["summary"]


# ---- SUPERVISOR_RESULT ----------------------------------------------------
def test_parse_finds_the_last_envelope_amid_noise():
    NL = chr(10)
    text = NL.join([
        "pass 1",
        SUPERVISOR_RESULT_PREFIX + '{"outcome": "budget_exhausted"}',
        SUPERVISOR_RESULT_PREFIX + '{"outcome": "verified", "passes": 2}',
        "trailing",
    ])
    assert parse_supervisor_result(text)["outcome"] == "verified"


def test_parse_is_total():
    assert parse_supervisor_result(None) is None
    assert parse_supervisor_result("no envelope here") is None
    assert parse_supervisor_result(SUPERVISOR_RESULT_PREFIX + "{broken") is None
    assert parse_supervisor_result(SUPERVISOR_RESULT_PREFIX + "[1,2]") is None


def test_close_body_never_says_completed():
    for hint in ("Completed", "New", "InProgress", "", None):
        body = supervisor_close_body({"outcome": "verified", "status_hint": hint})
        assert body["status"] in SUPERVISOR_TERMINAL_STATUSES
        assert body["status"] != "Completed"


def test_blocked_close_carries_a_reason():
    body = supervisor_close_body({"outcome": "blocked", "status_hint": "Blocked",
                                  "reason": "needs a human decision"})
    assert body["status"] == "Blocked"
    assert body["re_opened_or_retry_reason"] == "needs a human decision"


def test_close_body_of_nothing_is_none():
    assert supervisor_close_body(None) is None
    assert supervisor_close_body("not a dict") is None


# ---- version --------------------------------------------------------------
def test_version_user_agent_and_pyproject_agree():
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
        pyproject = fh.read()
    assert 'version = "%s"' % __version__ in pyproject
    assert DEFAULT_UA == "bare-taskbus-sdk/%s" % __version__
