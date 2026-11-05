"""2.7.9: registry/index.jsonl no longer loses rows across hosts.

Every filing read registry/index.jsonl, changed it and rewrote it under a host-local flock. A host whose
Dropbox copy lagged wrote an index without the rows other hosts had filed, Dropbox kept one version, and
eleven filed audits lost their row: `finalization_receipt.py check --require-registry` (and so publish)
refused each of them for good. A filing now also writes registry/<audit_id>/index_row.json, which only that
audit's filing writes and which `check` reads; index.jsonl is rebuilt as a view of every row the registry
holds, conflicted copies included. `registry-audit` lists what is missing and `registry-recover` restores a
row only when it hashes to the receipt.

Each test builds the registry state on disk and runs the real CLI.
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
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
RECEIPT = SKILL / "scripts" / "finalization_receipt.py"
STATUS = SKILL / "scripts" / "status.py"
CONFLICT = "index (Someone's conflicted copy 2026-10-18).jsonl"
# scripts/finalization_receipt.py exactly as 2.7.8 shipped it (pinned below): a teammate whose install lags
# re-files with it. It imports its unchanged helpers from scripts/.
RECEIPT_278 = SKILL / "tests" / "fixtures" / "finalization_receipt_2_7_8.py"
RECEIPT_278_SHA256 = "63256dbbf919f635d8b5e70ed64222e30aad54f88c0af625079b35a32ec455a7"
# A host whose clock runs three minutes ahead files with this (the version stamp is the host's clock).
FAST_CLOCK = """
import datetime as real, pathlib, sys
sys.path.insert(0, sys.argv[1])
import finalization_receipt
class Ahead(real.datetime):
    @classmethod
    def now(cls, tz=None):
        return real.datetime.now(tz) + real.timedelta(minutes=3)
class Clock:
    datetime, timezone, timedelta = Ahead, real.timezone, real.timedelta
finalization_receipt.dt = Clock
finalization_receipt.create(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
"""


def row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def rows(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def snapshot(root: pathlib.Path) -> dict[str, tuple[bytes, int]]:
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else b"<dir>", path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))}


class RegistryAcrossHosts(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="i4r-279-registry-")
        self.addCleanup(directory.cleanup)
        self.base = pathlib.Path(directory.name).resolve()
        self.registry = self.base / "tree" / "registry"
        self.registry.mkdir(parents=True)

    # -- fixtures ----------------------------------------------------------------------------------
    def audit(self, audit_id: str, findings: int = 3) -> pathlib.Path:
        """The smallest finalized (legacy-schema) audit `create` and `check` accept."""
        root = self.base / "tree" / "runs" / "Journal" / audit_id / "audit"
        (root / "report").mkdir(parents=True, exist_ok=True)
        (root / "run.json").write_text(json.dumps({
            "schema_version": "1.0.0", "audit_id": audit_id, "status": "complete",
            "ended_at": "2026-10-18T00:00:00Z", "paper_doi": "10.5555/" + audit_id, "rounds_run": 2,
            "stopping_reason": "converged", "compliance": {"compliance_pass": True},
        }, sort_keys=True) + "\n", encoding="utf-8")
        self.set_findings(root, findings)
        (root / "verification_manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "report" / "REPORT_final.md").write_text("# Audit\n", encoding="utf-8")
        return root

    def set_findings(self, root: pathlib.Path, findings: int) -> None:
        (root / "comment.json").write_text(json.dumps({
            "audit_id": json.loads((root / "run.json").read_text())["audit_id"],
            "bibliographic": {"journal": "Journal"}, "rollup": {"findings_total": findings},
        }, sort_keys=True) + "\n", encoding="utf-8")

    def cli(self, *arguments: object) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(RECEIPT), *[str(a) for a in arguments]],
                              text=True, capture_output=True, timeout=60)

    def file(self, root: pathlib.Path, registry: pathlib.Path | None = None) -> dict:
        result = self.cli("create", root, "--registry", registry or self.registry)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((root / "finalization_receipt.json").read_text())

    def check(self, root: pathlib.Path) -> subprocess.CompletedProcess:
        return self.cli("check", root, "--require-registry")

    def cli_278(self, *arguments: object) -> subprocess.CompletedProcess:
        self.assertEqual(RECEIPT_278_SHA256, hashlib.sha256(RECEIPT_278.read_bytes()).hexdigest())
        return subprocess.run([sys.executable, str(RECEIPT_278), *[str(a) for a in arguments]],
                              text=True, capture_output=True, timeout=60,
                              env=dict(os.environ, PYTHONPATH=str(SKILL / "scripts")))

    def status_attention(self, audit_id: str) -> list[str]:
        result = subprocess.run([sys.executable, str(STATUS), self.base / "tree", "--json"],
                                text=True, capture_output=True, timeout=60)
        return next(row for row in json.loads(result.stdout) if row["audit_id"] == audit_id)["attention"]

    def audit_lines(self, *arguments: object) -> tuple[int, dict[str, str], str]:
        """registry-audit: its exit code, its per-audit lines keyed by audit id, and its whole output."""
        result = self.cli("registry-audit", *(arguments or (self.registry,)))
        lines = {line.split()[1].rstrip(":"): line for line in result.stdout.splitlines() if line.startswith("  ")}
        return result.returncode, lines, result.stdout + result.stderr

    def row_in_index(self, audit_id: str) -> list[dict]:
        return [row for row in rows(self.registry / "index.jsonl") if row["audit_id"] == audit_id]

    def as_legacy(self, *audit_ids: str) -> None:
        """What a pre-2.7.9 filing left: no per-audit row file, only the index.jsonl row."""
        for audit_id in audit_ids:
            (self.registry / audit_id / "index_row.json").unlink(missing_ok=True)

    def drop_rows(self, *audit_ids: str) -> list[str]:
        """Rewrite index.jsonl without these audits, as a lagging host's filing did. Returns the lines removed."""
        index = self.registry / "index.jsonl"
        lines = index.read_text(encoding="utf-8").splitlines(keepends=True)
        removed = [line for line in lines if json.loads(line)["audit_id"] in audit_ids]
        index.write_text("".join(line for line in lines if line not in removed), encoding="utf-8")
        return removed

    # -- the defect --------------------------------------------------------------------------------
    def test_a_lagging_hosts_filing_no_longer_unfiles_another_hosts_audit(self) -> None:
        earlier = self.audit("20261017-audit-earlier")
        self.file(earlier)
        lagging_copy = self.base / "host-b-copy"
        shutil.copytree(self.registry, lagging_copy)      # host B's Dropbox copy stops here

        filed_by_a = self.audit("20261018-audit-hosta")
        receipt_a = self.file(filed_by_a)
        self.assertEqual(0, self.check(filed_by_a).returncode)

        # Host B files with its lagging copy. Dropbox then syncs the per-audit folders, which have one
        # writer each, and keeps B's index.jsonl, the later write, with no conflicted copy (the worst case).
        host_a_registry = self.base / "host-a-registry"
        self.registry.rename(host_a_registry)
        shutil.copytree(lagging_copy, self.registry)
        filed_by_b = self.audit("20261018-audit-hostb")
        self.file(filed_by_b)
        shutil.copytree(host_a_registry / "20261018-audit-hosta", self.registry / "20261018-audit-hosta")
        self.assertNotIn("20261018-audit-hosta", [row["audit_id"] for row in rows(self.registry / "index.jsonl")])

        # 2.7.8: "registry index does not contain exactly one row for this audit", and publish refused it for good.
        result = self.check(filed_by_a)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        own = self.registry / "20261018-audit-hosta" / "index_row.json"
        self.assertEqual(receipt_a["registry"]["index_row_sha256"], row_hash(json.loads(own.read_text())))

        # The next filing, from any host, writes the row back into the view.
        later = self.audit("20261019-audit-later")
        self.file(later)
        # one row each, in filing order whichever host wrote the view
        self.assertEqual(
            ["20261017-audit-earlier", "20261018-audit-hosta", "20261018-audit-hostb", "20261019-audit-later"],
            [row["audit_id"] for row in rows(self.registry / "index.jsonl")])
        for root in (earlier, filed_by_a, filed_by_b, later):
            self.assertEqual(0, self.check(root).returncode, root)

    def test_the_audits_own_row_file_holds_the_index_line_and_decides_check(self) -> None:
        root = self.audit("20261018-audit-own")
        receipt = self.file(root)
        own = self.registry / "20261018-audit-own" / "index_row.json"
        index = self.registry / "index.jsonl"
        # exactly the bytes index.jsonl holds for the row, and the hash the receipt binds
        self.assertEqual(own.read_bytes(), index.read_bytes())
        self.assertEqual(receipt["registry"]["index_row_sha256"], row_hash(json.loads(own.read_bytes())))

        line = index.read_bytes()
        for state, content in (("no row", b""), ("two rows", line + line)):
            with self.subTest(index=state):
                index.write_bytes(content)
                result = self.check(root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        tampered = json.loads(own.read_text())
        tampered["findings_total"] = 99
        own.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        result = self.check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("index row no longer matches the finalization receipt", result.stderr)

        tampered["findings_total"], tampered["audit_id"] = 3, "20261018-audit-other"
        own.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        result = self.check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not hold this audit's row", result.stderr)

        own.unlink()
        (self.base / "elsewhere.json").write_bytes(line)
        own.symlink_to(self.base / "elsewhere.json")
        result = self.check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or symlinked", result.stderr)

    def test_receipts_filed_before_the_change_still_check_against_index_jsonl(self) -> None:
        root = self.audit("20261001-audit-legacy")
        self.file(root)
        self.as_legacy("20261001-audit-legacy")
        self.assertEqual(0, self.check(root).returncode)          # the index.jsonl row, exactly as before
        removed = self.drop_rows("20261001-audit-legacy")
        result = self.check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registry index does not contain exactly one row for this audit", result.stderr)
        self.assertIn("registry-audit", result.stderr)              # and now says where to look
        (self.registry / "index.jsonl").write_text("".join(removed * 2))
        self.assertNotEqual(0, self.check(root).returncode)          # two rows still refuse

    def test_rows_in_conflicted_copies_are_taken_back_and_the_copies_left_alone(self) -> None:
        lost, refiled, other = (self.audit(name) for name in (
            "20261008-audit-lost", "20261009-audit-refiled", "20261010-audit-other"))
        for root in (lost, refiled, other):
            self.file(root)
        removed_audit = self.audit("20261009-audit-removed")
        self.file(removed_audit)
        self.as_legacy("20261008-audit-lost", "20261009-audit-refiled", "20261010-audit-other",
                       "20261009-audit-removed")
        # Dropbox kept the full index as a conflicted copy; the lagging host's index lacks one row
        conflict = self.registry / CONFLICT
        shutil.copyfile(self.registry / "index.jsonl", conflict)
        with conflict.open("a", encoding="utf-8") as handle:
            handle.write('{"audit_id": "20261010-audit-cut-off", "ver')    # a copy is salvage: a bad line never blocks filing
        lost_line = self.drop_rows("20261008-audit-lost")[0]
        # an audit taken out of the registry (folder and row) must not come back from an old copy
        self.drop_rows("20261009-audit-removed")
        shutil.rmtree(self.registry / "20261009-audit-removed")
        conflict_bytes = conflict.read_bytes()
        self.assertNotEqual(0, self.check(lost).returncode)

        # the next filing takes the row back; an audit re-filed since the copy was made keeps its new row
        self.set_findings(refiled, 5)
        new_receipt = self.file(refiled)
        later = self.audit("20261011-audit-later")
        self.file(later)
        index = rows(self.registry / "index.jsonl")
        ids = [row["audit_id"] for row in index]
        self.assertEqual(sorted(ids), ["20261008-audit-lost", "20261009-audit-refiled", "20261010-audit-other",
                                       "20261011-audit-later"])
        self.assertIn(lost_line.encode(), (self.registry / "index.jsonl").read_bytes())   # byte for byte
        refiled_row = next(row for row in index if row["audit_id"] == "20261009-audit-refiled")
        self.assertEqual(new_receipt["registry"]["version"], refiled_row["version"])
        self.assertEqual(5, refiled_row["findings_total"])
        # in filing order, so any host holding the same rows writes the same bytes
        self.assertEqual(sorted(index, key=lambda row: (row["filed_at"], row["version"])), index)
        self.assertEqual(conflict_bytes, conflict.read_bytes())
        self.assertEqual([CONFLICT], [path.name for path in self.registry.glob("*conflicted copy*")])
        for root in (lost, refiled, other, later):
            self.assertEqual(0, self.check(root).returncode, root)

    def test_one_row_per_audit_and_re_filing_replaces_its_own_row(self) -> None:
        first, second = self.audit("20261012-audit-first"), self.audit("20261012-audit-second")
        old_first = self.file(first)
        self.file(second)
        index = self.registry / "index.jsonl"
        first_line = next(line for line in index.read_text().splitlines(keepends=True)
                          if "20261012-audit-first" in line)
        stale = json.loads(first_line)
        stale["version"], stale["filed_at"] = "20200101T000000.000000Z-000000000000", "2020-01-01T00:00:00Z"
        second_line = next(line for line in index.read_text().splitlines(keepends=True)
                           if "20261012-audit-second" in line)
        skewed = json.loads(second_line)   # a row stamped by a host whose clock ran ahead
        skewed["version"], skewed["filed_at"] = "20991231T000000.000000Z-000000000000", "2099-12-31T00:00:00Z"
        # two copies of one audit's row, and an older filing of it, as hand merges have left
        index.write_text(index.read_text() + first_line + json.dumps(stale, sort_keys=True) + "\n"
                         + json.dumps(skewed, sort_keys=True) + "\n")
        self.set_findings(second, 7)
        self.file(second)
        self.file(second)
        listed = rows(index)
        self.assertEqual(["20261012-audit-first", "20261012-audit-second"], [row["audit_id"] for row in listed])
        self.assertEqual(old_first["registry"]["version"], listed[0]["version"])
        self.assertEqual(json.loads((self.registry / "20261012-audit-second" / "index_row.json").read_text()),
                         listed[1])
        self.assertEqual(0, self.check(first).returncode)

    # -- the operator commands -----------------------------------------------------------------
    def test_registry_audit_lists_what_is_missing_and_writes_nothing(self) -> None:
        names = ("20261001-audit-fine", "20261002-audit-incopy", "20261003-audit-nowhere", "20261004-audit-lagging")
        roots = {name: self.audit(name) for name in names}
        for root in roots.values():
            self.file(root)
        self.as_legacy("20261002-audit-incopy", "20261003-audit-nowhere")
        (self.registry / CONFLICT).write_text("".join(
            line for line in (self.registry / "index.jsonl").read_text().splitlines(keepends=True)
            if "audit-nowhere" not in line))
        self.drop_rows("20261002-audit-incopy", "20261003-audit-nowhere", "20261004-audit-lagging")
        (self.registry / "_spec").mkdir()                       # not an audit
        (self.registry / "_spec" / "SCHEME.md").write_text("notes\n")
        before = snapshot(self.registry)

        result = self.cli("registry-audit", self.registry)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("4 filed audit(s), 1 row(s) in index.jsonl, 2 index_row.json, 1 conflicted", result.stdout)
        lines = {line.split()[1]: line for line in result.stdout.splitlines() if line.startswith("  ")}
        self.assertEqual({"20261002-audit-incopy:", "20261003-audit-nowhere:", "20261004-audit-lagging:"}, set(lines))
        self.assertIn("MISSING", lines["20261002-audit-incopy:"])
        self.assertIn(CONFLICT, lines["20261002-audit-incopy:"])
        self.assertIn("MISSING", lines["20261003-audit-nowhere:"])
        self.assertIn("no index file holds it", lines["20261003-audit-nowhere:"])
        self.assertIn("lagging", lines["20261004-audit-lagging:"])
        self.assertEqual(before, snapshot(self.registry))

    def test_registry_recover_writes_only_a_row_the_receipt_hashes_to(self) -> None:
        incopy, nowhere, broken = (self.audit(name) for name in (
            "20261002-audit-incopy", "20261003-audit-nowhere", "20261005-audit-broken"))
        receipts = {root: self.file(root) for root in (incopy, nowhere, broken)}
        superseded = self.audit("20261006-audit-refiled")
        self.file(superseded)
        old_receipt = superseded.parent / "old_receipt.json"
        shutil.copyfile(superseded / "finalization_receipt.json", old_receipt)
        self.set_findings(superseded, 4)
        self.file(superseded)
        self.as_legacy("20261002-audit-incopy", "20261003-audit-nowhere", "20261005-audit-broken")
        full = (self.registry / "index.jsonl").read_text().splitlines(keepends=True)
        removed = self.drop_rows("20261002-audit-incopy", "20261003-audit-nowhere", "20261005-audit-broken")
        incopy_line = next(line for line in removed if "audit-incopy" in line)
        # The conflicted copy holds the in-copy row, no row for audit-nowhere, and a tampered row for the broken
        # audit, whose filed pair is gone too: nothing can prove a row for it.
        (self.registry / CONFLICT).write_text("".join(
            line.replace('"findings_total": 3', '"findings_total": 30') if "audit-broken" in line else line
            for line in full if "audit-nowhere" not in line))
        (self.registry / "20261005-audit-broken" / "versions").rename(self.registry / "20261005-audit-broken" / "gone")
        for root in (incopy, nowhere, broken):
            self.assertNotEqual(0, self.check(root).returncode)

        # refused: a receipt that is not the audit's current filing, and a tampered or unprovable row
        before = snapshot(self.registry)
        for target, reason in ((old_receipt, "not the audit's current registry filing"),
                               (broken, "no candidate row hashes to the receipt")):
            with self.subTest(target=target.name):
                result = self.cli("registry-recover", self.registry, target)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(reason, result.stderr)
        self.assertEqual(before, snapshot(self.registry))

        # recovered: from the conflicted copy (byte for byte), and rebuilt from the filed pair; a receipt
        # file outside the audit (the published metadata/ copy) serves as well as the audit directory
        published = self.base / "metadata-copy.json"
        shutil.copyfile(nowhere / "finalization_receipt.json", published)
        result = self.cli("registry-recover", self.registry, incopy, published)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("20261002-audit-incopy: recovered index_row.json from " + CONFLICT, result.stdout)
        self.assertIn("20261003-audit-nowhere: recovered index_row.json from versions/", result.stdout)
        self.assertEqual(incopy_line.encode(),
                         (self.registry / "20261002-audit-incopy" / "index_row.json").read_bytes())
        for root in (incopy, nowhere):
            own = json.loads((self.registry / root.parent.name / "index_row.json").read_text())
            self.assertEqual(receipts[root]["registry"]["index_row_sha256"], row_hash(own))
            self.assertEqual(0, self.check(root).returncode, root)
        self.assertIn("20261003-audit-nowhere", [row["audit_id"] for row in rows(self.registry / "index.jsonl")])
        self.assertFalse((self.registry / "20261005-audit-broken" / "index_row.json").exists())
        self.assertNotEqual(0, self.check(broken).returncode)

        again = self.cli("registry-recover", self.registry, incopy)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already holds the receipt's row", again.stdout)
        own = self.registry / "20261003-audit-nowhere" / "index_row.json"
        own.write_text(own.read_text().replace('"findings_total": 3', '"findings_total": 9'))
        refused = self.cli("registry-recover", self.registry, nowhere)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("differs from the receipt; not overwritten", refused.stderr)

    def test_status_flags_a_filed_audit_no_index_row_holds(self) -> None:
        root = self.audit("20261007-audit-unrowed")
        self.file(root)
        self.as_legacy("20261007-audit-unrowed")
        shutil.copyfile(self.registry / "index.jsonl", self.registry / CONFLICT)
        self.drop_rows("20261007-audit-unrowed")
        old = time.time() - 3600
        (root / "run.events.jsonl").write_text("")
        os.utime(root / "run.events.jsonl", (old, old))

        result = subprocess.run([sys.executable, str(STATUS), self.base / "tree", "--json"],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        row = next(row for row in json.loads(result.stdout) if row["audit_id"] == "20261007-audit-unrowed")
        self.assertTrue(any("no index row holds it" in why for why in row["attention"]), row["attention"])
        text = subprocess.run([sys.executable, str(STATUS), self.base / "tree"],
                              text=True, capture_output=True, timeout=60)
        self.assertIn("registry: 1 filed audit(s) without an index row, 1 Dropbox conflicted copies", text.stdout)

        recovered = self.cli("registry-recover", self.registry, root)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        result = subprocess.run([sys.executable, str(STATUS), self.base / "tree", "--json"],
                                text=True, capture_output=True, timeout=60)
        row = next(row for row in json.loads(result.stdout) if row["audit_id"] == "20261007-audit-unrowed")
        self.assertFalse(any("index row" in why for why in row["attention"]), row["attention"])

    # -- review round 1 ----------------------------------------------------------------------------
    def test_a_re_filing_from_2_7_8_leaves_a_stale_row_file_that_no_longer_refuses_the_audit(self) -> None:
        audit_id = "20261020-audit-mixed"
        root = self.audit(audit_id)
        first = self.file(root)
        # A teammate still on 2.7.8 re-files the audit (a correction): a new version, current.json and index.jsonl
        # row, and the receipt; 2.7.8 does not know index_row.json, so the 2.7.9 filing's row stays behind.
        self.set_findings(root, 4)
        result = self.cli_278("create", root, "--registry", self.registry)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads((root / "finalization_receipt.json").read_text())["registry"]
        own = self.registry / audit_id / "index_row.json"
        self.assertEqual(first["registry"]["version"], json.loads(own.read_text())["version"])
        self.assertNotEqual(first["registry"]["version"], receipt["version"])
        self.assertEqual(0, self.cli_278("check", root, "--require-registry").returncode)

        # 2.7.9 refused it here for good ("registry index row no longer matches the finalization receipt"), and
        # registry-recover would not replace the file. index.jsonl holds the receipt's row, so it decides.
        result = self.check(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("index_row.json is from an earlier filing", result.stderr)    # not a hidden pass
        code, lines, output = self.audit_lines()
        self.assertEqual(1, code, output)
        self.assertIn("STALE", lines[audit_id])
        self.assertIn("index.jsonl holds its current row", lines[audit_id])
        self.assertTrue(any("earlier filing" in why for why in self.status_attention(audit_id)))

        # Never open: the row index.jsonl gives must still hash to the receipt, and there must be one.
        index = self.registry / "index.jsonl"
        held = index.read_bytes()
        index.write_text(held.decode().replace('"findings_total": 4', '"findings_total": 9'))
        result = self.check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("index row no longer matches the finalization receipt", result.stderr)
        self.assertIn("registry-recover", result.stderr)
        self.drop_rows(audit_id)
        result = self.check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not contain exactly one row for this audit (its index_row.json is from an earlier filing",
                      result.stderr)
        code, lines, output = self.audit_lines()
        self.assertIn("does not hold its current row either", lines[audit_id])
        # status.py agrees: the stale row file holds nothing, so publish refuses it (it counted the file as a row)
        self.assertTrue(any("no index row holds its current filing" in why for why in self.status_attention(audit_id)))

        # registry-recover replaces the stale file, still only with the row the receipt hashes to
        result = self.cli("registry-recover", self.registry, root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("replaced a stale one", result.stdout)
        self.assertEqual(receipt["index_row_sha256"], row_hash(json.loads(own.read_text())))
        result = self.check(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("earlier filing", result.stderr)
        self.assertEqual(0, self.audit_lines()[0])
        self.assertFalse(any("index_row.json" in why for why in self.status_attention(audit_id)))

        # The same dead end from a sync conflict: Dropbox kept an earlier filing's index_row.json beside a newer
        # current.json, both written on 2.7.9.
        earlier = own.read_bytes()
        self.set_findings(root, 5)
        self.file(root)
        own.write_bytes(earlier)
        self.assertEqual(0, self.check(root).returncode)
        self.file(self.audit("20261021-audit-other"))       # another filing keeps the audit's current row
        self.assertEqual(5, self.row_in_index(audit_id)[0]["findings_total"])
        self.assertEqual(0, self.check(root).returncode)
        self.assertIn("STALE", self.audit_lines()[1][audit_id])

    def test_one_unusable_row_file_no_longer_blocks_every_filing(self) -> None:
        bad_id = "20261020-audit-a"
        bad, other, unrelated = (self.audit(name) for name in (bad_id, "20261020-audit-b", "20261020-audit-c"))
        receipt = self.file(bad)
        self.file(other)
        own = self.registry / bad_id / "index_row.json"
        good = own.read_bytes()
        states = {"empty": lambda: own.write_bytes(b""), "truncated": lambda: own.write_bytes(good[:40])}
        if os.geteuid() != 0:   # root reads a mode-000 file
            states["unreadable"] = lambda: os.chmod(own, 0)
        # a hand copy or a bad merge: valid, current for audit B, and not A's row
        states["another audit's row"] = lambda: own.write_bytes(
            (self.registry / "20261020-audit-b" / "index_row.json").read_bytes())
        for state, damage in states.items():
            with self.subTest(state=state):
                damage()
                # 2.7.9 refused every filing of every audit here, and registry-audit crashed
                result = self.cli("create", unrelated, "--registry", self.registry)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"registry/{bad_id}/index_row.json", result.stderr)
                self.assertIn("registry-recover", result.stderr)
                self.assertNotIn(str(self.base), result.stderr)          # the warning names no path
                self.assertEqual(1, len(self.row_in_index(bad_id)))     # its index.jsonl row is kept
                for root in (other, unrelated):
                    self.assertEqual(0, self.check(root).returncode, root)
                result = self.check(bad)                                # its own check stays strict
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("registry-recover", result.stderr)
                code, lines, output = self.audit_lines()
                self.assertEqual(1, code, output)
                self.assertIn("BROKEN", lines[bad_id])
                self.assertTrue(any("cannot be read or parsed" in why for why in self.status_attention(bad_id)))

                result = self.cli("registry-recover", self.registry, bad)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("(replaced one that", result.stdout)
                if state == "another audit's row":
                    self.assertIn("(replaced one that does not hold that audit's row)", result.stdout)
                self.assertNotIn("WARNING", result.stderr)     # the file it replaces is not reported as broken
                self.assertEqual(receipt["registry"]["index_row_sha256"], row_hash(json.loads(own.read_text())))
                self.assertEqual(0, self.check(bad).returncode)
                self.assertEqual(0, self.audit_lines()[0])

        # The audit's own filing does not read its own file: re-filing it replaces a bad one.
        own.write_bytes(b"")
        result = self.cli("create", bad, "--registry", self.registry)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(bad_id, result.stderr)
        self.assertEqual(0, self.check(bad).returncode)

    def test_a_superseded_row_stamped_by_a_fast_clock_no_longer_outranks_the_current_one(self) -> None:
        audit_id = "20261020-audit-skewed"
        root = self.audit(audit_id)
        result = subprocess.run([sys.executable, "-c", FAST_CLOCK, SKILL / "scripts", root, self.registry],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lagging_index = (self.registry / "index.jsonl").read_bytes()    # host C's copy of index.jsonl stops here
        self.set_findings(root, 4)
        current = self.file(root)["registry"]          # re-filed a minute later on a host with the right clock
        superseded = json.loads(lagging_index)
        self.assertGreater(superseded["version"], current["version"])
        (self.registry / "index.jsonl").write_bytes(lagging_index)
        # registry-audit said nothing: index.jsonl held a row for the audit, just not its current one
        code, lines, output = self.audit_lines()
        self.assertEqual(0, code, output)
        self.assertIn("lagging", lines[audit_id])
        self.assertEqual([], self.status_attention(audit_id))        # index_row.json holds it; check passes

        self.file(self.audit("20261020-audit-z"))                        # host C files another audit
        self.assertEqual(current["version"], self.row_in_index(audit_id)[0]["version"])
        self.assertEqual(4, self.row_in_index(audit_id)[0]["findings_total"])
        self.assertEqual(0, self.cli_278("check", root, "--require-registry").returncode)  # reads index.jsonl
        self.assertEqual(0, self.check(root).returncode)
        self.assertEqual({}, self.audit_lines()[1])

    def test_the_registry_commands_refuse_a_folder_that_is_not_a_registry(self) -> None:
        audit_id = "20261020-audit-lost"
        root = self.audit(audit_id)
        self.file(root)
        self.as_legacy(audit_id)
        self.drop_rows(audit_id)
        tree = self.base / "tree"
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        for command in (("registry-audit", elsewhere), ("registry-recover", elsewhere, root)):
            with self.subTest(command=command[0]):
                result = self.cli(*command)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("is not a registry folder", result.stderr)
                self.assertEqual([], list(elsewhere.iterdir()))           # no stray .registry.lock

        # the tree root, which status.py and publish take, means its registry/: it no longer reads as all-clear
        code, lines, output = self.audit_lines(tree)
        self.assertEqual(1, code, output)
        self.assertEqual(self.audit_lines(), (code, lines, output))
        self.assertIn("MISSING", lines[audit_id])
        before = sorted(path.name for path in tree.iterdir())
        result = self.cli("registry-recover", tree, root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, sorted(path.name for path in tree.iterdir()))
        self.assertEqual(0, self.check(root).returncode)

    # -- review round 3 ----------------------------------------------------------------------------
    def test_a_host_whose_current_json_lags_its_index_keeps_the_re_filings_row(self) -> None:
        # Dropbox syncs file by file. Host C has the re-filing's index.jsonl, but its copy of registry/<X>/ is from
        # before the re-filing (or has only its versions/ folder), and it files another audit in that window.
        # Round 2 trusted C's current.json and put the earlier filing's row back from the conflicted copy, or from
        # X's own index_row.json; once synced, every host's check of X refused until registry-recover was run.
        audit_id = "20261001-audit-x"
        for first, arrived in (("2.7.8", ()), ("2.7.8", ("versions",)), ("2.7.9", ())):
            with self.subTest(first_filed_on=first, arrived=arrived):
                for leftover in ("tree", "host-c-x", "synced-x"):
                    if (self.base / leftover).exists():
                        shutil.rmtree(self.base / leftover)
                self.registry.mkdir(parents=True)
                root = self.audit(audit_id)
                if first == "2.7.8":     # a legacy audit: no index_row.json
                    self.assertEqual(0, self.cli_278("create", root, "--registry", self.registry).returncode)
                else:
                    self.file(root)
                shutil.copyfile(self.registry / "index.jsonl", self.registry / CONFLICT)   # an older index
                host_c = self.base / "host-c-x"
                shutil.copytree(self.registry / audit_id, host_c)
                self.set_findings(root, 4)
                result = self.cli_278("create", root, "--registry", self.registry)    # a teammate re-files X
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                current = json.loads((root / "finalization_receipt.json").read_text())["registry"]["version"]
                synced = self.base / "synced-x"
                shutil.copytree(self.registry / audit_id, synced)
                shutil.rmtree(self.registry / audit_id)
                shutil.copytree(host_c, self.registry / audit_id)
                if "versions" in arrived:
                    shutil.copytree(synced / "versions" / current, self.registry / audit_id / "versions" / current)
                self.file(self.audit("20261021-audit-y"))                      # host C files Y
                self.assertEqual([current], [row["version"] for row in self.row_in_index(audit_id)])

                shutil.rmtree(self.registry / audit_id)                         # the rest of X arrives
                shutil.copytree(synced, self.registry / audit_id)
                result = self.check(root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(0, self.cli_278("check", root, "--require-registry").returncode)
                code, lines, output = self.audit_lines()
                self.assertNotIn("MISSING", lines.get(audit_id, ""), output)
                self.assertFalse(any("no index row" in why for why in self.status_attention(audit_id)))
                self.file(self.audit("20261022-audit-z"))                      # and the next filing keeps it
                self.assertEqual([current], [row["version"] for row in self.row_in_index(audit_id)])

    def test_a_legacy_audit_whose_only_index_row_is_superseded_is_reported_everywhere(self) -> None:
        # Filed and re-filed on 2.7.8, then the index.jsonl of a host that missed the re-filing won the sync:
        # index.jsonl holds an earlier filing's row only. check and publish refuse it and registry-audit lists it,
        # but status.py counted any row with the audit's id and gave an all-clear.
        audit_id = "20261001-audit-legacy"
        root = self.audit(audit_id)
        self.assertEqual(0, self.cli_278("create", root, "--registry", self.registry).returncode)
        lagging = (self.registry / "index.jsonl").read_bytes()
        self.set_findings(root, 4)
        self.assertEqual(0, self.cli_278("create", root, "--registry", self.registry).returncode)
        current = json.loads((root / "finalization_receipt.json").read_text())["registry"]["version"]
        refiled = (self.registry / "index.jsonl").read_bytes()
        (self.registry / "index.jsonl").write_bytes(lagging)
        (root / "run.events.jsonl").write_text("")
        old = time.time() - 3600
        os.utime(root / "run.events.jsonl", (old, old))

        result = self.check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("index row no longer matches the finalization receipt (filed before index_row.json existed; "
                      "`finalization_receipt.py registry-audit", result.stderr)          # it was the bare refusal
        code, lines, output = self.audit_lines()
        self.assertEqual(1, code, output)
        self.assertIn("MISSING", lines[audit_id])
        result = subprocess.run([sys.executable, str(STATUS), self.base / "tree", "--json"],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        row = next(row for row in json.loads(result.stdout) if row["audit_id"] == audit_id)
        self.assertTrue(any("no index row holds its current filing" in why for why in row["attention"]), row)
        text = subprocess.run([sys.executable, str(STATUS), self.base / "tree"],
                              text=True, capture_output=True, timeout=60)
        self.assertIn("registry: 1 filed audit(s) without an index row", text.stdout)
        # a row whose audit_id is not a string (a hand edit) is skipped, not a crash of the whole sweep
        with (self.registry / "index.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"audit_id": ["' + audit_id + '"]}\n')
        result = subprocess.run([sys.executable, str(STATUS), self.base / "tree", "--json"],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(any("no index row holds its current filing" in why for why in next(
            row for row in json.loads(result.stdout) if row["audit_id"] == audit_id)["attention"]))
        (self.registry / "index.jsonl").write_bytes(lagging)

        # Dropbox kept the re-filing host's index as a conflicted copy. This host holds the earlier filing and its
        # current.json has moved past it, so the next filing takes the current row back from the copy.
        (self.registry / CONFLICT).write_bytes(refiled)
        self.file(self.audit("20261002-audit-other"))
        self.assertEqual([current], [row["version"] for row in self.row_in_index(audit_id)])
        self.assertEqual(0, self.check(root).returncode)
        self.assertEqual(0, self.cli_278("check", root, "--require-registry").returncode)
        self.assertEqual(0, self.audit_lines()[0])
        self.assertEqual([], self.status_attention(audit_id))

    # -- review round 4 ----------------------------------------------------------------------------
    def test_the_states_no_filing_heals_are_reported_and_registry_recover_restores_the_row(self) -> None:
        # Both follow a re-filing on 2.7.8, which writes no index_row.json. (a) A clock was off, so the re-filing is
        # stamped earlier than the filing it supersedes, and the index.jsonl of host C, which missed the re-filing,
        # won the Dropbox conflict: a conflicted copy only replaces a row this host knows was superseded, and the
        # stamps say the opposite. (b) The audit was filed on 2.7.9 and C had the re-filing's index.jsonl and
        # versions/ folder but not its current.json, which looks exactly like a fast clock's superseded row. INSTALL.md
        # documents both: a later filing does not heal them, and registry-recover restores the row.
        audit_id = "20261001-audit-x"
        for state, first in (("clock off", "2.7.8"), ("clock off", "2.7.9"), ("partial sync", "2.7.9")):
            with self.subTest(state=state, first_filed_on=first):
                for leftover in ("tree", "host-c-x", "synced-x"):
                    if (self.base / leftover).exists():
                        shutil.rmtree(self.base / leftover)
                self.registry.mkdir(parents=True)
                root = self.audit(audit_id)
                if state == "clock off":     # host A's clock runs three minutes ahead
                    result = subprocess.run([sys.executable, "-c", FAST_CLOCK, SKILL / "scripts", root, self.registry],
                                            text=True, capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.file(root)
                if first == "2.7.8":     # a legacy filing: no index_row.json
                    self.as_legacy(audit_id)
                lagging = (self.registry / "index.jsonl").read_bytes()
                host_c = self.base / "host-c-x"
                shutil.copytree(self.registry / audit_id, host_c)
                self.set_findings(root, 4)
                result = self.cli_278("create", root, "--registry", self.registry)    # a teammate re-files X
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                current = json.loads((root / "finalization_receipt.json").read_text())["registry"]["version"]
                refiled = (self.registry / "index.jsonl").read_bytes()
                synced = self.base / "synced-x"
                shutil.copytree(self.registry / audit_id, synced)
                shutil.rmtree(self.registry / audit_id)
                shutil.copytree(host_c, self.registry / audit_id)
                if state == "clock off":
                    self.assertGreater(json.loads(lagging)["version"], current)
                    (self.registry / "index.jsonl").write_bytes(lagging)
                else:
                    shutil.copytree(synced / "versions" / current, self.registry / audit_id / "versions" / current)
                self.file(self.audit("20261002-audit-y"))                       # host C files Y
                shutil.rmtree(self.registry / audit_id)                         # the sync completes
                shutil.copytree(synced, self.registry / audit_id)
                if state == "clock off":     # C's index won; the re-filing's is kept as a conflicted copy
                    (self.registry / CONFLICT).write_bytes(refiled)
                self.file(self.audit("20261003-audit-z"))                       # a synced host's filing keeps it
                self.assertNotEqual([current], [row["version"] for row in self.row_in_index(audit_id)])

                self.assertNotEqual(0, self.check(root).returncode)
                self.assertNotEqual(0, self.cli_278("check", root, "--require-registry").returncode)
                code, lines, output = self.audit_lines()
                self.assertEqual(1, code, output)
                if first == "2.7.8":
                    self.assertIn(f"MISSING {audit_id}: no index row holds its current filing (its current row is "
                                  f"in '{CONFLICT}')", lines[audit_id])
                else:
                    self.assertIn(f"STALE {audit_id}", lines[audit_id])
                    self.assertIn("index.jsonl does not hold its current row either", lines[audit_id])
                self.assertTrue(any("no index row holds its current filing" in why
                                    for why in self.status_attention(audit_id)))

                result = self.cli("registry-recover", self.registry, root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([current], [row["version"] for row in self.row_in_index(audit_id)])
                self.assertEqual(0, self.check(root).returncode)
                self.assertEqual(0, self.cli_278("check", root, "--require-registry").returncode)
                self.assertEqual(0, self.audit_lines()[0])
                self.assertEqual([], self.status_attention(audit_id))
                self.file(self.audit("20261004-audit-w"))                       # and the next filing keeps it
                self.assertEqual([current], [row["version"] for row in self.row_in_index(audit_id)])

    def test_a_current_json_check_cannot_use_is_reported_as_that_alone(self) -> None:
        # Unreadable (a cloud-only file that could not be fetched), damaged, naming no filing, or a symlink: check,
        # registry-audit and registry-recover all need it. registry-audit called the audit's index_row.json STALE (to be replaced by
        # registry-recover, which refuses), the audit MISSING when it had none, or its broken row file BROKEN; status.py
        # said no index row holds it, and for an object that parses also that the row file was an earlier filing's.
        # Each now gets one line, in both, saying what is wrong with current.json and how to get it back.
        audit_id = "20261001-audit-x"
        root = self.audit(audit_id)
        self.file(root)
        pointer = self.registry / audit_id / "current.json"
        own = self.registry / audit_id / "index_row.json"
        good, row = pointer.read_bytes(), own.read_bytes()
        # filed elsewhere, its current.json not synced here yet: neither registry-audit nor status.py lists it
        self.file(self.audit("20261002-audit-unsynced"))
        (self.registry / "20261002-audit-unsynced" / "current.json").unlink()
        elsewhere = self.base / "current-elsewhere.json"
        elsewhere.write_bytes(good)
        unhashed = json.loads(good)
        del unhashed["pair_manifest_sha256"]
        no_filing = "names no filing (no version or pair_manifest_sha256 string)"
        states = {
            "truncated": ("is not valid JSON (truncated?)", lambda: pointer.write_bytes(good[:20])),
            "empty": ("is empty", lambda: pointer.write_bytes(b"")),
            "not an object": ("is not a JSON object", lambda: pointer.write_text("[]\n", encoding="utf-8")),
            "an empty object": (no_filing, lambda: pointer.write_text("{}\n", encoding="utf-8")),
            "no pair hash": (no_filing, lambda: pointer.write_text(json.dumps(unhashed) + "\n", encoding="utf-8")),
            # json.loads keeps the last of two keys, and status.py read that as the current filing; check refuses it
            "a repeated key": ("is not strict JSON (a repeated key, or NaN)", lambda: pointer.write_text(
                good.decode().rstrip().rstrip("}") + ', "version": "x"}\n', encoding="utf-8")),
            "a symlink out of the registry": ("is a symlink",
                                              lambda: (pointer.unlink(), pointer.symlink_to(elsewhere))),
            "truncated, beside a broken row file": ("is not valid JSON (truncated?)",
                                                    lambda: (pointer.write_bytes(good[:20]), own.write_bytes(b""))),
            "truncated, filed on 2.7.8 (no row file)": ("is not valid JSON (truncated?)",
                                                        lambda: (pointer.write_bytes(good[:20]), own.unlink())),
        }
        if os.geteuid() != 0:   # root reads a mode-000 file
            states["unreadable"] = ("cannot be read (Permission denied)", lambda: os.chmod(pointer, 0))
        for state, (problem, damage) in states.items():
            with self.subTest(state=state):
                damage()
                self.assertNotEqual(0, self.check(root).returncode)
                code, lines, output = self.audit_lines()
                self.assertEqual(1, code, output)
                self.assertEqual([audit_id], list(lines))       # no MISSING, STALE, BROKEN or lagging line beside it
                self.assertEqual(1, output.count(audit_id), output)
                self.assertTrue(lines[audit_id].startswith(f"  UNUSABLE {audit_id}: current.json {problem}"), output)
                self.assertIn("replace the link with a copy of its target" if problem == "is a symlink" else
                              "check, registry-audit and registry-recover all need it: let Dropbox finish syncing it, "
                              "or restore it from the folder's version history", lines[audit_id])
                self.assertNotIn(str(self.base), output)
                self.assertEqual(1, self.cli("registry-recover", self.registry, root).returncode)   # as it says
                attention = self.status_attention(audit_id)
                self.assertEqual(1, len(attention), attention)
                self.assertIn(f"filed into registry/, but its registry/<id>/current.json {problem}", attention[0])
                self.assertEqual([], self.status_attention("20261002-audit-unsynced"))
                text = subprocess.run([sys.executable, str(STATUS), self.base / "tree"],
                                      text=True, capture_output=True, timeout=60)
                self.assertIn("registry: 0 filed audit(s) without an index row, 1 whose current.json cannot be used, "
                              "0 Dropbox conflicted copies of index.jsonl, 0 stale and 0 unusable index_row.json "
                              "(finalization_receipt.py registry-audit <tree>/registry)\n", text.stdout)

                pointer.unlink()                                # restored from the folder's version history
                pointer.write_bytes(good)
                own.write_bytes(row)
                self.assertEqual(0, self.check(root).returncode)
                self.assertEqual([], self.status_attention(audit_id))
        self.assertEqual((0, {}), self.audit_lines()[:2])

    def test_a_current_json_symlinked_inside_the_registry_is_not_said_to_refuse_the_audit(self) -> None:
        # check resolves the pointer path, so it follows a symlink that stays inside the registry and passes the audit,
        # as publish does; registry-audit and registry-recover read current.json strictly and refuse it. status.py said
        # check and publish refuse the audit, and registry-audit called its index_row.json STALE.
        audit_id = "20261001-audit-x"
        for first in ("2.7.9", "2.7.8"):
            with self.subTest(first_filed_on=first):
                if (self.base / "tree").exists():
                    shutil.rmtree(self.base / "tree")
                self.registry.mkdir(parents=True)
                root = self.audit(audit_id)
                if first == "2.7.8":
                    self.assertEqual(0, self.cli_278("create", root, "--registry", self.registry).returncode)
                else:
                    self.file(root)
                pointer = self.registry / audit_id / "current.json"
                target = pointer.with_name("current.json.synced")
                target.write_bytes(pointer.read_bytes())
                pointer.unlink()
                pointer.symlink_to(target.name)
                self.assertEqual(0, self.check(root).returncode)
                code, lines, output = self.audit_lines()
                self.assertEqual(1, code, output)
                self.assertEqual({audit_id: f"  UNUSABLE {audit_id}: current.json is a symlink: check follows one that "
                                            "stays inside the registry, but registry-audit and registry-recover refuse "
                                            "it; replace the link with a copy of its target"}, lines)
                result = self.cli("registry-recover", self.registry, root)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("symlinked", result.stderr)
                self.assertEqual(["filed into registry/, but its registry/<id>/current.json is a symlink: its receipt "
                                  "check and publish follow one that stays inside the registry, but registry-audit and "
                                  "registry-recover refuse it; replace the link with a copy of its target"],
                                 self.status_attention(audit_id))

                pointer.unlink()                                # as both say
                shutil.copyfile(target, pointer)
                self.assertEqual((0, {}), self.audit_lines()[:2])
                self.assertEqual([], self.status_attention(audit_id))
                self.assertEqual(0, self.check(root).returncode)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_status_never_opens_an_index_jsonl_that_is_not_a_regular_file(self) -> None:
        # registry-audit refuses an index.jsonl that is not a regular file; status.py read it, and on a FIFO the whole
        # sweep blocked. Now it reads no row from it: an audit its own index_row.json holds is clear, as check says,
        # and one with no row file is held by no row. (check reads index.jsonl for that one and blocks on a FIFO, as
        # 2.7.8 did; it is not run there)
        audit_id = "20261001-audit-x"
        root = self.audit(audit_id)
        self.file(root)
        index, own = self.registry / "index.jsonl", self.registry / audit_id / "index_row.json"
        good, row = index.read_bytes(), own.read_bytes()
        index.unlink()
        os.mkfifo(index)
        try:
            self.assertNotEqual(0, self.cli("registry-audit", self.registry).returncode)
            for held in (True, False):
                with self.subTest(own_row=held):
                    if not held:
                        own.unlink()
                    result = subprocess.run([sys.executable, str(STATUS), self.base / "tree", "--json"],
                                            text=True, capture_output=True, timeout=60)
                    attention = next(item for item in json.loads(result.stdout)
                                     if item["audit_id"] == audit_id)["attention"]
                    if held:
                        self.assertEqual((0, []), (result.returncode, attention))
                        self.assertEqual(0, self.check(root).returncode)
                    else:
                        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                        self.assertTrue(any("no index row holds its current filing" in why for why in attention),
                                        attention)
        finally:
            index.unlink()
            index.write_bytes(good)
            own.write_bytes(row)
        self.assertEqual(0, self.check(root).returncode)
        self.assertEqual([], self.status_attention(audit_id))

    def test_status_flags_an_index_row_json_check_refuses_as_broken(self) -> None:
        # check and registry-audit test whether the path exists and read it strictly, so they refuse a directory and a
        # repeated key and list either as BROKEN; status.py tested is_file() and used json.loads, and gave an all-clear
        audit_id = "20261001-audit-x"
        root = self.audit(audit_id)
        self.file(root)
        own = self.registry / audit_id / "index_row.json"
        repeated = own.read_text(encoding="utf-8").rstrip().rstrip("}") + f', "audit_id": "{audit_id}"}}\n'
        states = {"a repeated key": (lambda: own.write_text(repeated, encoding="utf-8"), own.unlink),
                  "a directory": (lambda: (own.unlink(), own.mkdir()), own.rmdir)}
        if hasattr(os, "mkfifo"):   # status.py must not open it: reading a FIFO blocks the whole sweep
            states["a FIFO"] = (lambda: (own.unlink(), os.mkfifo(own)), own.unlink)
        for state, (damage, clear) in states.items():
            with self.subTest(state=state):
                damage()
                self.assertNotEqual(0, self.check(root).returncode)
                code, lines, output = self.audit_lines()
                self.assertEqual(1, code, output)
                self.assertIn(f"BROKEN {audit_id}", lines[audit_id])
                result = subprocess.run([sys.executable, str(STATUS), self.base / "tree", "--json"],
                                        text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                attention = next(row for row in json.loads(result.stdout) if row["audit_id"] == audit_id)["attention"]
                self.assertEqual(1, len(attention), attention)
                self.assertIn("its registry/<id>/index_row.json cannot be read or parsed", attention[0])
                text = subprocess.run([sys.executable, str(STATUS), self.base / "tree"],
                                      text=True, capture_output=True, timeout=60)
                # and with no unusable current.json the line reads as it did
                self.assertIn("registry: 0 filed audit(s) without an index row, 0 Dropbox conflicted copies of "
                              "index.jsonl, 0 stale and 1 unusable index_row.json (finalization_receipt.py "
                              "registry-audit <tree>/registry)\n", text.stdout)

                clear()
                self.assertEqual(0, self.cli("registry-recover", self.registry, root).returncode)
                self.assertEqual(0, self.check(root).returncode)
                self.assertEqual([], self.status_attention(audit_id))


if __name__ == "__main__":
    unittest.main()
