import pytest

from agents.describe import PHASES, describe, phase_label

# The words that made the console unreadable: the system's own vocabulary, leaking into sentences
# a person has to act on.
JARGON = ("probe", "baseline", "diff", "verdict", "gate", "dispatch", "mgn", "ec2",
          "wave_id", "source server", "lifecycle", "instance id")


@pytest.mark.parametrize(("name", "result", "expected"), [
    ("initialize_mgn", {"created": True}, "set up the migration service"),
    ("initialize_mgn", {"created": False}, "the migration service was already set up"),
    ("start_replication", {"replicating": ["s"] * 13}, "started copying 13 servers to AWS"),
    ("start_replication", {"replicating": ["s"]}, "started copying 1 server to AWS"),
    ("probe_apps", {"probed": 3, "healthy": 2}, "checked 3 applications; 2 looked healthy"),
    ("cutover", {"cutover": ["a", "b"]}, "switched 2 servers over to AWS"),
])
def test_a_step_reports_what_it_did_in_a_sentence(name, result, expected):
    assert describe(name, result) == expected


def test_a_failed_step_says_why_rather_than_reporting_success():
    said = describe("discover_probe_targets",
                    {"candidates": [], "reason": "none of the migrated servers answered"})
    assert "could not find" in said
    assert "none of the migrated servers answered" in said


def test_a_partial_result_reports_both_halves():
    said = describe("discover_probe_targets", {"candidates": [1], "not_serving": ["a", "b"]})
    assert "1 application answering" in said and "2 did not answer" in said


def test_an_unknown_step_still_reads_as_words_not_an_identifier():
    assert describe("some_new_step", {}) == "some new step"


def test_a_missing_or_malformed_result_never_raises():
    for bad in (None, {}, [], "nope"):
        assert describe("cutover", bad)


def test_no_sentence_carries_the_system_vocabulary():
    """The complaint that started this: the record read as internal step names and tool words."""
    samples = [
        describe("reconcile_wave_inventory",
                 {"reconciled": True, "added": ["a"] * 12, "removed": ["p"] * 12}),
        describe("discover_probe_targets", {"candidates": [1, 2, 3], "not_serving": []}),
        describe("terminate_test_instances", {"terminated": 12, "protected": []}),
        describe("launch_test", {"job_id": "j"}),
    ]
    for said in samples:
        low = said.lower()
        assert not any(word in low for word in JARGON), said


def test_every_contract_phase_has_a_human_label():
    for step in ("precheck", "interpret", "replicate", "test", "cutover", "parity", "finops"):
        assert step in PHASES
        assert phase_label(step) != step


def test_an_unmapped_phase_falls_back_to_readable_words():
    assert phase_label("some_phase") == "Some phase"


def test_the_reason_for_silence_reaches_the_sentence():
    """"nothing answered" was the whole report. Which kind of nothing is the diagnosis."""
    said = describe("discover_probe_targets", {
        "candidates": [], "not_serving": ["a", "b", "c"],
        "not_serving_why": {h: "accepted the connection but never replied - usually a dependency "
                               "it cannot reach" for h in ("a", "b", "c")}})
    assert said == ("none of the 3 applications answered - accepted the connection but never "
                    "replied - usually a dependency it cannot reach")


def test_mixed_reasons_are_not_flattened_into_one_wrong_cause():
    said = describe("discover_probe_targets", {
        "candidates": [], "not_serving": ["a", "b"],
        "not_serving_why": {"a": "refused the connection - nothing is listening",
                            "b": "no answer at all - a firewall is dropping traffic"}})
    assert said == "none of the 2 applications answered"


def test_a_partial_answer_still_names_the_reason_for_the_rest():
    said = describe("discover_probe_targets", {
        "candidates": [1], "not_serving": ["b"],
        "not_serving_why": {"b": "refused the connection - nothing is listening"}})
    assert "1 application answering" in said
    assert "1 did not answer - refused the connection" in said
