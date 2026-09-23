#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation.pipeline import VariantPipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize an automated experiment workspace."
    )
    parser.add_argument("--experiment-id")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    pipeline = VariantPipeline()
    experiment_dir = pipeline.init_experiment(
        args.pack,
        experiment_id=args.experiment_id,
        overwrite=args.overwrite,
    )
    request_dir = pipeline.prepare_stage("C", args.pack, experiment_dir.name)

    print(pipeline.paths.rel(experiment_dir))
    print(f"next_stage_request={pipeline.paths.rel(request_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
