#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import traceback
from argparse import Namespace
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
from automation.payloads import PayloadResolver  # noqa: E402
from automation.pipeline import VariantPipeline  # noqa: E402
from run_experiment_parallel import (  # noqa: E402
    _run_variant_to_terminal,
)
from run_experiment_from_variant import (  # noqa: E402
    DEFAULT_PROVIDER_BY_STAGE,
)


DEFAULT_PAYLOAD_POOL = "general"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run prepared CoordPoison experiments across packs by assigning one payload_id "
            "and one API key to each worker."
        )
    )
    parser.add_argument("--pack", action="append", default=[], help="Pack id. Can be repeated.")
    parser.add_argument("--packs", default="", help="Comma-separated pack ids.")
    parser.add_argument(
        "--pack-experiment",
        action="append",
        default=[],
        help="Explicit pack:experiment_id pair. Can be repeated.",
    )
    parser.add_argument(
        "--experiment-id-template",
        default="",
        help="Template used with --pack/--packs, for example '{pack}_exp_005'.",
    )
    parser.add_argument("--payload-pool", default=os.environ.get("PVF_PAYLOAD_POOL", DEFAULT_PAYLOAD_POOL))
    parser.add_argument("--payload-id", action="append", type=int, default=[])
    parser.add_argument(
        "--worker-api-keys-file",
        default=os.environ.get("PVF_WORKER_API_KEYS_FILE", ""),
        help="JSON list or object with a 'keys' list. One key is assigned to each payload worker.",
    )
    parser.add_argument("--batch-id", default="")
    parser.add_argument(
        "--require-upstream-targets",
        action="store_true",
        help="When variants are missing, expand only Stage B candidate targets with upstream skills.",
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
    parser.add_argument("--start-pack", default="")
    parser.add_argument("--stop-after-pack", default="")
    parser.add_argument("--start-variant-id", default="")
    parser.add_argument("--stop-after-variant", default="")
    parser.add_argument("--max-steps-per-worker", type=int, default=0)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--worker-index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--worker-count", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--worker-payload-id", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--runner-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()

    pipe = VariantPipeline(payloads=PayloadResolver(payload_pool=args.payload_pool))
    pack_experiments = _pack_experiments(args)
    payload_ids = args.payload_id or pipe.payloads.all_payload_ids()

    if args.worker_index is not None:
        return run_worker(args=args, pipe=pipe, pack_experiments=pack_experiments)

    if not payload_ids:
        raise RuntimeError(f"No payload ids resolved from payload pool {args.payload_pool!r}")

    _assert_prepared(pipe=pipe, args=args, pack_experiments=pack_experiments, payload_ids=payload_ids)

    keys = _load_worker_api_keys(args.worker_api_keys_file)
    if keys and len(keys) != len(payload_ids):
        raise RuntimeError(
            f"Expected {len(payload_ids)} worker API keys for payload ids {payload_ids}, got {len(keys)}"
        )

    batch_id = args.batch_id or _stamp()
    run_dir = pipe.paths.runs / "batches" / batch_id
    run_dir.mkdir(parents=True, exist_ok=True)
    controller_log = run_dir / "controller.jsonl"
    append_jsonl(
        controller_log,
        {
            "event": "payload_batch_start",
            "batch_id": batch_id,
            "payload_pool": args.payload_pool,
            "payload_ids": payload_ids,
            "pack_experiments": [
                {"pack_id": pack_id, "experiment_id": experiment_id}
                for pack_id, experiment_id in pack_experiments
            ],
            "worker_count": len(payload_ids),
            "started_at": now_iso(),
        },
    )

    if args.summary_only:
        summary = _batch_summary(pipe=pipe, pack_experiments=pack_experiments, payload_ids=payload_ids)
        summary_path = run_dir / "summary.json"
        write_json(summary_path, summary)
        print(json.dumps({"event": "summary", "path": pipe.paths.rel(summary_path), "summary": summary}, indent=2, ensure_ascii=False))
        return 0

    processes = []
    for index, payload_id in enumerate(payload_ids):
        env = os.environ.copy()
        key_digest = None
        if keys:
            key = keys[index]
            env["PVF_ATTACK_LLM_API_KEY"] = key
            env["PVF_CLAUDE_API_KEY"] = key
            env["PVF_LLM_API_KEY"] = key
            key_digest = _key_digest(key)
        cmd = _worker_command(args=args, run_dir=run_dir, worker_index=index, worker_count=len(payload_ids), payload_id=payload_id)
        append_jsonl(
            controller_log,
            {
                "event": "payload_worker_spawn",
                "worker_index": index,
                "worker_count": len(payload_ids),
                "payload_id": payload_id,
                "key_index": index if keys else None,
                "key_digest": key_digest,
                "started_at": now_iso(),
            },
        )
        processes.append(subprocess.Popen(cmd, cwd=WORKSPACE, env=env))

    returncodes = [proc.wait() for proc in processes]
    summary = _batch_summary(pipe=pipe, pack_experiments=pack_experiments, payload_ids=payload_ids)
    summary["returncodes"] = returncodes
    summary["runner_dir"] = pipe.paths.rel(run_dir)
    summary_path = run_dir / "summary.json"
    write_json(summary_path, summary)
    append_jsonl(
        controller_log,
        {
            "event": "payload_batch_complete" if not any(returncodes) else "payload_batch_error",
            "returncodes": returncodes,
            "summary_path": pipe.paths.rel(summary_path),
            "completed_at": now_iso(),
        },
    )
    print(json.dumps({"event": "summary", "path": pipe.paths.rel(summary_path), "summary": summary}, indent=2, ensure_ascii=False))
    if any(returncodes):
        raise RuntimeError(f"One or more payload workers failed: {returncodes}")
    return 0


def run_worker(*, args: argparse.Namespace, pipe: VariantPipeline, pack_experiments: list[tuple[str, str]]) -> int:
    worker_index = int(args.worker_index)
    worker_count = int(args.worker_count or 1)
    payload_id = int(args.worker_payload_id)
    run_dir = Path(str(args.runner_dir))
    worker_log_dir = run_dir / f"worker_{worker_index:02d}_payload_{payload_id:03d}"
    worker_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = worker_log_dir / "steps.jsonl"
    append_jsonl(
        log_path,
        {
            "event": "payload_worker_start",
            "worker_index": worker_index,
            "worker_count": worker_count,
            "payload_id": payload_id,
            "started_at": now_iso(),
        },
    )

    steps = 0
    for pack_id, experiment_id in _pack_slice(pack_experiments, args.start_pack, args.stop_after_pack):
        experiment = pipe.paths.pack_experiment(pack_id, experiment_id)
        variants = _payload_variants(
            experiment=experiment,
            payload_id=payload_id,
            start_variant_id=args.start_variant_id,
            stop_after_variant=args.stop_after_variant,
        )
        append_jsonl(
            log_path,
            {
                "event": "payload_pack_start",
                "pack_id": pack_id,
                "experiment_id": experiment_id,
                "payload_id": payload_id,
                "variant_count": len(variants),
                "started_at": now_iso(),
            },
        )
        worker_args = Namespace(
            pack=pack_id,
            experiment_id=experiment_id,
            d2_provider=args.d2_provider,
            d5_provider=args.d5_provider,
            local_provider=args.local_provider,
            max_steps_per_worker=args.max_steps_per_worker,
        )
        for variant_id in variants:
            if args.max_steps_per_worker and steps >= args.max_steps_per_worker:
                append_jsonl(log_path, {"event": "max_steps_reached", "max_steps": args.max_steps_per_worker})
                return 0
            try:
                steps += _run_variant_to_terminal(
                    pipe=pipe,
                    experiment=experiment,
                    variant_id=variant_id,
                    args=worker_args,
                    log_path=log_path,
                    steps_start=steps,
                )
            except Exception as exc:
                error = {
                    "event": "payload_worker_error",
                    "pack_id": pack_id,
                    "experiment_id": experiment_id,
                    "payload_id": payload_id,
                    "variant_id": variant_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc()[-8000:],
                    "failed_at": now_iso(),
                }
                append_jsonl(log_path, error)
                print(json.dumps(error, ensure_ascii=False))
                raise
        append_jsonl(
            log_path,
            {
                "event": "payload_pack_complete",
                "pack_id": pack_id,
                "experiment_id": experiment_id,
                "payload_id": payload_id,
                "completed_at": now_iso(),
                "steps": steps,
            },
        )

    append_jsonl(log_path, {"event": "payload_worker_complete", "completed_at": now_iso(), "steps": steps})
    return 0


def _worker_command(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    worker_index: int,
    worker_count: int,
    payload_id: int,
) -> list[str]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--payload-pool",
        args.payload_pool,
        "--d2-provider",
        args.d2_provider,
        "--d5-provider",
        args.d5_provider,
        "--local-provider",
        args.local_provider,
        "--worker-index",
        str(worker_index),
        "--worker-count",
        str(worker_count),
        "--worker-payload-id",
        str(payload_id),
        "--runner-dir",
        str(run_dir),
    ]
    for pack_id, experiment_id in _pack_experiments(args):
        cmd.extend(["--pack-experiment", f"{pack_id}:{experiment_id}"])
    if args.start_pack:
        cmd.extend(["--start-pack", args.start_pack])
    if args.stop_after_pack:
        cmd.extend(["--stop-after-pack", args.stop_after_pack])
    if args.start_variant_id:
        cmd.extend(["--start-variant-id", args.start_variant_id])
    if args.stop_after_variant:
        cmd.extend(["--stop-after-variant", args.stop_after_variant])
    if args.max_steps_per_worker:
        cmd.extend(["--max-steps-per-worker", str(args.max_steps_per_worker)])
    return cmd


def _pack_experiments(args: argparse.Namespace) -> list[tuple[str, str]]:
    explicit = []
    for item in args.pack_experiment:
        if ":" not in item:
            raise ValueError(f"--pack-experiment must be pack:experiment_id, got {item!r}")
        pack_id, experiment_id = item.split(":", 1)
        explicit.append((pack_id.strip(), experiment_id.strip()))
    if explicit:
        return explicit

    packs = list(args.pack)
    if args.packs:
        packs.extend(part.strip() for part in args.packs.split(",") if part.strip())
    packs = list(dict.fromkeys(packs))
    if not packs:
        raise RuntimeError("Pass --pack/--packs or --pack-experiment.")
    if not args.experiment_id_template:
        raise RuntimeError("Pass --experiment-id-template when using --pack/--packs.")
    return [(pack_id, args.experiment_id_template.format(pack=pack_id)) for pack_id in packs]


def _pack_slice(
    pack_experiments: list[tuple[str, str]],
    start_pack: str,
    stop_after_pack: str,
) -> list[tuple[str, str]]:
    selected = []
    for pack_id, experiment_id in pack_experiments:
        if start_pack and pack_id < start_pack:
            continue
        if stop_after_pack and pack_id > stop_after_pack:
            continue
        selected.append((pack_id, experiment_id))
    return selected


def _payload_variants(
    *,
    experiment: Path,
    payload_id: int,
    start_variant_id: str,
    stop_after_variant: str,
) -> list[str]:
    variants = []
    for path in sorted((experiment / "variants").glob("*/variant.json")):
        variant_id = path.parent.name
        if start_variant_id and variant_id < start_variant_id:
            continue
        if stop_after_variant and variant_id > stop_after_variant:
            continue
        variant = load_json(path)
        if int(variant.get("payload_id", -1)) == payload_id:
            variants.append(variant_id)
    return variants


def _assert_prepared(
    *,
    pipe: VariantPipeline,
    args: argparse.Namespace,
    pack_experiments: list[tuple[str, str]],
    payload_ids: list[int],
) -> None:
    expected_pool_path = pipe.paths.rel(pipe.payloads.payload_path)
    errors = []
    for pack_id, experiment_id in pack_experiments:
        experiment = pipe.paths.pack_experiment(pack_id, experiment_id)
        if not experiment.exists():
            errors.append(f"{pack_id}:{experiment_id} missing experiment dir {experiment}")
            continue
        required_paths = [
            experiment / "payload_selections.json",
            pipe.paths.stage_baseline(pack_id, experiment_id) / "candidate_targets.json",
        ]
        for required_path in required_paths:
            if not required_path.exists():
                errors.append(
                    f"{pack_id}:{experiment_id} missing {pipe.paths.rel(required_path)}; prepare Stage A-C first"
                )
        variants_root = experiment / "variants"
        if not list(variants_root.glob("*/variant.json")):
            errors.append(f"{pack_id}:{experiment_id} has no expanded variants; run expand-variants first")
            continue
        present_payload_ids = set()
        wrong_pool = []
        for variant_path in variants_root.glob("*/variant.json"):
            variant = load_json(variant_path)
            present_payload_ids.add(int(variant.get("payload_id", -1)))
            payload_source = str(variant.get("payload_source", ""))
            if payload_source and payload_source != expected_pool_path:
                wrong_pool.append(f"{variant.get('variant_id')} source={payload_source}")
        missing_payload_ids = [payload_id for payload_id in payload_ids if payload_id not in present_payload_ids]
        if missing_payload_ids:
            errors.append(f"{pack_id}:{experiment_id} missing variants for payload ids {missing_payload_ids}")
        if wrong_pool:
            errors.append(
                f"{pack_id}:{experiment_id} has variants from a different payload pool; "
                f"expected {expected_pool_path}; examples: {wrong_pool[:3]}"
            )
    if errors:
        raise RuntimeError("Batch preflight failed:\n- " + "\n- ".join(errors))


def _load_worker_api_keys(path: str) -> list[str]:
    if not path:
        return []
    data = load_json(Path(path))
    keys = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(keys, list):
        raise RuntimeError(f"Expected {path} to be a JSON list or object with a 'keys' list")
    normalized = [str(key).strip() for key in keys if str(key).strip()]
    if len(normalized) != len(keys):
        raise RuntimeError(f"{path} contains empty API keys")
    return normalized


def _batch_summary(
    *,
    pipe: VariantPipeline,
    pack_experiments: list[tuple[str, str]],
    payload_ids: list[int],
) -> dict[str, Any]:
    rows = []
    for pack_id, experiment_id in pack_experiments:
        experiment = pipe.paths.pack_experiment(pack_id, experiment_id)
        payload_status_counts: dict[str, dict[str, int]] = {}
        for payload_id in payload_ids:
            counts: dict[str, int] = {}
            for variant_id in _payload_variants(
                experiment=experiment,
                payload_id=payload_id,
                start_variant_id="",
                stop_after_variant="",
            ):
                variant = load_json(experiment / "variants" / variant_id / "variant.json")
                status = str(variant.get("status", "pending_d1"))
                counts[status] = counts.get(status, 0) + 1
            payload_status_counts[str(payload_id)] = counts
        rows.append(
            {
                "pack_id": pack_id,
                "experiment_id": experiment_id,
                "payload_status_counts": payload_status_counts,
            }
        )
    return {
        "schema_version": "2026-08-11.payload_batch_summary.v1",
        "generated_at": now_iso(),
        "payload_ids": payload_ids,
        "packs": rows,
    }


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
