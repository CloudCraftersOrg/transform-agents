from itertools import pairwise

import pytest

from state.models import WaveStatus as S
from state.transitions import IllegalTransition, assert_legal, is_legal, legal_transitions


def test_happy_path():
    path = [S.PENDING, S.PRECHECK, S.INTERPRETING, S.REPLICATING, S.TESTING,
            S.CUTTING_OVER, S.PARITY_CHECK, S.FINOPS, S.DONE]
    for a, b in pairwise(path):
        assert is_legal(a, b), f"{a} -> {b}"


def test_terminal_states_have_no_exit():
    for s in (S.DONE, S.ROLLED_BACK, S.FAILED):
        assert legal_transitions(s) == frozenset()


def test_replicating_self_loop():
    assert is_legal(S.REPLICATING, S.REPLICATING)


def test_rollback_reachable_from_cutover_stages():
    for s in (S.TESTING, S.CUTTING_OVER, S.PARITY_CHECK, S.FINOPS):
        assert is_legal(s, S.ROLLING_BACK)


def test_escalated_can_resume_active():
    assert is_legal(S.TESTING, S.ESCALATED)
    assert is_legal(S.ESCALATED, S.TESTING)


def test_assert_legal_raises_on_skip():
    with pytest.raises(IllegalTransition):
        assert_legal(S.PENDING, S.CUTTING_OVER)
