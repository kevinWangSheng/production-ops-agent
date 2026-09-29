"""Token counting for the model-view limit (M1-01, A).

The gateway bounds every tool view in real DeepSeek tokens, the unit upstream
HolmesGPT meters in (``holmes/core/tools_utils/tool_context_window_limiter.py``
:47-49), not in bytes. The counter is the DeepSeek V4.1 Flash ``tokenizer.json``
vendored next to this module (``tokenizer/PROVENANCE`` records source, commit and
hashes), loaded with the ``tokenizers`` package. Loading never touches the
network and fails closed: a missing or altered file, or an unimportable
package, is ``ToolContractError("TOKENIZER_UNAVAILABLE")`` -- there is no
silent fallback to a byte estimate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from threading import Lock

from .registry import ToolContractError

__all__ = [
    "VOCAB_FILE_SHA256",
    "TokenCounter",
    "count_tokens",
    "default_counter",
    "load_deepseek_counter",
]

TokenCounter = Callable[[str], int]

VENDORED_TOKENIZER = Path(__file__).parent / "tokenizer" / "tokenizer.json"
# DeepSeek-V4.1-Flash, Hugging Face commit dba1be0a40aa45a94ad051997016db3960a90277.
VOCAB_FILE_SHA256 = "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"

_default: TokenCounter | None = None
_lock = Lock()


def load_deepseek_counter(tokenizer_path: Path | None = None) -> TokenCounter:
    """A counter over ``tokenizer_path`` (default: the vendored file).

    The file's sha256 must equal ``VOCAB_FILE_SHA256``.
    """
    path = VENDORED_TOKENIZER if tokenizer_path is None else Path(tokenizer_path)
    try:
        data = path.read_bytes()
    except OSError:
        raise ToolContractError("TOKENIZER_UNAVAILABLE") from None
    if hashlib.sha256(data).hexdigest() != VOCAB_FILE_SHA256:
        raise ToolContractError("TOKENIZER_UNAVAILABLE")
    try:
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_str(data.decode("utf-8"))
    except Exception:  # ImportError, or a file the package cannot parse
        raise ToolContractError("TOKENIZER_UNAVAILABLE") from None

    def counter(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return counter


def default_counter() -> TokenCounter:
    """The process-wide counter over the vendored file (loaded once)."""
    global _default
    with _lock:
        if _default is None:
            _default = load_deepseek_counter()
        return _default


def count_tokens(text: str) -> int:
    return default_counter()(text)
