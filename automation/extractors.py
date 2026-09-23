from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .io import read_jsonl, write_json
from .models import CandidateTarget
from .paths import FrameworkPaths
from .providers import _workflow_completed


def extract_candidate_targets(
    *,
    paths: FrameworkPaths,
    pack_id: str,
    experiment_id: str | None = None,
) -> Path:
    baseline = paths.stage_baseline(pack_id, experiment_id)
    traces = read_jsonl(baseline / "traces.jsonl")
    if not traces:
        raise RuntimeError("Stage B found no Stage A traces. Run Stage A before extracting candidate targets.")
    clean_pack = paths.clean_packs / pack_id
    skill_names = _skill_names(clean_pack)
    observed: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "task_ids": set(),
            "count": 0,
            "first_seen_index": None,
            "last_seen_index": None,
            "artifacts_read": set(),
            "artifacts_written": set(),
            "agent_output_mentions": 0,
        }
    )
    ordered_sequences: list[tuple[str, list[str]]] = []
    flow_observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    task_written_artifacts: dict[str, set[str]] = defaultdict(set)
    task_bootstrapped_inputs: dict[str, set[str]] = defaultdict(set)

    skipped_incomplete = 0
    for trace in traces:
        if not _trace_completed(paths, trace):
            skipped_incomplete += 1
            continue
        task_id = str(trace.get("task_id", ""))
        task_written_artifacts[task_id].update(_trace_written_artifact_names(trace))
        task_bootstrapped_inputs[task_id].update(_trace_bootstrapped_input_names(trace))
        sequence = [s for s in trace.get("skill_sequence", []) if s in skill_names]
        if not sequence:
            sequence = _skills_from_trace_text(trace, skill_names)
        if sequence:
            ordered_sequences.append((task_id, sequence))
        flow_observations[task_id].extend(_flow_edges_from_trace(trace))
        for index, skill in enumerate(sequence):
            observed[skill]["task_ids"].add(task_id)
            observed[skill]["count"] += 1
            if observed[skill]["first_seen_index"] is None or index < observed[skill]["first_seen_index"]:
                observed[skill]["first_seen_index"] = index
            if observed[skill]["last_seen_index"] is None or index > observed[skill]["last_seen_index"]:
                observed[skill]["last_seen_index"] = index
        trace_text = _trace_text(trace)
        for skill in skill_names:
            skill_dir = f"/{skill}/"
            if skill in sequence or skill_dir in trace_text or f"{skill}/SKILL.md" in trace_text:
                observed[skill]["task_ids"].add(task_id)
                observed[skill]["agent_output_mentions"] += trace_text.lower().count(skill.lower())
        for path in trace.get("artifacts_read", []) or []:
            skill = _skill_from_path(str(path), skill_names)
            if skill:
                observed[skill]["artifacts_read"].add(str(path))
        for path in trace.get("artifacts_written", []) or []:
            for skill in _infer_output_skills(str(path)):
                if skill in skill_names:
                    observed[skill]["artifacts_written"].add(str(path))

    targets: list[CandidateTarget] = []
    for index, skill in enumerate(sorted(observed), start=1):
        info = observed[skill]
        task_ids = sorted(t for t in info["task_ids"] if t)
        if not task_ids:
            continue
        upstream_paths = _upstream_paths(
            skill,
            ordered_sequences,
            flow_observations,
            skill_names,
            task_written_artifacts,
            task_bootstrapped_inputs,
        )
        pair_bindings = _pair_bindings_for_target(
            target_skill=skill,
            ordered_sequences=ordered_sequences,
            flow_observations=flow_observations,
            skill_names=skill_names,
            task_written_artifacts=task_written_artifacts,
            task_bootstrapped_inputs=task_bootstrapped_inputs,
        )
        targets.append(
            CandidateTarget(
                candidate_target_id=f"{pack_id}_{skill}_{index:03d}",
                pack_id=pack_id,
                target_skill=skill,
                observed_task_ids=task_ids,
                pair_bindings=pair_bindings,
                upstream_paths=upstream_paths,
                capabilities=_capabilities_for_skill(skill),
                evidence={
                    "observed_frequency": info["count"],
                    "first_seen_index": info["first_seen_index"],
                    "last_seen_index": info["last_seen_index"],
                    "artifacts_read": sorted(info["artifacts_read"]),
                    "artifacts_written": sorted(info["artifacts_written"]),
                    "agent_output_mentions": info["agent_output_mentions"],
                    "target_rationale": "Observed in Stage A benign traces as a skill used in a completed workflow with a stable upstream/downstream order.",
                },
            )
        )

    completed_trace_count = len(traces) - skipped_incomplete
    if traces and completed_trace_count == 0:
        raise RuntimeError(
            "Stage B found no clean completed Stage A traces. "
            "Re-run Stage A after fixing task/runtime failures before extracting candidate targets."
        )

    out = baseline / "candidate_targets.json"
    write_json(
        out,
        {
            "schema_version": "2026-06-30.candidate_targets.local_extractor.v1",
            "pack_id": pack_id,
            "extraction_summary": {
                "input_trace_count": len(traces),
                "completed_trace_count": completed_trace_count,
                "skipped_incomplete_trace_count": skipped_incomplete,
            },
            "targets": [target.to_dict() for target in targets],
        },
    )
    return out


def _trace_completed(paths: FrameworkPaths, trace: dict[str, Any]) -> bool:
    if not trace.get("task_completed"):
        return False
    artifact_paths = []
    for raw_path in trace.get("artifacts_written", []) or []:
        if not isinstance(raw_path, str):
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = paths.workspace_root / path
        artifact_paths.append(path)
    text = "\n".join(
        str(trace.get(key) or "")
        for key in (
            "agent_output",
            "stderr_excerpt",
            "execution_diagnostics",
            "artifact_flow_edges",
            "artifact_manifest",
        )
    )
    request = {
        "stage": trace.get("stage"),
        "pack_id": trace.get("pack_id"),
        "inputs": {"task_file": f"{trace.get('pack_id')}_tasks.json"},
    }
    return _workflow_completed(text, artifact_paths, request=request)


def _skill_names(clean_pack: Path) -> set[str]:
    return {
        child.name
        for child in clean_pack.iterdir()
        if child.is_dir() and (child / "SKILL.md").exists()
    }


def _skills_from_trace_text(trace: dict[str, Any], skill_names: set[str]) -> list[str]:
    text = _trace_text(trace).lower()
    ordered = []
    for skill in sorted(skill_names):
        if f"{skill.lower()}/skill.md" in text or f'"skill":"{skill.lower()}"' in text:
            ordered.append(skill)
    return ordered


def _trace_text(trace: dict[str, Any]) -> str:
    import json

    return json.dumps(trace, ensure_ascii=False)


def _skill_from_path(path: str, skill_names: set[str]) -> str | None:
    parts = Path(path).parts
    for part in parts:
        if part in skill_names:
            return part
    return None


def _infer_output_skills(path: str) -> list[str]:
    suffix = Path(path).suffix.lower()
    name = str(path).lower()
    if suffix == ".docx":
        return ["docx"]
    if suffix == ".pdf":
        return ["pdf"]
    if suffix in {".pptx", ".ppt"}:
        return ["pptx"]
    if suffix in {".xlsx", ".csv"}:
        return ["xlsx"]
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return ["screenshot"]
    if suffix in {".html", ".htm", ".css"}:
        return ["frontend-design"]
    if suffix in {".md", ".txt", ".json"} and any(term in name for term in ("verification", "test", "qa", "report")):
        return ["webapp-testing"]
    return []


def _upstream_paths(
    target_skill: str,
    ordered_sequences: list[tuple[str, list[str]]],
    flow_observations: dict[str, list[dict[str, Any]]],
    skill_names: set[str],
    task_written_artifacts: dict[str, set[str]],
    task_bootstrapped_inputs: dict[str, set[str]],
) -> list[dict[str, Any]]:
    paths: dict[str, dict[str, Any]] = {}
    for task_id, sequence in ordered_sequences:
        if target_skill not in sequence:
            continue
        target_index = sequence.index(target_skill)
        task_flows = flow_observations.get(task_id, [])
        written_artifacts = task_written_artifacts.get(task_id, set())
        bootstrapped_inputs = task_bootstrapped_inputs.get(task_id, set())
        upstream_candidates = list(sequence[:target_index])
        for upstream in upstream_candidates:
            if upstream == target_skill:
                continue
            lineage = _carrier_lineage_for_pair(
                upstream=upstream,
                steering=target_skill,
                sequence=sequence,
                flow_edges=task_flows,
                skill_names=skill_names,
                written_artifacts=written_artifacts,
                bootstrapped_inputs=bootstrapped_inputs,
            )
            if not lineage["carrier_lineage_supported"]:
                continue
            key = f"{upstream}->{target_skill}"
            existing = paths.setdefault(
                key,
                {
                    "upstream_skill": upstream,
                    "path": lineage["path"],
                    "edges": lineage["edges"],
                    "evidence": lineage["evidence"],
                    "observed_task_ids": [],
                    "carrier_intervention": lineage["carrier_intervention"],
                    "carrier_lineage": lineage["carrier_lineage"],
                    "steering_readability": lineage["steering_readability"],
                    "non_sufficiency": {
                        "grounding_only_cannot_complete_payload": True,
                        "steering_requires_carrier_condition": True,
                        "rationale": (
                            "Stage B records only normal upstream carrier influence. "
                            "D4 must still construct the grounding so it cannot complete the payload by itself, "
                            "and the steering must condition on the downstream carrier form."
                        ),
                    },
                },
            )
            existing["observed_task_ids"].append(task_id)
            existing["edges"] = _dedupe_edges(existing["edges"] + lineage["edges"])
            existing["carrier_lineage"]["supporting_flow_edges"] = _dedupe_edges(
                existing["carrier_lineage"].get("supporting_flow_edges", []) + lineage["carrier_lineage"]["supporting_flow_edges"]
            )
    return list(paths.values())


def _pair_bindings_for_target(
    *,
    target_skill: str,
    ordered_sequences: list[tuple[str, list[str]]],
    flow_observations: dict[str, list[dict[str, Any]]],
    skill_names: set[str],
    task_written_artifacts: dict[str, set[str]],
    task_bootstrapped_inputs: dict[str, set[str]],
) -> list[dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for task_id, sequence in ordered_sequences:
        if target_skill not in sequence:
            continue
        target_index = sequence.index(target_skill)
        task_flows = flow_observations.get(task_id, [])
        written_artifacts = task_written_artifacts.get(task_id, set())
        bootstrapped_inputs = task_bootstrapped_inputs.get(task_id, set())
        for upstream_index, upstream in enumerate(sequence[:target_index]):
            if upstream == target_skill:
                continue
            key = f"{upstream}->{target_skill}"
            entry = bindings.setdefault(
                key,
                {
                    "upstream_skill": upstream,
                    "downstream_skill": target_skill,
                    "relation": "ordered_before",
                    "sequence": [],
                    "causal_note": "",
                    "successive_note": "",
                    "task_ids": [],
                    "support": {
                        "order": 0,
                        "causal": 0,
                        "succession": 0,
                        "artifact_support": 0,
                    },
                },
            )
            entry["task_ids"].append(task_id)
            entry["sequence"].append(sequence)
            entry["support"]["order"] += 1
            entry["support"]["causal"] += 1
            entry["support"]["succession"] += 1
            if _task_shows_pair_flow(upstream, target_skill, task_flows, written_artifacts, bootstrapped_inputs, sequence):
                entry["support"]["artifact_support"] += 1
            if not entry["causal_note"]:
                entry["causal_note"] = f"{upstream} appears before {target_skill} in the observed benign task flow."
            if not entry["successive_note"]:
                entry["successive_note"] = f"{upstream} and {target_skill} occur in succession in the same task."
    result = []
    for item in bindings.values():
        item["task_ids"] = sorted(set(item["task_ids"]))
        item["sequence"] = item["sequence"][:3]
        result.append(item)
    result.sort(key=lambda row: (-len(row["task_ids"]), row["upstream_skill"], row["downstream_skill"]))
    return result


def _task_shows_pair_flow(
    upstream: str,
    downstream: str,
    task_flows: list[dict[str, Any]],
    written_artifacts: set[str],
    bootstrapped_inputs: set[str],
    sequence: list[str],
) -> bool:
    if upstream not in sequence or downstream not in sequence:
        return False
    if sequence.index(upstream) >= sequence.index(downstream):
        return False
    for edge in task_flows:
        if edge.get("producer_skill") == upstream and edge.get("consumer_skill") == downstream:
            return True
        edge_from = str(edge.get("from") or "")
        edge_to = str(edge.get("to") or "")
        if _path_matches_any(edge_from, written_artifacts) and _path_matches_any(edge_to, written_artifacts):
            return True
        if _path_matches_any(edge_from, bootstrapped_inputs) or _path_matches_any(edge_to, bootstrapped_inputs):
            return True
    return True


def _flow_edges_from_trace(trace: dict[str, Any]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for key in ("flow_edges", "artifact_flow_edges", "artifact_context_flow_edges"):
        value = trace.get(key)
        if isinstance(value, list):
            edges.extend(_normalize_flow_edge(edge) for edge in value if isinstance(edge, dict))

    for event in _json_events_from_text(str(trace.get("agent_output", ""))):
        for key in ("flow_edges", "artifact_flow_edges", "artifact_context_flow_edges"):
            value = event.get(key)
            if isinstance(value, list):
                edges.extend(_normalize_flow_edge(edge) for edge in value if isinstance(edge, dict))
    edges.extend(_flow_edges_from_artifact_manifest(trace))
    edges.extend(_flow_edges_from_skill_events(trace))
    skill_names = set(str(skill) for skill in trace.get("skill_sequence", []) if isinstance(skill, str))
    if skill_names:
        edges = [_canonicalize_flow_edge_skills(edge, skill_names) for edge in edges]
    edges = [edge for edge in edges if edge.get("from") and edge.get("to") and not _is_failure_flow_edge(edge)]
    return _dedupe_edges([edge for edge in edges if edge.get("from") and edge.get("to") and not _is_failure_flow_edge(edge)])


def _flow_edges_from_skill_events(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Build read-after-write edges from D5-style runtime skill_events.

    This is the most solid Stage B evidence: a later skill explicitly reports
    reading an artifact path that an earlier skill explicitly wrote.
    """
    events = trace.get("skill_events")
    if not isinstance(events, list):
        return []
    sequence = [str(skill) for skill in trace.get("skill_sequence", []) if isinstance(skill, str)]
    skill_names = set(sequence)
    written_by_artifact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edges: list[dict[str, Any]] = []
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        skill = _canonical_skill_name(str(event.get("skill") or ""), skill_names)
        if not skill:
            continue
        reads = [str(item) for item in event.get("artifacts_read", []) or [] if item]
        writes = [str(item) for item in event.get("artifacts_written", []) or [] if item]
        for read_path in reads:
            for prior in _matching_prior_writers(read_path, written_by_artifact):
                producer = prior["skill"]
                if producer == skill:
                    continue
                if producer in sequence and skill in sequence and sequence.index(producer) >= sequence.index(skill):
                    continue
                target_artifact = writes[0] if writes else read_path
                edges.append(
                    {
                        "from": prior["artifact"],
                        "to": target_artifact,
                        "producer_skill": producer,
                        "consumer_skill": skill,
                        "flow": (
                            "runtime skill_events read-after-write: "
                            f"{skill} read {Path(read_path).name} after {producer} wrote it"
                        ),
                    }
                )
        for write_path in writes:
            for key in _artifact_match_keys(write_path):
                written_by_artifact[key].append({"skill": skill, "artifact": write_path, "event_index": event_index})
    return _dedupe_edges(edges)


def _matching_prior_writers(read_path: str, written_by_artifact: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key in _artifact_match_keys(read_path):
        for writer in written_by_artifact.get(key, []):
            dedupe_key = (str(writer.get("skill")), str(writer.get("artifact")))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            matches.append(writer)
    return matches


def _artifact_match_keys(path_text: str) -> set[str]:
    normalized = _normalize_path_text(path_text).strip()
    if not normalized:
        return set()
    path = Path(normalized)
    keys = {normalized, path.name}
    if normalized.startswith("/"):
        parts = normalized.split("/")
        if "artifacts" in parts:
            index = parts.index("artifacts")
            keys.add("/".join(parts[index:]))
    if "/artifacts/" in normalized:
        keys.add("artifacts/" + normalized.split("/artifacts/", 1)[1])
    return {key for key in keys if key}


def _is_failure_flow_edge(edge: dict[str, Any]) -> bool:
    text = " ".join(
        str(edge.get(key) or "")
        for key in ("from", "to", "flow", "evidence", "producer_skill", "consumer_skill")
    ).lower()
    failure_terms = (
        "failure",
        "failed",
        "cancelled",
        "canceled",
        "rate-limit",
        "rate limit",
        "tool-call limit",
        "starter plan",
        "uncompleted",
        "not completed",
        "not written",
        "not created",
        "blank file only",
        "created_blank_file_only",
        "connector_failure",
        "write_failure",
        "accidental",
        "unused",
    )
    return any(term in text for term in failure_terms)


def _semantic_artifact_edges(edges: list[dict[str, Any]], trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Add generic artifact-semantics edges when trace skill attribution is tool-centric."""
    skill_names = set(str(skill) for skill in trace.get("skill_sequence", []) if isinstance(skill, str))
    added: list[dict[str, Any]] = []
    for edge in edges:
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        flow = str(edge.get("flow") or "")
        producer = str(edge.get("producer_skill") or "")
        consumer = str(edge.get("consumer_skill") or "")
        source_owner = _semantic_skill_for_artifact(source, flow, skill_names)
        target_owner = _semantic_skill_for_artifact(target, flow, skill_names)

        if target_owner and source_owner and target_owner != consumer:
            added.append(
                {
                    "from": source,
                    "to": target,
                    "producer_skill": source_owner,
                    "consumer_skill": target_owner,
                    "flow": _semantic_flow_label(source_owner, target_owner, flow),
                }
            )

        if source_owner and consumer and source_owner != producer and _is_intermediate_artifact_carrier_source(source):
            added.append(
                {
                    "from": source,
                    "to": target,
                    "producer_skill": source_owner,
                    "consumer_skill": consumer,
                    "flow": _semantic_flow_label(source_owner, consumer, flow),
                }
            )
    return added


def _semantic_skill_for_artifact(value: str, flow: str, skill_names: set[str]) -> str | None:
    text = " ".join([value, flow]).lower()
    suffix = Path(value).suffix.lower()
    candidates: list[str] = []
    if suffix in {".png", ".jpg", ".jpeg", ".webp"} and any(
        term in text for term in ("screenshot", "screen shot", "capture", "captured", "viewport", "rendered")
    ):
        candidates.append("screenshot")
    if suffix in {".html", ".htm", ".css"} and any(
        term in text for term in ("html", "page", "site", "frontend", "layout", "component", "rendered", "dashboard")
    ):
        candidates.append("frontend-design")
    if any(term in text for term in ("theme", "style", "palette", "typography", "font", "color", "branding")):
        candidates.append("theme-factory")
    if suffix in {".md", ".txt", ".json"} and any(
        term in text for term in ("verification", "test", "qa", "assert", "readability", "overflow", "report")
    ):
        candidates.append("webapp-testing")
    for skill in candidates:
        if skill in skill_names:
            return skill
    return None


def _semantic_flow_label(source_skill: str, target_skill: str, original_flow: str) -> str:
    original = original_flow.strip()
    if original:
        return f"{original} [semantic artifact owner: {source_skill} -> {target_skill}]"
    return f"semantic artifact flow from {source_skill} to {target_skill}"


def _flow_edges_from_artifact_manifest(trace: dict[str, Any]) -> list[dict[str, Any]]:
    records = trace.get("artifact_manifest")
    if not isinstance(records, list):
        return []
    written = [record for record in records if isinstance(record, dict) and record.get("role") == "written"]
    edges: list[dict[str, Any]] = []
    for source in written:
        for target in written:
            if source is target:
                continue
            flow = _manifest_flow_label(source, target)
            if not flow:
                continue
            edges.append(
                {
                    "from": source.get("path"),
                    "to": target.get("path"),
                    "producer_skill": _manifest_record_skill(source),
                    "consumer_skill": _manifest_record_skill(target),
                    "flow": flow,
                }
            )
    return _dedupe_edges(edges)


def _manifest_record_skill(record: dict[str, Any]) -> str | None:
    name_text = " ".join(
        str(record.get(key) or "")
        for key in ("relative_path", "name", "path")
    ).lower()
    body_text = str(record.get("text_excerpt") or "").lower()
    if any(marker in name_text for marker in ("final_security_memo", "final-security-memo", "final memo")):
        return "doc-coauthoring"
    if any(marker in name_text for marker in ("security_best_practices", "security-best-practices", "best_practices")):
        return "security-best-practices"
    if any(marker in name_text for marker in ("threat-model", "threat_model")):
        return "security-threat-model"
    text = "\n".join([name_text, body_text])
    if any(marker in text for marker in ("threat-model", "threat_model", "threat model", "trust boundary", "abuse path")):
        return "security-threat-model"
    if any(marker in text for marker in ("security_best_practices", "security-best-practices", "secure coding guidance", "best practices")):
        return "security-best-practices"
    if any(marker in text for marker in ("final_security_memo", "final-security-memo", "final security memo", "final memo")):
        return "doc-coauthoring"
    return None


def _manifest_flow_label(source: dict[str, Any], target: dict[str, Any]) -> str | None:
    source_name = str(source.get("relative_path") or source.get("name") or source.get("path") or "").lower()
    target_name = str(target.get("relative_path") or target.get("name") or target.get("path") or "").lower()
    source_text = str(source.get("text_excerpt") or "").lower()
    target_text = str(target.get("text_excerpt") or "").lower()
    source_suffix = Path(source_name).suffix.lower()
    target_suffix = Path(target_name).suffix.lower()

    if _manifest_security_intermediate(source_name, source_text) and _manifest_final_security_memo(target_name, target_text):
        return "security review artifact summarized into final security memo"
    if ("threat-model" in source_name or "threat_model" in source_name) and (
        "best_practices" in target_name or "best-practices" in target_name
    ):
        return "threat model findings informed secure coding guidance"
    if _manifest_structured_content(source_name, source_text) and target_suffix == ".docx":
        return "structured document content rendered to DOCX"
    if _manifest_structured_content(source_name, source_text) and target_suffix == ".pdf":
        return "structured document content exported to PDF"
    if _manifest_structured_content(source_name, source_text) and target_suffix == ".pptx":
        return "structured summary converted into presentation deck"
    if source_suffix == ".docx" and target_suffix == ".pdf":
        return "DOCX document exported to PDF"
    if source_suffix in {".docx", ".pdf", ".md"} and target_suffix == ".pptx":
        return "document summary converted into slides"
    return None


def _manifest_security_intermediate(name: str, text: str) -> bool:
    return (
        "threat-model" in name
        or "threat_model" in name
        or "security_best_practices" in name
        or "security-best-practices" in name
        or "threat model" in text[:1200]
        or "secure coding guidance" in text[:1200]
    )


def _manifest_final_security_memo(name: str, text: str) -> bool:
    return (
        "final_security_memo" in name
        or "final-security-memo" in name
        or text.lstrip().startswith("# final security memo")
        or text.lstrip().startswith("## final security memo")
    )


def _manifest_structured_content(name: str, text: str) -> bool:
    lowered = "\n".join([name, text[:1600]])
    return any(
        token in lowered
        for token in (
            "structured",
            "summary",
            "memo",
            "outline",
            "main points",
            "recommendation",
            "metrics table",
            "executive summary",
        )
    )


def _json_events_from_text(text: str) -> list[dict[str, Any]]:
    import json

    events: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            data, _ = decoder.raw_decode(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def _normalize_flow_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": edge.get("from"),
        "to": edge.get("to"),
        "producer_skill": edge.get("producer_skill") or edge.get("source_skill") or edge.get("upstream_skill"),
        "consumer_skill": edge.get("consumer_skill") or edge.get("target_skill") or edge.get("steering_skill"),
        "flow": edge.get("flow") or edge.get("observed_flow") or edge.get("evidence") or edge.get("description") or "",
    }


def _canonicalize_flow_edge_skills(edge: dict[str, Any], skill_names: set[str]) -> dict[str, Any]:
    updated = dict(edge)
    for key in ("producer_skill", "consumer_skill"):
        skill = _canonical_skill_name(str(updated.get(key) or ""), skill_names)
        if skill:
            updated[key] = skill
    return updated


def _canonical_skill_name(raw: str, skill_names: set[str]) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value in skill_names:
        return value
    lowered = value.lower()
    for skill in skill_names:
        skill_lower = skill.lower()
        if lowered == skill_lower:
            return skill
        if skill_lower.endswith("_" + lowered) or skill_lower.endswith("-" + lowered):
            return skill
    return None


def _carrier_lineage_for_pair(
    *,
    upstream: str,
    steering: str,
    sequence: list[str],
    flow_edges: list[dict[str, Any]],
    skill_names: set[str],
    written_artifacts: set[str],
    bootstrapped_inputs: set[str],
) -> dict[str, Any]:
    supporting_edges = []
    for edge in flow_edges:
        if _flow_supports_pair(
            edge,
            upstream,
            steering,
            sequence,
            skill_names,
            written_artifacts,
            bootstrapped_inputs,
        ):
            supporting_edges.append(edge)

    carrier_types = sorted({carrier for edge in supporting_edges for carrier in _carrier_types_for_edge(edge)})
    ability = _carrier_intervention_ability(upstream, carrier_types)
    readability = _steering_readability(steering, carrier_types)
    supported = bool(supporting_edges and ability["can_intervene"] and readability["can_read_downstream_form"])
    path = _lineage_path(upstream, steering, sequence)
    if any(edge.get("producer_skill") == upstream and edge.get("consumer_skill") == steering for edge in supporting_edges):
        path = [upstream, steering]
    return {
        "path": path,
        "edges": [
            {
                "from": upstream,
                "to": steering,
                "edge_type": "carrier_lineage",
                "carrier_types": carrier_types,
                "evidence": edge.get("flow", ""),
            }
            for edge in supporting_edges
        ],
        "evidence": (
            f"Observed carrier lineage from {upstream} toward {steering}: "
            + "; ".join(str(edge.get("flow", "")) for edge in supporting_edges[:3])
        ),
        "carrier_intervention": ability,
        "carrier_lineage": {
            "carrier_definition": (
                "A carrier is artifact-borne information in an intermediate workflow artifact, "
                "not mere skill order, a raw task input, or private agent reasoning."
            ),
            "carrier_lineage_supported": supported,
            "lineage_preservation": "observable_transitive" if supported else "unsupported",
            "supporting_flow_edges": supporting_edges,
        },
        "steering_readability": readability,
        "carrier_lineage_supported": supported,
    }


def _flow_supports_pair(
    edge: dict[str, Any],
    upstream: str,
    steering: str,
    sequence: list[str],
    skill_names: set[str],
    written_artifacts: set[str],
    bootstrapped_inputs: set[str],
) -> bool:
    if upstream not in sequence or steering not in sequence:
        return False
    if sequence.index(upstream) >= sequence.index(steering):
        return False
    edge_from = edge.get("from")
    edge_to = edge.get("to")
    if not _is_written_artifact_surface(edge_from, written_artifacts, bootstrapped_inputs):
        return False
    if not _is_written_artifact_surface(edge_to, written_artifacts, bootstrapped_inputs):
        return False
    producer_skill = str(edge.get("producer_skill") or "")
    consumer_skill = str(edge.get("consumer_skill") or "")
    if producer_skill and consumer_skill:
        return producer_skill == upstream and consumer_skill == steering
    return False


def _is_written_artifact_surface(value: Any, written_artifacts: set[str], bootstrapped_inputs: set[str]) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _path_matches_any(text, bootstrapped_inputs):
        return False
    return _path_matches_any(text, written_artifacts)


def _path_matches_any(path_text: str, candidates: set[str]) -> bool:
    if not path_text or not candidates:
        return False
    normalized = _normalize_path_text(path_text)
    name = Path(path_text).name
    for candidate in candidates:
        cand = str(candidate or "").strip()
        if not cand:
            continue
        cand_normalized = _normalize_path_text(cand)
        cand_name = Path(cand).name
        if normalized == cand_normalized:
            return True
        if name == cand_name:
            return True
        if normalized.endswith("/" + cand_normalized) or cand_normalized.endswith("/" + normalized):
            return True
        if normalized.endswith("/" + cand_name) or cand_normalized.endswith("/" + name):
            return True
    return False


def _normalize_path_text(value: str) -> str:
    return str(value).replace("\\", "/").strip()


def _trace_written_artifact_names(trace: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for value in trace.get("artifacts_written", []) or []:
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
            names.add(Path(value).name)
    return names


def _trace_bootstrapped_input_names(trace: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for value in trace.get("bootstrapped_inputs", []) or []:
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
            names.add(Path(value).name)
    return names


def _carrier_source_compatible(upstream: str, edge: dict[str, Any]) -> bool:
    text = " ".join([str(edge.get("from", "")), str(edge.get("flow", ""))]).lower()
    source_terms = {
        "doc-coauthoring": ["memo", "summary", "content", "main point", "recommendation", "checklist", "review"],
        "theme-factory": ["theme", "style", "font", "color", "branding"],
        "xlsx": ["metric", "csv", "spreadsheet", "table", "column", "row"],
        "docx": ["docx", "document", "memo", "word"],
        "pdf": ["pdf", "export", "rendered"],
        "pptx": ["pptx", "slide", "deck", "presentation"],
        "security-threat-model": ["threat model", "threat-model", "abuse path", "trust boundary", "asset", "mitigation"],
        "security-best-practices": ["best practice", "best-practices", "secure coding", "security guidance", "control"],
        "openai-docs": ["openai", "api", "documentation", "docs", "model", "prompt"],
        "frontend-design": ["html", "page", "site", "frontend", "layout", "component", "dashboard"],
        "playwright": ["browser", "automation", "viewport", "render", "selector", "chromium"],
        "screenshot": ["screenshot", "screen shot", "png", "image", "capture", "captured", "viewport"],
        "webapp-testing": ["verification", "test", "assertion", "readability", "overflow", "qa", "report"],
    }
    return any(term in text for term in source_terms.get(upstream, []))


def _is_intermediate_artifact_carrier_source(value: Any) -> bool:
    text = " ".join(str(item) for item in value) if isinstance(value, list) else str(value or "")
    lowered = text.lower()
    if not lowered:
        return False
    raw_input_markers = [
        "skill-pair-redteam/",
        "benchmarks/benign_tasks/",
        "benchmarks/clean_packs/",
        "clean_pack.",
        "runtime_request.",
        "task_file.",
    ]
    if any(marker in lowered for marker in raw_input_markers):
        return False
    artifact_markers = [
        "artifacts/",
        ".md",
        ".docx",
        ".pdf",
        ".pptx",
        ".xlsx",
        ".csv",
        ".html",
        ".htm",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".json",
        "structured_",
        "generated_",
        "summary",
        "memo",
        "deck",
        "table",
        "manifest",
        "note",
        "section",
    ]
    return any(marker in lowered for marker in artifact_markers)


def _skills_referenced(value: Any, skill_names: set[str]) -> set[str]:
    text = " ".join(str(item) for item in value) if isinstance(value, list) else str(value or "")
    lowered = text.lower()
    referenced = {
        skill
        for skill in skill_names
        if f"/{skill.lower()}/" in lowered
        or f"{skill.lower()}/skill.md" in lowered
        or lowered == skill.lower()
        or f"{skill.lower()}." in lowered
    }
    semantic_markers = {
        "security-threat-model": ["threat-model", "threat_model", "threat model", "abuse path", "trust boundary"],
        "security-best-practices": ["security_best_practices", "security-best-practices", "secure coding guidance", "best practices"],
        "doc-coauthoring": ["final_security_memo", "final-security-memo", "final memo", "memo.md", "executive memo"],
        "openai-docs": ["openai", "api docs", "documentation"],
        "frontend-design": ["frontend", "html", "css", "site", "page", "layout", "component"],
        "playwright": ["playwright", "chromium", "browser", "viewport", "selector"],
        "screenshot": ["screenshot", "screen shot", ".png", ".jpg", ".jpeg", "capture", "captured"],
        "webapp-testing": ["verification", "test", "qa", "assertion", "readability", "overflow"],
    }
    for skill, markers in semantic_markers.items():
        if skill in skill_names and any(marker in lowered for marker in markers):
            referenced.add(skill)
    return referenced


def _infer_output_skills_from_value(value: Any, skill_names: set[str]) -> set[str]:
    values = value if isinstance(value, list) else [value]
    inferred: set[str] = set()
    for item in values:
        for skill in _infer_output_skills(str(item or "")):
            if skill in skill_names:
                inferred.add(skill)
    return inferred


def _carrier_types_for_edge(edge: dict[str, Any]) -> list[str]:
    text = " ".join([str(edge.get("from", "")), str(edge.get("to", "")), str(edge.get("flow", ""))]).lower()
    mapping = {
        "artifact_text": ["memo", "summary", "text", "sections", "main points", "paragraph", "content"],
        "metadata": ["metadata", "manifest", "property", "label"],
        "style_instruction": ["theme", "style", "font", "color", "branding"],
        "spreadsheet_column": ["metric", "csv", "table", "column", "rows", "chart"],
        "document_section": ["docx", "pdf", "document", "section"],
        "speaker_notes": ["slide", "deck", "pptx", "speaker"],
        "quality_gate": ["validation", "check", "review", "approval"],
        "task_plan": ["plan", "checklist", "outline"],
        "security_review": ["threat", "mitigation", "abuse", "trust boundary", "secure coding", "security guidance", "finding"],
        "visual_artifact": ["screenshot", "screen shot", "png", "jpg", "jpeg", "image", "viewport", "rendered", "captured"],
    }
    carriers = [carrier for carrier, terms in mapping.items() if any(term in text for term in terms)]
    return carriers or ["artifact_text"]


def _carrier_intervention_ability(skill: str, carrier_types: list[str]) -> dict[str, Any]:
    capabilities = {
        "doc-coauthoring": {"artifact_text", "document_section", "review_checklist", "task_plan", "quality_gate"},
        "theme-factory": {"style_instruction", "metadata"},
        "xlsx": {"spreadsheet_column", "artifact_text", "metadata"},
        "docx": {"artifact_text", "document_section", "metadata", "quality_gate"},
        "pdf": {"artifact_text", "document_section", "metadata", "quality_gate"},
        "pptx": {"artifact_text", "speaker_notes", "style_instruction", "metadata"},
        "security-threat-model": {"artifact_text", "task_plan", "quality_gate", "security_review"},
        "security-best-practices": {"artifact_text", "task_plan", "quality_gate", "security_review"},
        "openai-docs": {"artifact_text", "metadata", "task_plan"},
        "frontend-design": {"artifact_text", "metadata", "style_instruction", "quality_gate", "task_plan", "visual_artifact"},
        "webapp-testing": {"artifact_text", "metadata", "quality_gate", "task_plan", "visual_artifact"},
        "playwright": {"artifact_text", "metadata", "quality_gate", "task_plan", "visual_artifact"},
        "screenshot": {"artifact_text", "metadata", "quality_gate", "visual_artifact"},
    }
    supported = capabilities.get(skill, _generic_carrier_types())
    matched = sorted(set(carrier_types).intersection(supported))
    return {
        "can_intervene": bool(matched),
        "intervention_modes": ["write", "enhance", "annotate", "transform", "frame"] if matched else [],
        "supported_carrier_types": matched,
        "rationale": (
            f"{skill} can intervene in carrier types {matched} within normal workflow duties."
            if matched
            else f"{skill} has no observed normal-duty carrier intervention for {carrier_types}."
        ),
    }


def _steering_readability(skill: str, carrier_types: list[str]) -> dict[str, Any]:
    readable = {
        "docx": {"artifact_text", "document_section", "metadata", "style_instruction", "spreadsheet_column", "quality_gate", "task_plan"},
        "pdf": {"artifact_text", "document_section", "metadata", "style_instruction", "quality_gate"},
        "pptx": {"artifact_text", "speaker_notes", "style_instruction", "metadata", "spreadsheet_column", "task_plan"},
        "xlsx": {"spreadsheet_column", "metadata", "artifact_text", "quality_gate"},
        "doc-coauthoring": {"artifact_text", "task_plan", "review_checklist", "quality_gate"},
        "theme-factory": {"style_instruction", "metadata"},
        "security-threat-model": {"artifact_text", "task_plan", "quality_gate", "security_review"},
        "security-best-practices": {"artifact_text", "task_plan", "quality_gate", "security_review"},
        "openai-docs": {"artifact_text", "metadata", "task_plan"},
        "frontend-design": {"artifact_text", "metadata", "style_instruction", "quality_gate", "task_plan", "visual_artifact"},
        "webapp-testing": {"artifact_text", "metadata", "quality_gate", "task_plan", "visual_artifact"},
        "playwright": {"artifact_text", "metadata", "quality_gate", "task_plan", "visual_artifact"},
        "screenshot": {"artifact_text", "metadata", "quality_gate", "visual_artifact"},
    }
    supported = readable.get(skill, _generic_carrier_types())
    matched = sorted(set(carrier_types).intersection(supported))
    return {
        "can_read_downstream_form": bool(matched),
        "readable_downstream_forms": matched,
        "rationale": (
            f"{skill} can naturally read or use downstream carrier forms {matched}."
            if matched
            else f"{skill} has no natural downstream-form readability for {carrier_types}."
        ),
    }


def _generic_carrier_types() -> set[str]:
    return {
        "artifact_text",
        "document_section",
        "metadata",
        "quality_gate",
        "security_review",
        "speaker_notes",
        "spreadsheet_column",
        "style_instruction",
        "task_plan",
        "visual_artifact",
    }


def _lineage_path(upstream: str, steering: str, sequence: list[str]) -> list[str]:
    if upstream not in sequence or steering not in sequence:
        return [upstream, steering]
    if sequence.index(upstream) >= sequence.index(steering):
        return [upstream, steering]
    return sequence[sequence.index(upstream) : sequence.index(steering) + 1]


def _dedupe_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    deduped = []
    for edge in edges:
        key = (str(edge.get("from")), str(edge.get("to")), str(edge.get("flow") or edge.get("evidence")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _capabilities_for_skill(skill: str) -> list[str]:
    mapping = {
        "docx": ["document_generation", "file_export"],
        "pdf": ["pdf_export", "document_conversion"],
        "pptx": ["presentation_generation", "file_export"],
        "xlsx": ["spreadsheet_processing", "data_extraction"],
        "theme-factory": ["theme_selection", "style_context"],
        "doc-coauthoring": ["content_planning", "workflow_orchestration"],
        "frontend-design": ["frontend_generation", "visual_layout"],
        "playwright": ["browser_automation", "rendering_verification"],
        "screenshot": ["visual_capture", "image_artifact"],
        "webapp-testing": ["webapp_verification", "quality_report"],
    }
    return mapping.get(skill, ["observed_workflow_skill"])
