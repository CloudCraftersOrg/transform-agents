from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from agents.model import ModelLike
from agents.orchestrator import TOOL_CATALOG, route

CASES = Path(__file__).with_name("cases.jsonl")
REQUIRED = {"id", "prompt", "expected_tool"}
THRESHOLD = 0.9  # plan: if the Orchestrator does not pass 90%, use Claude Haiku just for that agent


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_cases(cases: list[dict]) -> list[str]:
    problems = []
    for c in cases:
        if not REQUIRED <= c.keys():
            problems.append(f"{c.get('id', '?')}: missing fields")
        elif c["expected_tool"] not in TOOL_CATALOG:
            problems.append(f"{c['id']}: unknown expected_tool {c['expected_tool']!r}")
    return problems


@dataclass
class Report:
    total: int
    correct: int
    by_tool: dict[str, tuple[int, int]]

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def score(model: ModelLike, cases: list[dict]) -> Report:
    correct = 0
    by_tool: dict[str, list[int]] = {}
    for c in cases:
        got, exp = route(model, c["prompt"]), c["expected_tool"]
        hit = int(got == exp)
        correct += hit
        slot = by_tool.setdefault(exp, [0, 0])
        slot[0] += hit
        slot[1] += 1
    return Report(len(cases), correct, {k: (v[0], v[1]) for k, v in by_tool.items()})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="bedrock:<model_id> to run real scoring")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args(argv)

    cases = load_cases()
    problems = validate_cases(cases)
    if problems:
        print("\n".join(problems))
        return 1

    if not args.model:
        print(f"{len(cases)} cases, {len(TOOL_CATALOG)} tools. OK. Pass --model bedrock:<id> to run scoring.")
        return 0
    if not args.model.startswith("bedrock:"):
        print("--model must be bedrock:<model_id>")
        return 1

    from agents.model import BedrockModel

    report = score(BedrockModel(args.model.split(":", 1)[1], args.region, max_tokens=64), cases)
    print(f"accuracy {report.accuracy:.0%} ({report.correct}/{report.total})")
    for tool, (ok, tot) in sorted(report.by_tool.items()):
        print(f"  {tool:<22} {ok}/{tot}")
    return 0 if report.accuracy >= THRESHOLD else 2


if __name__ == "__main__":
    sys.exit(main())
