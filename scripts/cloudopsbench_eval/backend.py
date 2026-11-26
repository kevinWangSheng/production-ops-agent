"""Start the benchmark's MCP server for one case view (#871 U1, U6).

The server is the benchmark's code, which is untrusted here: it runs only as
its own process, under the interpreter of its own venv (``--cob-python``),
isolated (``-I``: no ``PYTHON*`` variables, no user site, no script or
working directory on ``sys.path``), with the benchmark checkout put on
``sys.path`` explicitly. Its environment is the base paths plus its own
``CLOUDOPSBENCH_*`` settings: no model key, no trace key, no OpsPilot DSN.
It binds 127.0.0.1 and is given the case *view* (``views.build_view``), never
the case directory.
"""

from __future__ import annotations

import time
from pathlib import Path

from scripts.cloudopsbench_eval.mcp_client import McpClient, McpError
from scripts.itbench_lite import services
from scripts.itbench_lite.services import ServiceError, Started

#: ``python -I -c BOOT <checkout>``: the checkout is the only path added.
BOOT = (
    "import runpy, sys; root = sys.argv[1]; sys.path.insert(0, root); "
    "sys.argv = ['cloudopsbench_mcp']; "
    "runpy.run_module('cloudopsbench_mcp', run_name='__main__', alter_sys=True)"
)
READY_TIMEOUT_S = 60.0


def server_env(
    *,
    view: Path,
    system: str,
    category: str,
    port: int,
    trace_path: Path,
    run_id: str,
) -> dict[str, str]:
    return services.base_env() | {
        "CLOUDOPSBENCH_CASE_PATH": str(view),
        "CLOUDOPSBENCH_SYSTEM": system,
        "CLOUDOPSBENCH_FAULT_CATEGORY": category,
        "CLOUDOPSBENCH_MCP_HOST": "127.0.0.1",
        "CLOUDOPSBENCH_MCP_PORT": str(port),
        "CLOUDOPSBENCH_RUN_ID": run_id,
        "CLOUDOPSBENCH_TRACE_PATH": str(trace_path),
    }


def mcp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def start_mcp_server(
    *,
    cob_python: Path,
    checkout: Path,
    env: dict[str, str],
    port: int,
    log_path: Path,
    ready_timeout: float = READY_TIMEOUT_S,
) -> Started:
    """Spawn and wait until ``tools/list`` answers; returns the handle with
    the listed tools in ``info["tools"]``."""
    proc = services.spawn(
        [str(cob_python), "-I", "-c", BOOT, str(checkout)], env, log_path
    )
    started = Started(
        "mcp",
        lambda: services.stop_process(proc, 10),
        alive=lambda: proc.poll() is None,
    )
    client = McpClient(mcp_url(port))
    deadline = time.monotonic() + ready_timeout
    try:
        while True:
            if not started.alive():
                raise ServiceError("MCP_EXITED", f"rc={proc.returncode}")
            try:
                tools = client.list_tools(timeout=5.0)
                break
            except McpError:
                if time.monotonic() >= deadline:
                    raise ServiceError("MCP_NOT_READY") from None
                time.sleep(0.5)
    except BaseException:
        started.stop()
        raise
    started.info = {"pid": proc.pid, "tools": tools}
    return started
