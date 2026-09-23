from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FrameworkPaths:
    framework_root: Path
    workspace_root: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> "FrameworkPaths":
        root = (start or Path(__file__)).resolve()
        for candidate in [root, *root.parents]:
            if candidate.name in {"CoordPoison", "pair_poisoning_validation_framework", "poisoning_validation_framework"}:
                return cls(framework_root=candidate, workspace_root=candidate.parent)
        raise RuntimeError("Could not locate CoordPoison framework root")

    @property
    def benchmarks(self) -> Path:
        return self.framework_root / "benchmarks"

    @property
    def configs(self) -> Path:
        return self.framework_root / "configs"

    @property
    def schemas(self) -> Path:
        return self.framework_root / "schemas"

    @property
    def agents(self) -> Path:
        return self.framework_root / "agents"

    @property
    def clean_packs(self) -> Path:
        return self.benchmarks / "clean_packs"

    @property
    def benign_tasks(self) -> Path:
        return self.benchmarks / "benign_tasks"

    @property
    def benign_runs(self) -> Path:
        return self.benchmarks / "benign_runs"

    @property
    def runs(self) -> Path:
        return self.benchmarks / "runs"

    @property
    def experiments(self) -> Path:
        return self.benchmarks / "experiments"

    @property
    def judge_results(self) -> Path:
        return self.benchmarks / "judge_results"

    @property
    def exploits(self) -> Path:
        return self.benchmarks / "exploits"

    @property
    def stage_requests(self) -> Path:
        return self.benchmarks / "stage_requests"

    def pack_experiment(self, pack_id: str, experiment_id: str) -> Path:
        return self.runs / pack_id / "experiments" / experiment_id

    def pack_run(self, pack_id: str) -> Path:
        return self.runs / pack_id

    def baseline(self, pack_id: str) -> Path:
        return self.pack_run(pack_id) / "baseline"

    def experiment_baseline(self, pack_id: str, experiment_id: str) -> Path:
        return self.pack_experiment(pack_id, experiment_id) / "baseline"

    def stage_baseline(self, pack_id: str, experiment_id: str | None = None) -> Path:
        if experiment_id:
            return self.experiment_baseline(pack_id, experiment_id)
        return self.baseline(pack_id)

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace_root.resolve()))
        except ValueError:
            return str(path)
