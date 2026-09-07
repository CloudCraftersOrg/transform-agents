from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from agents.model import ModelLike
from agents.tools import build_tools

# Scores the tool surface the deployed agent actually has. The catalog is derived from the `@tool`
# definitions rather than kept as a second list: the previous version scored a hand-maintained
# TOOL_CATALOG that had drifted away from the agent entirely, so a 92% described tools that were
# never offered. Deriving it means the eval cannot go stale without the tools changing too.

CASES = Path(__file__).with_name("cases.jsonl")
REQUIRED = {"id", "prompt", "expected_tool"}
THRESHOLD = 0.9  # plan: below this, give the Orchestrator a stronger model


def tool_catalog() -> dict[str, str]:
    """{name: description} straight off the agent's own tools."""
    catalog = {}
    for t in build_tools(None):
        spec = t.tool_spec
        catalog[spec["name"]] = " ".join((spec.get("description") or "").split())
    return catalog


def route_system(catalog: dict[str, str]) -> str:
    listing = "\n".join(f"- {n}: {d}" for n, d in catalog.items())
    return (
        "You are the Orchestrator of an AWS migration. Pick ONE tool for the situation.\n"
        f"Tools:\n{listing}\n"
        "If nothing you have can clear the blocker, choose escalate.\n"
        "Reply with only the exact tool name."
    )


def route(model: ModelLike, situation: str, catalog: dict[str, str]) -> str:
    raw = model.complete(situation, system=route_system(catalog)).strip().strip("`'\"").strip()
    for name in catalog:
        if raw == name or name in raw:
            return name
    return raw[:40]


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validate_cases(cases: list[dict], catalog: dict[str, str] | None = None) -> list[str]:
    catalog = catalog if catalog is not None else tool_catalog()
    problems = []
    seen: set[str] = set()
    for c in cases:
        if not REQUIRED <= c.keys():
            problems.append(f"{c.get('id', '?')}: missing fields")
            continue
        if c["id"] in seen:
            problems.append(f"{c['id']}: duplicate id")
        seen.add(c["id"])
        if c["expected_tool"] not in catalog:
            problems.append(f"{c['id']}: unknown expected_tool {c['expected_tool']!r}")
    # every tool the agent has should be exercised, or the score hides a blind spot
    for name in catalog:
        if not any(c.get("expected_tool") == name for c in cases):
            problems.append(f"no case covers {name!r}")
    return problems


@dataclass
class Report:
    total: int
    correct: int
    by_tool: dict[str, tuple[int, int]]

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def score(model: ModelLike, cases: list[dict], catalog: dict[str, str] | None = None) -> Report:
    catalog = catalog if catalog is not None else tool_catalog()
    correct = 0
    by_tool: dict[str, list[int]] = {}
    for c in cases:
        hit = int(route(model, c["prompt"], catalog) == c["expected_tool"])
        correct += hit
        slot = by_tool.setdefault(c["expected_tool"], [0, 0])
        slot[0] += hit
        slot[1] += 1
    return Report(len(cases), correct, {k: (v[0], v[1]) for k, v in by_tool.items()})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="bedrock:<model_id> to run real scoring")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args(argv)

    catalog = tool_catalog()
    cases = load_cases()
    problems = validate_cases(cases, catalog)
    if problems:
        print("\n".join(problems))
        return 1

    if not args.model:
        print(f"{len(cases)} cases over the agent's {len(catalog)} real tools. OK. "
              f"Pass --model bedrock:<id> to score.")
        return 0
    if not args.model.startswith("bedrock:"):
        print("--model must be bedrock:<model_id>")
        return 1

    from agents.model import BedrockModel

    report = score(BedrockModel(args.model.split(":", 1)[1], args.region, max_tokens=64),
                   cases, catalog)
    print(f"accuracy {report.accuracy:.0%} ({report.correct}/{report.total})")
    for tool, (ok, tot) in sorted(report.by_tool.items()):
        print(f"  {tool:<24} {ok}/{tot}")
    return 0 if report.accuracy >= THRESHOLD else 2


if __name__ == "__main__":
    sys.exit(main())
