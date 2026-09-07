from __future__ import annotations

from dataclasses import dataclass

MANDATORY_ACCOUNTS = ("Management", "LogArchive", "Audit")
_DISABLED_SECURITY = ("guardduty", "macie", "securityHub", "accessAnalyzer")


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    error: str | None = None


def _load(doc: str | dict) -> tuple[dict | None, str | None]:
    if isinstance(doc, dict):
        return doc, None
    try:
        import yaml
    except ModuleNotFoundError:
        return None, "pyyaml not installed (extra 'agents' or 'dev')"
    try:
        data = yaml.safe_load(doc)
    except yaml.YAMLError as e:
        return None, f"invalid YAML: {e}"
    if not isinstance(data, dict):
        return None, "root is not a mapping"
    return data, None


def validate_lza_config(doc: str | dict) -> ValidationOutcome:
    """Deterministic oracle for the Interpreter. Minimal structural check aligned with the plan's
    minimal stack (section 4): no Control Tower, security services and config recorder off, the
    3 mandatory accounts resolved by email. `doc` is a mapping (or YAML/JSON) with `global_config`,
    `accounts_config` and, optionally, `security_config`."""
    data, err = _load(doc)
    if err:
        return ValidationOutcome(False, err)
    assert data is not None

    g = data.get("global_config")
    if not isinstance(g, dict):
        return ValidationOutcome(False, "missing global_config")
    home, regions = g.get("homeRegion"), g.get("enabledRegions")
    if not home or not isinstance(regions, list) or not regions:
        return ValidationOutcome(False, "global_config: homeRegion / enabledRegions incomplete")
    if home not in regions:
        return ValidationOutcome(False, f"homeRegion {home!r} is not in enabledRegions")
    if (g.get("controlTower") or {}).get("enable", False):
        return ValidationOutcome(False, "controlTower.enable must be false (no Control Tower)")

    a = data.get("accounts_config")
    if not isinstance(a, dict):
        return ValidationOutcome(False, "missing accounts_config")
    accounts = {x.get("name"): x for x in a.get("mandatoryAccounts", []) if isinstance(x, dict)}
    for name in MANDATORY_ACCOUNTS:
        acc = accounts.get(name)
        if acc is None:
            return ValidationOutcome(False, f"missing mandatory account {name}")
        if "@" not in str(acc.get("email", "")):
            return ValidationOutcome(False, f"account {name} has no valid email")

    s = data.get("security_config")
    if isinstance(s, dict):
        for svc in _DISABLED_SECURITY:
            if (s.get(svc) or {}).get("enable", False):
                return ValidationOutcome(False, f"{svc}.enable must be false")
        aws_config = s.get("awsConfig") or {}
        if aws_config.get("enableConfigurationRecorder", False):
            return ValidationOutcome(False, "awsConfig.enableConfigurationRecorder must be false")
        if aws_config.get("ruleSets"):
            return ValidationOutcome(False, "awsConfig.ruleSets must be empty")

    return ValidationOutcome(True)


# A verified modernization artifact must at least describe a container workload with an image source
# and a compute target. Minimal structural check, same spirit as validate_lza_config.
_CONTAINER_COMPUTE = ("aws_ecs_service", "aws_apprunner_service", "aws_ecs_task_definition")
_IMAGE_SOURCE = ("aws_ecr_repository", "image")


def _as_path(source: str):
    """`source` is either a path or the HCL itself. Only probe the filesystem when it could be a
    path: pathlib lets ENAMETOOLONG escape from is_dir()/is_file() on Linux, so handing it a
    multi-KB document raises OSError instead of returning False (Windows hides this)."""
    from pathlib import Path

    if len(source) > 255 or "\n" in source or "\r" in source:
        return None
    try:
        return Path(source)
    except (ValueError, OSError):
        return None


def validate_iac(source: str) -> ValidationOutcome:
    """Oracle for the Interpreter's modernization IaC. If the `terraform` binary is on PATH and
    `source` is a directory, run `terraform validate`; otherwise do an offline structural check of
    the HCL text (parses, declares a container compute target, references an image)."""
    import shutil
    import subprocess

    path = _as_path(source)
    terraform = shutil.which("terraform")
    if terraform is not None and path is not None and path.is_dir():
        p = subprocess.run(
            [terraform, "validate", "-no-color"],
            cwd=source, capture_output=True, text=True, check=False, shell=False,
        )
        return ValidationOutcome(p.returncode == 0, None if p.returncode == 0 else p.stderr.strip())

    text = path.read_text(encoding="utf-8") if path is not None and path.is_file() else source
    try:
        import hcl2

        parsed = hcl2.loads(text)
    except Exception as e:  # noqa: BLE001 - any HCL parse failure is a rejection
        return ValidationOutcome(False, f"HCL does not parse: {e}")

    # python-hcl2 keeps the literal quotes on block labels: {'"aws_ecs_service"': {...}}
    resources = {
        rtype.strip('"')
        for block in parsed.get("resource", [])
        if isinstance(block, dict)
        for rtype in block
    }
    if not resources & set(_CONTAINER_COMPUTE):
        return ValidationOutcome(False, f"no container compute target ({', '.join(_CONTAINER_COMPUTE)})")
    if not (resources & {"aws_ecr_repository"} or "image" in text):
        return ValidationOutcome(False, "no container image source (aws_ecr_repository or an image reference)")
    return ValidationOutcome(True)
