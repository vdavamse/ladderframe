"""`ladderframe eval`: run the pydantic-evals datasets in `<root>/evals/` against the agent.

Each dataset file (`*.yaml`, `*.yml`, `*.json`) is a pydantic-evals `Dataset` whose inputs are prompts and
whose outputs are the agent's final answers. Every case runs as a fresh, in-process turn (no session
history, no Temporal), so evals are cheap to run in CI. A case passes when all its assertion evaluators
pass; the command fails when the pass rate is below `--min-pass-rate`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime.runtime import Runtime

DATASET_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass
class DatasetResult:
    path: Path
    report: Any
    passed: int
    total: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0


def find_datasets(runtime: Runtime, names: list[str] | None = None) -> list[Path]:
    directory = runtime.root.path / "evals"
    if not directory.is_dir():
        return []
    paths = sorted(p for p in directory.iterdir() if p.suffix in DATASET_SUFFIXES and p.is_file())
    if names:
        paths = [p for p in paths if p.stem in names or p.name in names]
    return paths


def _case_passed(case: Any) -> bool:
    return all(result.value is True for result in case.assertions.values())


async def run_dataset(runtime: Runtime, path: Path, max_concurrency: int = 4) -> DatasetResult:
    from pydantic_evals import Dataset

    dataset = Dataset[str, str, Any].from_file(path)

    async def answer(prompt: str) -> str:
        result = await runtime.run(prompt, session_id=f"eval-{uuid.uuid4().hex[:12]}")
        return result.output

    report = await dataset.evaluate(answer, name=path.stem, max_concurrency=max_concurrency, progress=False)
    passed = sum(1 for case in report.cases if _case_passed(case))
    return DatasetResult(path, report, passed, len(report.cases) + len(report.failures))
