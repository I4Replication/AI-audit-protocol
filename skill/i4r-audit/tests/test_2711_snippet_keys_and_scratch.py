"""2.7.11: snippet keys holding more than one value, and the snippet harness's scratch folder.

1. One value per key. On 2026-10-27 two operators (2.7.9) wrote expect("C-n", key = c(...)) and the
   matching check(). The notebook's check() compares equal-length vectors and run_snippets.sh printed
   PASS, but the summary chunk writes one row per element under the same claim and key, so
   run_verification.sh refused the notebook ("verification summary repeats claim/key pair") and the
   rule was nowhere in reference/verification_guide.md. Vectors of different lengths are worse: the
   summary chunk stops, no status is written, and both tools said nothing about why.
2. The harness's scratch folder. Every run_snippets.sh harness lived in $TMPDIR/i4r-snippets.XXXXXX, a
   prefix shared by every audit run under the account, and an operator cleaned up kept harnesses with
   rm -rf "$TMPDIR"/i4r-snippets.* while other audits' operators were running theirs.

The fixtures are synthetic audits (a three-row CSV of made-up numbers). Real Rscript and rmarkdown are
used; the release comparison also needs pandoc. No provider or model is called. Every test but the three
controls fails on 2.7.10.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
RUN_SNIPPETS = SCRIPTS / "run_snippets.sh"
MAKE_NOTEBOOK = SCRIPTS / "make_verification_notebook.sh"
RUN_VERIFICATION = SCRIPTS / "run_verification.sh"


def _r_ok() -> bool:
    if not shutil.which("Rscript"):
        return False
    probe = subprocess.run(["Rscript", "--no-init-file", "-e",
                            "quit(status = !requireNamespace('rmarkdown', quietly = TRUE))"],
                           capture_output=True, timeout=120)
    return probe.returncode == 0


R_OK = _r_ok()
PANDOC_OK = R_OK and shutil.which("pandoc") is not None
NO_R = "needs Rscript with rmarkdown"
NO_PANDOC = "the release render writes HTML and needs pandoc"

REPORT = ("## 1. Data\n\nThe column means are 2 and 5 [claim 1]. The deposit has 3 rows [claim 2].\n")
C2 = 'expect("C-2", n = 3, note = "rows")\nd <- read_deposit("data.csv")\ncheck("C-2", n = nrow(d))\n'
VECTOR = ('expect("C-1", means = c(2, 5), tol = 0, note = "column means")\n'
          'd <- read_deposit("data.csv")\ncheck("C-1", means = unname(colMeans(d)))\n')
UNEQUAL = ('expect("C-1", means = c(2, 5), tol = 0, note = "column means")\n'
           'd <- read_deposit("data.csv")\ncheck("C-1", means = c(unname(colMeans(d)), 9))\n')
EMPTY = ('expect("C-1", means = numeric(0), tol = 0, note = "an empty value")\n'
         'd <- read_deposit("data.csv")\ncheck("C-1", means = numeric(0))\n')
SCALAR = ('expect("C-1", mean_x = 2, mean_y = 5, tol = 0, note = "column means")\n'
          'd <- read_deposit("data.csv")\nm <- colMeans(d)\ncheck("C-1", mean_x = m[["x"]], mean_y = m[["y"]])\n')
# Holds its knit open until the test says so: the snippet marks that it runs, then waits for a flag.
WAITING = ('expect("C-1", released = 1, note = "the test releases this check")\n'
           'file.create(Sys.getenv("I4R_TEST_STARTED"))\n'
           'for (i in seq_len(1200)) { if (file.exists(Sys.getenv("I4R_TEST_FLAG"))) break; Sys.sleep(0.1) }\n'
           'check("C-1", released = as.numeric(file.exists(Sys.getenv("I4R_TEST_FLAG"))))\n')


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-snippets.")
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.scratch = self.root / "tmp"
        self.scratch.mkdir()
        self.env = {**os.environ, "LC_ALL": "C", "TMPDIR": str(self.scratch), "I4R_SNIPPET_LINT": "0"}

    def audit(self, c1: str, *, name: str = "audit", audit_id: str | None = None) -> pathlib.Path:
        a = self.root / name
        (a / "pkg").mkdir(parents=True)
        (a / "pkg" / "data.csv").write_text("x,y\n1,4\n2,5\n3,6\n")
        (a / "report").mkdir()
        (a / "report" / "REPORT_final.md").write_text(REPORT)
        (a / "report" / "ADJUDICATION.md").write_text("## A1. Synthetic -- A1, CONFIRMED\n")
        (a / "work").mkdir()
        (a / "work" / "verification_plan.json").write_text(json.dumps([{"claim_id": "C-1"}, {"claim_id": "C-2"}]))
        (a / "comment.json").write_text(json.dumps({"findings": [{"id": "F1"}, {"id": "F2"}]}))
        snips = a / "verification_snippets"
        snips.mkdir()
        (snips / "C-1.R").write_text(c1)
        (snips / "C-2.R").write_text(C2)
        if audit_id:
            (a / "run.json").write_text(json.dumps({"audit_id": audit_id}))
        return a

    def snippets(self, audit: pathlib.Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(RUN_SNIPPETS), str(audit), *args], text=True, capture_output=True,
                              timeout=600, env=env or self.env)

    def release(self, audit: pathlib.Path) -> subprocess.CompletedProcess:
        gen = subprocess.run(["bash", str(MAKE_NOTEBOOK), str(audit)], text=True, capture_output=True,
                             timeout=300, env=self.env)
        self.assertEqual(gen.returncode, 0, gen.stderr)
        return subprocess.run(["bash", str(RUN_VERIFICATION), str(audit)], text=True, capture_output=True,
                              timeout=600, env=self.env)

    @staticmethod
    def verdicts(stdout: str) -> dict[str, str]:
        return {line.split()[0]: line.split()[1] for line in stdout.splitlines() if line.startswith("C-")}

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.scratch.iterdir()
                      if p.name.startswith(("i4r-snippets", "i4r-running-snippets")))

    def assert_removal_printed_last(self, result: subprocess.CompletedProcess) -> pathlib.Path:
        last = result.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("run_snippets: remove it with: rm -rf '"), last)
        kept = pathlib.Path(last.split("'")[1])
        self.assertEqual(kept.parent, self.scratch)
        self.assertTrue(kept.is_dir(), kept)
        return kept


@unittest.skipUnless(R_OK, NO_R)
class OneValuePerKey(Fixture):
    def test_the_harness_fails_a_key_holding_several_values(self) -> None:
        result = self.snippets(self.audit(VECTOR))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.verdicts(result.stdout), {"C-1": "FAIL", "C-2": "PASS"})
        self.assertIn("means: one key holds 2 values", result.stdout)
        self.assertIn("give each value its own key", result.stdout)

    @unittest.skipUnless(PANDOC_OK, NO_PANDOC)
    def test_the_release_refuses_the_same_key_and_says_why(self) -> None:
        audit = self.audit(VECTOR)
        result = self.release(audit)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("repeats claim/key pair C-1/means", result.stderr)
        self.assertIn("give each value its own key", result.stderr)
        self.assertFalse((audit / "verification_manifest.json").exists())

    def test_vectors_of_different_lengths_are_named_by_the_harness(self) -> None:
        result = self.snippets(self.audit(UNEQUAL), "--keep")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("differing number of rows", result.stderr)
        self.assertIn("not one number or one string", result.stderr)
        self.assertIn("give each value its own key", result.stderr)
        self.assertIn("C-1/means", result.stderr)
        self.assert_removal_printed_last(result)

    def test_an_empty_value_is_named_by_the_harness(self) -> None:
        result = self.snippets(self.audit(EMPTY))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("differing number of rows: 1, 0", result.stderr)
        self.assertIn("not one number or one string", result.stderr)
        self.assertIn("C-1/means", result.stderr)

    @unittest.skipUnless(PANDOC_OK, NO_PANDOC)
    def test_vectors_of_different_lengths_are_explained_by_the_release(self) -> None:
        audit = self.audit(UNEQUAL)
        result = self.release(audit)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("summary chunk did not finish", result.stderr)
        self.assertIn("differing number of rows", result.stderr)
        self.assertIn("give each value its own key", result.stderr)
        self.assertIn("C-1/means", result.stderr)
        self.assertFalse((audit / "verification_manifest.json").exists())

    def test_control_one_key_per_value_passes_the_harness(self) -> None:
        result = self.snippets(self.audit(SCALAR))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.verdicts(result.stdout), {"C-1": "PASS", "C-2": "PASS"})

    @unittest.skipUnless(PANDOC_OK, NO_PANDOC)
    def test_control_one_key_per_value_passes_the_release(self) -> None:
        audit = self.audit(SCALAR)
        result = self.release(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((audit / "verification_manifest.json").is_file())


@unittest.skipUnless(R_OK, NO_R)
class ScratchFolder(Fixture):
    def test_a_kept_harness_is_named_for_its_audit_and_printed_last(self) -> None:
        result = self.snippets(self.audit(SCALAR, audit_id="20261101-audit-0a1b2c3d"), "--keep")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kept = self.assert_removal_printed_last(result)
        self.assertTrue(kept.name.startswith("i4r-snippets.0a1b2c3d."), kept.name)
        self.assertIn(f"harness kept in {kept} ", result.stdout.strip().splitlines()[-2])
        for name in ("run_snippets.Rmd", "run_snippets.knit.md", "knit.log"):
            self.assertTrue((kept / name).is_file(), name)
        self.assertEqual(self.leftovers(), [kept.name], "only the kept harness stays behind")

    def test_a_failing_run_also_prints_its_kept_harness_last(self) -> None:
        result = self.snippets(self.audit(VECTOR, audit_id="20261101-audit-0a1b2c3d"), "--keep")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        kept = self.assert_removal_printed_last(result)
        self.assertTrue(kept.name.startswith("i4r-snippets.0a1b2c3d."), kept.name)

    def test_an_audit_without_run_json_still_runs_and_keeps_a_named_harness(self) -> None:
        audit = self.audit(SCALAR)
        self.assertFalse((audit / "run.json").exists())
        result = self.snippets(audit, "--keep")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.verdicts(result.stdout), {"C-1": "PASS", "C-2": "PASS"})
        kept = self.assert_removal_printed_last(result)
        # no audit id: the tag is the cksum of the audit path, the same for every run of that audit
        self.assertRegex(kept.name, r"^i4r-snippets\.[0-9]+\.[0-9]{8}T[0-9]{6}Z\.[0-9]+$")
        again = self.snippets(audit, "--keep")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.assert_removal_printed_last(again).name.split(".")[1], kept.name.split(".")[1])

    def test_control_without_keep_nothing_stays_behind(self) -> None:
        result = self.snippets(self.audit(SCALAR))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.leftovers(), [])

    def test_removing_kept_harnesses_spares_a_running_one(self) -> None:
        # audit B keeps a harness; audit A's operator runs one that is still knitting when B's operator
        # cleans up with the 2026-10-27 command
        kept_b = self.snippets(self.audit(SCALAR, name="b", audit_id="20261101-audit-bbbbbbbb"), "--keep")
        self.assertEqual(kept_b.returncode, 0, kept_b.stdout + kept_b.stderr)
        started, flag = self.root / "started", self.root / "flag"
        env = {**self.env, "I4R_TEST_STARTED": str(started), "I4R_TEST_FLAG": str(flag)}
        running = subprocess.Popen(["bash", str(RUN_SNIPPETS), str(self.audit(WAITING, name="a",
                                    audit_id="20261101-audit-aaaaaaaa")), "C-1"],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            deadline = time.monotonic() + 300
            while not started.exists():
                self.assertIsNone(running.poll(), "the waiting harness ended before its snippet ran")
                self.assertLess(time.monotonic(), deadline, "the waiting snippet never started")
                time.sleep(0.2)
            while_running = self.leftovers()
            subprocess.run(["bash", "-c", 'rm -rf "$TMPDIR"/i4r-snippets.*; rm -rf "$TMPDIR"/i4r-snippets*'],
                           env=self.env, check=True)
            self.assertFalse(any(name.startswith("i4r-snippets.bbbbbbbb.") for name in self.leftovers()),
                             "the cleanup removes kept harnesses")
            flag.touch()
            stdout, stderr = running.communicate(timeout=300)
        finally:
            if running.poll() is None:
                running.kill()
                running.communicate()
        self.assertEqual(running.returncode, 0, stdout + stderr)
        self.assertEqual(self.verdicts(stdout), {"C-1": "PASS"})
        self.assertEqual(self.leftovers(), [])
        # the running harness was named for its audit, under a prefix no cleanup glob of kept ones matches
        self.assertTrue(any(re.fullmatch(r"i4r-running-snippets\.aaaaaaaa\.\w+", name) for name in while_running),
                        while_running)


if __name__ == "__main__":
    unittest.main()
