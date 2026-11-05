"""2.7.9: two Phase-1 runner defects found by the review of real audits, each tested with the real scripts.

1. ``closed_file_set`` discarded complete roles. A role's last step is to clean its scratch out of
   output.partial, and the Claude CLI refused every such cleanup that had the form
   ``cd <output.partial> && rm ...``. The runner then refused the whole output, by a rule that is
   not repairable and whose error named no file, and re-ran the role from scratch: three complete
   roles (audits A91, A73, A90). Files outside the
   contract and outside evidence/ are now moved to <attempt>/non_contract_scratch/ and recorded in
   <attempt>/non_contract_scratch.json, and the output is admitted. An extra under evidence/ is
   still refused, and every refusal names up to five paths. The verifier's re-admission of a
   promoted tree is unchanged: it still refuses an extra, and moves nothing.
2. Phase-1 pause events journaled private paths. The preflight and capability-probe pause paths
   wrote ``str(exc)`` -- for an OSError, the absolute path, /mnt/c/Users/<account>/... on a WSL
   host -- into phase1/status.json and the ``pause_start`` event, and the coordinator then refused
   the projected run.json (audit A93: events masked by hand). Both, and
   the launch-failure and catch-all status errors, now take ``run_coordinator.redact_private_paths``.
3. (review of 2.7.9) The same text could still carry a participant-code-shaped deposit file name --
   a manifest mismatch, or the deadline's new step label ("... during copying SUBJ_P12.sav") -- and
   the coordinator refused run.json just the same. Every pause detail, status error, failures[role]
   and terminal-record error now goes through ``run_coordinator.provider_safe_text``, whose output
   passes that scan by construction. The set-aside retries a refused rename (fs_retry), makes a
   read-only directory writable before moving it, puts back what it moved when it still fails,
   names a colliding entry through ``_named``, and writes a name that is not UTF-8 as its escape.

Every test builds the condition on disk and runs the real runner or admission code; each has a
negative control.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import fs_retry  # noqa: E402
import lifecycle_journal  # noqa: E402
import phase1_runtime  # noqa: E402
import run_coordinator  # noqa: E402
import run_phase1  # noqa: E402
import run_verifier  # noqa: E402
import test_264_verifier_runtime as verifier_tests  # noqa: E402  (its Phase-1 audit fixture)
import test_run_phase1  # noqa: E402  (the Phase-1 audit fixture)

HOME = "/" + "Users/"
# Synthetic account names. None may survive into status.json, the journal or run.json.
ACCOUNTS = ("jdoe-example", "Anna Example", "wsl-example")
ROLE = "auditor_text_vs_data"
CONTRACT = {"REPORT.md", "result.json", "evidence"}
SCRATCH = "non_contract_scratch"  # phase1_runtime.NON_CONTRACT_SCRATCH, spelled out for the 2.7.8 controls
WITHHELD = "<code-shaped token withheld>"  # run_coordinator.CODE_WITHHELD, spelled out for the round-1 controls
# A file name that is not UTF-8, as Python lists it on Linux (a lone surrogate).
UNDECODABLE = os.fsdecode(b"caf\xe9.sav")

# The CLI double, wrapped: after the fake writes a contract-perfect output, leave the planned
# leftovers in the attempt's output directory, as a role whose final cleanup was refused does.
WRAPPER = '''#!{python}
import json, os, pathlib, subprocess, sys
FAKE = {fake!r}
if "--version" in sys.argv[1:]:
    os.execv(sys.executable, [sys.executable, FAKE, *sys.argv[1:]])
prompt = sys.stdin.read()
done = subprocess.run([sys.executable, FAKE, *sys.argv[1:]], input=prompt, capture_output=True, text=True)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
plan = json.loads(os.environ.get("STRAY_PLAN") or "null")
result = pathlib.Path.cwd() / "result.json"
if plan and result.is_file():
    role = json.loads(result.read_text(encoding="utf-8")).get("role", "")
    repair = "Perform one format-only repair" in prompt
    marker = pathlib.Path(plan["state"]) / role
    if role.startswith(tuple(plan["roles"])) and repair == plan["repair"] and not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("planted\\n", encoding="utf-8")
        for name, text in plan["files"].items():
            path = pathlib.Path.cwd() / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for name, target in plan.get("links", {{}}).items():
            (pathlib.Path.cwd() / name).symlink_to(target)
sys.exit(done.returncode)
'''

LEFTOVERS = {
    "work/notebook_dump.txt": "intermediate notebook output\n",
    "work/deep/REPORT.md": "# a working draft named like a contract file\n",
    "scratch_check.py": "print('aggregate check')\n",
    ".DS_Store": "finder metadata\n",
}


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listing(directory: pathlib.Path) -> set[str]:
    return {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}


def no_account(test: unittest.TestCase, text: str, where: str) -> None:
    for account in ACCOUNTS:
        test.assertNotIn(account, text, f"{where} names the account {account!r}")
    test.assertIsNone(run_coordinator.LOCAL_PATH.search(text), f"{where} keeps a private path: {text}")


def coordinator_admits(test: unittest.TestCase, root: pathlib.Path) -> str:
    """What run_coordinator does with run.json before any call, with the deposit's own exemptions."""
    run = (root / "run.json").read_text(encoding="utf-8")
    try:
        run_coordinator.validate_provider_visible_string(
            run, "context/run.json", run_coordinator.deposit_exemptions(root))
    except run_coordinator.CoordinatorError as exc:
        test.fail(f"the coordinator refuses run.json: {str(exc)[:160]}")
    return run


def provider_safe(test: unittest.TestCase, text: str, where: str) -> None:
    """The coordinator's scan, with no exemption, on the text and on it as JSON writes it."""
    for form in (text, json.dumps(text, ensure_ascii=False), json.dumps({"detail": text}, indent=2)):
        try:
            run_coordinator.validate_provider_visible_string(form, where)
        except run_coordinator.CoordinatorError as exc:
            test.fail(f"{where} is not provider-safe: {exc} in {form!r}")


# ------------------------------------------------------------------------------- C06, end to end
class LeftoversOutsideEvidence(verifier_tests.VerifierFixture):
    """The real runners with a CLI that leaves scratch beside a complete output."""

    def setUp(self) -> None:
        super().setUp()
        wrapper = self.base / "fake-claude-leftovers"
        wrapper.write_text(WRAPPER.format(python=sys.executable, fake=str(self.fake)), encoding="utf-8")
        wrapper.chmod(0o755)
        self.fake = wrapper

    def plan(self, files: dict[str, str], roles: tuple[str, ...] = (ROLE,), *,
             repair: bool = False, links: dict[str, str] | None = None) -> dict[str, str]:
        state = self.base / f"stray-state-{len(list(self.base.glob('stray-state-*')))}"
        return {"STRAY_PLAN": json.dumps({"files": files, "roles": list(roles), "repair": repair,
                                          "state": str(state), "links": links or {}})}

    def rows(self, root: pathlib.Path, role: str) -> list[dict]:
        return [row for row in self.ledger(root) if row.get("role") == role and row.get("provider_call")]

    def role_starts(self, role: str) -> int:
        return len([row for row in self.starts() if row["role"] == role])

    def attempts(self, root: pathlib.Path, role: str, prefix: str = "attempt_") -> list[pathlib.Path]:
        return sorted((root / "phase1/roles" / role).glob(f"*/{prefix}*"))

    def assert_set_aside(self, attempt: pathlib.Path, files: dict[str, str]) -> None:
        output = attempt / "output.partial"
        self.assertTrue({name.split("/")[0] for name in files}.isdisjoint(
            path.name for path in output.iterdir()), "no leftover stays in output.partial")
        self.assertEqual({path.name for path in output.iterdir()}, CONTRACT)
        self.assertEqual(phase1_runtime.NON_CONTRACT_SCRATCH, SCRATCH)
        scratch = attempt / SCRATCH
        self.assertEqual(listing(scratch), set(files), "every leftover moved, relative path kept")
        for name, text in files.items():
            self.assertEqual((scratch / name).read_text(encoding="utf-8"), text)
        record = json.loads((attempt / "non_contract_scratch.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema_version"], "i4r-phase1-non-contract-scratch-1")
        self.assertEqual(record["files"], [
            {"path": name, "bytes": (scratch / name).stat().st_size, "sha256": digest(scratch / name)}
            for name in sorted(files)
        ])

    def test_leftovers_are_set_aside_and_the_role_is_admitted_on_its_first_sample(self) -> None:
        root = self.make_audit()
        result = self.run_phase1(root, "success", extra_env=self.plan(LEFTOVERS))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        # 2.7.8 refused this sample (closed_file_set) and paid for a second one.
        self.assertEqual(self.role_starts(ROLE), 1)
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in self.rows(root, ROLE)],
                         [("completed", "role_inference")])
        [attempt] = self.attempts(root, ROLE)
        self.assert_set_aside(attempt, LEFTOVERS)
        # Nothing set aside is promoted: the canonical report and evidence are the contract's.
        summary = json.loads(result.stdout)
        completion = next(row for row in summary["roles"] if row["role"] == ROLE)
        evidence = root / completion["evidence"]
        self.assertEqual(digest(evidence), completion["evidence_manifest_sha256"])
        listed = {row["path"] for row in json.loads(evidence.read_text())["artifacts"]}
        self.assertEqual(listing(evidence.parent), {"manifest.json", *listed})
        self.assertEqual(digest(root / completion["report"]), completion["report_sha256"])
        self.assertEqual(listing(attempt / "output.partial"),
                         {"REPORT.md", "result.json", "evidence/manifest.json", *(f"evidence/{p}" for p in listed)})
        # Negative control: a role with no leftovers sets nothing aside.
        for other in phase1_runtime.ROLE_SPECS:
            if other != ROLE:
                [clean] = self.attempts(root, other)
                self.assertFalse((clean / SCRATCH).exists(), other)
                self.assertFalse((clean / "non_contract_scratch.json").exists(), other)

    def test_a_focused_repair_that_leaves_scratch_is_admitted(self) -> None:
        root = self.make_audit()
        files = {"work/repair_notes.txt": "structural fix only\n"}
        result = self.run_phase1(root, "repairable_output", target=ROLE,
                                 extra_env=self.plan(files, repair=True))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        # 2.7.8 refused the repair too and spent a third, fresh sample.
        self.assertEqual(self.role_starts(ROLE), 2)
        self.assertEqual([(r["status"], r["attempt_kind"]) for r in self.rows(root, ROLE)],
                         [("failed", "role_inference"), ("completed", "output_repair")])
        [repair] = self.attempts(root, ROLE, "repair_")
        self.assert_set_aside(repair, files)

    def test_a_leftover_under_evidence_is_still_refused_and_named(self) -> None:
        cases = {
            # Named like the contract's manifest, so the closure check (which skips that name) lets
            # it through to the closed file set.
            "contract_name": ({"evidence/sub/manifest.json": "{}\n", "work/notes.txt": "x\n"},
                              "closed output contract: evidence/sub/manifest.json, work/notes.txt"),
            "unlisted": ({"evidence/unlisted_notes.txt": "x\n", "work/notes.txt": "x\n"},
                         "does not close over every evidence file"),
        }
        for label, (files, message) in cases.items():
            with self.subTest(case=label):
                self.state = self.base / f"fake-state-{label}.jsonl"
                root = self.make_audit(f"audit-{label}")
                result = self.run_phase1(root, "success", extra_env=self.plan(files))
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(self.role_starts(ROLE), 2, "the sample is refused and re-sampled")
                rows = self.rows(root, ROLE)
                self.assertEqual([(r["status"], r["error_class"]) for r in rows],
                                 [("failed", "schema"), ("completed", None)])
                self.assertIn(message, rows[0]["error"])
                first, second = self.attempts(root, ROLE)
                if json.loads((first / "status.json").read_text())["state"] != "failed":
                    first, second = second, first
                # Nothing is moved from a refused sample: its output stays as the model left it.
                self.assertFalse((first / SCRATCH).exists())
                self.assertTrue(set(files) <= listing(first / "output.partial"))

    def test_a_refusal_naming_a_code_shaped_file_is_journaled_without_it(self) -> None:
        # An output fault named by its path, here a symlink the role left in work/. The refusal is
        # journaled through append_failure into the terminal row (run.json) and the attempt's
        # status.json; the coordinator refused run.json with the name in it, and a terminal row
        # cannot be masked by a later text_redaction.
        root = self.make_audit()
        plan = self.plan({"work/notes.txt": "x\n"}, links={"work/SUBJ_P12_rows": "notes.txt"})
        result = self.run_phase1(root, "success", extra_env=plan)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("SUBJ_P12", coordinator_admits(self, root))  # round 1 refused here
        rows = self.rows(root, ROLE)
        self.assertEqual([(r["status"], r["error_class"]) for r in rows], [("failed", "schema"), ("completed", None)])
        self.assertEqual(rows[0]["error"], f"role output contains a symlink: work/{WITHHELD}_rows")
        first = next(attempt for attempt in self.attempts(root, ROLE)
                     if json.loads((attempt / "status.json").read_text())["state"] == "failed")
        self.assertEqual(json.loads((first / "status.json").read_text())["error"], rows[0]["error"])
        self.assertTrue((first / "output.partial/work/SUBJ_P12_rows").is_symlink(), "the sample is left as it was")

    def test_the_verifier_sets_leftovers_aside_and_its_promoted_record_is_readmitted(self) -> None:
        root = self.audit_after_phase1()
        files = {"work/h1_scratch.py": "print('rerun')\n", "notes.txt": "draft\n"}
        result = self.run_verifier(root, ["F13"], extra_env=self.plan(files, ("verifier_",)))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        role = self.role_for("F13")
        self.assertEqual(len(self.verifier_starts()), 1, "admitted on its first sample")
        [attempt] = self.attempts(root, role)
        self.assert_set_aside(attempt, files)
        receipt_path, receipt = self.receipt(root, role)
        promoted = root / verifier_tests.verifier_contract.output_dir(role, receipt_path.stem)
        # The verifier promotes its whole output tree, so the set-aside is what keeps scratch out.
        self.assertEqual({path.name for path in promoted.iterdir()}, CONTRACT)
        verdicts, errors = self.verdicts(root)
        self.assertEqual(errors, [])
        self.assertEqual([verdict for _, verdict in verdicts[self.claim_hash("F13")]], ["CONFIRMED"])
        # Re-admission of a stored record is unchanged: an extra in the promoted tree is refused,
        # now by name, and is never moved.
        (promoted / "late_notes.txt").write_text("added after promotion\n", encoding="utf-8")
        verdicts, errors = self.verdicts(root)
        self.assertNotIn(self.claim_hash("F13"), verdicts)
        self.assertTrue(any("closed output contract: late_notes.txt" in str(error) for error in errors), errors)
        self.assertTrue((promoted / "late_notes.txt").is_file())
        self.assertFalse((promoted.parent / SCRATCH).exists())


# ------------------------------------------------------------------------------- C06, admission
class AdmissionOfOneOutput(unittest.TestCase):
    """validate_role_output on copies of a runner-valid auditor output."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = test_run_phase1.Phase1RunnerTests("test_normal_path_is_one_probe_plus_five_parallel_first_try_auditors")
        cls.fixture.setUp()
        result = cls.fixture.run_runner()
        assert result.returncode == 0, result.stderr + result.stdout
        root = cls.fixture.root
        quorum = json.loads((root / "phase1/quorum.json").read_text())
        cls.snapshot = json.loads((root / "phase1/snapshots" / quorum["source_snapshot_sha256"]
                                   / "snapshot_manifest.json").read_text())
        [cls.clean] = sorted((root / "phase1/roles/auditor_reproduction").glob("*/attempt_*/output.partial"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.tearDown()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-279-admission-")
        self.attempt = pathlib.Path(self.temporary.name) / "attempt_01_test"
        self.output = self.attempt / "output.partial"
        shutil.copytree(self.clean, self.output)
        self.scratch = self.attempt / SCRATCH

    def tearDown(self) -> None:
        for path in pathlib.Path(self.temporary.name).rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        self.temporary.cleanup()

    def plant(self, names) -> None:
        for name in names:
            path = self.output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"leftover {name}\n", encoding="utf-8")

    def admit(self, *, set_aside: bool = True):
        # Without set_aside: exactly the 2.7.8 call (the verifier's re-admission makes this one).
        extra = {"set_aside": self.scratch} if set_aside else {}
        return phase1_runtime.validate_role_output(self.output, "auditor_reproduction", self.snapshot, **extra)

    def refused(self, rule: str, *, set_aside: bool = True) -> str:
        with self.assertRaises(phase1_runtime.OutputValidationError) as caught:
            self.admit(set_aside=set_aside)
        self.assertEqual(caught.exception.rule, rule, str(caught.exception))
        self.assertFalse(caught.exception.repairable)
        return str(caught.exception)

    def test_leftovers_of_every_shape_are_set_aside_with_a_record(self) -> None:
        reference = self.admit()  # positive control: the clean copy is admitted, nothing set aside
        self.assertFalse(self.scratch.exists())
        names = [".hidden_tmp", "work/a.csv", "work/sub/result.json", "work/sub/REPORT.md",
                 ".claude/settings.local.json", "scratch.R"]
        self.plant(names)
        (self.output / "work/empty_dir").mkdir()
        validated = self.admit()
        for key in ("report_sha256", "evidence_manifest_sha256", "result_sha256"):
            self.assertEqual(getattr(validated, key), getattr(reference, key))
        self.assertEqual({path.name for path in self.output.iterdir()}, CONTRACT)
        self.assertEqual(listing(self.scratch), set(names))
        self.assertTrue((self.scratch / "work/empty_dir").is_dir())
        record = json.loads((self.attempt / "non_contract_scratch.json").read_text())
        self.assertEqual(record["directory"], "non_contract_scratch")
        self.assertEqual([row["path"] for row in record["files"]], sorted(names))
        self.assertTrue(all(row["sha256"] == digest(self.scratch / row["path"]) for row in record["files"]))
        # Admitting the same attempt again finds nothing to move and keeps the record.
        self.admit()
        self.assertEqual(listing(self.scratch), set(names))

    def test_without_a_set_aside_folder_the_refusal_names_five_paths(self) -> None:
        names = [f"work/file_{index}.txt" for index in range(7)]
        self.plant(names)
        message = self.refused("closed_file_set", set_aside=False)
        self.assertIn("closed output contract: " + ", ".join(names[:5]) + " and 2 more", message)
        self.assertEqual(listing(self.output) & set(names), set(names), "nothing moved")

    def test_a_code_shaped_name_is_withheld_from_the_error(self) -> None:
        self.plant(["work/SUBJ_P12_rows.csv", "work/summary.txt"])
        message = self.refused("closed_file_set", set_aside=False)
        self.assertNotIn("SUBJ_P12", message)
        self.assertIn("<code-shaped name withheld>, work/summary.txt", message)
        # run.json carries this text into the coordinator's provider-safety scan.
        run_coordinator.validate_provider_visible_string(message, "error")

    def test_a_symlink_leftover_is_refused_and_nothing_is_moved(self) -> None:
        self.plant(["work/notes.txt"])
        (self.output / "work/link").symlink_to(self.output / "REPORT.md")
        self.refused("output_symlink", set_aside=False)  # as 2.7.8
        self.refused("output_symlink")
        self.assertFalse(self.scratch.exists())
        self.assertTrue((self.output / "work/notes.txt").is_file())

    def test_a_host_that_refuses_the_move_gets_the_old_refusal(self) -> None:
        self.plant(["work/notes.txt"])
        self.scratch.mkdir()
        self.scratch.chmod(0o500)
        with mock.patch.object(fs_retry.time, "sleep") as slept:
            message = self.refused("closed_file_set")
        # The refusal is retried like every other rename (fs_retry), then the 2.7.8 refusal.
        self.assertEqual([call.args[0] for call in slept.call_args_list], list(fs_retry.REPLACE_RETRY_DELAYS))
        self.assertIn("work/notes.txt", message)
        self.assertIn("setting them aside failed", message)
        self.assertNotIn(str(self.attempt), message, "never the absolute path")
        self.assertTrue((self.output / "work/notes.txt").is_file())

    def refuse_renames(self, *, forward: set[str] = frozenset(), back: set[str] = frozenset(), once: bool = False):
        """os.replace refusing EACCES for the named top-level entries, as Windows does while a sync
        client holds a file inside: moving into the set-aside folder, or back out of it."""
        real, refused = os.replace, []

        def replace(source, destination, *args, **kwargs):
            source, destination = pathlib.Path(source), pathlib.Path(destination)
            name = source.name
            into = destination.parent == self.scratch and name in forward
            out = source.parent == self.scratch and name in back
            if (into or out) and not (once and refused):
                refused.append(name)
                raise PermissionError(errno.EACCES, "Permission denied", str(source), None, str(destination))
            return real(source, destination, *args, **kwargs)
        return mock.patch("os.replace", replace), refused

    def test_a_transient_refusal_of_the_move_is_retried_and_admitted(self) -> None:
        names = ["work/dump.csv", "scratch.py"]
        self.plant(names)
        patch, refused = self.refuse_renames(forward={"work"}, once=True)
        with patch, mock.patch.object(fs_retry.time, "sleep"):
            self.admit()  # round 1 refused this sample and paid for another
        self.assertEqual(refused, ["work"])
        self.assertEqual(listing(self.scratch), set(names))
        record = json.loads((self.attempt / "non_contract_scratch.json").read_text())
        self.assertEqual([row["path"] for row in record["files"]], sorted(names))

    def test_a_move_still_refused_puts_back_what_it_moved(self) -> None:
        names = ["a_work/x.txt", "z_work/y.txt"]  # a_work moves first
        self.plant(names)
        before = {path: digest(self.output / path) for path in listing(self.output)}
        patch, _ = self.refuse_renames(forward={"z_work"})
        with patch, mock.patch.object(fs_retry.time, "sleep"):
            message = self.refused("closed_file_set")
        self.assertIn("setting them aside failed: Permission denied", message)
        # output.partial is as the model left it, and no folder or record says otherwise.
        self.assertEqual({path: digest(self.output / path) for path in listing(self.output)}, before)
        self.assertFalse(self.scratch.exists())
        self.assertFalse((self.attempt / "non_contract_scratch.json").exists())
        # If putting it back is refused too, the record describes what the folder then holds.
        patch, _ = self.refuse_renames(forward={"z_work"}, back={"a_work"})
        with patch, mock.patch.object(fs_retry.time, "sleep"):
            self.refused("closed_file_set")
        self.assertEqual(listing(self.scratch), {"a_work/x.txt"})
        record = json.loads((self.attempt / "non_contract_scratch.json").read_text())
        self.assertEqual([row["path"] for row in record["files"]], ["a_work/x.txt"])
        self.assertTrue((self.output / "z_work/y.txt").is_file())

    def test_a_read_only_copied_directory_is_set_aside(self) -> None:
        # What `cp -R <snapshot>/raw/pkg/code ./code_copy` leaves: the frozen snapshot's modes.
        names = ["code_copy/a.R", "code_copy/sub/b.R"]
        self.plant(names)
        for name in names:
            (self.output / name).chmod(0o444)
        (self.output / "code_copy/sub").chmod(0o555)
        (self.output / "code_copy").chmod(0o555)
        self.admit()  # round 1: "setting them aside failed: Permission denied", a fresh sample
        self.assertEqual(listing(self.scratch), set(names))
        record = json.loads((self.attempt / "non_contract_scratch.json").read_text())
        self.assertEqual([row["path"] for row in record["files"]], sorted(names))
        self.assertEqual({path.name for path in self.output.iterdir()}, CONTRACT)

    def test_a_name_collision_withholds_a_code_shaped_name_and_moves_nothing(self) -> None:
        self.plant(["A_first.txt", "SUBJ_P12/notes.txt"])  # A_first.txt moves before the collision
        (self.scratch / "SUBJ_P12").mkdir(parents=True)
        message = self.refused("closed_file_set")
        self.assertIn(f"{SCRATCH}/<code-shaped name withheld> already exists", message)
        self.assertNotIn("SUBJ_P12", message)
        run_coordinator.validate_provider_visible_string(message, "error")
        self.assertTrue((self.output / "A_first.txt").is_file(), "the entry moved before it is put back")
        self.assertEqual(listing(self.scratch), set())

    def test_an_unsafe_set_aside_folder_is_refused_and_nothing_moves(self) -> None:
        self.plant(["work/notes.txt"])
        outside = pathlib.Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.scratch.symlink_to(outside, target_is_directory=True)
        message = self.refused("closed_file_set")
        self.assertIn(f"the set-aside folder {SCRATCH} is unsafe", message)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.output / "work/notes.txt").is_file())

    def test_a_name_that_is_not_utf8_is_written_as_its_escape(self) -> None:
        # The listing comes from disk (check_output_tree); this filesystem may refuse such a name,
        # so the name is handed to the set-aside directly, and its refusal is journaled as is.
        self.plant(["work/notes.txt"])
        self.scratch.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(phase1_runtime.OutputValidationError) as caught:
            phase1_runtime.set_aside_non_contract(self.output, self.scratch, [f"work/{UNDECODABLE}"])
        message = str(caught.exception)
        self.assertIn("work/caf\\udce9.sav", message)
        lifecycle_journal.canonical_bytes({"error": message})  # round 1: UnicodeEncodeError

    def test_a_name_that_is_not_utf8_on_disk_is_set_aside_or_refused_never_a_crash(self) -> None:
        name = os.fsencode(self.output / "work") + b"/caf\xe9.sav"
        (self.output / "work").mkdir()
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError as exc:  # APFS refuses (errno 92): only a Linux filesystem holds such a name
            self.skipTest(f"this filesystem refuses a name that is not UTF-8 ({exc.errno})")
        os.close(descriptor)
        message = self.refused("closed_file_set", set_aside=False)
        self.assertIn("work/caf\\udce9.sav", message)
        lifecycle_journal.canonical_bytes({"error": message})
        self.admit()
        record = json.loads((self.attempt / "non_contract_scratch.json").read_text(encoding="utf-8"))
        self.assertEqual([row["path"] for row in record["files"]], ["work/caf\\udce9.sav"])
        # Whatever else writes the name unescaped, the worst case is the 2.7.8 refusal.
        os.replace(self.scratch / "work", self.output / "work")
        (self.attempt / "non_contract_scratch.json").unlink()
        with mock.patch.object(phase1_runtime, "_printable", lambda text: text):
            self.refused("closed_file_set")

    def test_a_move_cut_short_is_finished_by_the_next_admission(self) -> None:
        names = ["work/a.txt", "scratch.py"]
        self.plant(names)
        self.scratch.mkdir()
        os.replace(self.output / "work", self.scratch / "work")  # a kill after the first move
        self.admit()
        self.assertEqual(listing(self.scratch), set(names))
        record = json.loads((self.attempt / "non_contract_scratch.json").read_text())
        self.assertEqual([row["path"] for row in record["files"]], sorted(names))

    def test_a_contract_name_in_another_case_is_refused_not_moved(self) -> None:
        probe = self.output / "CaseProbe"
        probe.write_text("", encoding="utf-8")
        insensitive = (self.output / "caseprobe").exists()
        probe.unlink()
        if not insensitive:
            self.skipTest("needs a case-insensitive filesystem")
        # REPORT.md still opens as REPORT.md, but the tree lists Report.md: moving it would promote
        # a report that is no longer there.
        os.replace(self.output / "REPORT.md", self.output / "tmp-name")
        os.replace(self.output / "tmp-name", self.output / "Report.md")
        message = self.refused("closed_file_set")
        self.assertIn("Report.md", message)
        self.assertTrue((self.output / "Report.md").is_file())
        self.assertFalse(self.scratch.exists())


# ------------------------------------------------------------------------------------------- C07
class PausePathsNameNoAccount(unittest.TestCase):
    """run_phase1.main and run_process on a metadata-2.1 audit, with private paths in the error."""

    def setUp(self) -> None:
        self.fixture = test_run_phase1.Phase1RunnerTests("test_normal_path_is_one_probe_plus_five_parallel_first_try_auditors")
        self.fixture.setUp()
        self.base = self.fixture.base

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def private_paths(self) -> str:
        return (f"{HOME}{ACCOUNTS[0]}/Library/CloudStorage/x.json -> "
                f"/mnt/c{HOME}{ACCOUNTS[1]}/Dropbox/audit/phase1/y.json")

    def assert_clean_pause(self, root: pathlib.Path, reason: str) -> None:
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"]), ("paused", reason))
        no_account(self, status["error"], "phase1/status.json")
        self.assertIn("<private-path>", status["error"], "the rest of the text is kept")
        [pause] = [event for event in lifecycle_journal.load_events(root) if event["type"] == "pause_start"]
        no_account(self, json.dumps(pause), "the pause_start event")
        self.assertEqual(pause["payload"]["detail"], status["error"])
        run = (root / "run.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(run)["pause_reason"], reason)
        # What the coordinator does with run.json before any call (2.7.8 refused here).
        run_coordinator.validate_provider_visible_string(run, "context/run.json",
                                                         run_coordinator.deposit_exemptions(root))

    def test_a_preflight_pause_journals_no_account_path(self) -> None:
        # The WSL shape: a CLI the host will not execute, under /mnt/c/Users/<account>/.
        cli = self.base / "mnt/c/Users" / ACCOUNTS[2] / "AppData/Local/claude"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\necho 2.1.206\n", encoding="utf-8")
        cli.chmod(0o644)
        root = self.fixture.make_audit("preflight-wsl", metadata21=True)
        self.assertEqual(run_phase1.main([str(root), "--claude-bin", str(cli)]), 3)
        self.assert_clean_pause(root, "local_preflight_failed")
        # The macOS shape, two paths in one OSError (a rename).
        root = self.fixture.make_audit("preflight-mac", metadata21=True)
        error = OSError(13, "Permission denied", f"{HOME}{ACCOUNTS[0]}/Library/x.json", None,
                        f"/mnt/c{HOME}{ACCOUNTS[1]}/Dropbox/y.json")
        with mock.patch.object(run_phase1, "cli_version", side_effect=error):
            self.assertEqual(run_phase1.main([str(root), "--claude-bin", str(self.fixture.fake)]), 3)
        self.assert_clean_pause(root, "local_preflight_failed")

    def test_a_probe_pause_journals_no_account_path(self) -> None:
        root = self.fixture.make_audit("probe", metadata21=True)
        error = run_phase1.RunnerError(f"capability probe could not read {self.private_paths()}")
        with mock.patch.object(run_phase1, "run_capability_probe", side_effect=error):
            self.assertEqual(run_phase1.main([str(root), "--claude-bin", str(self.fixture.fake)]), 4)
        self.assert_clean_pause(root, "capability_probe_failed")

    def test_the_catch_all_failure_status_names_no_account_path(self) -> None:
        root = self.fixture.make_audit("catchall", metadata21=True)
        error = run_phase1.RunnerError(f"policy snapshot unreadable: {self.private_paths()}")
        with mock.patch.object(run_phase1, "load_policy", side_effect=error):
            self.assertEqual(run_phase1.main([str(root), "--claude-bin", str(self.fixture.fake)]), 1)
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual(status["status"], "failed")
        no_account(self, status["error"], "phase1/status.json")
        self.assertIn("policy snapshot unreadable", status["error"])

    def test_a_launch_failure_status_names_no_account_path(self) -> None:
        root = self.fixture.make_audit("launch", metadata21=True)
        route = test_run_phase1.auditor_route(root)
        attempt = root / "phase1/roles" / ROLE / ("a" * 64) / "attempt_01_launch"
        (attempt / "output.partial").mkdir(parents=True)
        missing = self.base / "mnt/c/Users" / ACCOUNTS[2] / "bin/claude"  # never created
        with self.assertRaises(run_phase1.ProviderLaunchError):
            run_phase1.run_process(
                root, ROLE, "role_inference", [str(missing)], attempt / "output.partial", "prompt",
                "a" * 64, "b" * 64, 30, session_id="00000000-0000-4000-8000-000000000001", route=route,
            )
        status = json.loads((attempt / "status.json").read_text())
        self.assertEqual(status["state"], "failed")
        no_account(self, status["error"], "the attempt's status.json")
        self.assertIn("No such file or directory", status["error"])
        # Negative control: the terminal row was already redacted before 2.7.9.
        [row] = [row for row in run_phase1.read_ledger(root) if row.get("role") == ROLE]
        no_account(self, row["error"], "the terminal row")


# ---------------------------------------------------------------- C07-1 / PF-1 / F2 (review of 2.7.9)
class PauseTextWithholdsCodes(unittest.TestCase):
    """A participant-code-shaped deposit file name in journaled Phase-1 text (run_phase1.main)."""

    def setUp(self) -> None:
        self.fixture = test_run_phase1.Phase1RunnerTests("test_normal_path_is_one_probe_plus_five_parallel_first_try_auditors")
        self.fixture.setUp()
        self.base = self.fixture.base
        self.env = mock.patch.dict(os.environ, {"FAKE_PHASE1_SCENARIO": "success",
                                                "FAKE_PHASE1_STATE": str(self.fixture.state)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.fixture.tearDown()

    def main(self, root: pathlib.Path, *extra: str) -> int:
        return run_phase1.main([str(root), "--claude-bin", str(self.fixture.fake), *extra])

    def assert_withheld_pause(self, root: pathlib.Path, reason: str, kept: str, token: str) -> str:
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual((status["status"], status["pause_reason"]), ("paused", reason))
        self.assertNotIn(token, coordinator_admits(self, root))  # round 1 refused here
        [pause] = [event for event in lifecycle_journal.load_events(root) if event["type"] == "pause_start"]
        self.assertEqual(pause["payload"]["detail"], status["error"])
        self.assertIn(kept, status["error"], "the rest of the text is kept")
        self.assertIn(WITHHELD, status["error"])
        provider_safe(self, status["error"], "phase1/status.json error")
        return status["error"]

    def test_a_manifest_mismatch_on_a_code_named_deposit_file(self) -> None:
        root = self.fixture.make_audit("mismatch", metadata21=True)
        (root / "pkg/EMG_P01.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        self.fixture.write_package_manifest(root)
        (root / "pkg/EMG_P01.csv").write_text("a,b\n1,3\n", encoding="utf-8")  # changed since intake
        self.assertEqual(self.main(root), 3)
        detail = self.assert_withheld_pause(root, "local_preflight_failed", "pkg/", "EMG_P01")
        self.assertIn(f"pkg/{WITHHELD}.csv", detail)

    def test_the_alarm_during_the_copy_of_a_code_named_deposit_file(self) -> None:
        big = 8 * 1024 * 1024 + 7
        root = self.fixture.make_audit("alarm", metadata21=True)
        (root / "pkg/SUBJ_P12.sav").write_bytes(os.urandom(big))
        self.fixture.write_package_manifest(root)
        real_fsync, stalled = os.fsync, []

        def fsync(descriptor: int) -> None:
            if os.fstat(descriptor).st_size == big:  # the snapshot copy of that file blocks
                stalled.append(True)
                time.sleep(60)
            real_fsync(descriptor)
        with mock.patch("os.fsync", fsync):
            self.assertEqual(self.main(root, "--probe-only", "--pre-dispatch-deadline-seconds", "4"), 3)
        self.assertEqual(stalled, [True], "the alarm interrupted the blocked copy")
        detail = self.assert_withheld_pause(root, "local_preflight_failed", "deadline exceeded during ", "SUBJ_P12")
        self.assertIn(f"{WITHHELD}.sav", detail)

    def test_a_probe_pause_a_failed_role_and_the_catch_all_withhold_codes(self) -> None:
        root = self.fixture.make_audit("probe", metadata21=True)
        error = run_phase1.RunnerError("capability probe could not read SUBJ_P12_notes.txt")
        with mock.patch.object(run_phase1, "run_capability_probe", side_effect=error):
            self.assertEqual(self.main(root), 4)
        self.assert_withheld_pause(root, "capability_probe_failed", "capability probe could not read ", "SUBJ_P12")
        root = self.fixture.make_audit("roles", metadata21=True)
        error = run_phase1.RunnerError("output failed validation: evidence SUBJ_P12.json; rows x,P7,3.5")
        with mock.patch.object(run_phase1, "run_role", side_effect=error):
            self.assertEqual(self.main(root), 2)
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual(set(status["failed_roles"].values()), {
            f"output failed validation: evidence {WITHHELD}.json; rows x,{WITHHELD},3.5"})
        provider_safe(self, json.dumps(status), "phase1/status.json")
        coordinator_admits(self, root)
        root = self.fixture.make_audit("catchall", metadata21=True)
        error = run_phase1.RunnerError("policy snapshot names SUBJ_P12")
        with mock.patch.object(run_phase1, "load_policy", side_effect=error):
            self.assertEqual(self.main(root), 1)
        status = json.loads((root / "phase1/status.json").read_text())
        self.assertEqual(status["error"], f"policy snapshot names {WITHHELD}")

    def test_a_launch_failure_terminal_row_withholds_a_code(self) -> None:
        # make_terminal_record: every terminal row's error (run.json; no text_redaction reaches it).
        root = self.fixture.make_audit("launch", metadata21=True)
        route = test_run_phase1.auditor_route(root)
        attempt = root / "phase1/roles" / ROLE / ("a" * 64) / "attempt_01_launch"
        (attempt / "output.partial").mkdir(parents=True)
        with self.assertRaises(run_phase1.ProviderLaunchError):
            run_phase1.run_process(
                root, ROLE, "role_inference", ["SUBJ_P12-claude-missing"], attempt / "output.partial",
                "prompt", "a" * 64, "b" * 64, 30, session_id="00000000-0000-4000-8000-000000000001",
                route=route,
            )
        self.assertNotIn("SUBJ_P12", coordinator_admits(self, root))  # round 1 refused here
        [row] = [row for row in run_phase1.read_ledger(root) if row.get("role") == ROLE]
        self.assertIn(f"'{WITHHELD}-claude-missing'", row["error"])
        status = json.loads((attempt / "status.json").read_text())
        self.assertIn(f"'{WITHHELD}-claude-missing'", status["error"])

    def test_a_pause_detail_with_a_name_that_is_not_utf8_still_pauses(self) -> None:
        # The step label names a deposit file; on Linux its name need not be UTF-8.
        root = self.fixture.make_audit("undecodable", metadata21=True)
        error = run_phase1.RunnerError(f"Phase-1 pre-dispatch deadline exceeded during copying {UNDECODABLE}")
        with mock.patch.object(run_phase1, "cli_version", side_effect=error):
            self.assertEqual(self.main(root), 3)  # round 1: status.json could not be written; exit 1
        status = json.loads((root / "phase1/status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["error"], "Phase-1 pre-dispatch deadline exceeded during copying caf\\udce9.sav")
        coordinator_admits(self, root)


class VerifierStatusWithholdsCodes(verifier_tests.VerifierFixture):
    def test_a_verifier_preflight_failure_withholds_a_code(self) -> None:
        root = self.audit_after_phase1()
        error = phase1_runtime.Phase1Error("package manifest hash/size mismatch: pkg/SUBJ_P12.sav")
        with mock.patch.dict(os.environ, self.env("success", "verifier")), \
                mock.patch.object(phase1_runtime, "provider_free_preflight", side_effect=error):
            code = run_verifier.main([str(root), "--findings", "F13", "--claude-bin", str(self.fake)])
        self.assertEqual(code, run_verifier.EXIT_PAUSED)
        status = json.loads((root / "phase1/verifier" / self.role_for("F13") / "status.json").read_text())
        self.assertEqual((status["status"], status["error"]),
                         ("preflight_failed", f"package manifest hash/size mismatch: pkg/{WITHHELD}.sav"))


class ProviderSafeText(unittest.TestCase):
    """run_coordinator.provider_safe_text against the scan it must pass, on hard inputs."""

    def test_typical_text_is_unchanged(self) -> None:
        for text in ("Phase-1 pre-dispatch deadline exceeded during copying data.csv",
                     "role output contains files outside the closed output contract: work/notes.txt",
                     "cannot inspect Claude CLI version: timed out", "ITM_P05 is a column name? no: x"):
            safe = run_coordinator.provider_safe_text(text)
            if run_coordinator.provider_safety.first_participant_code(text) is None:
                self.assertEqual(safe, text)
            provider_safe(self, safe, "text")

    def test_the_output_always_passes_the_coordinators_scan(self) -> None:
        cases = [
            "/" + "Users//" + "Users/jdoe/x",          # redact_private_paths alone leaves /Users/<private-path>
            "/home/\njdoe",                         # JSON writes \n, and /home/\\n... reads as a path
            "/" + "Users/",                          # JSON closes the string with a quote: /Users/" is a path
            "C:" + "/" + 'Users"notes',              # JSON escapes the quote with a backslash, a Windows separator
            "\\".join(["C:", "Users", "Anna Example", "x.json"]),  # a Windows path, spaced account
            "rows: x,P12,3.4 and P7\t2\nP9|1",
            "SUBJ_P12 EMG_P01_L AB_CD_P3_P4 xSUBJ_P1 ABP1",
            f"name {UNDECODABLE} and \x00\x1f\u2028 /private/tmp/a b",
        ]
        alphabet = ["A", "B", "P", "_", "1", "2", "/", "\\", ":", " ", "\n", "\t", ",", "|", "x",
                    "/" + "Users/", "/home/", "/private/tmp/", "C:", "SUBJ_P12", "\udce9", "-", ".", "<", ">", '"']
        import random
        rng = random.Random(279)
        cases += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))) for _ in range(6000)]
        for text in cases:
            safe = run_coordinator.provider_safe_text(text, pathlib.Path("/" + "Users/jdoe/audit"))
            provider_safe(self, safe, "provider_safe_text")
            self.assertNotEqual(safe, run_coordinator.UNMASKABLE_TEXT, f"masked, not withheld whole: {text!r}")
            safe.encode("utf-8")
            self.assertEqual(run_coordinator.provider_safe_text(safe), safe, "a second pass changes nothing")

    def test_a_shape_the_masks_do_not_remove_withholds_the_whole_text_and_never_hangs(self) -> None:
        # Should the scan ever learn a shape the replacements do not remove, the loop is bounded.
        hit = run_coordinator.provider_safety.CodeHit(line=1, kind="raw")
        with mock.patch.object(run_coordinator.provider_safety, "first_participant_code", return_value=hit):
            self.assertEqual(run_coordinator.provider_safe_text("x" * 50), run_coordinator.UNMASKABLE_TEXT)
        provider_safe(self, run_coordinator.UNMASKABLE_TEXT, "the withheld text")


if __name__ == "__main__":
    unittest.main()
