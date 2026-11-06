"""Offline tests for the rules the client enforces before it spends a network call."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bare_taskbus.client import TaskbusClient, Claim, _val
from bare_taskbus.errors import TaskbusValidation, TaskbusForbidden, error_for, TaskbusConflict


class DummyResponse:
    def __init__(self, status_code=200, payload=None, content=b"{}"):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = str(payload)

    def json(self):
        return self._payload


class DummyClient:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or DummyResponse()

    def request(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": json})
        return self.response


def make_client(response=None):
    return TaskbusClient(api_key="bck-test", base_url="https://example.invalid",
                         client=DummyClient(response))


def test_no_api_key_is_refused():
    with pytest.raises(TaskbusValidation):
        TaskbusClient(api_key="", base_url="https://example.invalid", client=DummyClient())


def test_executor_cannot_set_completed():
    client = make_client()
    claim = Claim(row_id=1, nonce="n")
    with pytest.raises(TaskbusForbidden):
        client.update_task(claim, status="Completed")


def test_update_without_nonce_is_refused():
    client = make_client()
    with pytest.raises(TaskbusValidation):
        client.update_task(Claim(row_id=1, nonce=None), status="HumanReviewNeeded")


def test_nonce_goes_in_the_header():
    client = make_client()
    client.update_task(Claim(row_id=7, nonce="abc123"), status="HumanReviewNeeded")
    sent = client._client.calls[-1]
    assert sent["headers"].get("X-Claim-Nonce") == "abc123"
    assert "nonce" not in (sent["body"] or {})


def test_claim_and_user_agent_headers():
    client = make_client()
    client.list_tasks()
    headers = client._client.calls[-1]["headers"]
    assert headers.get("X-API-Key") == "bck-test"
    assert headers.get("User-Agent", "").startswith("bare-taskbus-sdk/")


def test_list_uses_page_size_not_size():
    client = make_client()
    client.list_tasks(page=2, page_size=50)
    url = client._client.calls[-1]["url"]
    assert "page_size=50" in url
    assert "size=" not in url.replace("page_size=", "")


def test_review_matrix_is_validated_locally():
    client = make_client()
    with pytest.raises(TaskbusValidation):
        client.review_task(1, verdict="Passed", status="Blocked", comments="x")
    with pytest.raises(TaskbusValidation):
        client.review_task(1, verdict="Failed", status="Completed", comments="x")
    with pytest.raises(TaskbusValidation):
        client.review_task(1, verdict="Maybe", status="Completed", comments="x")


def test_error_mapping():
    assert isinstance(error_for(409, "row_not_in_review", "nope"), TaskbusConflict)
    assert isinstance(error_for(403, "self_certification_forbidden", "no"), TaskbusForbidden)
    assert isinstance(error_for(422, "ttl_out_of_range", "min 15"), TaskbusValidation)


def test_value_unwrapping():
    assert _val({"id": 3, "value": "New"}) == "New"
    assert _val([{"id": 335, "value": "bare-sales@host"}]) == "bare-sales@host"
    assert _val(None) is None
