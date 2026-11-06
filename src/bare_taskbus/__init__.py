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
from .client import TaskbusClient, Claim, Row
from .worker import TaskbusWorker, HandlerResult

__version__ = "0.1.0"
__all__ = [
    "TaskbusClient", "Claim", "Row", "TaskbusWorker", "HandlerResult",
    "TaskbusError", "TaskbusAuthError", "TaskbusForbidden", "TaskbusConflict",
    "TaskbusValidation", "TaskbusUnavailable", "__version__",
]
