from __future__ import annotations

from state.models import WaveStatus

S = WaveStatus

_ACTIVE = (
    S.PRECHECK,
    S.INTERPRETING,
    S.REPLICATING,
    S.TESTING,
    S.CUTTING_OVER,
    S.PARITY_CHECK,
    S.FINOPS,
)

# Legal transition table. Lives in code, not in DynamoDB.
_LEGAL: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.PRECHECK, S.ESCALATED}),
    S.PRECHECK: frozenset({S.INTERPRETING, S.ESCALATED, S.FAILED}),
    S.INTERPRETING: frozenset({S.REPLICATING, S.ESCALATED, S.FAILED}),
    S.REPLICATING: frozenset({S.REPLICATING, S.TESTING, S.ESCALATED, S.FAILED}),
    S.TESTING: frozenset({S.CUTTING_OVER, S.ROLLING_BACK, S.ESCALATED, S.FAILED}),
    S.CUTTING_OVER: frozenset({S.PARITY_CHECK, S.ROLLING_BACK, S.ESCALATED, S.FAILED}),
    S.PARITY_CHECK: frozenset({S.FINOPS, S.ROLLING_BACK, S.ESCALATED, S.FAILED}),
    S.FINOPS: frozenset({S.DONE, S.ROLLING_BACK, S.ESCALATED}),
    S.ROLLING_BACK: frozenset({S.ROLLED_BACK, S.FAILED, S.ESCALATED}),
    S.ROLLED_BACK: frozenset(),
    S.DONE: frozenset(),
    S.ESCALATED: frozenset({*_ACTIVE, S.FAILED, S.ROLLING_BACK}),
    S.FAILED: frozenset(),
}


class IllegalTransition(RuntimeError):
    pass


def legal_transitions(current: WaveStatus) -> frozenset[WaveStatus]:
    return _LEGAL[current]


def is_legal(current: WaveStatus, target: WaveStatus) -> bool:
    return target in _LEGAL[current]


def assert_legal(current: WaveStatus, target: WaveStatus) -> None:
    if not is_legal(current, target):
        raise IllegalTransition(f"{current} -> {target} is not in the legal transition table")
