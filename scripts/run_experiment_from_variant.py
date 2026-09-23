#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation.io import load_json, write_json  # noqa: E402
from automation.payloads import PayloadResolver  # noqa: E402
from automation.pipeline import VariantPipeline  # noqa: E402


TERMINAL_STATUSES = {
    "steering_only_success",
    "coordinated_success",
    "failed_early_stop_plateau",
    "failed_max_loop_iterations",
    "failed_d4_static_lint",
}
PENDING_STATUSES = {
    "pending_d1",
    "pending_d2",
    "pending_d3",
    "pending_d4",
    "pending_d4_initial",
    "pending_d4_revision",
    "pending_d5",
    "pending_d6",
}
DEFAULT_PROVIDER_BY_STAGE = {
    "D1": "dry-run",
    "D2": "codex-cli",
    "D3": "dry-run",
    "D4": "dry-run",
    "D4_INITIAL": "dry-run",
    "D4_REVISION": "dry-run",
    "D5": "codex-cli",
    "D6": "dry-run",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run CoordPoison variants in the same order as repeated `auto_run.py run-next`, "
            "with stage-specific providers and final experiment metrics."
        )
    )
    parser.add_argument("--pack", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument(
        "--start-variant-id",
        required=True,
        help="Run this variant first, then continue through later pending variants in sorted order.",
    )
    parser.add_argument(
        "--reset-start-variant",
        action="store_true",
        help="Archive the start variant's generated outputs and reset its state to pending_d1 before running.",
    )
    parser.add_argument(
        "--reset-following-variants",
        action="store_true",
        help=(
            "Archive and reset every variant from --start-variant-id onward to pending_d1 before running. "
            "Use this when judge/constructor code changed and later variants must not reuse old D1/D2/D3/D4/D5/D6 outputs."
        ),
    )
    parser.add_argument(
        "--archive-reset",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Archive generated outputs before reset. Use --no-archive-reset to delete them directly.",
    )
    parser.add_argument(
        "--resume-early-stop",
        action="store_true",
        help=(
            "Resume failed_early_stop_plateau variants in the selected range from their recorded "
            "next_loop_iteration without deleting existing outputs."
        ),
    )
    parser.add_argument("--max-steps", type=int, default=0, help="Optional safety cap on run-next calls; 0 means unlimited.")
    parser.add_argument(
        "--stop-after-variant",
        help="Optional inclusive final variant id. Useful for testing a slice before the full run.",
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
        help="Provider argument passed for local constructor/judge stages; D1/D3/D4/D6 ignore it internally.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Do not run anything; just write and print current experiment metrics.",
    )
    parser.add_argument(
        "--payload-pool",
        choices=("general",),
        help="Payload pool used by delegated auto_run.py calls. Defaults to PVF_PAYLOAD_POOL or general.",
    )
    args = parser.parse_args()

    pipe = VariantPipeline(payloads=PayloadResolver(payload_pool=args.payload_pool))
    experiment = pipe.paths.pack_experiment(args.pack, args.experiment_id)
    if not experiment.exists():
        raise FileNotFoundError(experiment)

    start_variant_path = experiment / "variants" / args.start_variant_id / "variant.json"
    if not start_variant_path.exists():
        raise FileNotFoundError(start_variant_path)

    run_dir = experiment / "runner_logs" / _stamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "steps.jsonl"

    if args.reset_following_variants:
        reset_records = reset_variants_from_start_to_d1(
            pipe=pipe,
            experiment=experiment,
            start_variant_id=args.start_variant_id,
            stop_after_variant=args.stop_after_variant,
            archive=args.archive_reset,
        )
        for reset_record in reset_records:
            append_jsonl(log_path, {"event": "reset_following_variant", **reset_record})
            print(json.dumps({"event": "reset_following_variant", **reset_record}, ensure_ascii=False))
    elif args.reset_start_variant:
        reset_record = reset_variant_to_d1(
            pipe=pipe,
            experiment=experiment,
            variant_id=args.start_variant_id,
            archive=args.archive_reset,
        )
        append_jsonl(log_path, {"event": "reset_start_variant", **reset_record})
        print(json.dumps({"event": "reset_start_variant", **reset_record}, ensure_ascii=False))

    if args.resume_early_stop:
        resume_records = resume_early_stop_variants(
            pipe=pipe,
            experiment=experiment,
            start_variant_id=args.start_variant_id,
            stop_after_variant=args.stop_after_variant,
        )
        for resume_record in resume_records:
            append_jsonl(log_path, {"event": "resume_early_stop", **resume_record})
            print(json.dumps({"event": "resume_early_stop", **resume_record}, ensure_ascii=False))

    if not args.summary_only:
        run_loop(
            args=args,
            pipe=pipe,
            experiment=experiment,
            log_path=log_path,
        )

    summary = summarize_experiment(
        pipe=pipe,
        experiment=experiment,
        pack_id=args.pack,
        experiment_id=args.experiment_id,
        start_variant_id=args.start_variant_id,
        stop_after_variant=args.stop_after_variant,
    )
    summary["runner_log"] = pipe.paths.rel(log_path)
    summary_path = run_dir / "summary.json"
    write_json(summary_path, summary)
    print(json.dumps({"event": "summary", "path": pipe.paths.rel(summary_path), "summary": summary}, indent=2, ensure_ascii=False))
    return 0


def run_loop(*, args: argparse.Namespace, pipe: VariantPipeline, experiment: Path, log_path: Path) -> None:
    if args.stop_after_variant:
        run_slice_loop(args=args, pipe=pipe, experiment=experiment, log_path=log_path)
        return

    start_variant_id = args.start_variant_id
    stop_after_variant = args.stop_after_variant
    steps = 0

    while True:
        pending = pipe.next(args.pack, args.experiment_id)
        if pending.get("state") != "waiting_for_external_agent":
            append_jsonl(log_path, {"event": "pipeline_state", "state": pending})
            print(json.dumps({"event": "pipeline_state", "state": pending}, ensure_ascii=False))
            return

        variant_id = str(pending.get("variant_id") or "")
        if variant_id < start_variant_id:
            raise RuntimeError(
                f"Next pending variant {variant_id!r} sorts before start variant {start_variant_id!r}. "
                "Finish or reset earlier variants before using this runner."
            )
        if stop_after_variant and variant_id > stop_after_variant:
            append_jsonl(log_path, {"event": "stop_after_variant_reached", "next_variant_id": variant_id})
            print(json.dumps({"event": "stop_after_variant_reached", "next_variant_id": variant_id}, ensure_ascii=False))
            return

        stage = str(pending["next_stage"])
        provider = provider_for_stage(stage, args)
        before = load_variant(experiment, variant_id)
        step_record: dict[str, Any] = {
            "event": "run_next",
            "step": steps + 1,
            "stage": stage,
            "provider": provider,
            "variant_id": variant_id,
            "status_before": before.get("status"),
            "active_loop_before": before.get("active_loop_iteration"),
            "next_loop_before": before.get("next_loop_iteration"),
            "started_at": now_iso(),
        }
        print(json.dumps(step_record, ensure_ascii=False))
        append_jsonl(log_path, step_record)

        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "auto_run.py"),
            "run-next",
            "--pack",
            args.pack,
            "--experiment-id",
            args.experiment_id,
            "--provider",
            provider,
        ]
        if args.payload_pool:
            cmd.extend(["--payload-pool", args.payload_pool])
        completed = subprocess.run(
            cmd,
            cwd=WORKSPACE,
            text=True,
            capture_output=True,
        )
        after = load_variant(experiment, variant_id)
        result_record = {
            "event": "run_next_result",
            "step": steps + 1,
            "stage": stage,
            "provider": provider,
            "variant_id": variant_id,
            "returncode": completed.returncode,
            "stdout": parse_json_or_text(completed.stdout),
            "stderr": completed.stderr[-8000:],
            "status_after": after.get("status"),
            "active_loop_after": after.get("active_loop_iteration"),
            "next_loop_after": after.get("next_loop_iteration"),
            "finished_at": now_iso(),
        }
        append_jsonl(log_path, result_record)
        print(json.dumps(result_record, ensure_ascii=False))

        if completed.returncode != 0:
            raise RuntimeError(f"run-next failed at step {steps + 1}; see {log_path}")

        steps += 1
        if args.max_steps and steps >= args.max_steps:
            append_jsonl(log_path, {"event": "max_steps_reached", "max_steps": args.max_steps})
            print(json.dumps({"event": "max_steps_reached", "max_steps": args.max_steps}, ensure_ascii=False))
            return


def run_slice_loop(*, args: argparse.Namespace, pipe: VariantPipeline, experiment: Path, log_path: Path) -> None:
    variants = _assigned_variants(
        experiment=experiment,
        start_variant_id=args.start_variant_id,
        stop_after_variant=args.stop_after_variant,
    )
    if not variants:
        append_jsonl(
            log_path,
            {
                "event": "slice_empty",
                "start_variant_id": args.start_variant_id,
                "stop_after_variant": args.stop_after_variant,
            },
        )
        print(
            json.dumps(
                {
                    "event": "slice_empty",
                    "start_variant_id": args.start_variant_id,
                    "stop_after_variant": args.stop_after_variant,
                },
                ensure_ascii=False,
            )
        )
        return

    append_jsonl(
        log_path,
        {
            "event": "slice_start",
            "start_variant_id": args.start_variant_id,
            "stop_after_variant": args.stop_after_variant,
            "variant_count": len(variants),
        },
    )
    print(
        json.dumps(
            {
                "event": "slice_start",
                "start_variant_id": args.start_variant_id,
                "stop_after_variant": args.stop_after_variant,
                "variant_count": len(variants),
            },
            ensure_ascii=False,
        )
    )

    steps = 0
    for variant_id in variants:
        if args.max_steps and steps >= args.max_steps:
            append_jsonl(log_path, {"event": "max_steps_reached", "max_steps": args.max_steps})
            print(json.dumps({"event": "max_steps_reached", "max_steps": args.max_steps}, ensure_ascii=False))
            return
        steps += run_variant_to_terminal(
            pipe=pipe,
            experiment=experiment,
            variant_id=variant_id,
            args=args,
            log_path=log_path,
            steps_start=steps,
        )

    append_jsonl(log_path, {"event": "slice_complete", "steps": steps})
    print(json.dumps({"event": "slice_complete", "steps": steps}, ensure_ascii=False))


def run_variant_to_terminal(
    *,
    pipe: VariantPipeline,
    experiment: Path,
    variant_id: str,
    args: argparse.Namespace,
    log_path: Path,
    steps_start: int,
) -> int:
    steps = 0
    while True:
        variant = load_variant(experiment, variant_id)
        status = str(variant.get("status", "pending_d1"))
        if status in TERMINAL_STATUSES:
            append_jsonl(log_path, {"event": "variant_terminal", "variant_id": variant_id, "status": status})
            return steps
        stage_info = next_stage_for_variant(variant)
        if stage_info is None:
            append_jsonl(log_path, {"event": "variant_unhandled_status", "variant_id": variant_id, "status": status})
            return steps
        stage, loop_iteration = stage_info
        provider = provider_for_stage(stage, args)
        before = load_variant(experiment, variant_id)
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
            output = pipe.execute_stage(
                stage,
                args.pack,
                args.experiment_id,
                variant_id=variant_id,
                loop_iteration=loop_iteration,
                provider_name=provider,
                auto_ingest=True,
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
        after = load_variant(experiment, variant_id)
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
        if args.max_steps and steps_start + steps >= args.max_steps:
            append_jsonl(log_path, {"event": "max_steps_reached", "max_steps": args.max_steps})
            print(json.dumps({"event": "max_steps_reached", "max_steps": args.max_steps}, ensure_ascii=False))
            return steps
    return steps


def next_stage_for_variant(variant: dict[str, Any]) -> tuple[str, int | None] | None:
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
) -> list[str]:
    variants = []
    for path in sorted((experiment / "variants").glob("*/variant.json")):
        variant_id = path.parent.name
        if start_variant_id and variant_id < start_variant_id:
            continue
        if stop_after_variant and variant_id > stop_after_variant:
            continue
        variants.append(variant_id)
    return variants


def reset_variant_to_d1(*, pipe: VariantPipeline, experiment: Path, variant_id: str, archive: bool) -> dict[str, Any]:
    variant_dir = experiment / "variants" / variant_id
    variant_path = variant_dir / "variant.json"
    variant = load_json(variant_path)
    reset_archive = None
    children = ["steering_only", "coordinated", "requests"]
    if archive:
        reset_archive = variant_dir / "reset_archives" / _stamp()
        reset_archive.mkdir(parents=True, exist_ok=True)
    for child in children:
        path = variant_dir / child
        if not path.exists():
            continue
        if archive:
            shutil.move(str(path), str(reset_archive / child))
        else:
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    exploit_path = experiment / "exploits" / f"{variant_id}.json"
    if exploit_path.exists():
        if archive:
            assert reset_archive is not None
            shutil.move(str(exploit_path), str(reset_archive / exploit_path.name))
        else:
            exploit_path.unlink()

    for key in (
        "steering_only_verdict",
        "coordinated_verdict",
        "next_loop_iteration",
        "active_loop_iteration",
        "loop_iteration",
        "upstream_skill",
        "grounding_skill",
        "skip_reason",
        "skip_stage",
        "last_d4_static_lint",
    ):
        variant.pop(key, None)
    variant["variant_type"] = "steering_only"
    variant["status"] = "pending_d1"
    variant["upstream_skill"] = None
    variant["grounding_skill"] = None
    variant["upstream_path"] = []
    variant["loop_iteration"] = 0
    write_json(variant_path, variant)
    pipe._refresh_experiment_state(variant["pack_id"], variant["experiment_id"])
    return {
        "variant_id": variant_id,
        "status": "pending_d1",
        "archive": pipe.paths.rel(reset_archive) if reset_archive else None,
    }


def reset_variants_from_start_to_d1(
    *,
    pipe: VariantPipeline,
    experiment: Path,
    start_variant_id: str,
    stop_after_variant: str | None,
    archive: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for variant_path in sorted((experiment / "variants").glob("*/variant.json")):
        variant_id = variant_path.parent.name
        if variant_id < start_variant_id:
            continue
        if stop_after_variant and variant_id > stop_after_variant:
            continue
        records.append(
            reset_variant_to_d1(
                pipe=pipe,
                experiment=experiment,
                variant_id=variant_id,
                archive=archive,
            )
        )
    return records


def resume_early_stop_variants(
    *,
    pipe: VariantPipeline,
    experiment: Path,
    start_variant_id: str,
    stop_after_variant: str | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for variant_path in sorted((experiment / "variants").glob("*/variant.json")):
        variant_id = variant_path.parent.name
        if variant_id < start_variant_id:
            continue
        if stop_after_variant and variant_id > stop_after_variant:
            continue
        variant = load_json(variant_path)
        status_before = str(variant.get("status", "pending_d1"))
        if status_before != "failed_early_stop_plateau":
            records.append(
                {
                    "variant_id": variant_id,
                    "status_before": status_before,
                    "status_after": status_before,
                    "resumed": False,
                    "reason": "not_failed_early_stop_plateau",
                }
            )
            continue
        next_loop_iteration = int(variant.get("next_loop_iteration") or int(variant.get("active_loop_iteration", 0)) + 1)
        if next_loop_iteration > pipe._max_loop_iterations():
            records.append(
                {
                    "variant_id": variant_id,
                    "status_before": status_before,
                    "status_after": status_before,
                    "resumed": False,
                    "reason": "next_loop_iteration_exceeds_max",
                    "next_loop_iteration": next_loop_iteration,
                    "max_loop_iterations": pipe._max_loop_iterations(),
                }
            )
            continue
        resume_history = variant.get("resume_history")
        if not isinstance(resume_history, list):
            resume_history = []
        resume_history.append(
            {
                "resumed_at": now_iso(),
                "from_status": status_before,
                "to_status": "pending_d4_revision",
                "next_loop_iteration": next_loop_iteration,
                "early_stop": variant.get("early_stop"),
            }
        )
        variant["resume_history"] = resume_history
        variant["status"] = "pending_d4_revision"
        variant["next_loop_iteration"] = next_loop_iteration
        write_json(variant_path, variant)
        records.append(
            {
                "variant_id": variant_id,
                "status_before": status_before,
                "status_after": "pending_d4_revision",
                "resumed": True,
                "next_loop_iteration": next_loop_iteration,
            }
        )
    if records:
        first_variant_path = sorted((experiment / "variants").glob("*/variant.json"))[0]
        first_variant = load_json(first_variant_path)
        pipe._refresh_experiment_state(first_variant["pack_id"], first_variant["experiment_id"])
    return records


def summarize_experiment(
    *,
    pipe: VariantPipeline,
    experiment: Path,
    pack_id: str,
    experiment_id: str,
    start_variant_id: str,
    stop_after_variant: str | None,
) -> dict[str, Any]:
    variants = []
    for path in sorted((experiment / "variants").glob("*/variant.json")):
        variant = load_json(path)
        variant_id = str(variant["variant_id"])
        if variant_id < start_variant_id:
            continue
        if stop_after_variant and variant_id > stop_after_variant:
            continue
        variants.append(variant)

    by_status = Counter(str(variant.get("status")) for variant in variants)
    per_variant = [variant_metrics(experiment, variant) for variant in variants]
    terminal = [item for item in per_variant if item["status"] in TERMINAL_STATUSES]
    pending = [item for item in per_variant if item["status"] in PENDING_STATUSES]
    steering_success = [item for item in per_variant if item["status"] == "steering_only_success"]
    coordinated_success = [item for item in per_variant if item["status"] == "coordinated_success"]
    loop_counts = [item["coordinated_loops_used"] for item in terminal if item["coordinated_loops_used"] is not None]

    return {
        "schema_version": "2026-07-07.runner_summary.v1",
        "pack_id": pack_id,
        "experiment_id": experiment_id,
        "variant_range": {
            "start_variant_id": start_variant_id,
            "stop_after_variant": stop_after_variant,
        },
        "generated_at": now_iso(),
        "variant_count": len(variants),
        "terminal_count": len(terminal),
        "pending_count": len(pending),
        "by_status": dict(sorted(by_status.items())),
        "success": {
            "total_success_variants": len(steering_success) + len(coordinated_success),
            "steering_only_success_variants": len(steering_success),
            "coordinated_success_variants": len(coordinated_success),
            "successful_variant_ids": [item["variant_id"] for item in per_variant if item["success"]],
        },
        "loops": {
            "max_loop_iterations": pipe._max_loop_iterations(),
            "coordinated_loop_counts_for_terminal_variants": loop_counts,
            "average_coordinated_loops_for_terminal_variants": round(sum(loop_counts) / len(loop_counts), 3) if loop_counts else 0,
            "loop_count_by_success_variant": {
                item["variant_id"]: item["coordinated_loops_used"]
                for item in per_variant
                if item["success"]
            },
        },
        "variants": per_variant,
    }


def variant_metrics(experiment: Path, variant: dict[str, Any]) -> dict[str, Any]:
    variant_id = str(variant["variant_id"])
    variant_dir = experiment / "variants" / variant_id
    coordinated_verdicts = []
    for verdict_path in sorted((variant_dir / "coordinated").glob("loop_*/verdict.json")):
        loop = int(verdict_path.parent.name.rsplit("_", 1)[1])
        try:
            verdict = load_json(verdict_path)
        except json.JSONDecodeError:
            continue
        coordinated_verdicts.append(
            {
                "loop_iteration": loop,
                "verdict": verdict.get("verdict"),
                "coordinated_dependency_satisfied": verdict.get("coordinated_dependency_satisfied"),
            }
        )
    status = str(variant.get("status"))
    if status == "steering_only_success":
        loops_used = 0
    elif coordinated_verdicts:
        loops_used = max(item["loop_iteration"] for item in coordinated_verdicts)
    elif variant.get("active_loop_iteration") is not None:
        loops_used = int(variant["active_loop_iteration"])
    else:
        loops_used = None
    return {
        "variant_id": variant_id,
        "steering_skill": variant.get("steering_skill"),
        "payload_id": variant.get("payload_id"),
        "status": status,
        "success": status in {"steering_only_success", "coordinated_success"},
        "steering_only_verdict": variant.get("steering_only_verdict"),
        "coordinated_verdict": variant.get("coordinated_verdict"),
        "coordinated_loops_used": loops_used,
        "active_loop_iteration": variant.get("active_loop_iteration"),
        "next_loop_iteration": variant.get("next_loop_iteration"),
        "coordinated_verdicts": coordinated_verdicts,
    }


def provider_for_stage(stage: str, args: argparse.Namespace) -> str:
    if stage == "D2":
        return args.d2_provider
    if stage == "D5":
        return args.d5_provider
    return args.local_provider


def load_variant(experiment: Path, variant_id: str) -> dict[str, Any]:
    return load_json(experiment / "variants" / variant_id / "variant.json")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_json_or_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped[-8000:]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
