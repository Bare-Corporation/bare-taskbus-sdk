"""SecretsClient - the /v1/secrets routes, on the SAME transport as the task bus.

Deliberate design notes, each of which is a real trap on this platform:

  * authentication is X-API-Key ONLY. An "Authorization: Bearer <key>" header is rejected by the
    edge, so a client that sends the credential the way most REST APIs expect gets a 401 that
    looks like a bad key. This class inherits TaskbusClient._headers(), which already emits
    X-API-Key, rather than building a second header map.

  * a WRITE is POST to /v1/secrets/{name}, not a PUT. The body may be the data mapping itself or
    {"data": {...}}; the server answers 400 to anything else on purpose, because a silently
    coerced payload is how a secret ends up stored under the wrong keys. This client therefore
    never invents a shape: a str becomes {"value": <str>} and a Mapping is sent as itself.

  * LIST returns NAMES, never values, and its 200 body is promoted-to-a-typed-model only "in
    practice". _names() tolerates a bare list, a list of link-shaped dicts, and the handful of
    envelope keys the route has used, so a server-side reshape does not silently return [].
    An empty list is a valid answer, not a failure.

  * a 404 is the secrets API's "no such secret" and is raised as SecretNotFound, so callers do
    not have to compare status codes. Every other status still maps through errors.error_for,
    which keeps 401/403/409/5xx behaving exactly as they do for task-bus rows. error_for itself
    is deliberately NOT changed: it also serves the row routes, where a 404 means something else
    entirely (row_not_found, and a foreign tenant is answered the same way on purpose).

  * the escrow deposit is WRITE-ONLY. POST /v1/secrets/escrow stores an opaque envelope and
    returns no token and no ciphertext, so escrow() cannot verify its own write. escrow_status()
    exists as the read-back: it reports whether a deposit exists and its metadata only, which is
    what a caller can actually check after depositing.
"""
import urllib.parse
from typing import Any, Dict, List, Mapping, Union

from .client import TaskbusClient
from .errors import TaskbusError, TaskbusValidation

SECRETS_PATH = "/v1/secrets"
ESCROW_PATH = SECRETS_PATH + "/escrow"

# Envelope keys the list route has been seen to use for the name collection.
_NAME_KEYS = ("keys", "names", "secrets", "data", "items", "results")


class SecretNotFound(TaskbusError):
    """The named secret does not exist in this machine subtree (HTTP 404)."""


def _names(payload):
    """Pull the list of secret NAMES out of an untyped list response.

    Returns [] for an empty or unrecognised body. Deliberately never raises: a caller asking
    "what do I have?" should not have to catch anything to find out it has nothing.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        out = []
        for item in payload:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, Mapping):
                for key in ("name", "path", "key", "id"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        out.append(value)
                        break
        return out
    if isinstance(payload, Mapping):
        for key in _NAME_KEYS:
            if key in payload:
                return _names(payload[key])
    return []


class SecretsClient(TaskbusClient):
    """set() / get() / list() / delete() against /v1/secrets, plus the escrow routes.

    Inherits api_key handling, base_url resolution, the X-API-Key header and the error mapping
    from TaskbusClient. There is no second HTTP layer here by design: two transports drift.
    """

    # ---- paths -----------------------------------------------------------
    @staticmethod
    def _secret_name(name):
        """Validate a secret name before it is placed in a URL path.

        A name is ONE path segment. A slash would let a caller address a sibling subtree, so it
        is refused locally rather than percent-encoded and hoped for.
        """
        if not isinstance(name, str) or not name.strip():
            raise TaskbusValidation("secret name must be a non-empty string")
        if "/" in name:
            raise TaskbusValidation("secret name must be a single path segment (no slash)")
        if any(ord(ch) < 32 for ch in name):
            raise TaskbusValidation("secret name must not contain control characters")
        return name

    def _secret_path(self, name):
        return SECRETS_PATH + "/" + urllib.parse.quote(self._secret_name(name), safe="")

    def _request_404_as_not_found(self, method, path, body=None):
        """Run a request, translating a 404 into SecretNotFound and nothing else."""
        try:
            return self._request(method, path, body)
        except SecretNotFound:
            raise
        except TaskbusError as exc:
            if getattr(exc, "status", None) == 404:
                raise SecretNotFound(str(exc), 404, getattr(exc, "code", None)) from None
            raise

    # ---- CRUD ------------------------------------------------------------
    def set(self, name, value: Union[str, Mapping]) -> Dict[str, Any]:
        """Create or update one secret. A str stores {"value": <str>}; a mapping is sent as-is."""
        if isinstance(value, Mapping):
            body = dict(value)
            if not body:
                raise TaskbusValidation("secret value mapping must not be empty")
        elif isinstance(value, str):
            body = {"value": value}
        else:
            raise TaskbusValidation(
                "secret value must be a str or a mapping, not " + type(value).__name__)
        return self._request("POST", self._secret_path(name), body)

    def get(self, name) -> Dict[str, Any]:
        """Read one secret's data mapping. Raises SecretNotFound when it does not exist."""
        return self._request_404_as_not_found("GET", self._secret_path(name))

    def list(self) -> List[str]:
        """List the secret NAMES in this machine subtree. Never returns values."""
        payload = self._request("GET", SECRETS_PATH)
        return _names(payload)

    def delete(self, name):
        """Delete one secret and all its versions. Raises SecretNotFound when absent."""
        return self._request_404_as_not_found("DELETE", self._secret_path(name))

    # ---- escrow ----------------------------------------------------------
    def escrow(self, bundle):
        """Deposit an encrypted recovery bundle for the caller machine.

        Write-only: the response carries no token and no ciphertext. Verify with
        escrow_status() rather than by reading the deposit back.
        """
        if not isinstance(bundle, Mapping):
            raise TaskbusValidation(
                "escrow bundle must be a mapping, not " + type(bundle).__name__)
        if not bundle:
            raise TaskbusValidation("escrow bundle must not be empty")
        return self._request("POST", ESCROW_PATH, dict(bundle))

    def escrow_status(self, machine_uuid):
        """Report whether a deposit exists for this machine, metadata only."""
        if not isinstance(machine_uuid, str) or not machine_uuid.strip():
            raise TaskbusValidation("machine_uuid must be a non-empty string")
        path = ESCROW_PATH + "/" + urllib.parse.quote(machine_uuid.strip(), safe="")
        return self._request_404_as_not_found("GET", path)
