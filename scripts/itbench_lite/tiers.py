"""Coverage tier file generator for the ITBench-Lite SRE run (#605, r6 L-V1-L-V5, L-V10).

Skeleton: CLI only, implementation follows.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="directory holding the clean scenario copies")
    parser.add_argument("--out", required=True, help="tiers.json")
    parser.add_argument("--detail", required=True, help="tiers_detail.json")
    parser.add_argument("--census", help="optional census JSON for a cross-check")
    parser.parse_args(argv)
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
