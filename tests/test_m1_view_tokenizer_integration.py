"""Integration tests for M1-01 A with the real vendored DeepSeek tokenizer
(``docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md``, contract items 8 and
10 and the "真实 tokenizer 的集成测试" paragraph).

The vendored ``tokenizer.json`` is in the repository, so these run offline in
CI. Interface used: ``opspilot.tools.tokens`` (``count_tokens``,
``load_deepseek_counter``, ``TokenCounter``), the default counter of
``ReadOnlyToolExecutor`` / ``otel_demo_executor_factory`` (``token_counter``
left ``None``), and ``ToolContractError("TOKENIZER_UNAVAILABLE")``. The
vendored file is located by name under the ``opspilot`` package, so the tests
do not fix its directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from opspilot.tools.tokens import count_tokens, load_deepseek_counter

import opspilot
from opspilot.tools import ToolContractError, TransportResponse
from opspilot.tools.otel_demo import MAX_VIEW_TOKENS, METRICS_TOOL, TRACES_TOOL
from opspilot.tools.registry import canonical
from tests.m1_tool_support import body, request
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_TRACES,
    GOOD_EXPR,
    FakeOpener,
    _call,
)
from tests.test_m1_whole_view_tokens_contract import (
    _build,
    _factory_executor,
    _long_expr,
    _matrix,
    _Recording,
    _series,
    _spans_trace,
)

PACKAGE_DIR = Path(opspilot.__file__).parent
REPO_ROOT = PACKAGE_DIR.parent


def _vendored_tokenizer() -> Path:
    found = sorted(PACKAGE_DIR.rglob("tokenizer.json"))
    assert len(found) == 1, f"expected one vendored tokenizer.json, found {found}"
    return found[0]


def _tokens(view) -> int:
    return count_tokens(canonical(view))


# -- the counter itself --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("hello world", 2),
        ('{"a":1}', 5),
        ("x" * 1000, 125),
        ("0.123456789" * 100, 500),
        ("检查订单服务的错误率", 5),
    ],
)
def test_the_default_counter_reproduces_the_recorded_deepseek_counts(text, expected):
    assert count_tokens(text) == expected


def test_the_loaded_counter_agrees_with_the_default_one():
    counter = load_deepseek_counter()
    assert counter("hello world") == count_tokens("hello world") == 2


# -- 10. fail closed -----------------------------------------------------------


def test_a_tampered_tokenizer_file_is_refused(tmp_path):
    tampered = tmp_path / "tokenizer.json"
    data = _vendored_tokenizer().read_bytes()
    tampered.write_bytes(data[:-1] + (b" " if data[-1:] != b" " else b"\n"))
    with pytest.raises(ToolContractError, match="TOKENIZER_UNAVAILABLE"):
        load_deepseek_counter(tampered)


def test_a_missing_tokenizer_file_is_refused(tmp_path):
    with pytest.raises(ToolContractError, match="TOKENIZER_UNAVAILABLE"):
        load_deepseek_counter(tmp_path / "absent.json")


def test_an_empty_tokenizer_file_is_refused(tmp_path):
    empty = tmp_path / "tokenizer.json"
    empty.write_bytes(b"")
    with pytest.raises(ToolContractError, match="TOKENIZER_UNAVAILABLE"):
        load_deepseek_counter(empty)


_CHILD = textwrap.dedent(
    """
    import sys
    {block_import}
    sys.path.insert(0, ".")
    from opspilot.tools import ToolContractError
    import m1_tool_support as support
    try:
        executor, transport, sink, clock = support.build()
    except ToolContractError as error:
        print("CONSTRUCTION_REFUSED", error)
        sys.exit(0)
    print("CONSTRUCTED", transport.called)
    """
)


def _child_env(workdir: Path) -> dict[str, str]:
    """The parent's environment with the private copy first on the path."""

    inherited = os.environ.get("PYTHONPATH")
    path = str(workdir) if not inherited else f"{workdir}{os.pathsep}{inherited}"
    return {**os.environ, "PYTHONPATH": path}


def _run_child(workdir: Path, *, block_import: bool = False) -> str:
    block = "sys.modules['tokenizers'] = None" if block_import else ""
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD.format(block_import=block)],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
        env=_child_env(workdir),
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.strip()


@pytest.fixture()
def package_copy(tmp_path):
    """A private copy of the package and the test support module, so a test
    can delete or corrupt the vendored file without touching the repository."""

    shutil.copytree(
        PACKAGE_DIR,
        tmp_path / "opspilot",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(REPO_ROOT / "tests" / "m1_tool_support.py", tmp_path)
    return tmp_path


def _copied_tokenizer(workdir: Path) -> Path:
    (found,) = (workdir / "opspilot").rglob("tokenizer.json")
    return found


def test_control_an_untouched_copy_constructs_an_executor(package_copy):
    assert _run_child(package_copy) == "CONSTRUCTED False"


def test_a_deleted_tokenizer_file_stops_executor_construction(package_copy):
    _copied_tokenizer(package_copy).unlink()
    assert "CONSTRUCTION_REFUSED TOKENIZER_UNAVAILABLE" in _run_child(package_copy)


def test_a_tampered_tokenizer_file_stops_executor_construction(package_copy):
    path = _copied_tokenizer(package_copy)
    data = path.read_bytes()
    path.write_bytes(data[:-1] + (b" " if data[-1:] != b" " else b"\n"))
    assert "CONSTRUCTION_REFUSED TOKENIZER_UNAVAILABLE" in _run_child(package_copy)


def test_an_unimportable_tokenizers_package_stops_executor_construction(
    package_copy,
):
    output = _run_child(package_copy, block_import=True)
    assert "CONSTRUCTION_REFUSED TOKENIZER_UNAVAILABLE" in output


def test_an_injected_counter_needs_neither_the_file_nor_the_package(package_copy):
    """Contract: with ``token_counter`` given, no tokenizer file is loaded and
    ``tokenizers`` is not imported."""

    _copied_tokenizer(package_copy).unlink()
    script = textwrap.dedent(
        """
        import sys
        sys.modules['tokenizers'] = None
        sys.path.insert(0, ".")
        import m1_tool_support as support
        from opspilot.tools import ReadOnlyToolExecutor
        import dataclasses
        # build() has no counter parameter by contract of this helper; the
        # executor takes it directly.
        clock = support.FakeClock()
        tools = support.ToolRegistry([support.registration()])
        targets = support.TargetRegistry([support.target()])
        ReadOnlyToolExecutor(
            scope=support.scope(targets, tool_registry=tools),
            tools=tools, targets=targets,
            transport=support.FakeTransport(clock=clock),
            evidence=support.RecordingSink(), control=support.FixedControl(),
            clock=clock, ledger=support.RecordingLedger(),
            token_counter=lambda s: len(s),
        )
        print("OK")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=package_copy,
        capture_output=True,
        text=True,
        timeout=120,
        env=_child_env(package_copy),
    )
    assert completed.stdout.strip() == "OK", completed.stderr[-2000:]


# -- 8/10. real tools, real counts --------------------------------------------


def _most_rows_under(limit: int, *, pad: int = 60) -> list[dict]:
    """The longest series list whose row array alone counts <= ``limit``."""

    low, high = 1, 4 * limit // 10
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens(canonical(_series(mid, pad=pad))) <= limit:
            low = mid
        else:
            high = mid - 1
    return _series(low, pad=pad)


def _real_counter():
    """The real counter, remembering the texts it counted."""

    counter = _Recording()

    class Real:
        texts = counter.texts

        def __call__(self, text):
            counter.texts.append(text)
            return count_tokens(text)

    return Real()


@pytest.mark.parametrize(
    "expr",
    [GOOD_EXPR, _long_expr(1990)],
    ids=["short-expression", "1990-char-expression"],
)
def test_metrics_view_over_25k_real_tokens_is_refused_with_the_true_count(
    monkeypatch, expr
):
    rows = _most_rows_under(MAX_VIEW_TOKENS - 100)
    assert count_tokens(canonical(rows)) <= MAX_VIEW_TOKENS  # rows alone fit
    opener = FakeOpener(by_query={expr: _matrix(rows)})
    counter = _real_counter()
    executor = _factory_executor(monkeypatch, opener, counter)

    outcome = executor.execute(_call(METRICS_TOOL, {"expr": expr}))

    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None and view["content"] is None
    (counted,) = counter.texts
    assert view["view_tokens"] == count_tokens(counted) > MAX_VIEW_TOKENS
    assert view["max_view_tokens"] == MAX_VIEW_TOKENS
    assert view["message"].startswith(
        f"The tool call result is too large to return: {view['view_tokens']}/"
        f"{MAX_VIEW_TOKENS} tokens."
    )


def test_the_boundary_row_count_is_exact_with_the_real_tokenizer(monkeypatch):
    """Contract 1/2 on the real path: the largest row count whose whole view
    counts <= 25,000 is delivered whole; one more row is refused."""

    def run(count):
        opener = FakeOpener(by_query={GOOD_EXPR: _matrix(_series(count, pad=60))})
        executor = _factory_executor(monkeypatch, opener, None)
        return executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))

    low, high = 1, len(_most_rows_under(MAX_VIEW_TOKENS))
    assert run(low).status == "ok"
    while low < high:
        mid = (low + high + 1) // 2
        if run(mid).status == "ok":
            low = mid
        else:
            high = mid - 1
    fits, over = run(low), run(low + 1)
    assert fits.status == "ok"
    assert fits.model_view["content"] == _series(low, pad=60)
    assert fits.model_view["truncated"] is False
    assert _tokens(fits.model_view) <= MAX_VIEW_TOKENS
    assert (over.status, over.reason) == ("error", "RESULT_TOO_LARGE")
    assert over.model_view["view_tokens"] > MAX_VIEW_TOKENS


def test_a_wide_traces_view_over_25k_real_tokens_is_refused_whole(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": _spans_trace(260)})
    counter = _real_counter()
    executor = _factory_executor(monkeypatch, opener, counter)

    outcome = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 260})
    )

    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert view["content"] is None and "span_groups" not in view
    (counted,) = counter.texts
    assert view["view_tokens"] == count_tokens(counted) > MAX_VIEW_TOKENS


def test_a_small_traces_view_is_delivered_whole_with_the_real_tokenizer(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _factory_executor(monkeypatch, opener, None)

    view = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 2})
    ).model_view

    assert view["status"] == "ok" and view["truncated"] is False
    assert view["returned_count"] == view["result_count"] == len(view["content"])
    assert sum(g["rows"] for g in view["span_groups"]) == len(view["content"])
    assert _tokens(view) <= MAX_VIEW_TOKENS


# -- 9. bounded time with the real tokenizer -----------------------------------


def test_a_one_mebibyte_result_is_counted_and_refused_in_bounded_time():
    import dataclasses

    from tests.m1_tool_support import registration

    rows = [{"series": index, "pad": "y" * 100} for index in range(7000)]
    payload = body(rows)
    assert 700_000 < len(payload) <= 1024 * 1024
    reg = dataclasses.replace(
        registration(), max_result_bytes=1024 * 1024, max_view_tokens=MAX_VIEW_TOKENS
    )
    executor, transport, sink, _ = _build(MAX_VIEW_TOKENS, count_tokens, reg=reg)
    transport.response = TransportResponse(body=payload)

    started = time.monotonic()
    outcome = executor.execute(request())
    elapsed = time.monotonic() - started

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view["view_tokens"] > MAX_VIEW_TOKENS
    assert sink.records == []
    # One count of about a megabyte takes about 0.6 s (task record).
    assert elapsed < 5, f"{elapsed:.1f}s"
