"""bare-taskbus - standalone client for the Bare-AI Task Bus.

Nothing in this package knows about Vault, Baserow, SSH or any internal fleet path.
It talks to one gateway over HTTPS with one API key.

IMPORT COST IS PART OF THE CONTRACT. The criterion grammar is dependency-free
and is imported by the poller and the runner on every claim, on nodes that
deliberately have no HTTP client installed. Importing this package must
therefore NOT import httpx. The client, worker, supervisor and secrets modules
are loaded lazily on first attribute access (PEP 562), so both of these work:

    from bare_taskbus import TaskbusClient     # pulls in httpx
    from bare_taskbus.criterion import split_checks   # stdlib only
"""
from .errors import (
    TaskbusError,
    TaskbusAuthError,
    TaskbusForbidden,
    TaskbusConflict,
    TaskbusValidation,
    TaskbusUnavailable,
)
from .criterion import (GRAMMAR_VERSION, criterion_label, directives,
                        format_check, parse_check, split_checks)

__version__ = "0.2.1"

__all__ = [
    "TaskbusClient", "Claim", "ExecutionProfile", "Row", "TaskbusWorker",
    "SecretsClient", "SecretNotFound",
    "GRAMMAR_VERSION", "criterion_label", "directives", "format_check",
    "parse_check", "split_checks",
    "HandlerResult",
    "TaskbusError", "TaskbusAuthError", "TaskbusForbidden", "TaskbusConflict",
    "TaskbusValidation", "TaskbusUnavailable", "__version__",
    "LEASE_SECONDS", "HEARTBEAT_HORIZON_SECONDS", "MAX_SPAWN_DEPTH",
    "SUPERVISOR_RESULT_PREFIX", "parse_supervisor_result", "supervisor_close_body",
]

# name -> submodule. Everything here needs httpx (directly or through client.py),
# so none of it may be imported at package import time.
_LAZY_ATTRS = {
    "TaskbusClient": "client",
    "Claim": "client",
    "ExecutionProfile": "client",
    "Row": "client",
    "HEARTBEAT_HORIZON_SECONDS": "client",
    "LEASE_SECONDS": "client",
    "MAX_SPAWN_DEPTH": "client",
    "SUPERVISOR_RESULT_PREFIX": "supervisor",
    "parse_supervisor_result": "supervisor",
    "supervisor_close_body": "supervisor",
    "TaskbusWorker": "worker",
    "HandlerResult": "worker",
    "SecretNotFound": "secrets",
    "SecretsClient": "secrets",
}


def __getattr__(name):
    """PEP 562: resolve the httpx-backed names on first use, not on import."""
    submodule = _LAZY_ATTRS.get(name)
    if submodule is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    from importlib import import_module

    value = getattr(import_module("." + submodule, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))
