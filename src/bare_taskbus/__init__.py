"""bare-taskbus - standalone client for the Bare-AI Task Bus.

Nothing in this package knows about Vault, Baserow, SSH or any internal fleet path.
It talks to one gateway over HTTPS with one API key.
"""
from .errors import (
    TaskbusError,
    TaskbusAuthError,
    TaskbusForbidden,
    TaskbusConflict,
    TaskbusValidation,
    TaskbusUnavailable,
)
from .client import (HEARTBEAT_HORIZON_SECONDS, LEASE_SECONDS, MAX_SPAWN_DEPTH,
                     TaskbusClient, Claim, Row)
from .supervisor import (SUPERVISOR_RESULT_PREFIX, parse_supervisor_result,
                         supervisor_close_body)
from .worker import TaskbusWorker, HandlerResult

__version__ = "0.1.3"
__all__ = [
    "TaskbusClient", "Claim", "Row", "TaskbusWorker", "HandlerResult",
    "TaskbusError", "TaskbusAuthError", "TaskbusForbidden", "TaskbusConflict",
    "TaskbusValidation", "TaskbusUnavailable", "__version__",
    "LEASE_SECONDS", "HEARTBEAT_HORIZON_SECONDS", "MAX_SPAWN_DEPTH",
    "SUPERVISOR_RESULT_PREFIX", "parse_supervisor_result", "supervisor_close_body",
]
