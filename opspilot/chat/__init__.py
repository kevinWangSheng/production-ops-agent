"""The incident page's Chat panel (workbench redesign P7, contract E1-E7 r10).

``ChatStore`` holds the two tables of migration 0026; ``build_context``
makes the record the model reads (web side, at question time);
``ChatWorker`` answers in the worker process. Chat never starts or touches
an investigation: a ``steer`` answer only lets the web forward the person's
own stored question as a ``chat_forward`` message.
"""

from opspilot.chat.context import MAX_CONTEXT_BYTES, build_context, context_sha256
from opspilot.chat.store import ChatStore
from opspilot.chat.worker import ChatOutcome, ChatWorker, parse_reply

__all__ = [
    "MAX_CONTEXT_BYTES",
    "ChatOutcome",
    "ChatStore",
    "ChatWorker",
    "build_context",
    "context_sha256",
    "parse_reply",
]
