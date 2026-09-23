#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from automation.io import append_jsonl, load_json, write_json  # noqa: E402
from automation.pipeline import VariantPipeline  # noqa: E402
from run_experiment_from_variant import (  # noqa: E402
    DEFAULT_PROVIDER_BY_STAGE,
    PENDING_STATUSES,
    TERMINAL_STATUSES,
    parse_json_or_text,
    provider_for_stage,
    summarize_experiment,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run CoordPoison variants in parallel by sharding variants across worker processes."
    )
    parser.add_argument("--pack", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--start-variant-id", default="")
    parser.add_argument("--stop-after-variant")
    parser.add_argument("--payload-id", type=int, help="Only run variants with this payload_id.")
    parser.add_argument("--max-steps-per-worker", type=int, default=0)
    parser.add_argument(
        "--require-upstream-targets",
        action="store_true",
        help="When expanding variants, skip Stage B candidate targets that have no upstream skill.",
    )
    parser.add_argument(
        "--d2-provider",
        default=DEFAULT_PROVIDER_BY_STAGE["D2"],
        choices=("codex-cli", "codex-sandbox", "claude-code-sandbox", "dry-run", "openai-compatible"),
    )
    parser.add_argument(
        "--d5-provider",
        default=DEFAULT_PROVIDER_BY_STAGE["D5"],
        choices=("codex-cli", "codex-sandbox", "claude-code-sandbox", "dry-run", "openai-compatible"),
    )
    parser.add_argument(
        "--local-provider",
        default="dry-run",
        choices=("dry-run", "openai-compatible", "codex-cli", "codex-sandbox", "claude-code-sandbox"),
    )
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--worker-index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--worker-count", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--runner-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()

    pipe = VariantPipeline()
    experiment = pipe.paths.pack_experiment(args.pack, args.experiment_id)
    if not experiment.exists():
        raise FileNotFoundError(experiment)

    if args.worker_index is not None:
        return run_worker(args=args, pipe=pipe, experiment=experiment)

    if args.workers < 1:
        raise ValueError("--workers must be >= 1")

    if not (experiment / "payload_selections.json").exists():
        raise RuntimeError("payload_selections.json is missing; run Stage C before parallel execution.")

    if not list((experiment / "variants").glob("*/variant.json")):
        pipe.expand_variants(
            args.pack,
            args.experiment_id,
            require_upstream_targets=args.require_upstream_targets,
        )

    run_dir = experiment / "runner_logs" / _stamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    controller_log = run_dir / "controller.jsonl"
    append_jsonl(
        controller_log,
        {
            "event": "parallel_start",
            "workers": args.workers,
            "pack_id": args.pack,
            "experiment_id": args.experiment_id,
            "started_at": now_iso(),
        },
    )

    if args.summary_only:
        summary = summarize_experiment(
            pipe=pipe,
            experiment=experiment,
            pack_id=args.pack,
            experiment_id=args.experiment_id,
            start_variant_id=args.start_variant_id or "",
            stop_after_variant=args.stop_after_variant,
        )
        summary["parallel_runner"] = {
            "workers": args.workers,
            "summary_only": True,
        }
        summary_path = run_dir / "summary.json"
        write_json(summary_path, summary)
        print(json.dumps({"event": "summary", "path": pipe.paths.rel(summary_path), "summary": summary}, indent=2, ensure_ascii=False))
        return 0

    worker_commands = []
    for index in range(args.workers):
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--pack",
            args.pack,
            "--experiment-id",
            args.experiment_id,
            "--workers",
            str(args.workers),
            "--d2-provider",
            args.d2_provider,
            "--d5-provider",
            args.d5_provider,
            "--local-provider",
            args.local_provider,
            "--worker-index",
            str(index),
            "--worker-count",
            str(args.workers),
            "--runner-dir",
            str(run_dir),
        ]
        if args.start_variant_id:
            cmd.extend(["--start-variant-id", args.start_variant_id])
        if args.stop_after_variant:
            cmd.extend(["--stop-after-variant", args.stop_after_variant])
        if args.payload_id is not None:
            cmd.extend(["--payload-id", str(args.payload_id)])
        if args.max_steps_per_worker:
            cmd.extend(["--max-steps-per-worker", str(args.max_steps_per_worker)])
        worker_commands.append(cmd)

    processes = [subprocess.Popen(cmd, cwd=WORKSPACE) for cmd in worker_commands]
    returncodes = [proc.wait() for proc in processes]
    if any(code != 0 for code in returncodes):
        raise RuntimeError(f"One or more workers failed: {returncodes}")

    summary = summarize_experiment(
        pipe=pipe,
        experiment=experiment,
        pack_id=args.pack,
        experiment_id=args.experiment_id,
        start_variant_id=args.start_variant_id or "",
        stop_after_variant=args.stop_after_variant,
    )
    summary["parallel_runner"] = {
        "workers": args.workers,
        "returncodes": returncodes,
        "runner_dir": pipe.paths.rel(run_dir),
    }
    summary_path = run_dir / "summary.json"
    write_json(summary_path, summary)
    print(json.dumps({"event": "summary", "path": pipe.paths.rel(summary_path), "summary": summary}, indent=2, ensure_ascii=False))
    return 0


def run_worker(*, args: argparse.Namespace, pipe: VariantPipeline, experiment: Path) -> int:
    worker_index = int(args.worker_index)
    worker_count = int(args.worker_count or args.workers)
    run_dir = Path(str(args.runner_dir)) if args.runner_dir else experiment / "runner_logs" / _stamp()
    worker_log_dir = run_dir / f"worker_{worker_index:02d}"
    worker_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = worker_log_dir / "steps.jsonl"

    variants = _assigned_variants(
        experiment=experiment,
        start_variant_id=str(args.start_variant_id or ""),
        stop_after_variant=args.stop_after_variant,
        payload_id=args.payload_id,
        worker_index=worker_index,
        worker_count=worker_count,
    )
    append_jsonl(
        log_path,
        {
            "event": "worker_start",
            "worker_index": worker_index,
            "worker_count": worker_count,
            "variant_count": len(variants),
            "started_at": now_iso(),
        },
    )

    steps = 0
    for variant_id in variants:
        if args.max_steps_per_worker and steps >= args.max_steps_per_worker:
            append_jsonl(log_path, {"event": "max_steps_reached", "max_steps": args.max_steps_per_worker})
            return 0
        steps += _run_variant_to_terminal(
            pipe=pipe,
            experiment=experiment,
            variant_id=variant_id,
            args=args,
            log_path=log_path,
            steps_start=steps,
        )

    append_jsonl(log_path, {"event": "worker_complete", "completed_at": now_iso(), "steps": steps})
    return 0


def _run_variant_to_terminal(
    *,
    pipe: VariantPipeline,
    experiment: Path,
    variant_id: str,
    args: argparse.Namespace,
    log_path: Path,
    steps_start: int,
) -> int:
    steps = 0
    variant_path = experiment / "variants" / variant_id / "variant.json"
    while True:
        variant = load_json(variant_path)
        status = str(variant.get("status", "pending_d1"))
        if status in TERMINAL_STATUSES:
            append_jsonl(log_path, {"event": "variant_terminal", "variant_id": variant_id, "status": status})
            return steps
        stage_info = _next_stage_for_variant(variant)
        if stage_info is None:
            append_jsonl(log_path, {"event": "variant_unhandled_status", "variant_id": variant_id, "status": status})
            return steps
        stage, loop_iteration = stage_info
        provider = provider_for_stage(stage, args)
        before = load_json(variant_path)
        record: dict[str, Any] = {
            "event": "run_variant_step",
            "variant_id": variant_id,
            "stage": stage,
            "provider": provider,
            "status_before": before.get("status"),
            "active_loop_before": before.get("active_loop_iteration"),
            "next_loop_before": before.get("next_loop_iteration"),
            "step_index": steps_start + steps + 1,
            "started_at": now_iso(),
        }
        append_jsonl(log_path, record)
        print(json.dumps(record, ensure_ascii=False))
        try:
            output = execute_stage_with_transient_retry(
                pipe=pipe,
                stage=stage,
                pack_id=args.pack,
                experiment_id=args.experiment_id,
                variant_id=variant_id,
                loop_iteration=loop_iteration,
                provider=provider,
                log_path=log_path,
            )
        except Exception as exc:
            error = {
                "event": "run_variant_step_error",
                "variant_id": variant_id,
                "stage": stage,
                "provider": provider,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc()[-8000:],
                "failed_at": now_iso(),
            }
            append_jsonl(log_path, error)
            print(json.dumps(error, ensure_ascii=False))
            raise
        after = load_json(variant_path)
        result = {
            "event": "run_variant_step_result",
            "variant_id": variant_id,
            "stage": stage,
            "provider": provider,
            "returncode": 0,
            "output": pipe.paths.rel(output),
            "status_after": after.get("status"),
            "active_loop_after": after.get("active_loop_iteration"),
            "next_loop_after": after.get("next_loop_iteration"),
            "finished_at": now_iso(),
        }
        append_jsonl(log_path, result)
        print(json.dumps(result, ensure_ascii=False))
        steps += 1
        if args.max_steps_per_worker and steps_start + steps >= args.max_steps_per_worker:
            append_jsonl(log_path, {"event": "max_steps_reached", "max_steps": args.max_steps_per_worker})
            return steps
    return steps


def execute_stage_with_transient_retry(
    *,
    pipe: VariantPipeline,
    stage: str,
    pack_id: str,
    experiment_id: str,
    variant_id: str,
    loop_iteration: int | None,
    provider: str,
    log_path: Path,
) -> Path:
    max_attempts = d4_retry_attempts(stage)
    base_delay = float(os.environ.get("PVF_D4_STAGE_RETRY_BASE_SECONDS", "20"))
    max_delay = float(os.environ.get("PVF_D4_STAGE_RETRY_MAX_SECONDS", "180"))
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return pipe.execute_stage(
                stage,
                pack_id,
                experiment_id,
                variant_id=variant_id,
                loop_iteration=loop_iteration,
                provider_name=provider,
                auto_ingest=True,
            )
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_transient_stage_error(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            retry = {
                "event": "run_variant_step_retry",
                "variant_id": variant_id,
                "stage": stage,
                "provider": provider,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay_seconds": delay,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "retry_at": now_iso(),
            }
            append_jsonl(log_path, retry)
            print(json.dumps(retry, ensure_ascii=False), flush=True)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def d4_retry_attempts(stage: str) -> int:
    if stage in {"D2", "D5"}:
        return max(1, int(os.environ.get("PVF_EXECUTION_STAGE_MAX_ATTEMPTS", "3")))
    if stage in {"D1", "D3", "D6"}:
        return max(1, int(os.environ.get("PVF_LLM_STAGE_MAX_ATTEMPTS", "3")))
    if stage not in {"D4", "D4_INITIAL", "D4_REVISION"}:
        return 1
    return max(1, int(os.environ.get("PVF_D4_STAGE_MAX_ATTEMPTS", "3")))


def is_transient_stage_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    non_retry_terms = (
        "http error 400",
        "400: bad request",
        "bad request",
        "invalid_request_error",
        "cyber_policy",
        "content was flagged",
        "provider policy refusal",
        "cyber safeguard",
        "cyber-related safeguards",
        "real-time cyber safeguards",
        "appears to violate our usage policy",
        "stop_reason\":\"refusal",
        "429",
        "too many requests",
        "exceeded retry limit",
        "provider rate limiting",
        "quota exhaustion",
    )
    if any(term in text for term in non_retry_terms):
        return False
    transient_terms = (
        "timed out",
        "timeout",
        "broken pipe",
        "temporarily unavailable",
        "connection reset",
        "remote end closed connection",
        "peer closed connection",
        "incomplete chunked read",
        "incompleteread",
        "remoteprotocolerror",
        "sandboxinternalexception",
        "unexpected sdk error",
        "502",
        "503",
        "504",
        "529",
        "rate limit",
        "high demand",
        "temporary errors",
        "server overloaded",
        "overloaded",
        "reconnecting",
        "transient connection/provider error",
    )
    return any(term in text for term in transient_terms)

def _next_stage_for_variant(variant: dict[str, Any]) -> tuple[str, int | None] | None:
    status = str(variant.get("status", "pending_d1"))
    if status == "pending_d1":
        return "D1", None
    if status == "pending_d2":
        return "D2", None
    if status == "pending_d3":
        return "D3", None
    if status == "pending_d4_initial":
        return "D4_INITIAL", 1
    if status == "pending_d4_revision":
        return "D4_REVISION", int(variant.get("next_loop_iteration", 2))
    if status == "pending_d4":
        loop_iteration = int(variant.get("next_loop_iteration", 1))
        return ("D4_INITIAL" if loop_iteration <= 1 else "D4_REVISION"), loop_iteration
    if status == "pending_d5":
        return "D5", int(variant.get("active_loop_iteration", 1))
    if status == "pending_d6":
        return "D6", int(variant.get("active_loop_iteration", 1))
    return None


def _assigned_variants(
    *,
    experiment: Path,
    start_variant_id: str,
    stop_after_variant: str | None,
    payload_id: int | None,
    worker_index: int,
    worker_count: int,
) -> list[str]:
    variants = []
    for path in sorted((experiment / "variants").glob("*/variant.json")):
        variant_id = path.parent.name
        if start_variant_id and variant_id < start_variant_id:
            continue
        if stop_after_variant and variant_id > stop_after_variant:
            continue
        if payload_id is not None:
            variant = load_json(path)
            if int(variant.get("payload_id", -1)) != payload_id:
                continue
        variants.append(variant_id)
    return [variant_id for index, variant_id in enumerate(variants) if index % worker_count == worker_index]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
