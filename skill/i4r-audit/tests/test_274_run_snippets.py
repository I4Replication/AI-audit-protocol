"""2.7.4: snippets run through the release notebook's own setup before release (scripts/run_snippets.sh).

The Perfectionism audit (2026-09-30) wrote its seventeen snippets against an operator harness whose
hand-copied setup attached lavaan and psych. The generated notebook's setup attaches knitr and the
deposit loaders only, and verification_snippets/_prelude.R did not attach lavaan, so at release
run_verification.sh recorded five claims (C-1, C-2, C-5, C-10, C-12) as "could not find function sem"
and the release cycle was lost.

The fixture is a synthetic audit: a three-row CSV of made-up numbers, three claims, and a C-1 snippet
that calls toTitleCase() unqualified. tools is a base package that is installed with every R and not
attached by default, so it stands in for lavaan exactly: the snippet passes in any session that
attached it and fails in the notebook unless the prelude does. Real Rscript and rmarkdown are used
(no fakes: the defect is in what R sees); the release comparison also needs pandoc. No provider or
model is called. Every test fails on 2.7.2 except the byte pin, which guards existing audits.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
RUN_SNIPPETS = SCRIPTS / "run_snippets.sh"
MAKE_NOTEBOOK = SCRIPTS / "make_verification_notebook.sh"
RUN_VERIFICATION = SCRIPTS / "run_verification.sh"


def _r_ok() -> bool:
    if not shutil.which("Rscript"):
        return False
    probe = subprocess.run(["Rscript", "--no-init-file", "-e", "quit(status = !requireNamespace('rmarkdown', quietly = TRUE))"],
                           capture_output=True, timeout=120)
    return probe.returncode == 0


R_OK = _r_ok()
PANDOC_OK = R_OK and shutil.which("pandoc") is not None
NO_R = "needs Rscript with rmarkdown"

C1 = 'expect("C-1", title = "Hello World", note = "exact string")\ncheck("C-1", title = toTitleCase("hello world"))\n'
C2 = 'expect("C-2", mean_x = 2, tol = 0, note = "exact")\nd <- read_deposit("data.csv")\ncheck("C-2", mean_x = mean(d$x))\n'
C3 = "The deposit documents its one variable adequately.\nThe codebook names x and its unit.\n"
PRELUDE = "## synthetic helpers\ncol_mean <- function(d, v) mean(d[[v]])\n"
FIXED_PRELUDE = "suppressPackageStartupMessages(library(tools))\n" + PRELUDE
REPORT = ("## 1. Introduction\n\nThe title reads Hello World [claim 1]. The mean of x is 2 [claim 2]. "
          "The deposit is adequately documented [claim 3].\n")
# sha256 of the fixture notebook as 2.7.2 generates it (normal mode). A change here changes every
# existing audit's verification.Rmd, which generate_finding_artifacts' verify refuses byte for byte.
NOTEBOOK_SHA_272 = "eeaa439f50fe6ee6599a280ad85741a30a99712d853eecd81606a0caff553ad1"  # 2.7.2 make_verification_notebook.sh on this fixture


def tree_state(root: pathlib.Path) -> dict[str, tuple[str, int]]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return out


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-274-snippets.")
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self._cleanup)
        self.env = {**os.environ, "LC_ALL": "C", "TMPDIR": str(self.root / "tmp")}
        (self.root / "tmp").mkdir()

    def _cleanup(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_dir():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        self.tmp.cleanup()

    def audit(self, *, prelude: str = PRELUDE, projected: bool = True, name: str = "audit") -> pathlib.Path:
        a = self.root / name
        (a / "pkg").mkdir(parents=True)
        (a / "pkg" / "data.csv").write_text("x\n1\n2\n3\n")
        (a / "pkg" / "data.csv").chmod(0o444); (a / "pkg").chmod(0o555)   # read-only, as intake leaves it
        (a / "report").mkdir(); (a / "report" / "REPORT_final.md").write_text(REPORT)
        (a / "report" / "ADJUDICATION.md").write_text("## A1. Synthetic -- A1, CONFIRMED\n")
        (a / "work" / "coordinator" / "verification_R").mkdir(parents=True)
        (a / "work" / "verification_plan.json").write_text(json.dumps([{"claim_id": f"C-{n}"} for n in (1, 2, 3)]))
        (a / "comment.json").write_text(json.dumps({"findings": [{"id": f"F{n}"} for n in (1, 2, 3)]}))
        src = a / "work" / "coordinator" / "verification_R"
        (src / "f1_title.R").write_text(C1); (src / "f2_mean.R").write_text(C2)
        snips = a / "verification_snippets"; snips.mkdir()
        (snips / "_prelude.R").write_text(prelude)
        if projected:
            (snips / "C-1.R").write_text(C1); (snips / "C-2.R").write_text(C2); (snips / "C-3.judgement").write_text(C3)
        (a / "work" / "round1_decisions.json").write_text(json.dumps({
            "F1": {"verification_snippet": {"kind": "code", "path": "work/coordinator/verification_R/f1_title.R", "basis": "b"}},
            "F2": {"verification_snippet": {"kind": "code", "path": "work/coordinator/verification_R/f2_mean.R", "basis": "b"}},
            "F3": {"verification_snippet": {"kind": "judgement", "path": "n/a", "basis": C3}}}))
        return a

    def run_snippets(self, audit: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(RUN_SNIPPETS), str(audit), *args], text=True,
                              capture_output=True, timeout=600, env=self.env)

    def verdicts(self, stdout: str) -> dict[str, str]:
        rows = [line.split() for line in stdout.splitlines() if line.startswith("C-")]
        return {row[0]: row[1] for row in rows}

    def release(self, audit: pathlib.Path) -> tuple[int, dict[str, set[str]]]:
        """The real release path on a copy: generate the notebook, render and bind it."""
        gen = subprocess.run(["bash", str(MAKE_NOTEBOOK), str(audit)], text=True, capture_output=True,
                             timeout=300, env={**self.env, "I4R_SNIPPET_LINT": "0"})
        self.assertEqual(gen.returncode, 0, gen.stderr)
        run = subprocess.run(["bash", str(RUN_VERIFICATION), str(audit)], text=True, capture_output=True,
                             timeout=600, env=self.env)
        with open(audit / "verification_summary.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        out: dict[str, set[str]] = {}
        for row in rows:
            out.setdefault(row["claim"], set()).add(row["verdict"])
        return run.returncode, out


@unittest.skipUnless(R_OK, NO_R)
class HarnessReproducesTheReleaseNotebook(Fixture):
    def test_unattached_package_fails_in_the_harness_as_in_the_release(self) -> None:
        audit = self.audit()
        before = tree_state(audit)
        result = self.run_snippets(audit)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.verdicts(result.stdout), {"C-1": "FAIL", "C-2": "PASS", "C-3": "JUDGEMENT"})
        self.assertIn('could not find function "toTitleCase"', result.stdout)
        self.assertEqual(tree_state(audit), before, "the harness must write nothing into the audit")
        for bound in ("verification.Rmd", "verification_status.txt", "verification_summary.csv",
                      "verification_manifest.json", "verification.html"):
            self.assertFalse((audit / bound).exists(), bound)

    @unittest.skipUnless(PANDOC_OK, "the release comparison renders HTML and needs pandoc")
    def test_the_release_render_fails_the_same_claims(self) -> None:
        rc, verdicts = self.release(self.audit(name="release"))
        self.assertNotEqual(rc, 0)
        self.assertEqual({c for c, v in verdicts.items() if "FAIL" in v}, {"C-1"})
        harness = self.run_snippets(self.audit(name="harness"))
        self.assertEqual({c for c, v in self.verdicts(harness.stdout).items() if v == "FAIL"}, {"C-1"})

    def test_attaching_the_package_in_the_prelude_passes(self) -> None:
        result = self.run_snippets(self.audit(prelude=FIXED_PRELUDE))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.verdicts(result.stdout), {"C-1": "PASS", "C-2": "PASS", "C-3": "JUDGEMENT"})

    def test_an_operator_session_that_attached_the_package_is_not_the_notebook(self) -> None:
        # the 2.7.2 practice: a hand-made harness with its own library() lines passes the same snippet
        audit = self.audit()
        probe = subprocess.run(["Rscript", "--no-init-file", "-e",
                                "library(tools); expect <- function(...) 0; check <- function(id, title) "
                                "quit(status = !identical(title, 'Hello World')); source(commandArgs(TRUE)[1])",
                                str(audit / "verification_snippets" / "C-1.R")], capture_output=True, timeout=120)
        self.assertEqual(probe.returncode, 0)
        self.assertEqual(self.run_snippets(audit, "C-1").returncode, 1)


@unittest.skipUnless(R_OK, NO_R)
class SourcesBeforeTheProjection(Fixture):
    """Snippets are written before round-1 decisions exist; nothing is projected yet."""

    def test_decisions_file_runs_every_source(self) -> None:
        audit = self.audit(projected=False)
        result = self.run_snippets(audit, "--decisions", "work/round1_decisions.json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.verdicts(result.stdout), {"C-1": "FAIL", "C-2": "PASS", "C-3": "JUDGEMENT"})

    def test_named_and_inferred_sources(self) -> None:
        audit = self.audit(prelude=FIXED_PRELUDE, projected=False)
        named = self.run_snippets(audit, "C-1=work/coordinator/verification_R/f1_title.R")
        self.assertEqual(self.verdicts(named.stdout), {"C-1": "PASS"}, named.stdout + named.stderr)
        inferred = self.run_snippets(audit, "work/coordinator/verification_R/f2_mean.R")
        self.assertEqual(self.verdicts(inferred.stdout), {"C-2": "PASS"}, inferred.stdout + inferred.stderr)

    def test_a_projected_id_that_is_missing_names_the_remedy(self) -> None:
        result = self.run_snippets(self.audit(projected=False), "C-1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("C-1=<path>", result.stderr)

    def test_a_stale_claim_id_fails_as_in_the_notebook(self) -> None:
        audit = self.audit(prelude=FIXED_PRELUDE, projected=False)
        result = self.run_snippets(audit, "C-3=work/coordinator/verification_R/f1_title.R")
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale", result.stdout)

    def test_the_harness_never_overwrites_the_release_notebook(self) -> None:
        audit = self.audit()
        spec = self.root / "spec.json"
        spec.write_text(json.dumps({"claims": [{"id": "C-1", "path": str(audit / "verification_snippets/C-1.R")}],
                                    "summary_csv": str(self.root / "s.csv"), "status_txt": str(self.root / "s.txt")}))
        made = subprocess.run(["bash", str(MAKE_NOTEBOOK), str(audit), "--harness", str(spec), "-o", str(self.root / "h.Rmd")],
                              text=True, capture_output=True, timeout=120, env=self.env)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertIn("```{r C_1}", (self.root / "h.Rmd").read_text())
        self.assertIn(json.dumps(str(self.root / "s.csv")), (self.root / "h.Rmd").read_text())
        refused = subprocess.run(["bash", str(MAKE_NOTEBOOK), str(audit), "--harness", str(spec)],
                                 text=True, capture_output=True, timeout=120, env=self.env)
        self.assertEqual(refused.returncode, 2)
        self.assertFalse((audit / "verification.Rmd").exists())


@unittest.skipUnless(R_OK, NO_R)
class GeneratorLint(Fixture):
    def generate(self, audit: pathlib.Path, out: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(MAKE_NOTEBOOK), str(audit), "-o", str(self.root / out)], text=True,
                              capture_output=True, timeout=300, env={**self.env, **env})

    def test_warns_on_the_unattached_call_and_not_after_the_fix(self) -> None:
        warned = self.generate(self.audit(), "a.Rmd")
        self.assertEqual(warned.returncode, 0)
        self.assertIn("WARN: C-1 calls toTitleCase()", warned.stderr)
        self.assertIn("tools", warned.stderr)
        clean = self.generate(self.audit(prelude=FIXED_PRELUDE, name="fixed"), "b.Rmd")
        self.assertNotIn("WARN:", clean.stderr)

    def test_the_lint_never_changes_the_notebook(self) -> None:
        audit = self.audit()
        on, off = self.generate(audit, "on.Rmd"), self.generate(audit, "off.Rmd", I4R_SNIPPET_LINT="0")
        self.assertEqual((on.returncode, off.returncode), (0, 0))
        self.assertEqual((self.root / "on.Rmd").read_bytes(), (self.root / "off.Rmd").read_bytes())
        self.assertNotIn("WARN:", off.stderr)

    def test_quiet_on_non_standard_evaluation_and_loud_inside_a_qualified_call(self) -> None:
        audit = self.audit(prelude=FIXED_PRELUDE)
        (audit / "verification_snippets" / "C-2.R").write_text(
            'expect("C-2", n = 1)\nq <- bquote(interpSpline(.(x)))\nf <- y ~ bs(x)\n'
            'd2 <- d[, .(n = periodicSpline(id))]\ncheck("C-2", n = 1)\n')
        quiet = self.generate(audit, "q.Rmd")
        self.assertNotIn("WARN:", quiet.stderr)
        (audit / "verification_snippets" / "C-2.R").write_text(
            'expect("C-2", n = 1)\nk <- tools::file_ext(interpSpline(1:3, 1:3))\ncheck("C-2", n = 1)\n')
        loud = self.generate(audit, "l.Rmd")
        self.assertIn("WARN: C-2 calls interpSpline()", loud.stderr)


class NotebookBytesForExistingAudits(Fixture):
    """Normal mode is untouched: an existing audit's verification.Rmd is regenerated byte for byte."""

    def test_fixture_notebook_is_the_272_bytes(self) -> None:
        base = os.environ.get("I4R_BASE_272")
        if not base and NOTEBOOK_SHA_272 is None:
            self.skipTest("set I4R_BASE_272 to a pristine 2.7.2 tree (or pin NOTEBOOK_SHA_272)")
        audit = self.audit()
        env = {**self.env, "I4R_SNIPPET_LINT": "0"}
        subprocess.run(["bash", str(MAKE_NOTEBOOK), str(audit), "-o", str(self.root / "cand.Rmd")],
                       check=True, capture_output=True, env=env, timeout=300)
        got = hashlib.sha256((self.root / "cand.Rmd").read_bytes()).hexdigest()
        if base:
            subprocess.run(["bash", str(pathlib.Path(base) / "scripts/make_verification_notebook.sh"), str(audit),
                            "-o", str(self.root / "base.Rmd")], check=True, capture_output=True, env=env, timeout=300)
            self.assertEqual(got, hashlib.sha256((self.root / "base.Rmd").read_bytes()).hexdigest())
        if NOTEBOOK_SHA_272:
            self.assertEqual(got, NOTEBOOK_SHA_272)


if __name__ == "__main__":
    unittest.main()
