from __future__ import annotations

# The decision log recorded `dispatched launch_test` and put the facts in a detail blob, so the
# record read as a list of internal step names. The facts were always in the result; this turns
# them into the sentence a person needs. The blob stays for whoever wants the data.

# The contract's phases, as the console labels them.
PHASES = {
    "precheck": "Checks",
    "interpret": "Plan",
    "replicate": "Copy data",
    "test": "Test run",
    "cutover": "Switch over",
    "parity": "Verify",
    "finops": "Costs",
}

# What each dispatcher step is trying to do, for when its result says nothing more specific.
STEPS = {
    "initialize_mgn": "set up the migration service",
    "start_replication": "started copying the servers to AWS",
    "mgn_status": "checked how the copying is going",
    "reconcile_wave_inventory": "matched the wave to the real servers",
    "resize_replication_server": "resized the copying server",
    "launch_test": "launched test copies of the servers",
    "discover_probe_targets": "worked out where the applications answer",
    "probe_apps": "checked whether the applications respond",
    "terminate_test_instances": "removed the test servers",
    "cutover": "switched the servers over to AWS",
    "rollback": "rolled back",
    "finalize": "finished the migration",
    "apply_remediation": "applied a fix",
    "deploy_lza": "deployed the landing zone",
}


def phase_label(name: str) -> str:
    return PHASES.get(name, name.replace("_", " ").capitalize())


def _n(value) -> int:
    return len(value) if isinstance(value, (list, tuple, set)) else int(value or 0)


def _plural(n: int, one: str, many: str = "") -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"


def describe(name: str, result: dict | None) -> str:
    r = result if isinstance(result, dict) else {}
    fallback = STEPS.get(name, name.replace("_", " "))

    if name == "initialize_mgn":
        return "set up the migration service" if r.get("created") else \
               "the migration service was already set up"

    if name == "start_replication":
        return "started copying " + _plural(_n(r.get("replicating")), "server") + " to AWS"

    if name == "reconcile_wave_inventory":
        if not r.get("reconciled"):
            if r.get("dry_run"):
                return (f"would swap in {_n(r.get('would_add'))} real servers and drop "
                        f"{_n(r.get('would_remove'))} placeholders")
            return f"could not match the wave to real servers: {r.get('reason') or 'no match'}"
        return (f"matched the wave to the real servers - added {_n(r.get('added'))}, "
                f"dropped {_n(r.get('removed'))} placeholders")

    if name == "resize_replication_server":
        return (f"made the copying server bigger: {r.get('from')} to {r.get('to')}"
                if r.get("resized") else
                f"left the copying server as it is ({r.get('reason') or r.get('instance_type')})")

    if name == "launch_test":
        return "launched test copies of the servers"

    if name == "discover_probe_targets":
        found, silent = _n(r.get("candidates")), _n(r.get("not_serving"))
        # Why they were silent is the whole diagnosis: a hung service and a dead one look the same
        # from outside, and they call for opposite responses.
        reasons = sorted(set((r.get("not_serving_why") or {}).values()))
        because = f" - {reasons[0]}" if len(reasons) == 1 else ""
        if not found:
            if silent:
                return f"none of the {silent} applications answered{because}"
            return f"could not find anywhere to check the applications: {r.get('reason') or 'none answered'}"
        s = "found " + _plural(found, "application") + " answering"
        return s + (f"; {silent} did not answer{because}" if silent else "")

    if name == "probe_apps":
        checked = _n(r.get("probed"))
        if not checked:
            return f"checked nothing: {r.get('reason') or 'no applications to check'}"
        return f"checked {_plural(checked, 'application')}; {_n(r.get('healthy'))} looked healthy"

    if name == "terminate_test_instances":
        kept = _n(r.get("protected"))
        s = "removed " + _plural(_n(r.get("terminated")), "test server")
        return s + (f"; kept {kept} that had already been switched over" if kept else "")

    if name == "cutover":
        return "switched " + _plural(_n(r.get("cutover")), "server") + " over to AWS"

    if name == "finalize":
        return "finished " + _plural(_n(r.get("finalized")), "server")

    if name == "apply_remediation":
        return (f"applied a fix: {r.get('action')}" if r.get("resolved") else
                f"could not apply a fix: {r.get('detail') or 'not resolved'}")

    return fallback
