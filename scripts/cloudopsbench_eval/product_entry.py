"""Start the product's web or worker with the ``cloudopsbench`` profile (#871).

``python -m scripts.cloudopsbench_eval.product_entry web|worker``. The
product chooses its tool profile in exactly one call per process,
``select_profile(os.environ)`` (``opspilot.web.__main__._serve`` and
``opspilot.worker_main.build_loop``); this entry rebinds that one name in
the module that calls it to return the evaluation profile, then runs the
product's own ``main`` unchanged. No product file is modified and the
product's profile list does not gain an entry: only a process started
through this entry runs the evaluation tools.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    role = args[0] if args else ""
    from scripts.cloudopsbench_eval import profile as cob

    if role == "web":
        import opspilot.web.__main__ as web

        chosen = cob.from_env(os.environ, worker=False)
        web.select_profile = lambda env: chosen  # type: ignore[assignment]
        return web.main(["opspilot.web", "serve"])
    if role == "worker":
        import opspilot.worker_main as worker

        chosen = cob.from_env(os.environ, worker=True)
        worker.select_profile = lambda env: chosen  # type: ignore[assignment]
        return worker.main([])
    print("usage: python -m scripts.cloudopsbench_eval.product_entry web|worker")
    return 2


if __name__ == "__main__":
    sys.exit(main())
