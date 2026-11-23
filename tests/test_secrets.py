"""Offline tests for the /v1/secrets client.

Everything runs against a stateful in-memory transport that speaks the PUBLISHED wire shape:
POST /v1/secrets/{name} writes, GET /v1/secrets/{name} reads or 404s, GET /v1/secrets lists
names, DELETE /v1/secrets/{name} removes. No live key is required and no socket is opened.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bare_taskbus.errors import TaskbusError, TaskbusValidation
from bare_taskbus.secrets import SecretsClient, SecretNotFound, _names


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.content = b"{}"
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSecretsBackend:
    """A tiny stand-in for the gateway: enough to exercise real set/get/list/delete calls."""

    def __init__(self, fail_with=None):
        self.store = {}
        self.calls = []
        self.fail_with = fail_with

    def request(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "body": json})
        if self.fail_with is not None:
            return self.fail_with
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        parts = path.split("/")
        # parts[0] == "v1", parts[1] == "secrets"
        if parts[:2] != ["v1", "secrets"]:
            return FakeResponse(404, {"error": "not_found"})
        if len(parts) == 2:
            if method == "GET":
                return FakeResponse(200, {"keys": sorted(self.store)})
            return FakeResponse(405, {"error": "method_not_allowed"})
        name = "/".join(parts[2:])
        if method == "POST":
            self.store[name] = dict(json or {})
            return FakeResponse(200, {"name": name, "version": 1})
        if method == "GET":
            if name not in self.store:
                return FakeResponse(404, {"error": "secret_not_found"})
            return FakeResponse(200, dict(self.store[name]))
        if method == "DELETE":
            if name not in self.store:
                return FakeResponse(404, {"error": "secret_not_found"})
            del self.store[name]
            return FakeResponse(200, {"deleted": name})
        return FakeResponse(405, {"error": "method_not_allowed"})


def make_client(backend=None, api_key="bck-test"):
    backend = backend or FakeSecretsBackend()
    return SecretsClient(api_key=api_key, base_url="https://example.invalid",
                         client=backend), backend


def test_missing_key_raises_before_any_network_call(monkeypatch):
    """No api_key and no env fallback must fail closed, and must not open a socket."""
    monkeypatch.delenv("TASKBUS_API_KEY", raising=False)
    backend = FakeSecretsBackend()
    with pytest.raises(TaskbusValidation):
        SecretsClient(api_key="", base_url="https://example.invalid", client=backend)
    assert backend.calls == []


def test_set_then_get_round_trips():
    client, backend = make_client()
    client.set("greeting", "hello")
    written = backend.calls[-1]
    assert written["method"] == "POST"
    assert written["url"].endswith("/v1/secrets/greeting")
    assert written["body"] == {"value": "hello"}
    assert client.get("greeting")["value"] == "hello"


def test_set_accepts_a_mapping_as_the_body_itself():
    client, backend = make_client()
    client.set("db", {"user": "agent", "pass": "not-a-real-secret"})
    assert backend.calls[-1]["body"] == {"user": "agent", "pass": "not-a-real-secret"}
    assert client.get("db")["user"] == "agent"


def test_set_refuses_a_value_the_api_would_reject():
    client, backend = make_client()
    with pytest.raises(TaskbusValidation):
        client.set("bad", 12345)
    with pytest.raises(TaskbusValidation):
        client.set("bad", {})
    assert backend.calls == []


def test_list_returns_names_not_values():
    client, backend = make_client()
    client.set("alpha", "value-of-alpha")
    client.set("beta", "value-of-beta")
    names = client.list()
    assert names == ["alpha", "beta"]
    listed = backend.calls[-1]
    assert listed["method"] == "GET"
    assert listed["url"].endswith("/v1/secrets")
    flattened = " ".join(names)
    assert "value-of-alpha" not in flattened
    assert "value-of-beta" not in flattened


def test_delete_then_get_is_a_clean_not_found():
    client, backend = make_client()
    client.set("ephemeral", "x")
    client.delete("ephemeral")
    assert backend.calls[-1]["method"] == "DELETE"
    with pytest.raises(SecretNotFound) as missing:
        client.get("ephemeral")
    assert missing.value.status == 404
    assert backend.store == {}


def test_delete_of_an_absent_secret_is_also_not_found():
    client, _ = make_client()
    with pytest.raises(SecretNotFound):
        client.delete("never-existed")


def test_a_403_surfaces_as_the_sdk_error_type():
    backend = FakeSecretsBackend(fail_with=FakeResponse(403, {"error": "insufficient_scope"}))
    client, _ = make_client(backend)
    with pytest.raises(TaskbusError) as forbidden:
        client.get("anything")
    assert forbidden.value.status == 403
    assert not isinstance(forbidden.value, SecretNotFound)


def test_authentication_uses_x_api_key_and_never_bearer():
    """The edge rejects a Bearer credential, so the header shape is part of the contract."""
    client, backend = make_client(api_key="bck-example")
    client.list()
    headers = backend.calls[-1]["headers"]
    assert headers.get("X-API-Key") == "bck-example"
    assert "Authorization" not in headers


def test_a_name_with_a_slash_is_refused_locally():
    client, backend = make_client()
    with pytest.raises(TaskbusValidation):
        client.get("subtree/other")
    with pytest.raises(TaskbusValidation):
        client.set("", "x")
    assert backend.calls == []


def test_names_tolerates_the_shapes_the_untyped_route_has_used():
    assert _names({"keys": ["a", "b"]}) == ["a", "b"]
    assert _names(["a", "b"]) == ["a", "b"]
    assert _names({"data": {"names": ["a"]}}) == ["a"]
    assert _names({"results": [{"name": "a"}, {"path": "b"}]}) == ["a", "b"]
    assert _names({}) == []
    assert _names(None) == []


def test_escrow_deposit_goes_to_the_escrow_route():
    client, backend = make_client()
    client.escrow({"ciphertext": "b64", "shares": 5, "threshold": 3})
    sent = backend.calls[-1]
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/v1/secrets/escrow")
    assert sent["body"]["shares"] == 5
