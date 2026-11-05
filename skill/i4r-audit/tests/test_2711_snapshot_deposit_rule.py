"""2.7.11: a new audit's critic snapshots carry the verification snippets and the deposit, not only cited files.

Until 2.7.11 a round snapshot held only the files a registry row binds (evidence.source_hashes and existing
dependencies.artifact_paths). On audit A57 a finding about several deposited files bound
some of them, so every round's snapshot lacked the rest and the completeness critic could not check the finding;
on audit A58 round 2 the critics could not open the work/snippets/F<n>.R files the registry names. A new
audit freezes critique.snapshot_evidence = cited_plus_deposit_1: its snapshots also carry each row's snippet, the
snippets' _prelude.R and the deposit's uncited files within fixed bounds, after the coordinator's provider-safety
screens, and list each file left out with its reason. An audit initialised earlier froze no leaf and keeps cited-only
snapshots, byte for byte.

DepositRule runs the real driver (tests/fake_codex.sh) on a metadata-2.1 audit with a deposit, under the shipped
policy and under a policy snapshot without the leaf (what every older audit froze). ProviderSafetyScreens and
Bounds run the snapshot builder's rule step on audits written to disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
TESTS = SKILL / "tests"
DRIVER = SCRIPTS / "run_critique.sh"
FAKE_CODEX = TESTS / "fake_codex.sh"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import lifecycle_journal  # noqa: E402
import record_invocation  # noqa: E402
import snapshot_evidence_rules as rules  # noqa: E402
import test_run_critique_runtime as runtime  # noqa: E402  (the critique runtime helper, not a test)
import test_snapshot_mode_drift as drift  # noqa: E402

DEPOSIT = {
    "R scripts/analysis.R": b"x <- c(1, 2, 3)\nprint(mean(x))\n",
    "Study images/Setup photo.tif": b"II*\x00" + b"\x01" * 2048,
    "Study images/Figure 1.tif": b"II*\x00" + b"\x02" * 4096,
    "raw/SUBJ_P12.csv": b"trial,score\n1,4\n",   # a file named after a participant code
}
CITED = ("R scripts/analysis.R", "Study images/Setup photo.tif")
CODE_NAMED = "raw/SUBJ_P12.csv"
OVERSIZE = "raw/session.bin"   # larger than rules.MAX_FILE_BYTES: withheld, never read or copied
MISSING = "raw/gone.csv"       # in the manifest, not on disk
LISTING = "work/pkg_manifest.txt"   # intake's deposit listing, which every snapshot copies into its context
SNIPPETS = {"work/snippets/F1.R": b"expect(mean(c(1, 2, 3)) == 2)\n",
            "work/snippets/_prelude.R": b"helper <- function(x) x + 1\n"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def drop_leaf(audit: pathlib.Path) -> None:
    """Rewrite the frozen snapshot as every audit initialised before 2.7.11 froze it: no critique.snapshot_evidence."""
    path = audit / "pipeline-policy.snapshot.json"
    envelope = json.loads(path.read_text())
    envelope["policy"]["critique"].pop("snapshot_evidence")
    envelope["policy_sha256"] = record_invocation.policy_hash(envelope["policy"])
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def write_deposit_audit(audit: pathlib.Path, *, legacy: bool) -> subprocess.CompletedProcess[str]:
    """tests/test_run_critique.sh's fixture, plus a deposit, its intake manifest, a snippet and a finding citing two
    deposited files. Returns init_run.sh's result."""
    pkg = audit / "pkg"
    entries = []
    for relative, data in DEPOSIT.items():
        (pkg / relative).parent.mkdir(parents=True, exist_ok=True)
        (pkg / relative).write_bytes(data)
        entries.append({"path": relative, "size": len(data), "sha256": sha(data)})
    with (pkg / OVERSIZE).open("wb") as handle:
        handle.truncate(rules.MAX_FILE_BYTES + 1)
    entries.append({"path": OVERSIZE, "size": rules.MAX_FILE_BYTES + 1,
                    "sha256": hashlib.sha256((pkg / OVERSIZE).read_bytes()).hexdigest()})
    entries.append({"path": MISSING, "size": 9, "sha256": "c" * 64})
    entries.sort(key=lambda e: e["path"])
    (audit / "package_manifest.json").write_text(json.dumps({
        "schema_version": "2.0.0", "algorithm": "sha256", "package_sha256": "0" * 64,
        "file_count": len(entries), "file_types": {}, "files": entries}, indent=2) + "\n")
    (audit / "report").mkdir(parents=True)
    (audit / "work/snippets").mkdir(parents=True)
    for relative, data in SNIPPETS.items():
        (audit / relative).write_bytes(data)
    for name, text in (("PAPER.txt", "Example et al. (2026), deterministic runtime fixture.\n"),
                       ("ENV.txt", "python3 standard-library-only fixture\n"), ("SLOW.txt", "Do not run any long job.\n"),
                       ("WRAPUP.txt", "Review the current changed claims and their dependencies.\n")):
        (audit / name).write_text(text)
    report = audit / "report/REPORT_final.md"
    report.write_text("# Fixture report\n\nThe immutable audit draft contains one bounded claim.\n")
    row = {
        "schema_version": "2.0.0", "id": "F1", "claim": "The bounded fixture claim is correct.",
        "domain": "statistics", "source_location": {"path": "report/REPORT_final.md", "locator": "fixture claim"},
        "claim_ref": "fixture claim", "status": "draft",
        "ledger_disposition": {"status": "INCLUDED", "target": None, "reason": None},
        "coordinator_approved": False, "impact": "value_wrong", "impact_rank": 2,
        "verification_tier": 2, "paper_only": False, "confidence": "LIKELY",
        "evidence": {"source_hashes": [{"path": "report/REPORT_final.md", "sha256": sha(report.read_bytes())}]
                     + [{"path": "pkg/" + p, "sha256": sha(DEPOSIT[p])} for p in CITED],
                     "rerun_command": "sha256 report/REPORT_final.md", "expected": "bound fixture",
                     "observed": "bound fixture", "tolerance_basis": None},
        "unverifiable_basis": None, "verifications": [],
        "dependencies": {"environment_fingerprint": None, "finding_ids": [], "artifact_paths": []},
        "settled": None, "benign_explanations_ruled_out": [], "report_section": None,
        "verification_snippet": {"kind": "code", "path": "work/snippets/F1.R", "basis": "fixture check"},
        "disclosure_flag": False,
        "remedy": {"class": "one_number", "description": "Retain the bounded value.", "verified": None},
        "changes_conclusion": False, "changes_conclusion_which": None,
        "provenance": {"origin_role": "fixture", "independently_found_by": ["fixture"], "agreed_after_seeing": [],
                       "round_introduced": 0, "source_records": ["report/REPORT_final.md"]},
    }
    (audit / "work/findings.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n")
    frozen = subprocess.run([sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy", str(audit)],
                            capture_output=True, text=True, env=drift.env_without_refreeze(), check=False)
    if frozen.returncode != 0:
        raise AssertionError(f"record_invocation --init-policy failed:\n{frozen.stderr}")
    if legacy:
        drop_leaf(audit)   # init reuses the leftover, as it reuses any leftover whose values are live
    result = subprocess.run(["bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", "audit-deposit-21",
                             "--protocol-version", "test", "--gate-set-version", "test"],
                            capture_output=True, text=True, env=drift.env_without_refreeze(), check=False)
    if result.returncode != 0:
        raise AssertionError(f"init_run.sh failed:\n{result.stderr}")
    return result


class DepositRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="i4r-2711-deposit-")
        cls.base = pathlib.Path(cls.temporary.name).resolve()
        cls.snapshots, cls.audits, cls.inits = {}, {}, {}
        for label in ("new", "legacy"):
            audit = cls.base / label / "audit"
            audit.mkdir(parents=True)
            cls.inits[label] = write_deposit_audit(audit, legacy=label == "legacy")
            result = subprocess.run(["bash", str(DRIVER), "1"], capture_output=True, text=True, timeout=900,
                                    check=False, env=drift.env_without_refreeze(
                                        AUDIT_DIR=str(audit), CODEX=str(FAKE_CODEX),
                                        FAKE_CODEX_STATE=str(cls.base / label / "fake_state"),
                                        FAKE_CODEX_SCENARIO="success", I4R_INVOCATION_ID=f"dep-{label}",
                                        RETRY_BACKOFF_SECONDS="0", PROBE_LOCK_WAIT_SECONDS="3"))
            if result.returncode != 0:
                raise AssertionError(f"{label} round failed:\n{result.stderr[-4000:]}")
            status = json.loads((audit / "critique/round_1/ROUND_STATUS.json").read_text())
            cls.audits[label] = audit
            cls.snapshots[label] = audit / "critique/round_1/snapshots" / status["input_sha256"]

    @classmethod
    def tearDownClass(cls) -> None:
        drift.thaw(cls.base)
        cls.temporary.cleanup()

    def inventory(self, label: str) -> dict:
        return json.loads((self.snapshots[label] / "source/context/EVIDENCE_HASHES.json").read_text())

    def withheld(self) -> dict:
        return json.loads((self.snapshots["new"] / "source/context" / rules.WITHHELD_NAME).read_text())

    def test_a_new_audit_carries_the_snippets_and_the_uncited_deposit(self) -> None:
        inventory = self.inventory("new")
        for relative, data in DEPOSIT.items():
            if relative != CODE_NAMED:
                self.assertEqual(inventory[f"source/evidence/pkg/{relative}"]["sha256"], sha(data), relative)
        for relative, data in SNIPPETS.items():
            self.assertEqual(inventory[f"source/evidence/{relative}"]["sha256"], sha(data), relative)
            self.assertEqual((self.snapshots["new"] / "source/evidence" / relative).read_bytes(), data)
        self.assertIn("source/context/" + rules.WITHHELD_NAME, inventory, "a critic may cite the withheld list")
        packet = json.loads((self.snapshots["new"] / "source/role_packets/completeness.json").read_text())
        self.assertNotIn("source/evidence/pkg/Study images/Figure 1.tif",
                         packet["relevant_evidence_paths"], "the role packets still name the cited evidence only")

    def test_what_is_left_out_is_listed_and_a_code_shaped_name_is_never_written(self) -> None:
        records = self.withheld()["withheld"]
        named = [(row["path"], row["reason"]) for row in records if "path" in row]
        self.assertEqual(named, [("pkg/" + MISSING, rules.NOT_A_REGULAR_FILE),
                                 ("pkg/" + OVERSIZE, rules.FILE_OVER_CAP)])
        nameless = [row for row in records if "path" not in row]
        self.assertEqual(nameless, [{"reason": rules.NAME_NOT_PROVIDER_SAFE, "sha256": sha(DEPOSIT[CODE_NAMED]),
                                     "size": len(DEPOSIT[CODE_NAMED])}])
        self.assertFalse((self.snapshots["new"] / "source/evidence/pkg" / OVERSIZE).exists())
        self.assertFalse((self.snapshots["new"] / "source/evidence/pkg" / CODE_NAMED).exists())
        # the name is written nowhere in the snapshot: not in the inventory (the critic schema's path enum), the
        # withheld list, the rendered prompts and schemas, nor the context copy of work/pkg_manifest.txt, which
        # names every deposited file and is itself a row of the inventory (review NC-1)
        token = pathlib.PurePosixPath(CODE_NAMED).stem.encode()
        scanned = [path for path in sorted(self.snapshots["new"].rglob("*")) if path.is_file()]
        self.assertIn(self.snapshots["new"] / "source/context" / LISTING, scanned)
        for path in scanned:
            self.assertNotIn(token, path.read_bytes(), path.relative_to(self.snapshots["new"]).as_posix())
            self.assertNotIn(token, path.relative_to(self.snapshots["new"]).as_posix().encode())

    def test_the_deposit_listing_is_masked_for_a_new_audit_and_kept_for_an_older_one(self) -> None:
        audit_listing = (self.audits["new"] / LISTING).read_bytes()
        self.assertIn(b"  " + CODE_NAMED.encode() + b"\n", audit_listing, "the audit's own listing is unchanged")
        masked = (self.snapshots["new"] / "source/context" / LISTING).read_bytes()
        # only the code-named file's line changes: it keeps its sha256 (the withheld record's) and gets a placeholder
        expected = audit_listing.replace(b"  " + CODE_NAMED.encode() + b"\n", b"  raw/masked-file-001.csv\n")
        self.assertNotEqual(expected, audit_listing)
        self.assertEqual(masked, expected)
        self.assertIn(f"{sha(DEPOSIT[CODE_NAMED])}  raw/masked-file-001.csv\n".encode(), masked)
        self.assertEqual(self.inventory("new")["source/context/" + LISTING]["sha256"], sha(masked))
        # an audit without the leaf keeps the unmasked copy every snapshot has carried since 2.7.8, byte for byte
        legacy = (self.snapshots["legacy"] / "source/context" / LISTING).read_bytes()
        self.assertEqual(legacy, (self.audits["legacy"] / LISTING).read_bytes())
        self.assertIn(CODE_NAMED.encode(), legacy)

    def test_the_critic_schema_enum_admits_the_added_files(self) -> None:
        schema = json.loads((SKILL / "schemas/v2/critic-result.schema.json").read_text())
        runtime.constrain_evidence_bindings(schema, self.inventory("new"))
        paths = schema["$defs"]["artifact_binding"]["properties"]["path"]["enum"]
        self.assertIn("source/evidence/pkg/Study images/Figure 1.tif", paths)
        self.assertIn("source/evidence/work/snippets/F1.R", paths)
        self.assertLessEqual(len(paths), rules.MAX_INVENTORY_ENTRIES)
        self.assertLessEqual(sum(len(path) for path in paths), rules.MAX_INVENTORY_PATH_CHARS)

    def test_the_round_plan_names_the_added_data_classes(self) -> None:
        def classes(label: str) -> list:
            events = lifecycle_journal.load_events(self.audits[label])
            prepared = [e for e in events if e["type"] == "round_prepared"]
            return prepared[-1]["payload"]["plan"]["data_classes"]
        base = ["report", "canonical_findings", "deterministic_evidence", "package_metadata"]
        self.assertEqual(classes("legacy"), base)
        self.assertEqual(classes("new"), base + ["uncited_deposit_files", "verification_snippets"])

    def test_an_audit_without_the_leaf_keeps_cited_only_snapshots(self) -> None:
        inventory = self.inventory("legacy")
        carried = sorted(path for path in inventory if path.startswith("source/evidence/"))
        self.assertEqual(carried, sorted(["source/evidence/report/REPORT_final.md"]
                                         + [f"source/evidence/pkg/{p}" for p in CITED]))
        self.assertFalse((self.snapshots["legacy"] / "source/context" / rules.WITHHELD_NAME).exists())
        self.assertFalse((self.snapshots["legacy"] / "source/evidence/work").exists(), "no snippet without the leaf")
        # the two audits differ only in the added files and the withheld list
        added = {"source/evidence/" + p for p in SNIPPETS} | {
            f"source/evidence/pkg/{p}" for p in DEPOSIT if p not in CITED and p != CODE_NAMED}
        new_paths = set(self.inventory("new")) - added - {"source/context/" + rules.WITHHELD_NAME}
        self.assertEqual(new_paths, set(inventory))

    def test_init_prints_the_rule_and_warns_for_a_reused_leftover_without_the_leaf(self) -> None:
        self.assertIn("critic evidence:   cited files plus the deposit", self.inits["new"].stdout)
        self.assertIn("critic evidence:   cited files only", self.inits["legacy"].stdout)
        self.assertNotIn("froze no critique.snapshot_evidence", self.inits["new"].stderr)
        self.assertIn("froze no critique.snapshot_evidence", self.inits["legacy"].stderr)


def rule_workspace(root: pathlib.Path, *, leaf: bool = True) -> tuple[pathlib.Path, pathlib.Path]:
    """An audit directory with a frozen policy (with or without the leaf) and an empty snapshot source tree."""
    workspace, source = root / "audit", root / "source"
    workspace.mkdir(parents=True)
    critique = {"snapshot_evidence": rules.DEPOSIT_RULE} if leaf else {}
    (workspace / "pipeline-policy.snapshot.json").write_text(json.dumps({"policy": {"critique": critique}}))
    for folder in ("context", "evidence"):
        (source / folder).mkdir(parents=True)
    return workspace, source


def write_manifest(workspace: pathlib.Path, files: dict, declared: dict | None = None) -> None:
    entries = []
    for relative, data in files.items():
        (workspace / "pkg" / relative).parent.mkdir(parents=True, exist_ok=True)
        (workspace / "pkg" / relative).write_bytes(data)
        entries.append({"path": relative, "size": (declared or {}).get(relative, len(data)), "sha256": sha(data)})
    (workspace / "package_manifest.json").write_text(json.dumps({"files": entries}))


def write_rows(workspace: pathlib.Path, snippets: dict) -> None:
    (workspace / "work").mkdir(exist_ok=True)
    (workspace / "work/findings.jsonl").write_text("".join(
        json.dumps({"id": claim, "verification_snippet": {"kind": "code", "path": path, "basis": "fixture"}}) + "\n"
        for claim, path in snippets.items()))


def tree_bytes(source: pathlib.Path) -> bytes:
    return b"".join(path.as_posix().encode() + path.read_bytes() for path in sorted(source.rglob("*"))
                    if path.is_file())


class ProviderSafetyScreens(unittest.TestCase):
    """The coordinator's screens decide what is added: names (the 2.6.8 rule, deposit exemptions included, and the
    private-path pattern) for every candidate, and the provider-visible content screen for each snippet."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-2711-screens-")
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()

    def test_names_and_snippet_content_are_screened_and_failures_never_echoed(self) -> None:
        workspace, source = rule_workspace(self.root)
        private = "/" + "Users/" + "fixture-account/data.csv"
        # ITM_P05 is a column name of the deposit's own data and never a value: exempt, as the coordinator admits it
        write_manifest(workspace, {"data/scores.csv": b"trial,ITM_P05,score\n1,3,4\n2,5,6\n",
                                   "SUBJ_P07/trial.csv": b"trial,score\n1,2\n",
                                   "notes.txt": b"plain notes\n"})
        snippets = {
            "work/snippets/F1.R": "stopifnot(TRUE)\n",
            "work/snippets/F2.R": f'x <- read.csv("{private}")\n',
            "work/snippets/F3.R": 'subset(d, id == "SUBJ_P12")\n',
            "work/snippets/F4.R": "mean(d$ITM_P05)\n",
            "work/snippets/SUBJ_P13.R": "stopifnot(TRUE)\n",
            "work/snippets/_prelude.R": "helper <- function(x) x\n",
        }
        (workspace / "work/snippets").mkdir(parents=True)
        for relative, text in snippets.items():
            (workspace / relative).write_text(text)
        write_rows(workspace, {"F1": "work/snippets/F1.R", "F2": "work/snippets/F2.R", "F3": "work/snippets/F3.R",
                               "F4": "work/snippets/F4.R", "F5": "work/snippets/SUBJ_P13.R",
                               "F6": "../outside.R", "F7": "work/snippets/F7.R"})
        runtime.add_rule_evidence(workspace, source, {})
        carried = sorted(path.relative_to(source / "evidence").as_posix()
                         for path in (source / "evidence").rglob("*") if path.is_file())
        self.assertEqual(carried, ["pkg/data/scores.csv", "pkg/notes.txt", "work/snippets/F1.R",
                                   "work/snippets/F4.R", "work/snippets/_prelude.R"])
        records = json.loads((source / "context" / rules.WITHHELD_NAME).read_text())["withheld"]
        self.assertEqual([(row["path"], row["reason"]) for row in records if "path" in row],
                         [("work/snippets/F2.R", rules.CONTENT_NOT_PROVIDER_SAFE),
                          ("work/snippets/F3.R", rules.CONTENT_NOT_PROVIDER_SAFE),
                          ("work/snippets/F7.R", rules.NOT_A_REGULAR_FILE)])
        nameless = sorted((row["reason"], row["size"] or 0) for row in records if "path" not in row)
        # the code-named snippet and the file in the code-named deposit folder, 16 bytes each
        self.assertEqual(nameless, [(rules.INVALID_PATH, 0), (rules.NAME_NOT_PROVIDER_SAFE, 16),
                                    (rules.NAME_NOT_PROVIDER_SAFE, 16)])
        self.assertTrue(all(set(row) == {"sha256", "size", "reason"} for row in records if "path" not in row))
        everything = tree_bytes(source)
        for leaked in (b"SUBJ_P07", b"SUBJ_P13", b"SUBJ_P12", b"fixture-account", b"outside"):
            self.assertNotIn(leaked, everything, leaked)

    def test_a_private_path_in_a_deposit_name_is_withheld_without_the_name(self) -> None:
        workspace, source = rule_workspace(self.root)
        write_manifest(workspace, {"Users/fixture-account/data.csv": b"a\n1\n", "home/x.csv": b"b\n2\n"})
        runtime.add_rule_evidence(workspace, source, {})
        self.assertEqual(list((source / "evidence").rglob("*")), [])
        records = json.loads((source / "context" / rules.WITHHELD_NAME).read_text())["withheld"]
        self.assertEqual([row["reason"] for row in records], [rules.NAME_NOT_PROVIDER_SAFE] * 2)
        self.assertNotIn(b"fixture-account", tree_bytes(source))

    def test_a_deposited_file_changed_since_intake_stops_the_snapshot(self) -> None:
        workspace, source = rule_workspace(self.root)
        write_manifest(workspace, {"data.csv": b"a\n1\n"})
        (workspace / "pkg/data.csv").write_bytes(b"a\n2\n")
        with self.assertRaisesRegex(ValueError, "deposited file changed since intake: pkg/data.csv"):
            runtime.add_rule_evidence(workspace, source, {})

    def test_without_the_leaf_nothing_is_read_or_written(self) -> None:
        workspace, source = rule_workspace(self.root, leaf=False)
        write_manifest(workspace, {"data.csv": b"a\n1\n"})
        write_rows(workspace, {"F1": "work/snippets/F1.R"})
        (workspace / "package_manifest.json").write_text("not json")   # would refuse if it were read
        runtime.add_rule_evidence(workspace, source, {})
        self.assertEqual(tree_bytes(source), b"")
        # the context copy of the deposit listing keeps its bytes, code-shaped names and all (as since 2.7.8)
        listing = source / "context" / LISTING
        listing.parent.mkdir(parents=True)
        listing.write_bytes(b"a" * 64 + b"  raw/SUBJ_P12.csv\n")
        runtime.add_rule_evidence(workspace, source, {})
        self.assertEqual(listing.read_bytes(), b"a" * 64 + b"  raw/SUBJ_P12.csv\n")

    def test_the_deposit_listing_copy_is_masked_with_the_coordinators_rule(self) -> None:
        workspace, source = rule_workspace(self.root)
        # ITM_P05 is a column name of the deposit's own data and never a value: exempt, as the coordinator admits it
        write_manifest(workspace, {"data/scores.csv": b"trial,ITM_P05,score\n1,3,4\n2,5,6\n"})
        lines = [("a", "data/scores.csv"), ("b", "data/ITM_P05.csv"), ("c", "SUBJ_P07/trial.csv"),
                 ("d", "SUBJ_P07/SUBJ_P07_b.csv"), ("e", "raw/SUBJ_P12.txt"),
                 ("f", "Users/fixture-account/data.csv"), ("0", "x/home/y.csv")]
        listing = source / "context" / LISTING
        listing.parent.mkdir(parents=True)
        listing.write_text("".join(f"{digit * 64}  {path}\n" for digit, path in lines) + "note SUBJ_P13\n")
        runtime.add_rule_evidence(workspace, source, {})
        self.assertEqual(listing.read_text().split("\n"), [
            "a" * 64 + "  data/scores.csv",
            "b" * 64 + "  data/ITM_P05.csv",                       # an exempt deposit variable name stays
            "c" * 64 + "  masked-dir-001/trial.csv",                # a code-named folder, stable for every file in it
            "d" * 64 + "  masked-dir-001/masked-file-001.csv",
            "e" * 64 + "  raw/masked-file-002.txt",
            "f" * 64 + "  masked-file-003.csv",                     # the private-path pattern spans components
            "0" * 64 + "  masked-file-004.csv",
            "masked-file-005",                                      # not a listing line: screened whole
            ""])
        for leaked in (b"SUBJ_P", b"fixture-account", b"Users", b"home"):
            self.assertNotIn(leaked, tree_bytes(source), leaked)
        # a listing with nothing to mask is left as it was copied
        workspace, source = rule_workspace(self.root / "clean")
        write_manifest(workspace, {"data.csv": b"a\n1\n"})
        listing = source / "context" / LISTING
        listing.parent.mkdir(parents=True)
        listing.write_bytes(b"a" * 64 + b"  data.csv\n")
        runtime.add_rule_evidence(workspace, source, {})
        self.assertEqual(listing.read_bytes(), b"a" * 64 + b"  data.csv\n")


class Bounds(unittest.TestCase):
    """The rule's bounds keep the critic schema's evidence enums inside the provider's structured-output limits."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-2711-bounds-")
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()

    @staticmethod
    def added(source: pathlib.Path) -> list:
        return [path.name for path in (source / "evidence").rglob("*") if path.is_file()]

    @staticmethod
    def records(source: pathlib.Path) -> list:
        return json.loads((source / "context" / rules.WITHHELD_NAME).read_text())["withheld"]

    def test_the_inventory_row_bound(self) -> None:
        workspace, source = rule_workspace(self.root)
        write_manifest(workspace, {f"d/{index:03d}.csv": f"{index}\n".encode() for index in range(300)})
        runtime.add_rule_evidence(workspace, source, {})
        # two rows are the report's and the withheld list's
        self.assertEqual(len(self.added(source)), rules.MAX_INVENTORY_ENTRIES - 2)
        self.assertEqual({row["reason"] for row in self.records(source)}, {rules.INVENTORY_FULL})
        self.assertEqual(len(self.records(source)), 300 - rules.MAX_INVENTORY_ENTRIES + 2)
        # the provider-facing invariant itself (review DT-6): the critic schema built from this full inventory keeps
        # both evidence enums under the provider's 250-value structured-output limit
        self.assertLess(rules.MAX_INVENTORY_ENTRIES, 250)
        (source / "REPORT_TO_REVIEW.md").write_text("# Fixture report\n")
        inventory = {"source/" + path.relative_to(source).as_posix(): {"sha256": sha(path.read_bytes()),
                                                                       "size": path.stat().st_size}
                     for path in source.rglob("*") if path.is_file()}
        self.assertEqual(len(inventory), rules.MAX_INVENTORY_ENTRIES)
        schema = json.loads((SKILL / "schemas/v2/critic-result.schema.json").read_text())
        runtime.constrain_evidence_bindings(schema, inventory)
        binding = schema["$defs"]["artifact_binding"]["properties"]
        self.assertEqual(len(binding["sha256"]["enum"]), len(inventory), "every carried file has its own hash")
        self.assertLess(len(binding["path"]["enum"]), 250)
        self.assertLess(len(binding["sha256"]["enum"]), 250)

    def test_the_inventory_path_character_bound(self) -> None:
        workspace, source = rule_workspace(self.root)
        folder = "a-long-deposit-folder-name-" * 6
        write_manifest(workspace, {f"{folder}/{index:03d}.csv": b"x\n" for index in range(120)})
        runtime.add_rule_evidence(workspace, source, {})
        listed = ["source/REPORT_TO_REVIEW.md", "source/context/" + rules.WITHHELD_NAME] + [
            "source/" + path.relative_to(source).as_posix()
            for path in (source / "evidence").rglob("*") if path.is_file()]
        self.assertLessEqual(sum(len(path) for path in listed), rules.MAX_INVENTORY_PATH_CHARS)
        self.assertLess(len(listed), 100, "the character bound bit before the row bound")
        self.assertEqual({row["reason"] for row in self.records(source)}, {rules.INVENTORY_FULL})

    def test_the_byte_bounds_and_the_order(self) -> None:
        workspace, source = rule_workspace(self.root)
        # declared sizes: a file over the per-file cap is withheld unread, so its bytes on disk do not matter
        write_manifest(workspace, {"big.bin": b"x", "small.csv": b"y\n"}, {"big.bin": rules.MAX_FILE_BYTES + 1})
        runtime.add_rule_evidence(workspace, source, {})
        self.assertEqual(self.added(source), ["small.csv"])
        self.assertEqual([(row["path"], row["reason"]) for row in self.records(source)],
                         [("pkg/big.bin", rules.FILE_OVER_CAP)])
        # the per-snapshot cap and the order (snippets first, then the deposit smallest first)
        mib = 1024 * 1024
        carried, withheld = rules.plan_additions(
            [{"path": "work/snippets/F1.R", "size": 10, "sha256": "a" * 64}],
            [{"path": f"pkg/{index}.bin", "size": 20 * mib + index, "sha256": "b" * 64} for index in range(6)],
            2, 40)
        self.assertEqual([row["path"] for row in carried],
                         ["work/snippets/F1.R", "pkg/0.bin", "pkg/1.bin", "pkg/2.bin", "pkg/3.bin"])
        self.assertEqual([row["reason"] for row in withheld], [rules.SNAPSHOT_OVER_CAP] * 2)


class DriftPinsTheRule(unittest.TestCase):
    """config/check_policy_drift.py pins the shipped rule and the four bounds that belong to it (a new bound is a new
    rule name): an audit that froze cited_plus_deposit_1 keeps the snapshots it was initialised with (review DT-6)."""

    def drift(self, old: str, new: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="i4r-2711-rule-drift-") as name:
            tree = pathlib.Path(name) / "i4r-audit"
            shutil.copytree(SKILL, tree, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"), symlinks=True)
            if old:
                path = tree / "scripts/snapshot_evidence_rules.py"
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count(old), 1, old)
                path.write_text(text.replace(old, new), encoding="utf-8")
            return subprocess.run([sys.executable, str(tree / "config/check_policy_drift.py")], capture_output=True,
                                  text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)

    def test_the_rule_and_its_bounds_are_pinned(self) -> None:
        self.assertEqual(self.drift("", "").returncode, 0)
        for old, new, message in (
                ("MAX_INVENTORY_ENTRIES = 230", "MAX_INVENTORY_ENTRIES = 260", "bounds"),
                ("MAX_INVENTORY_PATH_CHARS = 15_000", "MAX_INVENTORY_PATH_CHARS = 16_000", "bounds"),
                ("MAX_FILE_BYTES = 25 * 1024 * 1024", "MAX_FILE_BYTES = 26 * 1024 * 1024", "bounds"),
                ("MAX_ADDED_BYTES = 100 * 1024 * 1024", "MAX_ADDED_BYTES = 200 * 1024 * 1024", "bounds"),
                ('DEPOSIT_RULE = "cited_plus_deposit_1"', 'DEPOSIT_RULE = "cited_plus_deposit_2"', "SHIPPED_RULE")):
            with self.subTest(new=new):
                result = self.drift(old, new)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(message, result.stdout + result.stderr)


class RuleValues(unittest.TestCase):
    def test_unknown_values_are_refused_and_the_legacy_value_is_never_frozen(self) -> None:
        for value in (rules.LEGACY_RULE, "everything", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(rules.RuleError):
                    rules.rule_of({"critique": {"snapshot_evidence": value}})
        self.assertEqual(rules.rule_of({"critique": {}}), rules.LEGACY_RULE)

    def test_init_refuses_a_leftover_snapshot_with_an_unknown_rule(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-2711-leftover-") as name:
            audit = pathlib.Path(name).resolve() / "audit"
            (audit / "pkg").mkdir(parents=True)
            (audit / "pkg/data.csv").write_text("a\n1\n")
            frozen = subprocess.run([sys.executable, str(SCRIPTS / "record_invocation.py"), "--init-policy",
                                     str(audit)], capture_output=True, text=True, env=drift.env_without_refreeze())
            self.assertEqual(frozen.returncode, 0, frozen.stderr)
            path = audit / "pipeline-policy.snapshot.json"
            envelope = json.loads(path.read_text())
            envelope["policy"]["critique"]["snapshot_evidence"] = "everything"
            envelope["policy_sha256"] = record_invocation.policy_hash(envelope["policy"])
            path.write_text(json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            result = subprocess.run(["bash", str(SCRIPTS / "init_run.sh"), str(audit), "--audit-id", "audit-left-21",
                                     "--protocol-version", "test", "--gate-set-version", "test"],
                                    capture_output=True, text=True, env=drift.env_without_refreeze())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("freezes a snapshot-evidence rule this skill does not run", result.stderr)
            self.assertFalse((audit / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
