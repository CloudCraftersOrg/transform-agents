from tools.trace import MARKS, PREFIX, fail, note, trace


def test_a_decision_reaches_stdout_as_one_scannable_line(capsys):
    trace("wave-0", "policy_denial", "cutover withheld: 9 of 12 servers fail",
          {"gate": "proceed_wave_cutover", "job_id": "8bd958ab-a395-459a", "ignored": "x"})
    line = capsys.readouterr().out.strip()
    assert line.startswith(f"{PREFIX} {MARKS['policy_denial']} policy_denial")
    assert "wave-0" in line and "cutover withheld" in line
    # highlights ride on the line; the rest of the detail stays in the decision log
    assert "gate=proceed_wave_cutover" in line and "job_id=8bd958a" in line
    assert "ignored" not in line


def test_a_multiline_summary_stays_on_one_line(capsys):
    trace("w", "decision", "first\nsecond")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_runtime_notes_and_failures_carry_the_same_prefix(capsys):
    note("migration workspace: EPAM-PoC-Business-Case")
    fail("RuntimeError: contract did not converge")
    out, err = capsys.readouterr()
    assert all(line.startswith(PREFIX) for line in out.strip().splitlines())
    # a failure also reaches stderr, so a reader tailing only errors still sees it
    assert "contract did not converge" in err
