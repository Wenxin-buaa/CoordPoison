#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation.payloads import PayloadResolver  # noqa: E402
from automation.pipeline import VariantPipeline  # noqa: E402


PROVIDERS = ["dry-run", "openai-compatible", "codex-cli", "codex-sandbox", "claude-code-sandbox"]
PAYLOAD_POOLS = ["general"]


def add_payload_pool_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--payload-pool",
        choices=PAYLOAD_POOLS,
        help="Payload pool to use for Stage C/variant payload resolution. Defaults to PVF_PAYLOAD_POOL or general.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automation entrypoint for CoordPoison"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-baseline", help="Create benign run dirs and Stage A request")
    p.add_argument("--pack", required=True)

    p = sub.add_parser("init-experiment", help="Create an experiment workspace before Stage C")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("prepare-stage", help="Generate a resolved prompt bundle for one stage")
    p.add_argument("--stage", required=True)
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id")
    p.add_argument("--variant-id")
    p.add_argument("--loop-iteration", type=int)
    add_payload_pool_arg(p)

    p = sub.add_parser("execute-stage", help="Build prompt and execute it with a provider")
    p.add_argument("--stage", required=True)
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id")
    p.add_argument("--variant-id")
    p.add_argument("--loop-iteration", type=int)
    p.add_argument("--provider", default="dry-run", choices=PROVIDERS)
    p.add_argument("--no-ingest", action="store_true", help="Do not auto-ingest materialized provider/local output")
    p.add_argument(
        "--require-upstream-targets",
        action="store_true",
        help="For Stage C, keep pair-compatible candidate targets while preserving compatibility filtering metadata.",
    )
    add_payload_pool_arg(p)

    p = sub.add_parser("auto-select-payloads", help="Select all payload IDs from the active payload pool for each candidate target")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id", required=True)
    p.add_argument(
        "--require-upstream-targets",
        action="store_true",
        help="Keep pair-compatible candidate targets while preserving compatibility filtering metadata.",
    )
    add_payload_pool_arg(p)

    p = sub.add_parser("rule-select-payloads", help="Deterministic fallback payload selection by skill-name matching")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id", required=True)
    p.add_argument(
        "--require-upstream-targets",
        action="store_true",
        help="Keep pair-compatible candidate targets while preserving compatibility filtering metadata.",
    )
    add_payload_pool_arg(p)

    p = sub.add_parser("expand-variants", help="Expand payload selections into per-variant workspaces")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id", required=True)
    p.add_argument(
        "--require-upstream-targets",
        action="store_true",
        help="Preserve pair-compatibility metadata when expanding variants.",
    )
    add_payload_pool_arg(p)

    p = sub.add_parser("limit-variant-tasks", help="Rewrite existing variants so each runs one selected task")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id", required=True)

    p = sub.add_parser("ingest", help="Validate and copy an external agent output into its canonical path")
    p.add_argument("--stage", required=True)
    p.add_argument("--pack", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--experiment-id")
    p.add_argument("--variant-id")
    p.add_argument("--loop-iteration", type=int)

    p = sub.add_parser("status", help="Show baseline or experiment status")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id")

    p = sub.add_parser("next", help="Prepare the next pending stage request")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id", required=True)

    p = sub.add_parser("run-next", help="Execute the next pending stage and advance state when possible")
    p.add_argument("--pack", required=True)
    p.add_argument("--experiment-id", required=True)
    p.add_argument("--provider", default="dry-run", choices=PROVIDERS)
    add_payload_pool_arg(p)

    sub.add_parser("summarize", help="Run result summarization")

    args = parser.parse_args()
    pipe = VariantPipeline(payloads=PayloadResolver(payload_pool=getattr(args, "payload_pool", None)))

    if args.command == "init-baseline":
        print(pipe.paths.rel(pipe.init_baseline(args.pack)))
        return 0

    if args.command == "init-experiment":
        print(pipe.paths.rel(pipe.init_experiment(args.pack, args.experiment_id, overwrite=args.overwrite)))
        return 0

    if args.command == "prepare-stage":
        print(pipe.paths.rel(pipe.prepare_stage(
            args.stage,
            args.pack,
            args.experiment_id,
            variant_id=args.variant_id,
            loop_iteration=args.loop_iteration,
        )))
        return 0

    if args.command == "execute-stage":
        print(pipe.paths.rel(pipe.execute_stage(
            args.stage,
            args.pack,
            args.experiment_id,
            variant_id=args.variant_id,
            loop_iteration=args.loop_iteration,
            provider_name=args.provider,
            auto_ingest=not args.no_ingest,
            require_upstream_targets=args.require_upstream_targets,
        )))
        return 0

    if args.command == "auto-select-payloads":
        print(pipe.paths.rel(pipe.auto_stage_c(
            args.pack,
            args.experiment_id,
            require_upstream_targets=args.require_upstream_targets,
        )))
        return 0

    if args.command == "rule-select-payloads":
        print(pipe.paths.rel(pipe.rule_stage_c(
            args.pack,
            args.experiment_id,
            require_upstream_targets=args.require_upstream_targets,
        )))
        return 0

    if args.command == "expand-variants":
        variants = pipe.expand_variants(
            args.pack,
            args.experiment_id,
            require_upstream_targets=args.require_upstream_targets,
        )
        print(json.dumps({"variant_count": len(variants)}, indent=2))
        return 0

    if args.command == "limit-variant-tasks":
        print(pipe.paths.rel(pipe.limit_variant_tasks(args.pack, args.experiment_id)))
        return 0

    if args.command == "ingest":
        print(pipe.paths.rel(pipe.ingest(
            args.stage,
            args.pack,
            Path(args.source),
            args.experiment_id,
            variant_id=args.variant_id,
            loop_iteration=args.loop_iteration,
        )))
        return 0

    if args.command == "status":
        print(json.dumps(pipe.status(args.pack, args.experiment_id), indent=2, ensure_ascii=False))
        return 0

    if args.command == "next":
        print(json.dumps(pipe.next(args.pack, args.experiment_id), indent=2, ensure_ascii=False))
        return 0

    if args.command == "run-next":
        print(json.dumps(pipe.run_next(
            args.pack,
            args.experiment_id,
            provider_name=args.provider,
        ), indent=2, ensure_ascii=False))
        return 0

    if args.command == "summarize":
        import subprocess

        subprocess.run([sys.executable, str(ROOT / "scripts" / "summarize_results.py")], check=True)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
