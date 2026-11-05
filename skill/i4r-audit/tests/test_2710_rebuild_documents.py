"""2.7.10: amend_claims_script.py --rebuild-documents rebuilds a published comment's PDF and DOCX so that its claims
script can be re-bound.

Twelve audits published on 2.7.4 carry a document_build_manifest.json that names 2.7.4's document_release.py, which
2.7.5 changed. fr.artifact_paths refuses such a manifest ("code identity differs from the release verifier"), so the
receipt cannot be created again and amend_claims_script.py refused all twelve. Every test here builds that condition on
disk and runs the real code. A real audit (tests.test_release_pipeline.make_audit) has its comment rendered by the real
build_docx.sh (pandoc, LibreOffice, Poppler), is given a claims script that this release's builder would not write, is
filed, and is published by publish_completed.sh. A copy of the skill whose document scripts differ, standing in for the
later release, then runs the tool against it. Each test fails on the code before this mode: argparse refuses
--rebuild-documents with exit 2.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(SKILL / "scripts"))
from tests.test_release_pipeline import make_audit  # noqa: E402
import finalization_receipt as fr  # noqa: E402

JOURNAL, PAPER, AUDIT_ID = "Journal-Sanitized", "Paper-Docs", "audit-integration-001"
REASON = "comment documents rebuilt with the current builder so the claims script can be re-bound"
JOURNAL_PROJECTIONS = {"run.json", "run.events.jsonl", "invocations.jsonl", "invocations.watermark.json",
                       "run_metrics.json", "RUN_METADATA.md"}
OLDER_HARNESS = "# written by the claims-script harness of an earlier release\n"


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / "miniconda3/envs/i4rdoc/bin" / name
    return str(candidate) if candidate.is_file() else None


TOOLS = {name: find_tool(name) for name in ("pandoc", "pdfinfo", "pdftotext", "Rscript")}
SOFFICE = shutil.which("soffice") or next(
    (path for path in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",) if Path(path).is_file()), None)
READY = SOFFICE is not None and all(TOOLS.values())
TOOL_PATH = os.pathsep.join(dict.fromkeys(str(Path(path).parent) for path in TOOLS.values() if path))
ENV = {key: value for key, value in os.environ.items() if key != "BUILD_DOCX_CACHE_MODE"}
ENV.update({"PATH": TOOL_PATH + os.pathsep + os.environ.get("PATH", ""), "I4R_RESERVE_GRACE": "0"})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[relative] = "link:" + os.readlink(path)
        elif path.is_file() and path.name != ".DS_Store":
            out[relative] = digest(path)
    return out


def run_ok(command: list[str]) -> str:
    done = subprocess.run(command, capture_output=True, text=True, env=ENV, timeout=900)
    if done.returncode:
        raise AssertionError(f"{' '.join(command)} exited {done.returncode}:\n{done.stdout}\n{done.stderr}")
    return done.stdout


def published_fixture(base: Path) -> Path:
    """A published audit as 2.7.4 left it, apart from the document scripts: a real comment, an older claims script."""
    with mock.patch.dict(os.environ, {"PATH": ENV["PATH"]}):
        audit = make_audit(base, PAPER, layout="new")
    run_ok(["bash", str(SKILL / "scripts/build_docx.sh"), str(audit), "comment"])
    claims = audit / "claims_script.R"
    claims.write_text(claims.read_text() + OLDER_HARNESS)
    projection_path = audit / "work/finding_projections.manifest.json"
    projection = json.loads(projection_path.read_text())
    for row in projection["artifacts"]:
        if row["path"] == "claims_script.R":
            row["sha256"] = digest(claims)
    projection_path.write_text(json.dumps(projection, indent=2, ensure_ascii=False) + "\n")
    with mock.patch.dict(os.environ, {"PATH": ENV["PATH"]}), contextlib.redirect_stdout(io.StringIO()):
        fr.create(audit, base / "_registry")
    run_ok(["bash", str(SKILL / "scripts/publish_completed.sh"), str(audit)])
    return audit


def later_release(target: Path, *, append: tuple[str, ...] = ("document_release.py",),
                  edits: dict[str, tuple[str, str]] | None = None) -> Path:
    """A copy of this skill whose named document scripts differ, as the next release's would."""
    for name in ("scripts", "schemas", "config", "prompts", "reference"):
        shutil.copytree(SKILL / name, target / name,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".DS_Store"))
    shutil.copy2(SKILL / "VERSION", target / "VERSION")
    for name in append:
        with (target / "scripts" / name).open("a", encoding="utf-8") as handle:
            handle.write("\n# a later release\n")
    for name, (old, new) in (edits or {}).items():
        path = target / "scripts" / name
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise AssertionError(f"{name} no longer contains {old!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")
    return target / "scripts" / "amend_claims_script.py"


@unittest.skipUnless(READY, "pandoc, LibreOffice, Poppler and R are required")
class RebuildDocumentsTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._fixture = tempfile.TemporaryDirectory(prefix="i4r-2710-docs-fixture-")
        cls.fixture = Path(cls._fixture.name) / "base"
        cls.fixture.mkdir()
        published_fixture(cls.fixture)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._fixture.cleanup()

    def setUp(self) -> None:
        work = tempfile.TemporaryDirectory(prefix="i4r-2710-docs-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.base = self.work / "base"
        shutil.copytree(self.fixture, self.base, symlinks=True)
        self.tree = self.base / "Discipline"
        self.audit = self.tree / "runs" / JOURNAL / PAPER / "audit"
        self.comments = self.tree / "comments" / JOURNAL / PAPER
        self.metadata = self.tree / "metadata" / JOURNAL / PAPER
        self.registry = self.base / "_registry"

    def tool(self, script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(script), str(self.audit), *args], capture_output=True, text=True,
                              env=env or ENV, timeout=900)

    def receipt_check(self, release: Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(release.parent / "finalization_receipt.py"), "check",
                               "--require-registry", "--require-publication", str(self.audit)],
                              capture_output=True, text=True, env=ENV, timeout=300)

    def record(self) -> dict:
        records = list(self.audit.glob("amendments/*-claims_script/AMENDMENT.json"))
        self.assertEqual(len(records), 1, records)
        return json.loads(records[0].read_text())

    def assert_republished(self, release: Path, comments_before: dict, metadata_before: dict) -> None:
        check = self.receipt_check(release)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        manifest = json.loads((self.metadata / "release-manifest.json").read_text())
        files = {row["file"]: row["sha256"] for row in manifest["files"]}
        on_disk = {**tree_digest(self.comments), **tree_digest(self.metadata)}
        on_disk.pop("release-manifest.json")
        self.assertEqual(on_disk, files, "the published folders hold exactly the release manifest's files")
        for name, own in (("comment.pdf", "report/comment.pdf"), ("comment.docx", "report/comment.docx"),
                          ("claims_script.R", "claims_script.R"), ("finalization_receipt.json", fr.RECEIPT),
                          ("document_build_manifest.json", "document_build_manifest.json")):
            self.assertEqual(files[name], digest(self.audit / own), name)
        archives = list(self.tree.glob("old_runs/archived_*_claims_amendment"))
        self.assertEqual(len(archives), 1, archives)
        self.assertEqual(tree_digest(archives[0] / "comments" / JOURNAL / PAPER), comments_before)
        self.assertEqual(tree_digest(archives[0] / "metadata" / JOURNAL / PAPER), metadata_before)

    def test_the_dry_run_rebuilds_on_a_scratch_copy_and_changes_nothing(self) -> None:
        amend = later_release(self.work / "release")
        (self.audit / "pkg").chmod(0o755)   # the synced folder strips intake's read-only mode
        before = tree_digest(self.base)
        done = self.tool(amend, "--rebuild-documents")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn("were built by older document scripts (document_release.py); nothing else about them is stale",
                      out)
        self.assertIn("changed: document_build_manifest.json, report/comment.docx, report/comment.pdf", out)
        self.assertRegex(out, r"created: \.cache/document-builds/[0-9a-f]{64}\.zip")
        self.assertIn("fr.artifact_paths after the rebuild: pass", out)
        self.assertRegex(out, r"new manifest: 1 body / \d+ total pages \(cap 10; were 1 / \d+\), release eligible: yes")
        self.assertIn("comment text, PDF: same text", out)
        self.assertIn("comment text, DOCX: same text", out)
        self.assertIn("pkg/: 1 entries have write permission", out)
        self.assertIn("publish_completed.sh passed", out)
        self.assertIn("dry run: nothing changed", out)
        self.assertEqual(tree_digest(self.base), before, "the dry run wrote outside its scratch folder")
        self.assertEqual((self.audit / "pkg").stat().st_mode & 0o777, 0o755, "the dry run changed pkg/'s mode")

    def test_apply_rebuilds_rebinds_and_republishes_both_folders_once(self) -> None:
        amend = later_release(self.work / "release")
        audit_before = tree_digest(self.audit)
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        pointer_before = json.loads((self.registry / AUDIT_ID / "current.json").read_text())
        (self.audit / "pkg").chmod(0o755)   # the synced folder strips intake's read-only mode
        done = self.tool(amend, "--rebuild-documents", "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("amended and republished", done.stdout)
        self.assertIn("pkg/: 1 entries made read-only again for the archive gate", done.stdout)
        self.assertEqual((self.audit / "pkg").stat().st_mode & 0o222, 0)
        self.assert_republished(amend, comments_before, metadata_before)
        pointer = json.loads((self.registry / AUDIT_ID / "current.json").read_text())
        self.assertNotEqual(pointer["version"], pointer_before["version"], "the audit was not re-filed")

        after = tree_digest(self.audit)
        changed = {name for name in audit_before if after.get(name) != audit_before[name]}
        self.assertEqual(changed - JOURNAL_PROJECTIONS,
                         {"claims_script.R", "work/finding_projections.manifest.json", fr.RECEIPT,
                          "document_build_manifest.json", "report/comment.pdf", "report/comment.docx"})
        created = set(after) - set(audit_before)
        self.assertTrue(created and all(name.startswith(("amendments/", ".cache/document-builds/"))
                                        for name in created), created)
        for source in ("report/REPORT_final.md", "report/front_matter.md", "report/captions.txt"):
            self.assertEqual(after[source], audit_before[source], source)
        self.assertNotIn(OLDER_HARNESS, (self.audit / "claims_script.R").read_text())

        events = [json.loads(line) for line in (self.audit / "run.events.jsonl").read_text().splitlines()]
        self.assertEqual([event["type"] for event in events[-2:]], ["note", "delivery"])
        note = events[-2]["payload"]
        self.assertEqual((note["kind"], note["documents"], note["text"]),
                         ("claims_script_amendment", "rebuilt", REASON))
        self.assertEqual(note["pdf_from_sha256"], audit_before["report/comment.pdf"])
        self.assertEqual(note["pdf_to_sha256"], after["report/comment.pdf"])
        self.assertEqual(note["docx_to_sha256"], after["report/comment.docx"])
        self.assertEqual(note["to_sha256"], after["claims_script.R"])

        record = self.record()
        self.assertEqual((record["mode"], record["reason"]), ("rebuild_documents", REASON))
        self.assertEqual(record["documents"]["pdf"]["from_sha256"], audit_before["report/comment.pdf"])
        self.assertEqual(record["documents"]["docx"]["to_sha256"], after["report/comment.docx"])
        self.assertTrue(record["text_comparison"]["same"])
        self.assertFalse(record["accepted_rendering_changes"])
        self.assertEqual(record["pkg_made_read_only"], 1)
        backups = next(self.audit.glob("amendments/*-claims_script"))
        for name in ("claims_script.R", "report/comment.pdf", "report/comment.docx", fr.RECEIPT,
                     "work/finding_projections.manifest.json", "document_build_manifest.json"):
            self.assertEqual(digest(backups / name), audit_before[name], name)

        published = {**tree_digest(self.comments), **tree_digest(self.metadata)}
        again = self.tool(amend, "--rebuild-documents", "--apply")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already amended and republished: nothing to do", again.stdout)
        self.assertEqual({**tree_digest(self.comments), **tree_digest(self.metadata)}, published)

    def test_a_text_difference_is_shown_and_applied_only_when_accepted(self) -> None:
        # the later release renders section headings differently: words of the comment change
        amend = later_release(self.work / "release", append=(), edits={"build_docx.sh": (
            'middle.append(f"# Section {section.group(1)}', 'middle.append(f"# Part {section.group(1)}')})
        before = tree_digest(self.base)
        dry = self.tool(amend, "--rebuild-documents")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("were built by older document scripts (build_docx.sh)", dry.stdout)
        self.assertIn("comment text, PDF: WORDS OR NUMBERS DIFFER", dry.stdout)
        self.assertIn("comment text, DOCX: WORDS OR NUMBERS DIFFER", dry.stdout)
        self.assertIn("words only in the old: Section\n    words only in the new: Part\n", dry.stdout)
        self.assertRegex(dry.stdout, r"\| - Section\n +\| \+ Part\n +\|   1\. Introduction")
        self.assertIn("only with --accept-rendering-changes", dry.stdout)

        refused = self.tool(amend, "--rebuild-documents", "--apply")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("refused: the rebuilt comment's text differs", refused.stdout)
        self.assertEqual(tree_digest(self.base), before)

        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        accepted = self.tool(amend, "--rebuild-documents", "--apply", "--accept-rendering-changes")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assert_republished(amend, comments_before, metadata_before)
        record = self.record()
        self.assertTrue(record["accepted_rendering_changes"])
        self.assertEqual(record["text_comparison"]["pdf"]["result"], "words")
        self.assertIn("- Section\n+ Part\n  1. Introduction", "\n".join(record["text_comparison"]["pdf"]["diff"]))
        note = [json.loads(line) for line in (self.audit / "run.events.jsonl").read_text().splitlines()][-2]
        self.assertEqual(note["payload"]["comment_text"], "accepted:pdf=words,docx=words")

    def test_documents_stale_in_more_than_their_code_identity_are_refused(self) -> None:
        # the later verifier would also re-validate the published PDF differently, not only name other code
        amend = later_release(self.work / "release", edits={"document_release.py": (
            'f"pdfinfo counted {total} page(s)"', 'f"pdfinfo counted {total} pages"')})
        before = tree_digest(self.base)
        done = self.tool(amend, "--rebuild-documents", "--apply")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("fail for a reason other than the document scripts' code identity",
                      done.stdout + done.stderr)
        self.assertIn("validation results are stale", done.stdout + done.stderr)
        self.assertEqual(tree_digest(self.base), before)
        usage = self.tool(amend, "--apply", "--accept-rendering-changes")
        self.assertEqual(usage.returncode, 2, usage.stdout + usage.stderr)
        self.assertIn("--accept-rendering-changes goes with --rebuild-documents", usage.stderr)

    def test_documents_this_release_built_are_refused(self) -> None:
        before = tree_digest(self.base)
        done = self.tool(SKILL / "scripts/amend_claims_script.py", "--rebuild-documents", "--apply")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("the comment's documents are current on this release", done.stdout)
        self.assertEqual(tree_digest(self.base), before)

    def test_a_failure_before_the_note_puts_the_audit_back(self) -> None:
        amend = later_release(self.work / "release")
        lock = (self.audit / ".run.events.lock").open("a+b")   # the journal cannot be written
        self.addCleanup(lock.close)
        before = tree_digest(self.base)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            done = self.tool(amend, "--rebuild-documents", "--apply", env={**ENV, "LOCK_TIMEOUT": "2"})
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("could not journal the note event", done.stderr)
        self.assertIn("rolled back: the audit is as it was before this run", done.stderr)
        self.assertNotIn("NOT RESTORED", done.stderr)
        self.assertEqual(tree_digest(self.base), before, "the rollback left a change behind")

    def test_a_refused_publication_restores_both_folders_and_apply_resumes(self) -> None:
        amend = later_release(self.work / "release")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        lock = self.metadata.parent / f"{PAPER}.publish.lock"
        lock.write_text(f"{os.getpid()}\n")   # a live publication of this paper holds it
        done = self.tool(amend, "--rebuild-documents", "--apply")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("the previous comments/ and metadata/ folders are back in place", done.stdout)
        self.assertEqual(tree_digest(self.comments), comments_before)
        self.assertEqual(tree_digest(self.metadata), metadata_before)
        self.assertEqual([p for p in self.tree.glob("old_runs/**/*") if p.is_file()], [])
        check = self.receipt_check(amend)
        self.assertEqual(check.returncode, 0, "the audit itself stays amended: " + check.stdout + check.stderr)

        lock.unlink()
        resumed = self.tool(amend, "--rebuild-documents", "--apply")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("a --rebuild-documents amendment is journaled", resumed.stdout)
        self.assertIn("amended and republished", resumed.stdout)
        self.assert_republished(amend, comments_before, metadata_before)
        self.assertTrue(self.record()["documents"]["pdf"]["to_sha256"])

    def test_a_failure_after_the_note_is_finished_by_apply(self) -> None:
        amend = later_release(self.work / "release")
        comments_before, metadata_before = tree_digest(self.comments), tree_digest(self.metadata)
        versions = self.registry / AUDIT_ID / "versions"
        mode = versions.stat().st_mode
        versions.chmod(0o555)   # the filing cannot write its new version
        try:
            done = self.tool(amend, "--rebuild-documents", "--apply")
        finally:
            versions.chmod(mode)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("stopped after the amendment was journaled: run --apply again to finish (it resumes)",
                      done.stderr)
        # the receipt still binds the old claims script, so the plain amendment refuses; this one resumes
        plain = self.tool(amend)
        self.assertEqual(plain.returncode, 1, plain.stdout + plain.stderr)
        self.assertIn("does not check before the amendment", plain.stdout)
        dry = self.tool(amend, "--rebuild-documents")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("the receipt was not created again after it", dry.stdout)
        resumed = self.tool(amend, "--rebuild-documents", "--apply")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assert_republished(amend, comments_before, metadata_before)
        self.assertEqual(self.record()["reason"], REASON)


if __name__ == "__main__":
    unittest.main()
