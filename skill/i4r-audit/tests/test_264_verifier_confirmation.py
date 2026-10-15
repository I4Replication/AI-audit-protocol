"""2.6.4: a finding raised after fan-out is confirmed by the packaged verifier, end to end.

The Neudorfer incident (20260923-audit-3309cfcf). Three article-text findings (F13-F15) that the
operator raised at coordinator time, and that every critic confirmed, were excluded: a registry row
needs a family-A (Claude) confirmation, and before 2.6.4 only the Phase-1 auditor that raised a row
could give one. The hand-made verification records the operator tried were refused one gate after
another: make_settled rejected the `role_class other` record, and the one that ran after round-1
integration; finalize counted the record relabelled `role_class auditor` as a sixth auditor.

These tests build an on-disk audit (five Phase-1 auditors, a round-1 critic quorum, twelve
auditor-raised rows and an operator-raised row F13 that two gpt critics confirmed), record each
verifier call with tests/verifier_fixtures.record_verifier -- the one way a test makes the record
scripts/run_verifier.py would have made (real metadata-2.1 journal events, the frozen packet, the
promoted output tree and the runner's exact completion receipt), which every consumer re-admits
through run_verifier.readmit_record -- and drive the SHIPPED scripts: integrate_round.py plan and
apply, approve_settled.py, make_settled.sh, make_diff_packet.py, renumber_findings.py and
generate_finding_artifacts.py. Every positive path has negative controls that build their own
condition. An inadmissible verifier record stops integrate_round.py (naming the remedy) and makes
make_settled.sh reject exactly the rows that cite a verifier; a verifier record that does not count
always rejects its row, so a settled fact never omits a record its registry row carries.

The golden class runs the same no-verifier fixture, through round-1 and round-2 integration (with
literal rebinds and a claim edit), two make_settled runs, the round-2 and round-3 diff packets and
generate_finding_artifacts --apply, through these scripts and through the 2.6.3 scripts (set
I4R_V263_SKILL to the live 2.6.3 skill tree, which ships an inert verifier_contract.py) and requires
byte-identical outputs; it also shows the positive fixture cannot promote F13 under 2.6.3.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SKILL / "schemas/tools"))

import claude_routes  # noqa: E402
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402
import verifier_contract  # noqa: E402

# Import the MODULE, not the class, so the loader does not collect DiffPacketTests here again.
from tests import test_diff_packet  # noqa: E402
from tests import verifier_fixtures  # noqa: E402

digest = test_diff_packet.digest
ROW_FIXTURE = test_diff_packet.FIXTURE

_PRISTINE = os.environ.get("I4R_V263_SKILL", "").strip()
PRISTINE = pathlib.Path(_PRISTINE).resolve() if _PRISTINE else None
# Guard on content, not on a file's presence: the live 2.6.3 tree ships an inert
# scripts/verifier_contract.py that none of its scripts import, so the file alone proves nothing.
_GOLDEN_SCRIPTS = ("make_settled.sh", "integrate_round.py", "make_diff_packet.py")
HAVE_PRISTINE = bool(PRISTINE and all(
    (PRISTINE / "scripts" / name).is_file()
    and "verifier_contract" not in (PRISTINE / "scripts" / name).read_text(encoding="utf-8")
    for name in _GOLDEN_SCRIPTS))

AUDIT_ID = "audit-264-confirm"
PREFIX = "r1-audit-264-confirm"
AUDITORS = {
    "auditor_reproduction": "report/01_reproduction.md",
    "auditor_data_integrity": "report/02_data_integrity.md",
    "auditor_text_vs_data": "report/03_text_vs_data.md",
    "auditor_code_methods": "report/04_code_methods.md",
    "auditor_privacy_archive": "report/05_privacy_archive.md",
}
CRITICS = ("stats", "data", "fairness", "completeness")
F13_CLAIM = ("Table 2 of the article reports a mean bias of 3.1 bpm for the wrist sensor, but the "
             "Results text states 1.3 bpm for the same comparison.")
EDITED_CLAIM = ("Table 2 of the article reports a mean bias of 3.1 bpm for the wrist sensor, while "
                "the Results text states 1.3 bpm.")
OTHER_CLAIM = "The article's abstract reports n = 40 participants but Table 1 lists 38."

T_INIT = "2026-08-27T08:00:00Z"
T_AUDIT = ("2026-08-27T09:00:00Z", "2026-08-27T09:01:00Z")
T_ROUND_START = "2026-08-27T10:00:00Z"
T_CRITIC = ("2026-08-27T10:00:00Z", "2026-08-27T10:10:00Z")
T_QUORUM = "2026-08-27T10:20:00Z"
T_VERIFIER = ("2026-08-27T10:31:00Z", "2026-08-27T10:40:00Z")
T_INTEGRATE = "2026-08-27T11:00:00Z"
T_CONSISTENCY = "2026-08-27T11:05:00Z"
T_LATE = ("2026-08-27T11:20:00Z", "2026-08-27T11:30:00Z")
T_FIXED_SETTLED = "2026-08-27T12:00:00Z"


def write(path: pathlib.Path, content: str | bytes) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


@contextlib.contextmanager
def clock(at: str):
    """Journal events without an explicit time carry `at` (reservations take no time argument)."""
    with mock.patch.object(lifecycle_journal, "utc_now", lambda: at):
        yield


def usage() -> dict:
    return {"obtainable": True, "input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0,
            "reasoning_tokens": 5, "total_tokens": 125, "source": "api_response"}


def claude_route(root: pathlib.Path) -> dict:
    """The Phase-1 auditors' route as this audit's snapshot froze it (2.7.0: Sonnet 5.5 when frozen
    from the shipped policy, Sonnet 5 for an audit initialised before)."""
    return record_invocation.load_policy_snapshot(root)["models"]["phase_1_auditors"]["auditor_reproduction"]


def critic_route(root: pathlib.Path) -> dict:
    """The critics' route as this audit's snapshot froze it (2.7.0: gpt-6.1-sol when frozen from the
    shipped policy, gpt-5.6-terra for an audit initialised before)."""
    return record_invocation.load_policy_snapshot(root)["models"]["critics"]


def terminal_record(invocation_id: str, role: str, role_class: str, round_number: int, *,
                    family: str, output: str, output_sha256: str, times: tuple[str, str],
                    input_hash: str = "a" * 64, prompt_hash: str = "b" * 64,
                    route: dict | None = None) -> dict:
    """``route`` is the snapshot's route for the row's family: its Claude route for a claude-family row,
    its critic route for a gpt one (default: Sonnet 5, and the Terra critics)."""
    anthropic = family == "claude"
    requested = (route or {}).get("requested_model", "sonnet-5" if anthropic else "gpt-5.6-terra")
    served = claude_routes.model_id(requested) if anthropic else verifier_fixtures.served_critic_model(requested)
    return {
        "invocation_id": invocation_id, "role": role, "role_class": role_class,
        "round": round_number, "attempt": 1, "attempt_kind": "role_inference",
        "status": "completed", "provider": "anthropic" if anthropic else "openai",
        "provider_call": True, "provider_request_status": "accepted",
        "inference_start_status": "started", "cache_usage": "none",
        "session_id": f"session-{invocation_id}",
        "requested_model": requested,
        "observed_model": served, "served_model": served, "identity_assurance": "observed",
        "model_identity_evidence": None, "observed_model_ids": [served],
        "model_family": family, "model_source": "observed",
        # the route's own effort, as requested_model above (2.7.3: a shipped-policy audit's Claude rows are xhigh)
        "reasoning_effort": (route or {}).get("reasoning_effort", "max" if anthropic else "high"),
        "started_at": times[0], "ended_at": times[1], "elapsed_seconds": elapsed(times),
        "provider_duration_seconds": 60, "turn_count": 1,
        "provider_cost": {"amount": 0.1, "currency": "USD", "source": "api_response",
                          "unavailable_reason": None},
        "input_artifact_sha256": input_hash, "prompt_sha256": prompt_hash,
        "output_artifact": output, "output_sha256": output_sha256,
        "usage": usage(), "usage_by_model": [], "unattributed_usage": usage(),
        "source_invocation_id": None, "error": None, "error_class": None, "legacy_source": None,
    }


def elapsed(times: tuple[str, str]) -> int:
    start, end = (lifecycle_journal.parse_time(value) for value in times)
    return int((end - start).total_seconds())


def base_run(snapshot_sha256: str) -> dict:
    return {
        "schema_version": "2.1.0", "normalization": None, "audit_id": AUDIT_ID,
        "started_at": T_INIT, "ended_at": None, "status": "initialized", "pause_reason": None,
        "post_delta_gates_passed": False, "round_target": 2, "round_cap": 7,
        "rounds": [], "rounds_run": 0, "stopping_reason": None,
        "invocations": [], "events": [],
        "human_effort": {"person_minutes_by_phase": {}},
        "toolkit": {"pipeline_policy_snapshot_sha256": snapshot_sha256},
    }


REPORT = """# Independent audit

## Abstract

The audit re-checks the reported agreement statistics against the article and its deposit.

## 1. Introduction

A bounded fixture for the post-fan-out verifier.

## 2. Findings

{markers}

## 3. Conclusion

The fixture supports only the narrow implementation claim it tests.
"""


def report_text(order: list[str]) -> str:
    return REPORT.format(markers="\n".join(f"{fid} is discussed here. <!-- finding:{fid} -->"
                                            for fid in order))


def finding_row(number: int, root: pathlib.Path, operator_id: str = "F13") -> dict:
    row = copy.deepcopy(ROW_FIXTURE)
    fid = operator_id if number == 13 else f"F{number}"
    role = list(AUDITORS)[(number - 1) % len(AUDITORS)]
    row.update({
        "id": fid, "status": "draft", "coordinator_approved": False, "settled": None,
        "claim": f"{fid}: the deposited analysis reproduces value {number} only after correction.",
        "source_location": {"path": "paper/paper.txt", "locator": f"Results, paragraph {number}"},
        "claim_ref": f"Results, paragraph {number}", "report_section": None,
        "verification_snippet": None, "verifications": [],
        "changes_conclusion": False, "changes_conclusion_which": None, "disclosure_flag": False,
    })
    row["evidence"]["source_hashes"] = [
        {"path": "work/check_primary.txt", "sha256": digest(root / "work/check_primary.txt")}]
    row["dependencies"] = {"environment_fingerprint": "sha256:environment-a", "finding_ids": [],
                           "artifact_paths": ["work/check_primary.txt", "work/check_primary.R"]}
    row["provenance"] = {"origin_role": role, "independently_found_by": [role],
                         "agreed_after_seeing": [], "round_introduced": 0,
                         "source_records": [AUDITORS[role]]}
    if number == 13:
        row.update({"claim": F13_CLAIM, "domain": "text",
                    "source_location": {"path": "paper/paper.txt", "locator": "Table 2; Results"},
                    "claim_ref": "Table 2; Results"})
        row["evidence"]["source_hashes"] = [
            {"path": "paper/paper.txt", "sha256": digest(root / "paper/paper.txt")}]
        row["evidence"]["rerun_command"] = "grep -n 'bpm' paper/paper.txt"
        row["provenance"] = {"origin_role": "operator", "independently_found_by": ["operator"],
                             "agreed_after_seeing": [], "round_introduced": 0,
                             "source_records": ["work/registry_draft/COORDINATOR_NOTES.md#N1"]}
    if number == 12:
        row["dependencies"]["finding_ids"] = [operator_id]
    return row


def critic_items(short: str, operator_id: str = "F13") -> tuple[list[dict], list[dict], list[dict]]:
    def item(fid: str, verdict: str, correction: str | None = None) -> dict:
        return {"item_id": f"{short}-{fid}", "finding_id": fid, "claim": fid, "verdict": verdict,
                "priority": "NON_BLOCKING", "confidence": "CONFIRMED",
                "requested_correction": correction,
                "evidence": {"result": f"{short} checked {fid} against paper/paper.txt."}}
    if short == "stats":
        return [item(operator_id, "CONFIRMED"), item("F2", "OVERSTATED", "Say 'about 2' rather than '2'.")], [], []
    if short == "data":
        return ([item(operator_id, "CONFIRMED"), item("F5", "REFUTED", "The deposit gives 5.2, not 5.")],
                [], [{"conflict_id": "C1", "claim_ids": ["F2", "F5"],
                      "description": "F2 and F5 read the same table differently."}])
    if short == "fairness":
        return [item(f"F{n}", "CONFIRMED") for n in range(1, 13)], [], []
    return ([item("F1", "UNVERIFIABLE")],
            [{"item_id": "completeness-N1", "finding_id": None, "claim": "Table 3 omits a column.",
              "verdict": "CONFIRMED", "priority": "NON_BLOCKING", "confidence": "LIKELY",
              "requested_correction": "Name the omitted column."}], [])


def build_template(root: pathlib.Path, *, legacy_rows: bool = False, operator_id: str = "F13") -> None:
    """Everything up to the end of the round-1 critic quorum; no verifier, no integration.

    ``operator_id`` is the operator row's draft id: F13, or a pre-renumbering id such as OPR-N1
    (the Neudorfer draft ids), whose report marker then comes first.
    """
    for relative in ("report", "work", "paper", "critique/round_1", "pkg"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    write(root / "paper/paper.txt",
          "Results. The wrist sensor showed a mean bias of 1.3 bpm against the chest strap.\n"
          "Table 2. Mean bias (bpm): wrist 3.1; arm 0.8.\n")
    write(root / "pkg/README.txt", "deposit\n")
    write(root / "work/check_primary.txt", "expected=2 observed=2\n")
    write(root / "work/check_primary.R", "stopifnot(1 + 1 == 2)\n")
    order = [f"F{n}" for n in range(1, 13)]
    order = order + [operator_id] if operator_id == "F13" else [operator_id, *order]
    write(root / "report/REPORT_final.md", report_text(order))
    write(root / "work/report_before.md", (root / "report/REPORT_final.md").read_bytes())
    write(root / "report/ADJUDICATION.md", "# Adjudication\n\nCoordinator decisions by round.\n")
    for role, relative in AUDITORS.items():
        write(root / relative, f"# {role}\n\nComplete.\n")
    comment = json.loads((SKILL / "schemas/EXAMPLE_comment.json").read_text())
    comment["audit_id"] = AUDIT_ID
    comment["findings"] = []
    # No finding here, so no personal data is reported: since 2.6.8 pii_present true or disclosure
    # flags require a disclosure-flagged finding.
    comment["governance"].update({"pii_present": False, "pii_description": None})
    comment["disclosure"] = {"flags": [], "urgency": "none", "statement": None, "consistent_with": None}
    write(root / "comment.json", json.dumps(comment, indent=2) + "\n")
    record_invocation.initialize_policy_snapshot(root)
    with clock(T_INIT):
        lifecycle_journal.initialize(root, base_run(digest(root / "pipeline-policy.snapshot.json")))
    for index, (role, relative) in enumerate(AUDITORS.items()):
        with clock(T_AUDIT[0]):
            lifecycle_journal.record_terminal(root, terminal_record(
                f"inv-{role}", role, "auditor", 0, family="claude", output=relative,
                output_sha256=digest(root / relative), times=T_AUDIT, input_hash=f"{index + 1}" * 64,
                route=claude_route(root)))
    lifecycle_journal.append_event(root, "round_start", {"round": 1}, at=T_ROUND_START)
    for short in CRITICS:
        items, new_issues, conflicts = critic_items(short, operator_id)
        output = f"critique/round_1/critic_{short}.json"
        write(root / output, json.dumps({
            "audit_id": AUDIT_ID, "round": 1, "role": f"critic_{short}", "items": items,
            "new_issues": new_issues, "conflicts": conflicts, "settled_spot_checks": [],
            "overall": {"publishable": True, "blocking_count": 0,
                        "summary": f"critic_{short} completed its assigned checks."},
        }, sort_keys=True) + "\n")
        with clock(T_CRITIC[0]):
            lifecycle_journal.record_terminal(root, terminal_record(
                f"{PREFIX}-critic_{short}", f"critic_{short}", "critic", 1, family="gpt",
                output=output, output_sha256=digest(root / output), times=T_CRITIC, route=critic_route(root)))
    lifecycle_journal.append_event(root, "round_quorum_complete", {
        "round": 1, "critics_run": ",".join(CRITICS)}, at=T_QUORUM)
    write(root / "critique/round_1/CURRENT_INVOCATION", PREFIX + "\n")
    write(root / "critique/round_1/ROUND_STATUS.json", json.dumps({
        "audit_id": AUDIT_ID, "round": 1, "outcome": "complete", "exit_code": 0,
        "quorum_met": True, "expected_roles": list(CRITICS), "completed_roles": list(CRITICS),
        "failed_roles": []}) + "\n")
    if legacy_rows:
        # The Neudorfer out-of-protocol shapes: named like verifiers, but role_class other/auditor
        # and corrected to legacy_record. Nothing may key on their names.
        for invocation_id, role, role_class in (
                ("operator-verifier-legacy", "operator_verifier", "other"),
                ("auditor-operator-verification-legacy", "auditor_operator_verification", "auditor")):
            output = f"work/coordinator/{role}/verification_result.json"
            write(root / output, json.dumps({"verifications": [
                {"claim_id": "C1", "verdict": "CONFIRMED", "evidence": "quoted", "notes": None}]}) + "\n")
            with clock(T_VERIFIER[0]):
                lifecycle_journal.record_terminal(root, terminal_record(
                    invocation_id, role, role_class, 1, family="claude", output=output,
                    output_sha256=digest(root / output), times=T_VERIFIER, route=claude_route(root)))
            with clock(T_LATE[1]):
                lifecycle_journal.correct_invocation(
                    root, invocation_id, {"status": "legacy_record"},
                    reason="Out-of-protocol operator verification call; superseded")
    write_jsonl(root / "work/findings.jsonl", [finding_row(n, root, operator_id) for n in range(1, 14)])


def write_snippets(audit: pathlib.Path) -> None:
    """Each row's code snippet in the notebook's expect()/check() form under the row's CURRENT id (the
    projection refuses a snippet without both calls, or one naming another claim's id, e.g. after a renumber)."""
    for row in jsonl(audit / "work/findings.jsonl"):
        snippet = row.get("verification_snippet") or {}
        match = re.fullmatch(r"F([1-9][0-9]*)", str(row.get("id")))
        if snippet.get("kind") == "code" and snippet.get("path") and match:
            claim = f"C-{match.group(1)}"
            write(audit / snippet["path"], f'expect("{claim}", two = 2, tol = 0, note = "fixture")\n'
                                           f'check("{claim}", two = 1 + 1)\n')


def step_one(audit: pathlib.Path, rnd: int) -> None:
    """SKILL's integration step 1: preserve the round's pre-integration report and registry (once)."""
    for source, name in (("report/REPORT_final.md", "REPORT_BEFORE_INTEGRATION.md"),
                         ("work/findings.jsonl", "FINDINGS_BEFORE_INTEGRATION.jsonl")):
        target = audit / f"critique/round_{rnd}/{name}"
        if not target.exists():
            write(target, (audit / source).read_bytes())


def decisions(*, approve_f13: bool = True, extra: dict | None = None, operator_id: str = "F13") -> dict:
    out = {}
    for number in range(1, 14):
        if number == 13 and not approve_f13:
            continue
        out[operator_id if number == 13 else f"F{number}"] = {
            "status": "confirmed", "coordinator_approved": True, "report_section": "Section 2",
            "verification_snippet": {"kind": "code", "path": f"work/check_{operator_id if number == 13 else f'F{number}'}.R",
                                     "basis": "The bounded check reproduces the stated value."}}
    # the stats critic's OVERSTATED on F2 is answered in the report prose, not by rewording the claim: since 2.6.8 a
    # claim reworded at an integration settles only after a later round confirms the new wording
    out["F2"]["applied_note"] = "wording softened in Section 2"
    for key, value in (extra or {}).items():
        out.setdefault(key, {}).update(value)
    return out


class Fixture(unittest.TestCase):
    """A fresh copy of the template audit per test; scripts run from `skill` (default: this tree)."""

    legacy_rows = False
    operator_id = "F13"
    _templates: dict[tuple[bool, str], pathlib.Path] = {}
    _template_dir: tempfile.TemporaryDirectory | None = None

    @classmethod
    def template(cls) -> pathlib.Path:
        if Fixture._template_dir is None:
            Fixture._template_dir = tempfile.TemporaryDirectory(prefix="i4r-264-template.")
        key = (cls.legacy_rows, cls.operator_id)
        if key not in Fixture._templates:
            root = pathlib.Path(Fixture._template_dir.name) / f"audit-{len(Fixture._templates)}"
            build_template(root, legacy_rows=cls.legacy_rows, operator_id=cls.operator_id)
            Fixture._templates[key] = root
        return Fixture._templates[key]

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="i4r-264.")
        self.audit = self.clone(pathlib.Path(self.temp.name) / "audit")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def clone(self, target: pathlib.Path) -> pathlib.Path:
        shutil.copytree(self.template(), target, symlinks=True)
        return target

    # ------------------------------------------------------------------ verifier records
    FINDING_AT_RUN = {F13_CLAIM: "F13", EDITED_CLAIM: "F13", OTHER_CLAIM: "F99"}

    def verifier(self, claims: list[str], verdicts: list[str], *, invocation_id: str,
                 times: tuple[str, str] = T_VERIFIER, finding_ids: list[str] | None = None,
                 audit: pathlib.Path | None = None, **options) -> dict:
        """One verifier call exactly as scripts/run_verifier.py records it (tests/verifier_fixtures.py).

        ``options`` pass through: family="gpt", receipt=False, relabel=(role, role_class),
        legacy_correction=reason (with corrected_at), tamper=... . The result carries role,
        invocation_id, result, result_sha256 and family, which hand_bind uses.
        """
        return verifier_fixtures.record_verifier(
            audit or self.audit, claims, verdicts, invocation_id=invocation_id,
            finding_ids=finding_ids or [self.FINDING_AT_RUN.get(claim, "F13") for claim in claims],
            started_at=times[0], ended_at=times[1], **options)

    # ------------------------------------------------------------------ real scripts
    def run_script(self, *args: str, skill: pathlib.Path = SKILL, audit: pathlib.Path | None = None,
                   check: bool = True) -> subprocess.CompletedProcess:
        audit = audit or self.audit
        script = skill / "scripts" / args[0]
        command = (["bash", str(script)] if script.suffix == ".sh" else [sys.executable, str(script)])
        result = subprocess.run([*command, str(audit), *args[1:]], text=True, capture_output=True)
        if check:
            self.assertEqual(result.returncode, 0, f"{args}: {result.stdout}\n{result.stderr}")
        return result

    def plan(self, rnd: int = 1, **kw) -> subprocess.CompletedProcess:
        return self.run_script("integrate_round.py", "--round", str(rnd), "plan", **kw)

    def apply(self, decided: dict, *, rnd: int = 1, at: str = T_INTEGRATE, contested: dict | None = None,
              **kw) -> subprocess.CompletedProcess:
        audit = kw.get("audit") or self.audit
        path = write(audit / f"work/coordinator/decisions_round{rnd}.json", json.dumps(decided, indent=2))
        step_one(audit, rnd)
        args = ["integrate_round.py", "--round", str(rnd), "apply", "--decisions", str(path), "--at", at]
        if contested is None and rnd == 1:
            # the data critic's REFUTED on F5 asks for a correction the decisions do not make: contest it, as the
            # full-pipeline run does (integrate_round refuses an unanswered requested correction)
            contested = {"critic_data:data-F5": {"position": "The deposit rounds to 5.",
                                                 "evidence": "work/check_primary.txt"}}
        if contested:
            cpath = write(audit / f"work/coordinator/contested_round{rnd}.json", json.dumps(contested))
            args += ["--contested", str(cpath)]
        return self.run_script(*args, **kw)

    def log_integration(self, *, at: str = T_INTEGRATE, audit: pathlib.Path | None = None) -> None:
        audit = audit or self.audit
        lifecycle_journal.append_event(audit, "round_integrated", {"round": 1, "integrated_at": at}, at=at)
        lifecycle_journal.append_event(audit, "gate", {"gate": "check_consistency", "round": 1,
                                                       "exit_status": 0}, at=T_CONSISTENCY)

    def settle(self, **kw) -> subprocess.CompletedProcess:
        self.run_script("approve_settled.py", **{k: v for k, v in kw.items() if k != "check"})
        return self.run_script("make_settled.sh", **kw)

    def diff(self, **kw) -> subprocess.CompletedProcess:
        audit = kw.get("audit") or self.audit
        return self.run_script(
            "make_diff_packet.py", "--round", "2",
            "--before-report", str(audit / "critique/round_1/REPORT_BEFORE_INTEGRATION.md"),
            "--after-report", str(audit / "report/REPORT_final.md"),
            "--before-findings", str(audit / "critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"),
            "--after-findings", str(audit / "work/findings.jsonl"),
            "--output", str(audit / "critique/DIFF_MANIFEST_round_2.json"), **kw)

    # ------------------------------------------------------------------ views
    def registry(self, audit: pathlib.Path | None = None) -> dict[str, dict]:
        return {row["id"]: row for row in jsonl((audit or self.audit) / "work/findings.jsonl")}

    def plan_json(self, rnd: int = 1) -> dict:
        return json.loads((self.audit / f"critique/round_{rnd}/INTEGRATION_PLAN.json").read_text())

    def settled(self) -> dict:
        return json.loads((self.audit / "SETTLED.json").read_text())

    def reviewers(self, row: dict) -> list[str]:
        return sorted(v["reviewer_id"] for v in row["verifications"])

    def hand_bind(self, verifier: dict, *, fid: str = "F13", outcome: str = "confirmed",
                  approve: bool = True) -> None:
        """Attach a verifier record by editing the registry, as the Neudorfer decisions script did."""
        rows = jsonl(self.audit / "work/findings.jsonl")
        for row in rows:
            if row["id"] != fid:
                continue
            row["verifications"] = [v for v in row["verifications"] if v["reviewer_id"] != verifier["role"]]
            row["verifications"].append({
                "reviewer_id": verifier["role"], "invocation_id": verifier["invocation_id"],
                "evidence_record": {"path": verifier["result"], "sha256": verifier["result_sha256"]},
                "model_family": verifier["family"], "outcome": outcome})
            if approve:
                row.update(decisions()["F13"])
        write_jsonl(self.audit / "work/findings.jsonl", rows)

    def integrate_without_f13(self) -> None:
        self.plan()
        self.apply(decisions(approve_f13=False))
        self.log_integration()

    def open_round_two(self) -> None:
        """A complete round 2 with no critic rows, so only the coordinator's decisions apply."""
        round_two = self.audit / "critique/round_2"
        write(round_two / "CURRENT_INVOCATION", "r2-no-critic-rows\n")
        write(round_two / "ROUND_STATUS.json", json.dumps({
            "audit_id": AUDIT_ID, "round": 2, "outcome": "complete", "exit_code": 0,
            "quorum_met": True, "failed_roles": []}) + "\n")

    def log_round_integrated(self, rnd: int, at: str) -> None:
        lifecycle_journal.append_event(self.audit, "round_integrated", {"round": rnd, "integrated_at": at}, at=at)


class VerifierConfirmsARowRaisedAfterFanOut(Fixture):
    def test_positive_path_through_every_shipped_script(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1")
        planned = self.plan()
        self.assertIn('"verifier_verifications"', planned.stdout)
        plan = self.plan_json()
        f13 = next(row for row in plan["findings"] if row["id"] == "F13")
        expected = {"reviewer_id": verifier["role"], "invocation_id": "inv-verifier-1",
                    "evidence_record": {"path": verifier["result"], "sha256": verifier["result_sha256"]},
                    "model_family": "claude", "outcome": "confirmed"}
        self.assertEqual(f13["verifier_verifications"], [expected])
        self.assertEqual([v["reviewer_id"] for v in f13["candidate_verifications"]],
                         ["critic_data", "critic_stats"])
        self.assertEqual(set(plan["verifiers"]), {"inv-verifier-1"})
        self.assertEqual(plan["verifiers"]["inv-verifier-1"]["verdicts"],
                         {verifier_contract.claim_sha256(F13_CLAIM): "CONFIRMED"})
        self.assertTrue(all("verifier_verifications" not in row for row in plan["findings"]
                            if row["id"] != "F13"))

        self.apply(decisions())
        registry = self.registry()
        self.assertEqual(self.reviewers(registry["F13"]),
                         sorted(["critic_data", "critic_stats", verifier["role"]]))
        self.assertIn(expected, registry["F13"]["verifications"])
        self.assertEqual(self.reviewers(registry["F1"]),
                         ["auditor_reproduction", "critic_completeness", "critic_fairness"])
        notes = (self.audit / "critique/round_1/RESPONSE_notes.md").read_text()
        self.assertIn(f"- verifier `{verifier['role']}` (claude; invocation `inv-verifier-1`): "
                      "**CONFIRMED** on the current claim text", notes)
        adjudication = (self.audit / "report/ADJUDICATION.md").read_text()
        self.assertIn("Post-fan-out verifier verdicts are bound", adjudication)
        self.assertIn(f"- verifier `{verifier['role']}` (claude; invocation `inv-verifier-1`)", adjudication)

        self.log_integration()
        made = self.settle()
        self.assertIn("13 promoted", made.stdout)
        settled = self.settled()
        fact = next(f for f in settled["facts"] if f["id"] == "F13")
        self.assertEqual(sorted(c["reviewer_id"] for c in fact["confirmations"]),
                         sorted(["critic_data", "critic_stats", verifier["role"]]))
        self.assertEqual({c["model_family"] for c in fact["confirmations"]}, {"claude", "gpt"})
        self.assertIn(f"`{verifier['role']}` (claude; invocation `inv-verifier-1`",
                      (self.audit / "SETTLED.md").read_text())

        self.diff()
        packet = json.loads((self.audit / "critique/DIFF_MANIFEST_round_2.json").read_text())
        self.assertEqual([ref["fact_id"] for ref in packet["settled_fact_references"]], ["F13"])
        self.assertIn(verifier["role"], [c["reviewer_id"]
                                         for c in packet["settled_fact_references"][0]["confirmations"]])
        verified = subprocess.run([sys.executable, str(SCRIPTS / "verify_diff_packet.py"), str(self.audit),
                                   str(self.audit / "critique/DIFF_MANIFEST_round_2.json"), "--round", "2"],
                                  text=True, capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)

        write_snippets(self.audit)
        self.run_script("generate_finding_artifacts.py", "--apply")
        self.run_script("generate_finding_artifacts.py", "--check")
        comment = json.loads((self.audit / "comment.json").read_text())
        f13_comment = next(f for f in comment["findings"] if f["id"] == "F13")
        self.assertIn({"verifier": verifier["role"], "at": None, "method": "grep -n 'bpm' paper/paper.txt",
                       "outcome": "reproduced", "notes": None}, f13_comment["verification"]["verifiers"])
        self.assertEqual(f13_comment["provenance"]["independently_found_by"], ["operator"])

    def test_a_later_round_binds_the_verifier_and_drops_it_when_the_claim_is_edited(self) -> None:
        """Verifier records bind in every round, and a later hand edit of the claim drops them."""
        self.integrate_without_f13()
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-2",
                                 times=("2026-08-27T11:30:00Z", "2026-08-27T11:40:00Z"))
        self.open_round_two()
        round_two = self.audit / "critique/round_2"
        self.apply({"F13": decisions()["F13"]}, rnd=2, at="2026-08-27T12:00:00Z")
        self.assertIn(verifier["role"], self.reviewers(self.registry()["F13"]))
        self.assertEqual(self.registry()["F13"]["status"], "confirmed")
        # The call ended after round 1's integration: until round 2's integration is journaled, the
        # latest integration predates it and make_settled refuses it; afterwards it settles.
        self.run_script("approve_settled.py")
        early = self.run_script("make_settled.sh", check=False)
        self.assertNotEqual(early.returncode, 0)
        self.assertIn("verification invocation ended after round 1 integration, the latest integration: "
                      "inv-verifier-2", early.stderr)
        self.log_round_integrated(2, "2026-08-27T12:00:00Z")
        self.assertIn("13 promoted", self.settle().stdout)

        # the claim is edited through the round's decisions (a re-apply plans from the pre-integration backup,
        # so a hand edit of the registry is not how a claim changes)
        self.apply({"F13": {"claim": EDITED_CLAIM, "status": "draft", "coordinator_approved": False}}, rnd=2,
                   at="2026-08-27T12:30:00Z")
        self.assertNotIn(verifier["role"], self.reviewers(self.registry()["F13"]))
        self.assertIn("record dropped — the claim text is not the text it verified",
                      (round_two / "RESPONSE_notes.md").read_text())


class RenumberingAfterBindingKeepsTheBinding(Fixture):
    """Ids are renumbered after the verifier ran (Neudorfer: OPR-N1 -> F2 -> F13); claims are not."""

    operator_id = "OPR-N1"

    def test_the_operator_row_still_settles_under_its_new_id(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1",
                                 finding_ids=["OPR-N1"])
        self.plan()
        self.apply(decisions(operator_id="OPR-N1"))
        self.log_integration()
        self.assertIn(verifier["role"], self.reviewers(self.registry()["OPR-N1"]))
        # The report discusses the operator row first, so every id moves: OPR-N1 -> F1, Fn -> Fn+1.
        self.run_script("renumber_findings.py", "--apply")
        self.assertEqual(json.loads((self.audit / "work/id_map.json").read_text())["OPR-N1"], "F1")
        registry = self.registry()
        self.assertEqual(registry["F1"]["claim"], F13_CLAIM)
        self.assertIn(verifier["role"], self.reviewers(registry["F1"]))
        self.assertEqual(registry["F13"]["dependencies"]["finding_ids"], ["F1"])
        made = self.settle()
        self.assertIn("13 promoted", made.stdout)
        fact = next(f for f in self.settled()["facts"] if f["claim"] == F13_CLAIM)
        self.assertEqual(fact["id"], "F1")
        self.assertIn(verifier["role"], [c["reviewer_id"] for c in fact["confirmations"]])
        self.diff()
        packet = json.loads((self.audit / "critique/DIFF_MANIFEST_round_2.json").read_text())
        self.assertEqual([ref["fact_id"] for ref in packet["settled_fact_references"]], ["F1"])
        write_snippets(self.audit)
        self.run_script("generate_finding_artifacts.py", "--apply")
        self.run_script("generate_finding_artifacts.py", "--check")
        # Control: the same registry with the claim edited after renumbering no longer settles it.
        rows = jsonl(self.audit / "work/findings.jsonl")
        rows[[r["id"] for r in rows].index("F1")]["claim"] = EDITED_CLAIM
        write_jsonl(self.audit / "work/findings.jsonl", rows)
        self.settle()
        rejected = {item["id"]: item["reasons"] for item in self.settled()["rejected"]}
        self.assertIn("verifier: the cited verifier call returned no verdict for this row's current "
                      "claim text", rejected["F1"])


class VerifierNegativeControls(Fixture):
    def assert_f13_not_promoted(self, *reasons: str) -> dict:
        settled = self.settled()
        self.assertNotIn("F13", [fact["id"] for fact in settled["facts"]])
        rejected = {item["id"]: item["reasons"] for item in settled["rejected"]}
        for reason in reasons:
            self.assertIn(reason, rejected["F13"])
        return rejected

    def assert_integrate_does_not_bind(self) -> None:
        self.plan()
        plan = self.plan_json()
        self.assertNotIn("verifiers", plan)
        self.assertTrue(all("verifier_verifications" not in row for row in plan["findings"]))
        refused = self.apply(decisions(), check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("(F13).verifications: confirmations must span two known model families", refused.stderr)

    def assert_integrate_stops_on_inadmissible(self, planned: subprocess.CompletedProcess, message: str) -> None:
        """integrate_round.py stops on an inadmissible record and names the working remedy."""
        self.assertEqual(planned.returncode, 1, planned.stderr)
        self.assertIn(f"FATAL: verifier record is not admissible: {message}", planned.stderr)
        for remedy in ("re-run that batch's own command, python3 scripts/run_verifier.py <audit-dir> --findings",
                       "rebuilds the lost receipt from the retained response with no provider call",
                       "to its recorded hash",
                       "python3 scripts/record_invocation.py --correct <audit-dir> <invocation-id> "
                       "<correction.json>",
                       '{"set": {"status": "legacy_record"}, "reason": "..."}',
                       "never do this for a lost receipt"):
            self.assertIn(remedy, planned.stderr)

    def assert_only_the_citing_row_is_rejected(self, invocation_id: str, fragment: str) -> None:
        """make_settled settles every other fact and rejects F13 (which cites a verifier) with the
        reason; make_diff_packet accepts the cache, since no fact cites a verifier."""
        if not (self.audit / "run.json").read_text().count('"round_integrated"'):
            self.log_integration()
        self.run_script("approve_settled.py")
        made = self.run_script("make_settled.sh")
        self.assertIn("settled facts: 12 promoted, 1 rejected", made.stdout)
        settled = self.settled()
        self.assertEqual([fact["id"] for fact in settled["facts"]], sorted(f"F{n}" for n in range(1, 13)))
        reasons = {item["id"]: item["reasons"] for item in settled["rejected"]}["F13"]
        prefix = f"verifier: verifier record is not admissible: {invocation_id}: "
        notes = [reason for reason in reasons if reason.startswith(prefix)]
        self.assertEqual(len(notes), 1, reasons)
        self.assertIn(fragment, notes[0])
        self.diff()

    def test_a_verifier_run_on_a_different_claim(self) -> None:
        verifier = self.verifier([OTHER_CLAIM], ["CONFIRMED"], invocation_id="inv-other")
        self.assert_integrate_does_not_bind()
        self.integrate_without_f13()
        self.hand_bind(verifier)
        self.settle()
        self.assert_f13_not_promoted(
            "verifier: the cited verifier call returned no verdict for this row's current claim text",
            "confirmations do not contain two distinct model families")

    def test_a_claim_edited_after_verification(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1")
        # (1) The decision that binds the record also edits the claim: the record is dropped.
        self.plan()
        self.assertIn("verifier_verifications", next(r for r in self.plan_json()["findings"] if r["id"] == "F13"))
        self.apply(decisions(approve_f13=False, extra={"F13": {"claim": EDITED_CLAIM}}))
        self.assertNotIn(verifier["role"], self.reviewers(self.registry()["F13"]))
        notes = (self.audit / "critique/round_1/RESPONSE_notes.md").read_text()
        self.assertIn(f"- verifier `{verifier['role']}` (invocation `inv-verifier-1`): record dropped — "
                      "the claim text is not the text it verified.", notes)
        # (2) The claim is edited by hand after the record was bound: make_settled does not count it.
        self.log_integration()
        self.hand_bind(verifier)
        rows = jsonl(self.audit / "work/findings.jsonl")
        self.assertEqual(next(r for r in rows if r["id"] == "F13")["claim"], EDITED_CLAIM)
        self.settle()
        self.assert_f13_not_promoted(
            "verifier: the cited verifier call returned no verdict for this row's current claim text")

    def test_a_refuted_verdict_and_a_second_call_that_refutes_the_same_claim(self) -> None:
        with self.subTest("REFUTED"):
            refuted = self.verifier([F13_CLAIM], ["REFUTED"], invocation_id="inv-refuted")
            self.plan()
            f13 = next(r for r in self.plan_json()["findings"] if r["id"] == "F13")
            self.assertEqual([v["outcome"] for v in f13["verifier_verifications"]], ["refuted"])
            refused = self.apply(decisions(), check=False)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("confirmations must span two known model families", refused.stderr)
            self.apply(decisions(approve_f13=False))
            bound = [v for v in self.registry()["F13"]["verifications"] if v["reviewer_id"] == refuted["role"]]
            self.assertEqual([v["outcome"] for v in bound], ["refuted"])
            self.assertIn("**REFUTED** on the current claim text",
                          (self.audit / "critique/round_1/RESPONSE_notes.md").read_text())
        with self.subTest("UNVERIFIABLE on an unverifiable-impact row is not a confirmation"):
            self.tearDown()
            self.setUp()
            rows = jsonl(self.audit / "work/findings.jsonl")
            for row in rows:
                if row["id"] == "F13":
                    row.update({"impact": "unverifiable", "impact_rank": 1, "unverifiable_basis": {
                        "reason": "data_missing", "blocked_tier": 2,
                        "attempted_check": "searched the deposit", "basis": "no file carries Table 2"}})
            write_jsonl(self.audit / "work/findings.jsonl", rows)
            self.verifier([F13_CLAIM], ["UNVERIFIABLE"], invocation_id="inv-unverifiable")
            self.plan()
            f13 = next(r for r in self.plan_json()["findings"] if r["id"] == "F13")
            self.assertEqual([v["outcome"] for v in f13["verifier_verifications"]], ["inconclusive"])
            refused = self.apply(decisions(), check=False)
            self.assertIn("confirmations must span two known model families", refused.stderr)
        with self.subTest("CONFIRMED, then another call REFUTED the same claim text"):
            self.tearDown()
            self.setUp()
            confirmed = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-confirmed")
            second = self.verifier([F13_CLAIM, OTHER_CLAIM], ["REFUTED", "CONFIRMED"],
                                   invocation_id="inv-second", times=("2026-08-27T10:41:00Z",
                                                                      "2026-08-27T10:50:00Z"))
            self.assertNotEqual(confirmed["role"], second["role"])
            self.plan()
            self.apply(decisions())
            outcomes = {v["reviewer_id"]: v["outcome"] for v in self.registry()["F13"]["verifications"]}
            self.assertEqual((outcomes[confirmed["role"]], outcomes[second["role"]]), ("confirmed", "refuted"))
            self.log_integration()
            self.settle()
            self.assert_f13_not_promoted(
                "verifier: another verifier call returned REFUTED for the same claim text",
                "confirmations do not contain two distinct model families")

    def test_a_refuted_verdict_corrected_to_legacy_record_still_blocks_its_claim(self) -> None:
        """confirm-2: the documented correction must not reopen verdict shopping.

        An admissible REFUTED is corrected to legacy_record, and a second batch returns CONFIRMED
        for the same claim text. integrate_round never binds the superseded call (before the fix it
        bound its REFUTED as a verification record citing a legacy_record call), notes that it still
        blocks; make_settled rejects the row on the superseded REFUTED; make_diff_packet accepts the
        cache that leaves F13 out."""
        refuted = self.verifier([F13_CLAIM], ["REFUTED"], invocation_id="inv-refuted",
                                legacy_correction="superseded by a later batch",
                                corrected_at="2026-08-27T10:41:00Z")
        confirmed = self.verifier([F13_CLAIM, OTHER_CLAIM], ["CONFIRMED", "CONFIRMED"],
                                  invocation_id="inv-confirmed",
                                  times=("2026-08-27T10:42:00Z", "2026-08-27T10:50:00Z"))
        self.assertNotEqual(refuted["role"], confirmed["role"])
        self.assertEqual({row["invocation_id"]: row["status"] for row in jsonl(self.audit / "invocations.jsonl")
                          if row.get("role_class") == "verifier"},
                         {"inv-refuted": "legacy_record", "inv-confirmed": "completed"})
        self.plan()
        plan = self.plan_json()
        f13 = next(r for r in plan["findings"] if r["id"] == "F13")
        self.assertEqual([(v["invocation_id"], v["outcome"]) for v in f13["verifier_verifications"]],
                         [("inv-confirmed", "confirmed")])
        self.assertEqual(set(plan["verifiers"]), {"inv-confirmed"})
        self.apply(decisions())
        registry = self.registry()
        self.assertEqual(self.reviewers(registry["F13"]), sorted(["critic_data", "critic_stats", confirmed["role"]]))
        self.assertNotIn("inv-refuted", [v["invocation_id"] for row in registry.values() for v in row["verifications"]])
        notes = (self.audit / "critique/round_1/RESPONSE_notes.md").read_text()
        self.assertIn("- verifier invocation `inv-refuted` (corrected to legacy_record): returned **REFUTED** for "
                      "the current claim text — not bound, but it still blocks a verifier confirmation of this "
                      "text.", notes)
        self.log_integration()
        made = self.settle()
        self.assertIn("settled facts: 12 promoted, 1 rejected", made.stdout)
        self.assert_f13_not_promoted("verifier: another verifier call returned REFUTED for the same claim text",
                                     "confirmations do not contain two distinct model families")
        self.diff()
        packet = json.loads((self.audit / "critique/DIFF_MANIFEST_round_2.json").read_text())
        self.assertNotIn("F13", [ref["fact_id"] for ref in packet["settled_fact_references"]])
        with self.subTest("control: a superseded CONFIRMED neither blocks nor is bound"):
            self.tearDown()
            self.setUp()
            superseded = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-superseded",
                                       legacy_correction="superseded by a later batch",
                                       corrected_at="2026-08-27T10:41:00Z")
            confirmed = self.verifier([F13_CLAIM, OTHER_CLAIM], ["CONFIRMED", "CONFIRMED"],
                                      invocation_id="inv-confirmed",
                                      times=("2026-08-27T10:42:00Z", "2026-08-27T10:50:00Z"))
            self.plan()
            self.assertEqual(set(self.plan_json()["verifiers"]), {"inv-confirmed"})
            self.apply(decisions())
            self.log_integration()
            self.assertIn("13 promoted", self.settle().stdout)
            fact = next(f for f in self.settled()["facts"] if f["id"] == "F13")
            self.assertEqual(sorted(c["reviewer_id"] for c in fact["confirmations"]),
                             sorted(["critic_data", "critic_stats", confirmed["role"]]))
            self.assertNotIn(superseded["role"], [c["reviewer_id"] for c in fact["confirmations"]])
            self.diff()

    def test_a_bound_record_whose_call_is_later_superseded_is_dropped_with_its_reason(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1")
        self.plan()
        self.apply(decisions())
        self.log_integration()
        self.assertIn("13 promoted", self.settle().stdout)
        lifecycle_journal.correct_invocation(self.audit, "inv-verifier-1", {"status": "legacy_record"},
                                             reason="superseded", at="2026-08-27T11:10:00Z")
        # The 2.6.3 rule for any reviewer record: a record citing a non-completed call stops make_settled.
        stopped = self.run_script("make_settled.sh", check=False)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("verification invocation is not a completed reviewer provider call: inv-verifier-1",
                      stopped.stderr)
        # The next integration drops it and says why (before the fix: "the claim text is not the
        # text it verified", which was false); the row then fails on families, not on the record.
        self.open_round_two()
        self.apply({"F13": {"status": "draft", "coordinator_approved": False}}, rnd=2, at="2026-08-27T12:00:00Z")
        self.assertNotIn(verifier["role"], self.reviewers(self.registry()["F13"]))
        self.assertIn(f"- verifier `{verifier['role']}` (invocation `inv-verifier-1`): record dropped — its call "
                      "is not a completed verifier call (it was corrected to legacy_record), so it confirms "
                      "nothing.", (self.audit / "critique/round_2/RESPONSE_notes.md").read_text())
        self.log_round_integrated(2, "2026-08-27T12:00:00Z")
        self.assertIn("12 promoted, 1 rejected", self.settle().stdout)
        self.assert_f13_not_promoted("confirmations do not contain two distinct model families")

    def test_a_verifier_of_the_critic_family(self) -> None:
        self.integrate_without_f13()
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-gpt-verifier", family="gpt")
        planned = self.plan(check=False)
        self.assert_integrate_stops_on_inadmissible(planned, "inv-gpt-verifier:")
        self.assertIn("is not the Phase-1 auditor family", planned.stderr)
        # Labelled claude in the registry, so validate_v2 sees two families: make_settled must still
        # join the record to its call and refuse to count it -- rejecting the row that cites it.
        self.hand_bind({**verifier, "family": "claude"})
        self.assert_only_the_citing_row_is_rejected("inv-gpt-verifier", "is not the Phase-1 auditor family")
        # Remedy (3), run exactly as the FATAL prints it: a foreign record is marked superseded.
        correction = write(self.audit / "work/coordinator/correction.json", json.dumps({
            "set": {"status": "legacy_record"}, "reason": "a critic-family call, not the runner's"}))
        corrected = subprocess.run([sys.executable, str(SCRIPTS / "record_invocation.py"), "--correct",
                                    str(self.audit), "inv-gpt-verifier", str(correction)],
                                   text=True, capture_output=True)
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        self.plan()
        # The next integration drops the record that cites the superseded call, and says why; the
        # row, left with one family, goes back to draft.
        self.open_round_two()
        self.apply({"F13": {"status": "draft", "coordinator_approved": False}}, rnd=2, at="2026-08-27T12:00:00Z")
        self.assertNotIn(verifier["role"], self.reviewers(self.registry()["F13"]))
        self.assertIn("record dropped — its call is not a completed verifier call",
                      (self.audit / "critique/round_2/RESPONSE_notes.md").read_text())
        self.log_round_integrated(2, "2026-08-27T12:00:00Z")
        self.assertIn("12 promoted, 1 rejected", self.settle().stdout)
        self.assert_f13_not_promoted("status is not confirmed",
                                     "confirmations do not contain two distinct model families")

    def _settle_raises(self, message: str) -> None:
        if not (self.audit / "run.json").read_text().count('"round_integrated"'):
            self.log_integration()
        made = self.run_script("make_settled.sh", check=False)
        self.assertNotEqual(made.returncode, 0)
        self.assertIn(message, made.stderr)

    def test_a_verifier_that_ended_after_the_latest_integration(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-late", times=T_LATE)
        # An integration timestamped before the call ended does not bind it (plan uses now).
        refused = self.apply(decisions(), check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("(F13).verifications: confirmations must span two known model families", refused.stderr)
        plan = self.plan_json()
        self.assertNotIn("verifiers", plan)
        self.assertTrue(all("verifier_verifications" not in row for row in plan["findings"]))
        self.apply(decisions(approve_f13=False))
        self.assertNotIn(verifier["role"], self.reviewers(self.registry()["F13"]))
        self.log_integration()
        # Attached by hand anyway (the Neudorfer r1b record ended after round-1 integration).
        self.hand_bind(verifier)
        self.run_script("approve_settled.py")
        self._settle_raises("verification invocation ended after round 1 integration, the latest "
                            "integration: inv-late")
        # Control: the same call is bound by, and settles after, an integration later than its end.
        self.tearDown()
        self.setUp()
        self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-late", times=T_LATE)
        self.apply(decisions(), at="2026-08-27T11:45:00Z")
        self.log_integration(at="2026-08-27T11:45:00Z")
        self.assertIn("13 promoted", self.settle().stdout)

    def test_a_row_raised_by_a_phase_one_auditor(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1")
        for provenance in ({"origin_role": "auditor_text_vs_data",
                            "independently_found_by": ["auditor_text_vs_data"]},
                           {"origin_role": "operator",
                            "independently_found_by": ["operator", "auditor_text_vs_data"]}):
            with self.subTest(provenance=provenance):
                rows = jsonl(self.audit / "work/findings.jsonl")
                for row in rows:
                    if row["id"] == "F13":
                        row["provenance"].update(provenance)
                write_jsonl(self.audit / "work/findings.jsonl", rows)
                self.plan()
                f13 = next(r for r in self.plan_json()["findings"] if r["id"] == "F13")
                self.assertNotIn("verifier_verifications", f13)
                self.assertNotIn("verifiers", self.plan_json())
                self.assertIn("auditor_text_vs_data", [v["reviewer_id"] for v in f13["candidate_verifications"]])
        self.apply(decisions())
        self.log_integration()
        self.hand_bind(verifier)
        # confirm-1: the auditor and the critics alone would promote the row, but the verifier
        # record it carries does not count. Promoting it anyway wrote a fact whose confirmations
        # omitted a record the registry row holds, and make_diff_packet then refused the whole
        # cache ("confirmations do not match the canonical registry"). The row is rejected instead.
        made = self.settle()
        self.assertIn("settled facts: 12 promoted, 1 rejected", made.stdout)
        self.assert_f13_not_promoted(
            "verifier: the row was raised by a Phase-1 auditor, so the verifier cannot confirm it")
        self.diff()
        # The next integration drops the hand-bound record, with its reason; the row then settles on
        # its own auditor and the critics.
        self.open_round_two()
        self.apply({}, rnd=2, at="2026-08-27T12:00:00Z")
        self.assertNotIn(verifier["role"], self.reviewers(self.registry()["F13"]))
        self.assertIn(f"- verifier `{verifier['role']}` (invocation `inv-verifier-1`): record dropped — a Phase-1 "
                      "auditor raised or independently found this row, so its auditor confirms it.",
                      (self.audit / "critique/round_2/RESPONSE_notes.md").read_text())
        self.log_round_integrated(2, "2026-08-27T12:00:00Z")
        self.assertIn("13 promoted", self.settle().stdout)
        fact = next(f for f in self.settled()["facts"] if f["id"] == "F13")
        self.assertEqual(sorted(c["reviewer_id"] for c in fact["confirmations"]),
                         ["auditor_text_vs_data", "critic_data", "critic_stats"])

    def test_legacy_role_class_other_and_auditor_shapes(self) -> None:
        for role, role_class, status in (("operator_verifier", "other", "completed"),
                                         ("auditor_operator_verification", "auditor", "legacy_record")):
            with self.subTest(role_class=role_class):
                self.tearDown()
                self.setUp()
                # The identical call, output, receipt and CONFIRMED verdict the runner would make,
                # journaled under the Neudorfer role and class: nothing may admit it by its name or
                # its files.
                shaped = self.verifier(
                    [F13_CLAIM], ["CONFIRMED"], invocation_id=f"inv-{role}", relabel=(role, role_class),
                    **({} if status == "completed" else {
                        "legacy_correction": "Out-of-protocol operator verification call; superseded",
                        "corrected_at": T_LATE[1]}))
                self.assertEqual(next(row["status"] for row in jsonl(self.audit / "invocations.jsonl")
                                      if row["invocation_id"] == f"inv-{role}"), status)
                self.assert_integrate_does_not_bind()
                self.integrate_without_f13()
                self.hand_bind(shaped)
                self.run_script("approve_settled.py")
                self._settle_raises(f"verification invocation is not a completed reviewer provider call: "
                                    f"inv-{role}")

    def test_a_hand_made_record_without_the_runner_receipt(self) -> None:
        self.integrate_without_f13()
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-hand-made", receipt=False)
        with self.subTest("the runner's result, but no receipt"):
            self.assert_integrate_stops_on_inadmissible(
                self.plan(check=False), "inv-hand-made: verifier completion receipt is not a regular file")
        # The make_record.py shape: stdout parsed into a bare verdict list, no hashes, no manifest.
        output = self.audit / verifier["result"]
        output.write_text(json.dumps({"verifications": [{"claim_id": "C1", "verdict": "CONFIRMED"}]}) + "\n")
        lifecycle_journal.correct_invocation(self.audit, "inv-hand-made", {"output_sha256": digest(output)},
                                             reason="bind the hand-made output", at=T_VERIFIER[1])
        self.assert_integrate_stops_on_inadmissible(
            self.plan(check=False), "inv-hand-made: verifier completion receipt is not a regular file")
        self.hand_bind({**verifier, "result_sha256": digest(output)})
        self.assert_only_the_citing_row_is_rejected("inv-hand-made", "verifier completion receipt is not a regular file")
        with self.subTest("the forge.py record: a hand receipt, a '{}' manifest, a report reading 'hand made'"):
            self.tearDown()
            self.setUp()
            # A sound runner record on another claim shares the ledger (and the quorum snapshot).
            self.verifier([OTHER_CLAIM], ["CONFIRMED"], invocation_id="inv-other")
            self.integrate_without_f13()
            forged = verifier_fixtures.forge_hand_record(self.audit, F13_CLAIM, started_at=T_VERIFIER[0],
                                                         ended_at=T_VERIFIER[1])
            self.assert_integrate_stops_on_inadmissible(self.plan(check=False), "hand-verifier-1: ")
            self.hand_bind({**forged, "family": "claude"})
            self.assert_only_the_citing_row_is_rejected("hand-verifier-1", "is not the runner's admitted output")

    def test_tampered_result_bytes(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1")
        self.plan()
        self.apply(decisions())
        self.log_integration()
        result = self.audit / verifier["result"]
        result.write_text(result.read_text().replace('"CONFIRMED"', '"CONFIRMED" '))
        self.assert_integrate_stops_on_inadmissible(
            self.plan(check=False), "inv-verifier-1: verifier result changed after it was recorded")
        self.assert_only_the_citing_row_is_rejected("inv-verifier-1", "verifier result changed after it was recorded")
        # Restored, the same record settles F13 again; make_diff_packet then refuses a cache that
        # cites it once another of its files changes.
        result.write_text(result.read_text().replace('"CONFIRMED" ', '"CONFIRMED"'))
        self.assertEqual(digest(result), verifier["result_sha256"])
        self.assertIn("13 promoted", self.run_script("make_settled.sh").stdout)
        self.diff()
        report = self.audit / pathlib.Path(verifier["result"]).parent / "REPORT.md"
        report.write_text(report.read_text() + "\nedited after promotion\n")
        refused = self.diff(check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("verifier confirmation is not admissible: verifier record is not admissible: "
                      "inv-verifier-1: verifier report changed after promotion", refused.stderr)

    def test_one_inadmissible_record_on_another_claim_rejects_only_the_rows_citing_a_verifier(self) -> None:
        """confirm-3: an unrelated verifier batch lost its receipt after F13 was settled. Before the
        fix make_settled aborted (no SETTLED.json at all, the twelve auditor-raised facts lost too);
        now it rejects only F13, whose verifier verdict set is no longer known."""
        self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1")
        self.plan()
        self.apply(decisions())
        self.log_integration()
        self.assertIn("13 promoted", self.settle().stdout)
        before = {fact["id"]: fact for fact in self.settled()["facts"]}
        self.verifier([OTHER_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-2",
                      times=("2026-08-27T10:41:00Z", "2026-08-27T10:45:00Z"), receipt=False)
        made = self.run_script("make_settled.sh", check=False)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertIn("settled facts: 12 promoted, 1 rejected", made.stdout)
        after = {fact["id"]: fact for fact in self.settled()["facts"]}
        self.assertEqual(after, {fid: fact for fid, fact in before.items() if fid != "F13"})
        reasons = {item["id"]: item["reasons"] for item in self.settled()["rejected"]}["F13"]
        self.assertEqual(reasons[0].split(" phase1/")[0],
                         "verifier: verifier record is not admissible: inv-verifier-2: verifier completion "
                         "receipt is not a regular file:")
        self.assertIn("confirmations do not contain two distinct model families", reasons)
        self.diff()
        # integrate_round.py still stops on the same state, and names the working remedy.
        self.assert_integrate_stops_on_inadmissible(
            self.plan(check=False), "inv-verifier-2: verifier completion receipt is not a regular file")


class NoVerifierIsInert(Fixture):
    """Always-on: without a verifier row nothing new is written anywhere."""

    legacy_rows = True

    def test_no_new_keys_lines_or_reasons_without_a_verifier_row(self) -> None:
        self.plan()
        plan = self.plan_json()
        self.assertNotIn("verifiers", plan)
        self.assertFalse(any("verifier_verifications" in row for row in plan["findings"]))
        self.apply(decisions(approve_f13=False))
        self.log_integration()
        self.settle()
        self.diff()
        for relative in ("critique/round_1/RESPONSE_notes.md", "report/ADJUDICATION.md", "SETTLED.md",
                         "SETTLED.json", "critique/round_1/INTEGRATION_PLAN.json"):
            self.assertNotRegex((self.audit / relative).read_text(), r"(?i)post-fan-out|verifier[:` ]")
        rejected = {item["id"]: item["reasons"] for item in self.settled()["rejected"]}
        self.assertEqual(rejected["F13"], ["status is not confirmed", "coordinator approval missing",
                                           "settled promotion approval missing",
                                           "valid_for_report_hash does not match the current canonical report",
                                           "confirmations do not contain two distinct model families"])
        self.assertFalse((self.audit / "phase1").exists())


R2_PREFIX = "r2-audit-264-confirm"
T_R2_START = "2026-08-27T13:00:00Z"
T_R2_CRITIC = ("2026-08-27T13:01:00Z", "2026-08-27T13:05:00Z")
T_R2_QUORUM = "2026-08-27T13:10:00Z"
T_R2_INTEGRATE = "2026-08-27T14:00:00Z"


@unittest.skipUnless(HAVE_PRISTINE, "set I4R_V263_SKILL to a pristine 2.6.3 skill tree")
class NoVerifierIsByteIdenticalTo263(Fixture):
    """The no-verifier invariance (spec item 8) against the live 2.6.3 scripts, file by file.

    Both copies of the audit go through round-1 plan/apply (a contested verdict), approve_settled
    and make_settled, the round-2 diff packet, a round-2 critic quorum, round-2 plan/apply with
    rebind_literal_confirmations and a claim edit, a second make_settled, the round-3 diff packet and
    generate_finding_artifacts --apply (comment.json). Every stdout, stderr and file must match.
    Journal events are written by this test process at fixed times, so run.json and the ledger are
    compared too."""

    legacy_rows = True

    def round_two_critics(self, audit: pathlib.Path) -> None:
        round_dir = audit / "critique/round_2"
        lifecycle_journal.append_event(audit, "round_start", {"round": 2}, at=T_R2_START)
        for short in CRITICS:
            f13_verdict = "OVERSTATED" if short == "data" else "CONFIRMED"
            items = [{"item_id": f"{short}-F2", "finding_id": "F2", "claim": "F2", "verdict": "CONFIRMED",
                      "priority": "NON_BLOCKING", "confidence": "CONFIRMED", "requested_correction": None,
                      "evidence": {"result": f"{short} re-checked F2 as corrected."}},
                     {"item_id": f"{short}-F13", "finding_id": "F13", "claim": "F13", "verdict": f13_verdict,
                      "priority": "NON_BLOCKING", "confidence": "CONFIRMED",
                      "requested_correction": "Soften the comparison." if f13_verdict == "OVERSTATED" else None,
                      "evidence": {"result": f"{short} checked F13 against paper/paper.txt."}}]
            output = f"critique/round_2/critic_{short}.json"
            write(audit / output, json.dumps({
                "audit_id": AUDIT_ID, "round": 2, "role": f"critic_{short}", "items": items,
                "new_issues": [], "conflicts": [], "settled_spot_checks": [],
                "overall": {"publishable": True, "blocking_count": 0,
                            "summary": f"critic_{short} completed its round-2 checks."},
            }, sort_keys=True) + "\n")
            with clock(T_R2_CRITIC[1]):
                lifecycle_journal.record_terminal(audit, terminal_record(
                    f"{R2_PREFIX}-critic_{short}", f"critic_{short}", "critic", 2, family="gpt",
                    output=output, output_sha256=digest(audit / output), times=T_R2_CRITIC,
                    route=critic_route(audit)))
        lifecycle_journal.append_event(audit, "round_quorum_complete", {
            "round": 2, "critics_run": ",".join(CRITICS)}, at=T_R2_QUORUM)
        write(round_dir / "CURRENT_INVOCATION", R2_PREFIX + "\n")
        write(round_dir / "ROUND_STATUS.json", json.dumps({
            "audit_id": AUDIT_ID, "round": 2, "outcome": "complete", "exit_code": 0,
            "quorum_met": True, "expected_roles": list(CRITICS), "completed_roles": list(CRITICS),
            "failed_roles": []}) + "\n")

    def pipeline(self, audit: pathlib.Path, skill: pathlib.Path) -> dict[str, object]:
        def run(*args: str) -> tuple[int, str, str]:
            done = self.run_script(*args, audit=audit, skill=skill)
            return (done.returncode, done.stdout.replace(str(audit), "<AUDIT>"),
                    done.stderr.replace(str(audit), "<AUDIT>"))

        def fix_generated_at() -> None:
            settled = audit / "SETTLED.json"
            settled.write_text(re.sub(r'"generated_at": "[^"]+"', f'"generated_at": "{T_FIXED_SETTLED}"',
                                      settled.read_text()))

        out: dict[str, object] = {}
        out["plan1"] = run("integrate_round.py", "--round", "1", "plan")
        decided = write(audit / "work/coordinator/decisions_round1.json",
                        json.dumps(decisions(approve_f13=False), indent=2))
        contested = write(audit / "work/coordinator/contested_round1.json", json.dumps({
            "critic_data:data-F5": {"position": "The deposit rounds to 5.", "evidence": "work/check_primary.txt"}}))
        step_one(audit, 1)
        out["apply1"] = run("integrate_round.py", "--round", "1", "apply", "--decisions", str(decided),
                            "--at", T_INTEGRATE, "--contested", str(contested))
        self.log_integration(audit=audit)
        out["approve1"] = run("approve_settled.py")
        out["settle1"] = run("make_settled.sh")
        fix_generated_at()
        out["diff2"] = run("make_diff_packet.py", "--round", "2",
                           "--before-report", str(audit / "critique/round_1/REPORT_BEFORE_INTEGRATION.md"),
                           "--after-report", str(audit / "report/REPORT_final.md"),
                           "--before-findings", str(audit / "critique/round_1/FINDINGS_BEFORE_INTEGRATION.jsonl"),
                           "--after-findings", str(audit / "work/findings.jsonl"),
                           "--output", str(audit / "critique/DIFF_MANIFEST_round_2.json"))
        write(audit / "work/SETTLED.round1.json", (audit / "SETTLED.json").read_bytes())
        write(audit / "work/report_before_round3.md", (audit / "report/REPORT_final.md").read_bytes())
        self.round_two_critics(audit)
        out["plan2"] = run("integrate_round.py", "--round", "2", "plan")
        decided = write(audit / "work/coordinator/decisions_round2.json", json.dumps({
            "F2": {"rebind_literal_confirmations": True, "applied_note": "round-2 wording kept"},
            "F13": {"claim": "F13: Table 2 and the Results text disagree on the wrist bias.",
                    "status": "draft", "rebind_literal_confirmations": True}}, indent=2))
        out["apply2"] = run("integrate_round.py", "--round", "2", "apply", "--decisions", str(decided),
                            "--at", T_R2_INTEGRATE)
        lifecycle_journal.append_event(audit, "round_integrated", {"round": 2, "integrated_at": T_R2_INTEGRATE},
                                       at=T_R2_INTEGRATE)
        out["approve2"] = run("approve_settled.py")
        out["settle2"] = run("make_settled.sh")
        fix_generated_at()
        out["diff3"] = run("make_diff_packet.py", "--round", "3",
                           "--before-report", str(audit / "work/report_before_round3.md"),
                           "--after-report", str(audit / "report/REPORT_final.md"),
                           "--before-findings", str(audit / "work/findings.before_round2_integration.jsonl"),
                           "--after-findings", str(audit / "work/findings.jsonl"),
                           "--output", str(audit / "critique/DIFF_MANIFEST_round_3.json"))
        write_snippets(audit)
        out["artifacts"] = run("generate_finding_artifacts.py", "--apply")
        return out

    # The one file whose bytes carry the wall clock of the script that wrote it (SETTLED.json's
    # generated_at is pinned in the pipeline instead, because make_diff_packet reads it back).
    WALL_CLOCK = {"work/finding_projections.manifest.json": re.compile(rb'"generated_at": "[^"]+"')}

    def tree(self, audit: pathlib.Path) -> dict[str, bytes]:
        out = {}
        for path in sorted(audit.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(audit).as_posix()
            data = path.read_bytes().replace(str(audit).encode(), b"<AUDIT>")
            if relative in self.WALL_CLOCK:
                data, count = self.WALL_CLOCK[relative].subn(b'"generated_at": "<WALL CLOCK>"', data)
                self.assertEqual(count, 1, relative)
            out[relative] = data
        return out

    def test_every_output_matches_263_byte_for_byte(self) -> None:
        old = self.audit.parent / "audit-263"
        shutil.copytree(self.audit, old, symlinks=True)
        new_out = self.pipeline(self.audit, SKILL)
        old_out = self.pipeline(old, PRISTINE)
        self.assertEqual(new_out, old_out)
        new_tree, old_tree = self.tree(self.audit), self.tree(old)
        self.assertEqual(sorted(new_tree), sorted(old_tree))
        for relative in sorted(old_tree):
            with self.subTest(relative):
                self.assertEqual(new_tree[relative], old_tree[relative])
        # The pipeline reached every stage it claims to compare.
        for relative in ("critique/round_2/INTEGRATION_PLAN.json", "critique/round_2/RESPONSE_notes.md",
                         "work/findings.before_round2_integration.jsonl", "critique/DIFF_MANIFEST_round_3.json",
                         "comment.json", "SETTLED.json"):
            self.assertIn(relative, new_tree)
        self.assertIn("F13", [r["id"] for r in self.settled()["rejected"]])
        self.assertNotEqual(new_tree["SETTLED.json"], new_tree["work/SETTLED.round1.json"])
        comment = json.loads(new_tree["comment.json"])
        self.assertTrue(comment["findings"])

    def test_the_positive_fixture_cannot_promote_f13_under_263(self) -> None:
        verifier = self.verifier([F13_CLAIM], ["CONFIRMED"], invocation_id="inv-verifier-1")
        self.run_script("integrate_round.py", "--round", "1", "plan", skill=PRISTINE)
        refused = self.apply(decisions(), skill=PRISTINE, check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("(F13).verifications: confirmations must span two known model families", refused.stderr)
        # 2.6.3 cannot settle the registry 2.6.4 writes either: it refuses role_class verifier outright.
        self.apply(decisions())
        self.log_integration()
        self.run_script("approve_settled.py", skill=PRISTINE)
        made = self.run_script("make_settled.sh", skill=PRISTINE, check=False)
        self.assertNotEqual(made.returncode, 0)
        # The 2.6.3 schema refuses the verifier ledger row; with only the 2.6.4 enum it would still
        # refuse the record (role_class outside {auditor, critic, adjudicator}).
        self.assertRegex(made.stderr, r"role_class: 'verifier' is not one of|"
                                      r"not a completed reviewer provider call: inv-verifier-1")
        self.assertIn("13 promoted", self.run_script("make_settled.sh").stdout)
        self.assertTrue(verifier["role"].startswith("verifier_"))


def tearDownModule() -> None:
    if Fixture._template_dir is not None:
        Fixture._template_dir.cleanup()


if __name__ == "__main__":
    unittest.main()


class QaMergeInteractions(Fixture):
    """2.6.8: the QA gates merged onto the verifier keep its promotions and never discard hand edits."""

    def test_a_reworded_claim_confirmed_by_the_verifier_settles(self) -> None:
        # Round 1 rewords F13; the verifier then confirms the NEW wording; round 2 binds it. The reworded-claim
        # hold (a later confirmation of the new wording) is satisfied by the verifier's hash-bound verdict.
        self.plan()
        decided = decisions(approve_f13=False)
        decided["F13"] = {"claim": EDITED_CLAIM, "status": "draft", "coordinator_approved": False}
        self.apply(decided)
        self.log_integration()
        self.verifier([EDITED_CLAIM], ["CONFIRMED"], invocation_id="inv-v-new",
                      times=("2026-08-27T11:30:00Z", "2026-08-27T11:40:00Z"))
        self.open_round_two()
        self.apply({"F13": decisions()["F13"] | {"claim": EDITED_CLAIM}}, rnd=2, at="2026-08-27T12:00:00Z")
        self.log_round_integrated(2, "2026-08-27T12:00:00Z")
        self.settle()
        settled = self.settled()
        self.assertNotIn("F13", {item["id"] for item in settled["rejected"]})
        fact = next(item for item in settled["facts"] if item["id"] == "F13")
        self.assertIn("claude", fact.get("new_wording_confirmed_by_families", []))

    def test_a_reapply_refuses_to_discard_a_row_added_by_hand(self) -> None:
        self.plan()
        self.apply(decisions(approve_f13=False))
        rows = jsonl(self.audit / "work/findings.jsonl")
        added = copy.deepcopy(rows[0])
        added.update({"id": "F14", "claim": "A critic's new issue, added by hand for the verifier."})
        write_jsonl(self.audit / "work/findings.jsonl", rows + [added])
        again = self.apply(decisions(approve_f13=False), check=False)
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn("changed since round 1's apply", again.stderr)
        self.assertIn("F14", {row["id"] for row in jsonl(self.audit / "work/findings.jsonl")})
