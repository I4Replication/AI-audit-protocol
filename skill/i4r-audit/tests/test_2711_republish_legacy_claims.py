"""2.7.11: re-publishing an audit whose claims script predates 2.7.10 does not require a rebuild.

2.7.10's check_claims_script.sh fails every claims script built before 2.7.10 by construction: that harness prints no
'deposit unmodified' line, even when every check passes. stage_release.sh and publish_completed.sh run the check, so
re-filing any audit published before 2.7.10 (a corrected Appendix, a metadata correction, a set moved aside and filed
again) was refused until its claims script was rebuilt, and the published claims scripts stay as they were sent
(decided 2026-11-01). Both now pass --release: a pre-2.7.10 script this audit already published (initialised before
2.7.10, publish's filing in its journal, no 2.7.10 harness, the bytes its receipt binds) passes on exit 0 with 0 FAIL,
and otherwise is named in a WARNING without refusing. Anything else is checked in full, as in 2.7.10. Each test builds
the audit on disk and runs the real check and R; on 2.7.10 --release is not an option (exit 2), so each fails there.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
CHECK = SKILL / "scripts" / "check_claims_script.sh"
BUILDER = SKILL / "scripts" / "make_claims_script.sh"
HAS_R = shutil.which("Rscript") is not None
FILING = "comments/ and metadata/ (filed publication set)"


def legacy_script(expected: int) -> str:
    """A claims script as make_claims_script.sh wrote it before 2.7.10: its own expect()/check(), a summary line, and
    no report on the deposit."""
    return "\n".join([
        "## CLAIM 1 [comment material] the deposit has two rows",
        "## ---- standalone check harness (same contract as verification.Rmd) ----",
        'PKG_DIR <- Sys.getenv("I4R_PKG_DIR")',
        ".V <- new.env(); .V$exp <- list(); .V$res <- list()",
        "expect <- function(id, ...) { v <- list(...); for (k in names(v)) .V$exp[[paste(id, k)]] <- v[[k]] }",
        "check <- function(id, ...) { v <- list(...); for (k in names(v)) { ok <- isTRUE(all.equal(.V$exp[[paste(id, k)]],"
        " v[[k]])); .V$res[[paste(id, k)]] <- ok",
        '  cat(sprintf("[%-6s %-34s] OBSERVED  %s  %s\\n", id, k, format(v[[k]]), if (ok) "PASS" else "FAIL")) } }',
        "claims_summary <- function() { f <- sum(!unlist(.V$res))",
        '  cat(sprintf("\\n%d checks, %d PASS, %d FAIL\\n", length(.V$res), length(.V$res) - f, f)) }',
        f'expect("C-1", rows = {expected})',
        'check("C-1", rows = nrow(read.csv(file.path(PKG_DIR, "data.csv"))))',
        "claims_summary()", ""])


@unittest.skipUnless(HAS_R, "R is not installed")
class RepublishLegacyClaimsScript(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-republish-")
        self.addCleanup(tmp.cleanup)
        self.audit = pathlib.Path(tmp.name) / "audit"
        (self.audit / "pkg").mkdir(parents=True)
        (self.audit / "paper").mkdir()
        (self.audit / "pkg" / "data.csv").write_text("x,y\n1,2\n3,4\n")
        (self.audit / "paper" / "paper.txt").write_text("Two rows.\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("I4R_")}
        self.env["I4R_CLAIMS_LOG_DIR"] = "none"

    def published_audit(self, script: str, *, protocol: str = "2.7.7", filed: bool = True,
                        receipt_binds_script: bool = True) -> None:
        claims = self.audit / "claims_script.R"
        claims.write_text(script)
        (self.audit / "run.json").write_text(json.dumps({"schema_version": "2.1.0", "protocol_version": protocol}))
        events = [{"seq": 1, "type": "init", "payload": {}}]
        if filed:
            events.append({"seq": 2, "type": "delivery", "payload": {
                "artifact_path": "x.pdf", "artifact_sha256": "0" * 64, "recipient": FILING,
                "release_dir": "comments/Journal/Paper"}})
        (self.audit / "run.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        digest = hashlib.sha256(claims.read_bytes() if receipt_binds_script else b"an earlier script").hexdigest()
        (self.audit / "finalization_receipt.json").write_text(json.dumps(
            {"artifacts": [{"path": "claims_script.R", "sha256": digest, "bytes": 1}]}))

    def check(self, *flags: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(CHECK), *flags, str(self.audit), str(self.audit / "claims_script.R")],
                              capture_output=True, text=True, env=self.env, timeout=300)

    def test_a_published_pre_2710_script_that_passes_is_republished_as_sent(self) -> None:
        self.published_audit(legacy_script(2))
        release = self.check("--release")
        self.assertEqual(release.returncode, 0, release.stdout + release.stderr)
        self.assertIn("1 checks, 1 PASS, 0 FAIL (built before 2.7.10 and already published", release.stdout)
        # without --release (the amendment tool, an operator's own run) the 2.7.10 standard still applies
        plain = self.check()
        self.assertEqual(plain.returncode, 1, plain.stdout + plain.stderr)
        self.assertIn("it was built before 2.7.10 (no 'deposit unmodified' line)", plain.stdout)

    def test_a_published_pre_2710_script_that_fails_is_named_and_never_refuses(self) -> None:
        self.published_audit(legacy_script(3))
        release = self.check("--release")
        self.assertEqual(release.returncode, 0, release.stdout + release.stderr)
        self.assertIn("WARNING: claims_script.R was built before 2.7.10 (the audit was initialised on 2.7.7)",
                      release.stdout)
        self.assertIn("re-published as it was sent, although it does not run cleanly on its own here", release.stdout)
        self.assertRegex(release.stdout, r"\[C-1\s+rows\s*\] OBSERVED  2  FAIL")
        self.assertIn("amend_claims_script.py", release.stdout)

    def test_an_unversioned_older_copy_counts_as_before_2710(self) -> None:
        self.published_audit(legacy_script(2), protocol="unversioned-skill-1a2b3c4d")
        release = self.check("--release")
        self.assertEqual(release.returncode, 0, release.stdout + release.stderr)
        self.assertIn("built before 2.7.10 and already published", release.stdout)

    def test_a_published_pre_2710_script_that_cannot_run_here_is_named_and_never_refuses(self) -> None:
        self.published_audit(legacy_script(2))
        shutil.rmtree(self.audit / "pkg")
        release = self.check("--release")
        self.assertEqual(release.returncode, 0, release.stdout + release.stderr)
        self.assertIn("although it could not be run here (the audit has no pkg/)", release.stdout)
        # any other audit without a deposit is still a setup failure
        self.published_audit(legacy_script(2), filed=False)
        self.assertEqual(self.check("--release").returncode, 2)

    def test_a_pre_2710_script_never_published_is_checked_in_full(self) -> None:
        # an in-flight audit initialised before 2.7.10: its first publication still needs a script that runs on its own
        self.published_audit(legacy_script(2), filed=False)
        release = self.check("--release")
        self.assertEqual(release.returncode, 1, release.stdout + release.stderr)
        self.assertIn("does not run cleanly on its own", release.stdout)

    def test_an_audit_initialised_on_2710_or_later_is_checked_in_full(self) -> None:
        for protocol in ("2.7.10", "2.7.11", "2.10.0", "2.7.10-dev"):
            with self.subTest(protocol=protocol):
                self.published_audit(legacy_script(2), protocol=protocol)
                release = self.check("--release")
                self.assertEqual(release.returncode, 1, release.stdout + release.stderr)

    def test_a_script_other_than_the_one_finalized_is_checked_in_full(self) -> None:
        self.published_audit(legacy_script(2), receipt_binds_script=False)
        release = self.check("--release")
        self.assertEqual(release.returncode, 1, release.stdout + release.stderr)

    def test_a_script_rebuilt_with_the_2710_harness_is_checked_in_full(self) -> None:
        # an amended (or corrected and rebuilt) script of a published pre-2.7.10 audit: the 2.7.10 standard applies
        (self.audit / "verification_snippets").mkdir()
        (self.audit / "verification_snippets" / "C-1.R").write_text(
            'expect("C-1", rows = 3, tol = 0)\ncheck("C-1", rows = nrow(read_deposit("data.csv")))\n')
        (self.audit / "comment.json").write_text(json.dumps({"findings": [
            {"id": "F1", "claim": "The deposit has three rows.", "impact": "value_wrong", "confidence": "CONFIRMED",
             "section": "Section 2", "evidence": {"operation": "count rows"}}]}))
        (self.audit / "claims_map.tsv").write_text("F1\tsnippet:verification_snippets/C-1.R\n")
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.published_audit((self.audit / "claims_script.R").read_text())
        release = self.check("--release")
        self.assertEqual(release.returncode, 1, release.stdout + release.stderr)
        self.assertIn("deposit unmodified: yes", release.stdout)
        self.assertNotIn("WARNING", release.stdout)


PUBLISH = SKILL / "scripts" / "publish_completed.sh"
RECEIPT = SKILL / "scripts" / "finalization_receipt.py"
FIXTURE_ENV = {"I4R_PUBLISH_ANY_ROOT": "1", "I4R_CREATE_REGISTRY": "1", "I4R_RESERVE_GRACE": "0", "LC_ALL": "C"}


@unittest.skipUnless(HAS_R, "R is not installed")
class RepublishThroughPublication(unittest.TestCase):
    """The same through publish_completed.sh itself: an audit published with a pre-2.7.10 claims script is published
    again with the script unchanged, while its first publication is held to the 2.7.10 standard."""

    def setUp(self) -> None:
        from tests.test_release_pipeline import make_audit
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-republish-e2e-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("I4R_")}, **FIXTURE_ENV}
        self.audit = make_audit(self.root, paper="Paper-Legacy", layout="new")

    def publish(self) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(PUBLISH), str(self.audit), str(self.root / "completed")],
                              capture_output=True, text=True, env=self.env, timeout=600)

    def replace_claims_script(self, text: str) -> None:
        """The audit as if it had been finalized with this claims script: the script, its projection row and the
        receipt (created again, as amend_claims_script.py does)."""
        claims = self.audit / "claims_script.R"
        claims.write_text(text)
        projection_path = self.audit / "work" / "finding_projections.manifest.json"
        projection = json.loads(projection_path.read_text())
        for row in projection["artifacts"]:
            if row["path"] == "claims_script.R":
                row["sha256"] = hashlib.sha256(claims.read_bytes()).hexdigest()
        projection_path.write_text(json.dumps(projection, indent=2, ensure_ascii=False) + "\n")
        receipt = json.loads((self.audit / "finalization_receipt.json").read_text())
        registry = (self.audit / receipt["registry"]["root"]).resolve()
        created = subprocess.run(["python3", str(RECEIPT), "create", str(self.audit), "--registry", str(registry)],
                                 capture_output=True, text=True, env=self.env, timeout=120)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)

    def file_as_published(self) -> None:
        """publish_completed.sh's own filing, as 2.7.9 journaled it (it ran no claims check)."""
        subprocess.run(["bash", str(SKILL / "scripts" / "log_event.sh"), "--run", str(self.audit / "run.json"),
                        "delivery", "artifact_path=I4R_comment.pdf", "artifact_sha256=" + hashlib.sha256(b"set").hexdigest(),
                        "release_dir=comments/Journal-Sanitized/Paper-Legacy",
                        "recipient=comments/ and metadata/ (filed publication set)"],
                       check=True, capture_output=True, text=True, env=self.env)

    def test_a_published_pre_2710_script_is_restaged_unchanged(self) -> None:
        legacy = legacy_script(2).replace('nrow(read.csv(file.path(PKG_DIR, "data.csv")))', "1 + 1").replace(
            "## CLAIM 1 [comment material] the deposit has two rows", "## CLAIM 1 [CORRIGENDUM MATERIAL]  (finding F1)")
        self.replace_claims_script(legacy)
        self.file_as_published()
        staged = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "stage_release.sh"), str(self.audit),
                                 str(self.root / "restaged")], capture_output=True, text=True, env=self.env, timeout=600)
        self.assertEqual(staged.returncode, 0, staged.stdout[-3000:] + staged.stderr[-3000:])
        self.assertIn("built before 2.7.10 and already published", staged.stdout)
        self.assertEqual((self.root / "restaged" / "claims_script.R").read_text(), legacy)

    def test_a_published_pre_2710_script_is_published_again_unchanged(self) -> None:
        legacy = legacy_script(2).replace('nrow(read.csv(file.path(PKG_DIR, "data.csv")))', "1 + 1").replace(
            "## CLAIM 1 [comment material] the deposit has two rows", "## CLAIM 1 [CORRIGENDUM MATERIAL]  (finding F1)")
        self.replace_claims_script(legacy)
        # its first publication: an audit never published is held to the 2.7.10 standard, as before
        first = self.publish()
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertIn("does not run on its own", first.stdout + first.stderr)
        # filed as publish_completed.sh 2.7.9 filed it (it ran no claims check); the set is then filed again
        self.file_as_published()
        again = self.publish()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("built before 2.7.10 and already published", again.stdout)
        published = self.root / "completed" / "metadata" / "Journal-Sanitized" / "Paper-Legacy" / "claims_script.R"
        self.assertEqual(published.read_text(), legacy)


if __name__ == "__main__":
    unittest.main()
