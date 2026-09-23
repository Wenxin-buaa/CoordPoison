from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import load_json
from .paths import FrameworkPaths


@dataclass(frozen=True)
class ResolvedPayload:
    id: int
    title: str
    injection_goal: str
    judge_prompt: str
    type: str
    task_scripts: dict[str, str]
    task_files: dict[str, str]
    example_instructions: dict[str, Any]
    example_tasks: list[dict[str, Any]]
    injection_style_examples: list[dict[str, Any]]
    judge_rule: dict[str, Any] | None
    source: str
    source_path: str
    payload_hash: str

    def to_reference(self) -> dict[str, Any]:
        return {
            "payload_id": self.id,
            "payload_source": self.source_path,
            "payload_hash": self.payload_hash,
            "title": self.title,
            "type": self.type,
            "injection_goal": self.injection_goal,
            "judge_prompt": self.judge_prompt,
            "example_instructions": self.example_instructions,
            "example_tasks": self.example_tasks,
            "injection_style_examples": self.injection_style_examples,
            "task_scripts": self.task_scripts,
            "task_files": self.task_files,
            "judge_rule": self.judge_rule,
            "source": self.source,
        }


class PayloadResolver:
    def __init__(
        self,
        paths: FrameworkPaths | None = None,
        *,
        payload_path: Path | str | None = None,
        judge_rules_path: Path | str | None = None,
        payload_pool: str | None = None,
    ):
        self.paths = paths or FrameworkPaths.discover()
        pool = payload_pool or os.environ.get("PVF_PAYLOAD_POOL", "general")
        if pool in {"skillject", "skillject-obvious-general-no003"}:
            pool = "general"
        if payload_path is not None:
            self.payload_path = self._resolve_path(payload_path)
        else:
            self.payload_path = self.paths.framework_root / "obvious_payload" / "general_injections.json"
        if judge_rules_path is not None:
            self.judge_rules_path = self._resolve_path(judge_rules_path)
        elif payload_path is not None and self.payload_path.name in {
            "general_injections.json",
        }:
            self.judge_rules_path = self.paths.framework_root / "obvious_payload" / "general_judge_rules.json"
        elif payload_path is not None:
            self.judge_rules_path = self.paths.framework_root / "obvious_payload" / "general_judge_rules.json"
        else:
            self.judge_rules_path = self.paths.framework_root / "obvious_payload" / "general_judge_rules.json"
        self._payloads: dict[int, dict[str, Any]] | None = None
        self._judge_rules: dict[int, dict[str, Any]] | None = None

    def _resolve_path(self, path: Path | str) -> Path:
        path = Path(path)
        if path.is_absolute():
            return path
        return self.paths.workspace_root / path

    def resolve(self, payload_id: int) -> ResolvedPayload:
        payloads = self._load_payloads()
        if payload_id not in payloads:
            raise KeyError(f"Unknown payload_id: {payload_id}")
        raw = payloads[payload_id]
        raw_bytes = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
        payload_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]
        return ResolvedPayload(
            id=int(raw["id"]),
            title=str(raw.get("title", "")),
            injection_goal=str(raw.get("injection_goal", "")),
            judge_prompt=str(raw.get("judge_prompt", "")),
            type=str(raw.get("type", "")),
            task_scripts=_normalize_mapping(raw.get("task_scripts")),
            task_files=_normalize_mapping(raw.get("task_files")),
            example_instructions=_normalize_mapping(raw.get("example_instructions")),
            example_tasks=_normalize_list_of_dicts(raw.get("example_tasks")),
            injection_style_examples=_normalize_list_of_dicts(raw.get("injection_style_examples")),
            judge_rule=self._load_judge_rules().get(payload_id),
            source=str(raw.get("source", "unspecified")),
            source_path=self.paths.rel(self.payload_path),
            payload_hash=payload_hash,
        )

    def all_payload_ids(self) -> list[int]:
        return sorted(self._load_payloads())

    def all_payload_records(self) -> list[dict[str, Any]]:
        return [self._load_payloads()[payload_id] for payload_id in self.all_payload_ids()]

    def compatible_payload_ids(self, target_skill: str, limit: int = 5) -> list[int]:
        payloads = self._load_payloads()
        scored: list[tuple[int, int]] = []
        for payload_id, payload in payloads.items():
            score = 0
            for task in payload.get("example_tasks", []):
                if task.get("skill") == target_skill:
                    score += 3
                elif target_skill in str(task.get("skill", "")) or str(task.get("skill", "")) in target_skill:
                    score += 1
            if target_skill.lower() in json.dumps(payload, ensure_ascii=False).lower():
                score += 1
            scored.append((score, payload_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [payload_id for _score, payload_id in scored[:limit]]

    def _load_payloads(self) -> dict[int, dict[str, Any]]:
        if self._payloads is None:
            data = load_json(self.payload_path)
            if not isinstance(data, list):
                raise ValueError(f"Expected list payload taxonomy: {self.payload_path}")
            self._payloads = {int(item["id"]): item for item in data}
        return self._payloads

    def _load_judge_rules(self) -> dict[int, dict[str, Any]]:
        if self._judge_rules is None:
            data = load_json(self.judge_rules_path)
            rules = data.get("payloads", []) if isinstance(data, dict) else data
            self._judge_rules = {int(item["id"]): item for item in rules}
        return self._judge_rules


def _normalize_mapping(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        normalized = {}
        for index, item in enumerate(value, start=1):
            if isinstance(item, dict):
                key = str(item.get("name") or item.get("path") or f"item_{index}")
                normalized[key] = item
            else:
                normalized[str(item)] = item
        return normalized
    return {str(value): value}


def _normalize_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
