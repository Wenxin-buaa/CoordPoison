from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import write_json


@dataclass(frozen=True)
class StageRunResult:
    mode: str
    request_dir: Path
    prompt_path: Path
    output_path: Path | None = None
    ingested_path: Path | None = None
    raw_output_path: Path | None = None
    status: str = "prepared"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "request_dir": str(self.request_dir),
            "prompt_path": str(self.prompt_path),
            "output_path": str(self.output_path) if self.output_path else None,
            "ingested_path": str(self.ingested_path) if self.ingested_path else None,
            "raw_output_path": str(self.raw_output_path) if self.raw_output_path else None,
            "status": self.status,
            "message": self.message,
        }


class OpenAICompatibleStageRunner:
    """Minimal OpenAI-compatible chat runner for stage prompts."""

    def __init__(
        self,
        *,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        base_url_env: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: int = 180,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = (
            base_url
            or (os.getenv(base_url_env) if base_url_env else None)
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def run(self, prompt: str) -> str:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing API key env var: {self.api_key_env}")

        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an automation stage runner for a controlled benchmark framework. "
                        "Follow the user's stage request exactly and return only the requested artifact content."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {body}") from exc

        return data["choices"][0]["message"]["content"]


def materialize_model_output(
    *,
    request: dict[str, Any],
    raw_output: str,
    request_dir: Path,
) -> Path | None:
    """Convert model output into a file for ingest when the contract is simple."""
    contract = request.get("contract", {})
    output_type = contract.get("type")
    try:
        if output_type == "json":
            data = _extract_json(raw_output)
            out = request_dir / "api_output.json"
            write_json(out, data)
            return out
        if output_type == "jsonl":
            rows = _extract_jsonl(raw_output)
            out = request_dir / "api_output.jsonl"
            with out.open("w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return out
    except Exception as exc:
        request_dir.mkdir(parents=True, exist_ok=True)
        error_path = request_dir / "materialize_error.txt"
        error_path.write_text(
            "\n".join(
                [
                    f"Failed to materialize provider output as {output_type}.",
                    f"Error: {exc}",
                    "",
                    "The raw provider response is saved in provider_response.txt.",
                    "Fix that response into the expected output format, then run `auto_run.py ingest ...`.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return None
    return None


def _extract_json(text: str) -> Any:
    candidates = _candidate_json_blocks(text)
    errors = []
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
    raise ValueError("No valid JSON object/array found in provider output: " + "; ".join(errors[:3]))


def _extract_jsonl(text: str) -> list[dict[str, Any]]:
    candidates = _candidate_json_blocks(text)
    errors = []
    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped:
            continue
        try:
            if stripped.startswith("["):
                data = json.loads(stripped)
                if not isinstance(data, list):
                    raise ValueError("Expected JSON array for jsonl materialization")
                return data
            rows = []
            for line in stripped.splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
            return rows
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError("No valid JSONL rows or JSON array found in provider output: " + "; ".join(errors[:3]))


def _candidate_json_blocks(text: str) -> list[str]:
    stripped = text.strip()
    candidates = []

    lines = stripped.splitlines()
    in_fence = False
    fence_lines: list[str] = []
    for line in lines:
        if line.strip().startswith("```"):
            if in_fence:
                candidates.append("\n".join(fence_lines).strip())
                fence_lines = []
                in_fence = False
            else:
                in_fence = True
                fence_lines = []
            continue
        if in_fence:
            fence_lines.append(line)

    candidates.append(stripped)

    first_object = _slice_balanced(stripped, "{", "}")
    if first_object:
        candidates.append(first_object)
    first_array = _slice_balanced(stripped, "[", "]")
    if first_array:
        candidates.append(first_array)

    return [candidate for candidate in candidates if candidate]


def _slice_balanced(text: str, opener: str, closer: str) -> str:
    start = text.find(opener)
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return ""
