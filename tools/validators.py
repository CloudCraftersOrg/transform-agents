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


def validate_iac(workdir: str) -> ValidationOutcome:
    """Oracle for the Interpreter's IaC output. Stub: `terraform validate` if it's on PATH.
    Runs the resolved absolute path with a fixed argument list, never a shell."""
    import shutil
    import subprocess

    terraform = shutil.which("terraform")
    if terraform is None:
        return ValidationOutcome(False, "terraform is not on PATH")
    p = subprocess.run(
        [terraform, "validate", "-no-color"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    return ValidationOutcome(p.returncode == 0, None if p.returncode == 0 else p.stderr.strip())
