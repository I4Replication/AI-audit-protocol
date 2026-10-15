"""2.7.7: two defects that stopped real audits, each tested with the real scripts.

1. Phase-1 output admission refused any line that held the word "participant(s)" or "subject(s)" and any d:dd, as a
   participant linked to a date or time. A sleep study's mean bedtime (23:23) or conditions named by a ratio (20:80)
   are on such lines, so one audit was parked before Phase 1 and one role failed all six attempts. The admission rule
   now needs an identifier holding a digit and a real date or clock time. The privacy scan's patterns, whose counts
   are Phase-1 evidence, are unchanged.
2. A consistency check run before round 1 is logged round=0, and the projection made a round-0 row of it, which
   finalize refuses (rounds must run 1..N) and the append-only journal could never remove. A round-0 gate now stays
   in gates_run and makes no round row.

Each test has a negative control: what was refused before is still refused.
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lifecycle_journal as lj  # noqa: E402
import phase1_runtime  # noqa: E402


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("I4R_", "RUN_JSON"))}
    env.update(PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", CODEX="/nonexistent/codex", **extra)
    return env


def run(argv: list, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(item) for item in argv], text=True, capture_output=True, timeout=300,
                          env=clean_env(**env), check=False)


def admit(line: str) -> None:
    phase1_runtime.check_output_hygiene(f"# Report\n\n{line}\n", {})


# Lines about participants as a group beside a time, a ratio or a date: the lines that stopped the two audits
# (reworded; no paper text, no participant value) and the review's group-level lines.
FALSE_POSITIVES = (
    'Across participants, mean bedtime was 23:23 and mean wake time 7:41 (SD 0:38).',
    'Participants in the 20:80 and 50:50 conditions were compared with a mixed model.',
    'Subjects went to bed at 22:45 on average in the intensified week.',
    'The participant pool split 70:30 between the two schedules; subject to the 12:00 cut-off.',
    'Participants 18 to 30 years old went to bed at a mean of 23:23.',
    'Subjects 1-24 completed the protocol; mean bedtime 23:23.',
    'Participants (N = 24) went to bed at 23:23 on 2019-03-02.',
    'Sessions ran from 1900-01-01; the Name column identifies 57 participants.',
    'participant 20 was in the 20:80 condition and participant 21 in the 50:50 one',
    'Recomputed Monday week-1 bedtime 23:23 ± 00:42 (between-participant SD) matches Table 2.',
    "The paper's ± values are the between-subject SD (e.g., 23:23 ± 00:42), not the SE.",
    'Half of the participants (12/24) went to bed after 23:00.',
    'Participants (12 and 12 per group) went to bed at 23:23.',
    'Participants 12 or 13 years old went to bed at 21:30 on average.',
    'Participants were tested between 08:00 and 10:00 on weekdays.',
    'Participant characteristics: mean age 21, bedtime 23:23.',
    "The participants' sleep onset (00:15) followed lights-out (23:40).",
    'PARTICIPANTS AND METHODS section reports 23:23 bedtimes.',
    'Data from all subjects were collected between 2019-03-02 and 2019-04-02.',
    'Participant flow: 30 screened on 2019-03-02, 24 analysed.',
    'participants no longer slept past 07:00 after the intervention.',
    'Subjects with no 3 valid nights are dropped, which moves the mean bedtime from 23:23 to 23:31.',
    'Participants with no 7-day actigraphy record were excluded before the mean bedtime (23:23) was computed.',
    'Participant N = 24; the mean bedtime was 23:23.',
    'Participant M = 23:23, SD = 0:42.',
    'Participant IDs were anonymised before 2019-03-02.',
    'Participants "slept" until 07:00 on average.',
)
# A participant identifier beside a date or clock time, in the forms an auditor might write (all refused by 2.7.6).
LINKAGES = (
    'participant 12 went to bed at 23:23',
    'subject07 was tested on 2019-03-02',
    'Participant P07 woke at 6:05.',
    'pid_3 2024-1-5',
    'SUBJ-4A 2021/11/30 visit',
    'subject 3 slept from 23:10:05',
    'Participant ID 12 woke at 05:30.',
    'Participant no. 12 was tested on 2019-03-02.',
    'Participant number 12 was tested on 2019-03-02.',
    'subject code S07 completed the session on 2019-03-02.',
    'Participants 12 and 14 went to bed after 01:30.',
    'Subjects 3, 7 and 9 were tested on 2019-03-02.',
    'Participant A was tested on 2019-03-02.',
    "participant 12's first record is 2019-03-02T08:30:00.",
    'Participants P07 and P09 went to bed after 01:30.',
    'Subjects S03 and S07 were tested on 2019-03-02.',
    'Participants S1, S2 and S3 slept past 02:00.',
    'subjects s01 and s02 were tested on 2019-03-02',
    'Participant IDs 12 and 14 were tested on 2019-03-02.',
    'Participant numbers 12 and 14 were tested on 2019-03-02.',
    'Participants #12 and #14 were tested on 2019-03-02.',
    'Participants: 12 and 14 were both tested on 2019-03-02.',
    'The participant with ID 12 went to bed at 01:30.',
    'Participant identifier 12 was tested on 2019-03-02.',
    'subject id number 12 was tested on 2019-03-02.',
    'Participant n° 12 was tested on 2019-03-02.',
    'Participant nº 12 went to bed at 23:15.',
    'The script drops participant_id == 12 for the session on 2019-03-02.',
    '`participant_id == 12` is dropped for the 2019-03-02 session.',
    'The log line reads participant_id=12 start=08:30.',
    'subject_id = 7 was tested on 2019-03-02.',
    'Participant TEN-07 played the 20:80 block on 2019-03-02.',
    'Participant ABC-12 was tested on 2019-03-02.',
    "subject sub-01's scan began at 08:30.",
    'Participant VPN 07 was tested on 2019-03-02.',
    'participant p-07 went to bed at 23:15.',
    'subject s.07 was tested on 2019-03-02.',
    'participant p_07 was tested on 2019-03-02.',
    'Participant JMK went to bed at 23:15.',
    'participant 12 bedtime:23:15',
    'participant 12 went to bed at 23:15: the latest in the sample.',
    'At 08:30: participant 12 arrived.',
    'participant 12 [02/Mar/2019:08:30:00 +0000] GET /task',
    'participant 12 went to bed at ２３:１５.',
    'participant 12 went to bed at 24:00.',
    'Participant #12 went to bed at 23:15, later than the other participants.',
    'Participant #12 (one of 24 participants) was tested on 2019-03-02.',
    'Of the 24 participants, Subj. 12 went to bed latest (01:30).',
    "Participant\xa012 went to bed at 23:15 (the participants' mean was 23:23).",
    "Participant **12** went to bed at 23:15 (the participants' mean was 23:23).",
    'Subject ID `S07` was tested on 2019-03-02.',
    'Participant ID: `P07` went to bed at 01:30.',
    'Participants `P07` and `P09` went to bed after 01:30.',
    'Participants **12** and **14** went to bed after 01:30.',
    'Subjects `S03`, `S07` and `S09` were tested on 2019-03-02.',
    'Participant IDs `12` and `14` were tested on 2019-03-02.',
    'The script drops `participant_id == "P07"` for the 2019-03-02 session.',
    "R line 88, `filter(subject_id != 'S07')`, removes the 2019-03-02 session.",
    'The log line reads {"participant_id": "P07", "start": "2019-03-02T08:30:00"}.',
    "Row 5 is {'participant_id': 12, 'date': '2019-03-02'}.",
    'Participant F woke at 05:30.',
    'Participant M went to bed at 01:30 on 2019-03-02.',
    'The participant whose ID is 12 went to bed at 01:30.',
    'The participant with the ID 12 went to bed at 01:30.',
    'Participant SLEEP-07 was tested on 2019-03-02.',
)


class ParticipantDateAdmission(unittest.TestCase):
    def test_the_lines_that_stopped_two_audits_are_admitted(self) -> None:
        for line in FALSE_POSITIVES:
            with self.subTest(line=line):
                self.assertTrue(phase1_runtime.PARTICIPANT_ID.search(line) and phase1_runtime.DATE.search(line),
                                "the 2.7.6 rule refused this line")
                admit(line)

    def test_negative_control_an_identifier_beside_a_date_or_time_is_still_refused(self) -> None:
        for line in LINKAGES:
            with self.subTest(line=line):
                with self.assertRaises(phase1_runtime.OutputValidationError) as caught:
                    admit(line)
                self.assertEqual(caught.exception.rule, "participant_date")

    def test_a_ratio_beside_a_numbered_participant_is_not_a_time(self) -> None:
        admit("participant 20 was in the 20:80 condition and participant 21 in the 50:50 one")

    def test_the_rule_only_loosened(self) -> None:
        """Every line the 2.7.7 rule refuses was refused by the 2.7.6 rule, so a report admitted before is admitted
        again on re-admission (the verifier re-admits old records with this check)."""
        labels = ("participant", "Participants", "subject", "subj", "PID", "sid", "rapid", "Subject-", "participant_")
        tokens = ("", " 7", "07", " P07", "s", " 4A", "A", " to", " #3", "_12", "-x9", " 12345678901234567")
        times = ("23:23", "0:41", "9:05", "24:00", "20:80", "12:60", "T14:32", "14:32h", "1:23:45", "12:30:75",
                 "2019-03-02", "2024-1-5", "1999/12/31", "1:2", "123:45", "")
        seen = refused = 0
        for label, token, time in itertools.product(labels, tokens, times):
            for line in (f"{label}{token} at {time}", f"{time} {label}{token}", f"{label}{token}{time}"):
                old = bool(phase1_runtime.PARTICIPANT_ID.search(line) and phase1_runtime.DATE.search(line))
                new = phase1_runtime.participant_date_linked(line)
                seen += 1
                refused += new
                self.assertFalse(new and not old, line)
        self.assertGreater(refused, 100, "the generated lines exercise the refusal")
        self.assertGreater(seen, 4000)

    def test_long_lines_are_checked_in_linear_time(self) -> None:
        # Adjacent unbounded separator runs made a line of "participant" and 100k spaces take minutes.
        for line in ("participant" + " " * 100_000 + "x", "subjects" + "_-_ " * 25_000 + "12",
                     "participants " + " and ".join(["P07"] * 20_000) + " 23:23", "participant " * 9_000 + "23:23"):
            started = time.monotonic()
            phase1_runtime.participant_date_linked(line)
            phase1_runtime.PARTICIPANT_LABELLED_ID.search(line)
            self.assertLess(time.monotonic() - started, 2.0, line[:40])

    def test_the_documented_time_conditions(self) -> None:
        clock = phase1_runtime.DATE_OR_CLOCK_TIME
        for text in ("23:59", "0:00", "9:05", "06:58:10", "24:00", "bedtime:23:15", "2019-03-02", "2024-1-5", "1999/12/31",
                     "2019-03-02T08:30:00", "2019-03-02t23:10", "２３:１５"):
            with self.subTest(time=text):
                self.assertTrue(clock.search(f"at {text}."), "a date or clock time")
        for text in ("12:60", "20:80", "50:50", "70:30", "25:10", "123:45", "T14:32", "14:32h"):
            with self.subTest(not_a_time=text):
                self.assertIsNone(clock.search(f"at {text}."), "an hour of 0-24 and minutes of 00-59")

    def test_the_privacy_scan_patterns_and_counts_are_unchanged(self) -> None:
        self.assertEqual(phase1_runtime.PARTICIPANT_ID.pattern,
                         r"(?i)(?<![A-Za-z0-9])(?:participant|subject|subj|pid|sid)[-_ ]?[A-Za-z0-9]{1,16}(?![A-Za-z0-9])")
        self.assertEqual(phase1_runtime.DATE.pattern,
                         r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")
        with tempfile.TemporaryDirectory(prefix="i4r-277-scan-") as directory:
            path = pathlib.Path(directory) / "notes.txt"
            path.write_text("\n".join(FALSE_POSITIVES + LINKAGES) + "\n", encoding="utf-8")
            counts = phase1_runtime.privacy_scan([("notes.txt", path)])["detector_match_counts"]
        # Phase-1 evidence counts every line the 2.7.6 pattern pair matched, the false positives included.
        self.assertEqual(counts["participant_identifier_near_date_or_time"], len(FALSE_POSITIVES) + len(LINKAGES))


class RoundZeroGate(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-277-journal-")
        self.addCleanup(directory.cleanup)
        self.audit = pathlib.Path(directory.name).resolve() / "audit"
        self.audit.mkdir()
        result = run(["/bin/bash", SCRIPTS / "init_run.sh", self.audit, "--audit-id", "t-277"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def log(self, *arguments: str) -> None:
        result = run(["/bin/bash", SCRIPTS / "log_event.sh", *arguments], RUN_JSON=str(self.audit / "run.json"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_json(self) -> dict:
        return json.loads((self.audit / "run.json").read_text(encoding="utf-8"))

    def test_a_round_zero_consistency_check_is_recorded_and_makes_no_round(self) -> None:
        self.log("gate", "gate=check_consistency", "round=0", "exit_status=0")
        self.log("gate", "gate=check_consistency", "round=0", "exit_status=1")
        run_json = self.run_json()
        self.assertEqual([row.get("round") for row in run_json.get("rounds", [])], [])
        gates = [row for row in run_json["compliance"]["gates_run"] if row["gate"] == "check_consistency"]
        self.assertEqual([(row["round"], row["passed"]) for row in gates], [(0, True), (0, False)])

    def test_negative_control_a_round_one_consistency_check_still_marks_its_round(self) -> None:
        self.log("gate", "gate=check_consistency", "round=1", "exit_status=0")
        rounds = self.run_json()["rounds"]
        self.assertEqual([row["round"] for row in rounds], [1])
        self.assertIs(rounds[0]["consistency_check_run"], True)
        self.assertEqual(rounds[0]["consistency_check_exit"], 0)

    def test_project_rewrites_a_run_json_projected_by_2_7_6(self) -> None:
        """The upgrade remedy for an audit stopped at finalize: `lifecycle_journal.py project` re-projects run.json
        from the unchanged journal, and the round-0 row an earlier release projected is gone."""
        self.log("gate", "gate=check_consistency", "round=0", "exit_status=0")
        self.log("gate", "gate=check_consistency", "round=1", "exit_status=0")
        journal = (self.audit / lj.JOURNAL_NAME).read_bytes()
        stale = self.run_json()
        stale["rounds"].insert(0, dict(stale["rounds"][0], round=0))
        (self.audit / "run.json").write_text(json.dumps(stale), encoding="utf-8")
        result = run([sys.executable, SCRIPTS / "lifecycle_journal.py", "project", self.audit])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row["round"] for row in self.run_json()["rounds"]], [1])
        self.assertEqual((self.audit / lj.JOURNAL_NAME).read_bytes(), journal)

    def test_only_a_positive_integer_round_makes_a_row(self) -> None:
        events = lj.load_events(self.audit)
        # project_run reads only seq and event_hash besides type and payload (the watermark); no chain check here
        base = {"type": "gate", "at": events[-1]["at"], "source": "test", "seq": events[-1]["seq"] + 1,
                "event_hash": "0" * 64}
        for value in (0, -1, True, "1", None):
            with self.subTest(round=value):
                projected = lj.project_run(events + [dict(base, payload={
                    "gate": "check_consistency", "round": value, "exit_status": 0})])
                self.assertEqual(projected.get("rounds", []), [])


if __name__ == "__main__":
    unittest.main()
