from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


VariantType = Literal["steering_only", "coordinated"]
VerdictValue = Literal["success", "failure", "inconclusive", "technical"]


@dataclass(frozen=True)
class CandidateTarget:
    candidate_target_id: str
    pack_id: str
    target_skill: str
    observed_task_ids: list[str] = field(default_factory=list)
    pair_bindings: list[dict[str, Any]] = field(default_factory=list)
    upstream_paths: list[dict[str, Any]] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PayloadSelection:
    candidate_target_id: str
    target_skill: str
    payload_ids: list[int]
    selection_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VariantSpec:
    variant_id: str
    pack_id: str
    experiment_id: str
    candidate_target_id: str
    variant_type: VariantType
    target_skill: str
    steering_skill: str
    payload_id: int
    payload_source: str
    variant_dir: str
    task_ids: list[str]
    upstream_skill: str | None = None
    grounding_skill: str | None = None
    upstream_path: list[str] = field(default_factory=list)
    loop_iteration: int = 0
    parent_variant_id: str | None = None
    feedback_source: str | None = None
    payload_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunTrace:
    variant_id: str
    task_id: str
    task_prompt: str
    skill_sequence: list[str]
    commands: list[str] = field(default_factory=list)
    artifacts_read: list[str] = field(default_factory=list)
    artifacts_written: list[str] = field(default_factory=list)
    task_completed: bool = False
    agent_output: str = ""
    raw_trace_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Feedback:
    mode: str
    reason: str
    recommended_next_stage: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Verdict:
    variant_id: str
    verdict: VerdictValue
    payload_id: int
    judge_prompt_satisfied: bool
    payload_observed: bool
    task_completed: bool
    coordinated_dependency_satisfied: bool | None = None
    feedback: Feedback | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.feedback is None:
            data["feedback"] = None
        return data


def normalize_candidate_targets(raw: dict[str, Any], pack_id: str) -> list[CandidateTarget]:
    targets = raw.get("candidate_targets", raw.get("targets", []))
    normalized: list[CandidateTarget] = []
    for index, item in enumerate(targets, start=1):
        target_skill = item.get("target_skill") or item.get("steering_skill") or item.get("skill")
        if not target_skill:
            continue
        target_id = item.get("candidate_target_id") or f"{pack_id}_{target_skill}_{index:03d}"
        pair_bindings = []
        for binding in item.get("pair_bindings", []):
            if not isinstance(binding, dict):
                continue
            pair_bindings.append(
                {
                    "upstream_skill": binding.get("upstream_skill"),
                    "downstream_skill": binding.get("downstream_skill") or target_skill,
                    "relation": binding.get("relation", "ordered_before"),
                    "sequence": binding.get("sequence", []),
                    "causal_note": binding.get("causal_note", ""),
                    "successive_note": binding.get("successive_note", ""),
                    "task_ids": binding.get("task_ids", []),
                    "support": binding.get("support", {}),
                }
            )
        upstream_paths = []
        for path_info in item.get("upstream_paths", []):
            upstream_paths.append(
                {
                    "upstream_skill": path_info.get("upstream_skill"),
                    "path": path_info.get("path", []),
                    "edges": path_info.get("edges", []),
                    "evidence": path_info.get("evidence", ""),
                    "observed_task_ids": path_info.get("observed_task_ids", []),
                    "carrier_intervention": path_info.get("carrier_intervention", {}),
                    "carrier_lineage": path_info.get("carrier_lineage", {}),
                    "steering_readability": path_info.get("steering_readability", {}),
                    "non_sufficiency": path_info.get("non_sufficiency", {}),
                }
            )
        for upstream in item.get("upstream_skills", []):
            for path in upstream.get("supported_paths_to_target", upstream.get("workflow_paths_to_target", [])):
                upstream_paths.append(
                    {
                        "upstream_skill": upstream.get("upstream_skill"),
                        "path": path.get("nodes", []),
                        "edges": path.get("edges", []),
                        "evidence": path.get("path_rationale") or path.get("path_rationale_from_stage_b", ""),
                        "carrier_intervention": path.get("carrier_intervention", {}),
                        "carrier_lineage": path.get("carrier_lineage", {}),
                        "steering_readability": path.get("steering_readability", {}),
                        "non_sufficiency": path.get("non_sufficiency", {}),
                    }
                )
        caps = item.get("target_capabilities", {})
        capabilities = [key for key, value in caps.items() if value] if isinstance(caps, dict) else []
        normalized.append(
            CandidateTarget(
                candidate_target_id=target_id,
                pack_id=pack_id,
                target_skill=target_skill,
                observed_task_ids=item.get("observed_task_prompt_ids", item.get("observed_task_ids", [])),
                pair_bindings=pair_bindings,
                upstream_paths=upstream_paths,
                capabilities=capabilities,
                evidence={
                    "target_rationale": item.get("target_rationale", ""),
                    "observed_frequency": item.get("observed_frequency", 0),
                },
            )
        )
    return normalized
