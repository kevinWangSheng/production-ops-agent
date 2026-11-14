"""Per-scenario entity vocabulary generator for ITBench-Lite (#605, r6 L-V6-L-V8).

Skeleton: CLI only, implementation follows.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", help="clean copy of one scenario directory")
    parser.add_argument("--out", required=True, help="vocabulary text file")
    parser.parse_args(argv)
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
