from __future__ import annotations

from agents.model import ModelLike, strip_code_fence
from agents.trust import ConvergeResult, converge
from tools.validators import validate_iac, validate_lza_config

SYSTEM_PROMPT = (
    "You are the Interpreter. You generate valid configuration (LZA, IaC) from an ambiguous "
    "source. You never hand off anything that hasn't passed the deterministic validator. Once "
    "the iteration budget is exhausted, you escalate with a diagnosis instead of failing silently."
)

TOOLS = ["read_spec", "read_transform_artifact", "query_kb", "validate_lza_config", "validate_iac"]


# The oracle's contract, stated for the model. Without it a real model has to guess key names and
# burns the whole iteration budget on schema, not on the objective.
LZA_SPEC = """The validator requires exactly this YAML shape:

global_config:
  homeRegion: <region>
  enabledRegions: [<must include homeRegion>]
  controlTower: {enable: false}
accounts_config:
  mandatoryAccounts:
    - {name: Management, email: <address with @>}
    - {name: LogArchive, email: <address with @>}
    - {name: Audit, email: <address with @>}
security_config:
  guardduty: {enable: false}
  macie: {enable: false}
  securityHub: {enable: false}
  accessAnalyzer: {enable: false}
  awsConfig: {enableConfigurationRecorder: false, ruleSets: []}"""


def _prompt(objective: str, spec: str, errors: list[str]) -> str:
    parts = [f"OBJECTIVE: {objective}", f"SPECIFICATION:\n{spec or LZA_SPEC}"]
    if errors:
        parts.append(f"The validator rejected the previous attempt: {errors[-1]}\nFix it.")
    parts.append("Return only the YAML config, nothing else.")
    return "\n\n".join(parts)


def generate_lza_config(
    objective: str, model: ModelLike, *, spec: str = "", max_iter: int = 5
) -> ConvergeResult:
    def generate(errors: list[str]) -> str:
        return strip_code_fence(model.complete(_prompt(objective, spec, errors), system=SYSTEM_PROMPT))

    return converge(generate, validate_lza_config, max_iter=max_iter)


_MODERNIZATION_SYSTEM = (
    "You are the Interpreter, generating the modernization scenario as a VERIFIED Terraform "
    "artifact - it is never applied, only checked. Target: a container deployment (ECS/Fargate or "
    "App Runner) with an ECR image source. Return only HCL."
)


def _iac_prompt(objective: str, spec: str, errors: list[str]) -> str:
    parts = [f"OBJECTIVE: containerize - {objective}"]
    if spec:
        parts.append(f"SPECIFICATION:\n{spec}")
    if errors:
        parts.append(f"The IaC validator rejected the previous attempt: {errors[-1]}\nFix it.")
    parts.append("Return only Terraform HCL for the container deployment.")
    return "\n\n".join(parts)


def generate_modernization_iac(
    objective: str, model: ModelLike, *, spec: str = "", max_iter: int = 5
) -> ConvergeResult:
    """The modernization half of deliverable 1: a container-deployment IaC artifact, generated and
    verified against `validate_iac`, never executed."""

    def generate(errors: list[str]) -> str:
        return strip_code_fence(model.complete(_iac_prompt(objective, spec, errors), system=_MODERNIZATION_SYSTEM))

    return converge(generate, validate_iac, max_iter=max_iter)


def build_interpreter(model: ModelLike):
    """agent-as-tool wrapper for the Orchestrator. The Strands `@tool` variant lands in Sprint 2."""

    def delegate_interpreter(objective: str, spec: str = "") -> ConvergeResult:
        return generate_lza_config(objective, model, spec=spec)

    return delegate_interpreter
