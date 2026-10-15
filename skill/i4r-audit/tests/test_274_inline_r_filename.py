"""2.7.4: a deposit file whose name reads as knitr inline R code no longer breaks the verification notebook.

The Röthlin audit (2026-10-04) deposited a file named "r code OSF.docx". make_verification_notebook.sh wrote every
deposit file name in one-backtick code spans in the notebook's deposit inventory, and knitr reads `r code OSF.docx` as
inline R code (`r expr`), so run_snippets.sh and the release render stopped with "Failed to parse the inline R code".
Escaping the generated notebook by hand did not help: stage_release.sh regenerates the notebook and requires byte
identity. The generator now pads such a name in a double-backtick span, which renders the same text and is not inline
code. Every other name keeps its one-backtick span, so existing audits' notebooks are unchanged (the 2.7.2 byte pin in
test_274_run_snippets.py still holds).

The fixture is a synthetic audit with one made-up CSV and a second deposit file named "r code.docx"; one claim with an
exact check. No provider or model is called.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
MAKE_NOTEBOOK = SCRIPTS / "make_verification_notebook.sh"
RUN_SNIPPETS = SCRIPTS / "run_snippets.sh"


def _r_ok() -> bool:
    if not shutil.which("Rscript"):
        return False
    probe = subprocess.run(["Rscript", "--no-init-file", "-e", "quit(status = !requireNamespace('rmarkdown', quietly = TRUE))"],
                           capture_output=True, timeout=120)
    return probe.returncode == 0


R_OK = _r_ok()
C1 = 'expect("C-1", mean_x = 2, tol = 0, note = "exact")\nd <- read_deposit("data.csv")\ncheck("C-1", mean_x = mean(d$x))\n'
REPORT = "## 1. Introduction\n\nThe mean of x is 2 [claim 1].\n"
INLINE_R = re.compile(r"(?<!`)`r[ #][^`]*`")


class InlineRFileName(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-274-inline-r.")
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self._cleanup)
        self.env = {**os.environ, "LC_ALL": "C", "TMPDIR": str(self.root / "tmp"), "I4R_SNIPPET_LINT": "0"}
        (self.root / "tmp").mkdir()
        a = self.audit = self.root / "audit"
        (a / "pkg").mkdir(parents=True)
        (a / "pkg" / "data.csv").write_text("x\n1\n2\n3\n")
        (a / "pkg" / "r code.docx").write_bytes(b"not a real document")
        for f in (a / "pkg").iterdir():
            f.chmod(0o444)
        (a / "pkg").chmod(0o555)
        (a / "report").mkdir(); (a / "report" / "REPORT_final.md").write_text(REPORT)
        (a / "report" / "ADJUDICATION.md").write_text("## A1. Synthetic -- A1, CONFIRMED\n")
        (a / "work").mkdir()
        (a / "work" / "verification_plan.json").write_text(json.dumps([{"claim_id": "C-1"}]))
        (a / "comment.json").write_text(json.dumps({"findings": [{"id": "F1"}]}))
        snips = a / "verification_snippets"; snips.mkdir()
        (snips / "_prelude.R").write_text("## synthetic helpers\n")
        (snips / "C-1.R").write_text(C1)

    def _cleanup(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_dir():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        self.tmp.cleanup()

    def notebook(self) -> str:
        out = self.root / "nb.Rmd"
        gen = subprocess.run(["bash", str(MAKE_NOTEBOOK), str(self.audit), "-o", str(out)], text=True,
                             capture_output=True, timeout=300, env=self.env)
        self.assertEqual(gen.returncode, 0, gen.stderr)
        return out.read_text(encoding="utf-8")

    def test_the_inventory_pads_a_name_that_reads_as_inline_r(self) -> None:
        text = self.notebook()
        self.assertIn("| `` r code.docx `` | `unknown` (19 bytes) |", text)
        prose = re.sub(r"```\{r.*?```", "", text, flags=re.S)
        self.assertEqual(INLINE_R.findall(prose), [])

    def test_every_other_name_keeps_one_backtick(self) -> None:
        self.assertIn("| `data.csv` | `csv` (8 bytes) |", self.notebook())

    @unittest.skipUnless(R_OK, "needs Rscript with rmarkdown")
    def test_the_harness_knits_and_passes(self) -> None:
        run = subprocess.run(["bash", str(RUN_SNIPPETS), str(self.audit)], text=True, capture_output=True,
                             timeout=600, env=self.env)
        # before the fix knitr evaluated `r code.docx` ("object 'code.docx' not found"; for "r code OSF.docx" a parse
        # error) and the knit stopped at the inventory line, so run_snippets exited 2 with no ledger
        self.assertNotIn("inline_exec", run.stdout + run.stderr)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertRegex(run.stdout, r"(?m)^C-1\s+PASS")


if __name__ == "__main__":
    unittest.main()
