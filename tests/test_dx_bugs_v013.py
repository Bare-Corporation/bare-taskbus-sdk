"""Regression tests for the two DX bugs found in the live onboarding test (0.1.3).

Both were reproduced against https://api.bare-ai.net before being fixed:

  * list_tasks() sent ?status=new,in_progress and the gateway answered
    400 "Unknown status value: 'new,in_progress'". status is a REPEATABLE query
    parameter: status=new&status=in_progress is accepted, and omitting it entirely
    defaults to New and InProgress.
  * Row properties returned None instead of the field value. The list and
    single-row endpoints return bare rows with single-selects as
    {"id":..,"value":..,"color":..}, but the create response is untyped in the
    OpenAPI spec ("field-level shape is stable in practice but not yet promoted to
    a typed model"), so the same field can arrive nested, as a bare id, or as a
    link row with no "value".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bare_taskbus.client import Row, TaskbusClient, _val  # noqa: E402


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
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "body": json})
        return self.response


def make_client(response=None):
    return TaskbusClient(api_key="bck-test", base_url="https://example.invalid",
                         client=DummyClient(response))


def last_url(client):
    return client._client.calls[-1]["url"]


# ---------------------------------------------------------------- list_tasks status

def test_default_list_sends_repeated_status_params():
    client = make_client()
    client.list_tasks()
    url = last_url(client)
    assert "status=new" in url
    assert "status=in_progress" in url
    # A comma-joined value is the 400 the gateway actually returns.
    assert "status=new,in_progress" not in url
    assert "," not in url


def test_default_list_has_no_single_status_only():
    """Control for the test above: both values must be present, not just one."""
    client = make_client()
    client.list_tasks()
    assert last_url(client).count("status=") == 2


def test_comma_string_is_split_into_repeated_params():
    client = make_client()
    client.list_tasks(status="new,in_progress")
    url = last_url(client)
    assert "status=new" in url
    assert "status=in_progress" in url
    assert "new,in_progress" not in url


def test_a_list_of_statuses_is_repeated():
    client = make_client()
    client.list_tasks(status=["new", "InProgress"])
    url = last_url(client)
    assert "status=new" in url
    assert "status=InProgress" in url


def test_none_omits_the_status_parameter_entirely():
    client = make_client()
    client.list_tasks(status=None)
    url = last_url(client)
    assert "status=" not in url
    assert "page_size=100" in url


def test_a_status_with_spaces_is_url_encoded():
    # "Draft - Not Ready for Poller" is a real label on this board.
    client = make_client()
    client.list_tasks(status="Draft - Not Ready for Poller")
    url = last_url(client)
    assert "Draft%20-%20Not%20Ready%20for%20Poller" in url
    assert " " not in url


def test_pagination_still_uses_page_size():
    client = make_client()
    client.list_tasks(page=2, page_size=50)
    url = last_url(client)
    assert "page=2" in url
    assert "page_size=50" in url


# ------------------------------------------------------------------- Row unwrapping

GET_ROW = {
    "id": 718,
    "status": {"id": 8262, "value": "New", "color": "blue"},
    "priority": {"id": 1, "value": "low", "color": "gray"},
    "short_description": "a row as the LIST endpoint returns it",
    "success_criterion": "file_exists:/tmp/x",
    "assigned_to_stakeholder_representative": [
        {"id": 84, "order": 1, "value": "bare-sales@host"}
    ],
}


def test_get_shape_unwraps_single_selects_and_plain_fields():
    row = Row(GET_ROW)
    assert row.id == 718
    assert row.status == "New"
    assert row.priority == "low"
    assert row.short_description == "a row as the LIST endpoint returns it"
    assert row.success_criterion == "file_exists:/tmp/x"
    assert row.assigned == "bare-sales@host"


def test_create_echo_nested_under_fields_is_unwrapped():
    row = Row({"id": 900, "fields": {"status": {"id": 8262, "value": "New"},
                                     "short_description": "nested echo",
                                     "priority": "medium"}})
    assert row.id == 900
    assert row.status == "New"
    assert row.short_description == "nested echo"
    assert row.priority == "medium"


def test_create_echo_with_bare_status_string():
    row = Row({"id": 901, "status": "New", "priority": "high",
               "short_description": "flat echo"})
    assert row.status == "New"
    assert row.priority == "high"
    assert row.short_description == "flat echo"


def test_link_without_a_value_falls_back_to_its_id():
    row = Row({"id": 902, "assigned_to_stakeholder_representative": [84]})
    assert row.assigned == 84
    assert _val({"id": 84, "order": 1}) == 84


def test_absent_field_is_none_and_get_takes_a_default():
    row = Row({"id": 903})
    assert row.status is None
    assert row.priority is None
    assert row.short_description is None
    assert row.get("status", "unknown") == "unknown"
    assert row.get("status") is None


def test_a_non_dict_raw_does_not_raise():
    assert Row(None).status is None
    assert Row(None).get("status", "x") == "x"


# --------------------------------------------------------- pre-existing contracts

def test_value_unwrapping_unchanged():
    assert _val({"id": 3, "value": "New"}) == "New"
    assert _val([{"id": 335, "value": "bare-sales@host"}]) == "bare-sales@host"
    assert _val(None) is None
    assert _val("plain") == "plain"
