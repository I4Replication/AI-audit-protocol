"""2.7.10: the published claims script runs on its own, as the verification notebook runs its checks.

Before 2.7.10 make_claims_script.sh gave the claims script a cut-down copy of the notebook's setup: read_deposit()
loaded csv/tsv/txt/rds only, and there was no SCRATCH, SNIPS_DIR, judgement() or per-claim error capture, and
check() refused vectors the notebook compares. Snippets are only ever run inside the notebook, so nothing caught it:
of the 122 published claims scripts with a deposit on disk, 61 stopped with an error when run on their own
(2026-10-29). Every test here builds the condition on disk and runs the real builder and R; each fails on 2.7.9.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
BUILDER = SKILL / "scripts" / "make_claims_script.sh"
CHECK = SKILL / "scripts" / "check_claims_script.sh"
HAS_R = shutil.which("Rscript") is not None

FINDINGS = [
    {"id": f"F{n}", "claim": f"Claim number {n}.", "impact": "value_wrong", "confidence": "CONFIRMED",
     "section": f"Section {n + 1}", "evidence": {"operation": f"operation {n}"}}
    for n in range(1, 7)
]


@unittest.skipUnless(HAS_R, "R is not installed")
class StandaloneClaimsScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2710-claims-")
        self.addCleanup(tmp.cleanup)
        self.audit = pathlib.Path(tmp.name) / "audit"
        (self.audit / "verification_snippets").mkdir(parents=True)
        (self.audit / "pkg").mkdir()
        (self.audit / "paper").mkdir()
        (self.audit / "paper" / "paper.txt").write_text("The mean of x is 2.0.\n")
        subprocess.run(["Rscript", "--vanilla", "-e",
                        "d <- data.frame(x = c(1, 3), y = c(2, 4)); save(d, file = 'data.RData'); "
                        "write.csv(d, 'data.csv', row.names = FALSE)"],
                       cwd=self.audit / "pkg", check=True, capture_output=True)
        self.cwd = pathlib.Path(tmp.name) / "elsewhere"
        self.cwd.mkdir()

    def snippets(self, **files: str) -> None:
        for name, text in files.items():
            (self.audit / "verification_snippets" / name.replace("__", ".")).write_text(text)
        mapped = {name.split("__")[0] for name in files if name.startswith("C_")}
        findings = [f for f in FINDINGS if f"C_{f['id'][1:]}" in mapped]
        (self.audit / "comment.json").write_text(json.dumps({"findings": findings}))
        (self.audit / "claims_map.tsv").write_text(
            "".join(f"{f['id']}\tsnippet:verification_snippets/C-{f['id'][1:]}.R\n" for f in findings))
        for f in findings:   # the builder reads C-<n>.R; the test names them C_<n>__R
            src = self.audit / "verification_snippets" / f"C_{f['id'][1:]}.R"
            src.rename(self.audit / "verification_snippets" / f"C-{f['id'][1:]}.R")

    def build_and_run(self) -> tuple[str, int]:
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        env = {k: v for k, v in os.environ.items() if not k.startswith("I4R_")}
        env["I4R_PKG_DIR"] = str(self.audit / "pkg")
        done = subprocess.run(["Rscript", "--vanilla", str(self.audit / "claims_script.R")], cwd=self.cwd,
                              env=env, capture_output=True, text=True, timeout=300)
        return done.stdout + done.stderr, done.returncode

    def summary(self, log: str) -> tuple[int, int, int]:
        lines = [l for l in log.splitlines() if l.endswith(" FAIL") and " checks, " in l]
        self.assertTrue(lines, log)
        n, p, f = (int(x) for x in lines[-1].replace(" checks,", "").replace(" PASS,", "").replace(" FAIL", "").split())
        return n, p, f

    def test_the_notebooks_helpers_exist_so_every_check_runs(self) -> None:
        self.snippets(
            _prelude__R="frame <- function() read_deposit('data.RData')$d\n",
            C_1__R='expect("C-1", mean_x = 2, tol = 0)\ncheck("C-1", mean_x = mean(frame()$x))\n',
            C_2__R='f <- file.path(SCRATCH, "t.txt"); writeLines("a", f)\n'
                   'expect("C-2", lines = 1, tol = 0)\ncheck("C-2", lines = length(readLines(f)))\n',
            C_3__R='judgement("C-3", "The column x is the variable the paper names.", "Codebook wording.")\n',
            C_4__R='expect("C-4", means = c(2, 3), tol = 0)\n'
                   'check("C-4", means = unname(colMeans(read_deposit("data.csv"))))\n',
            C_5__R='expect("C-5", has_snips = TRUE)\ncheck("C-5", has_snips = dir.exists(SNIPS_DIR))\n',
            C_6__R='expect("C-6", paper_says = TRUE)\n'
                   'check("C-6", paper_says = grepl("2.0", readLines(file.path("paper", "paper.txt"))))\n',
        )
        log, rc = self.build_and_run()
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.summary(log), (5, 5, 0), log)
        self.assertIn("claims marked JUDGEMENT: 1; UNVERIFIABLE: 0", log)
        self.assertIn("deposit unmodified: yes", log)

    def test_a_claim_that_stops_is_its_own_fail_and_the_rest_still_run(self) -> None:
        self.snippets(
            C_1__R='expect("C-1", a = 1)\nstop("this deposit file is missing")\ncheck("C-1", a = 1)\n',
            C_2__R='expect("C-2", b = 2, tol = 0)\ncheck("C-2", b = 1 + 1)\n',
        )
        log, rc = self.build_and_run()
        self.assertEqual(rc, 0, log)
        self.assertRegex(log, r"\[C-1\s+-\s*\] FAIL  runtime error: this deposit file is missing")
        self.assertRegex(log, r"\[C-2\s+b\s*\] OBSERVED  2  PASS")
        self.assertEqual(self.summary(log), (3, 1, 2), "C-1's unchecked key and its runtime error both count")
        self.assertIn("claims that stopped with an error: C-1", log)

    def test_a_missing_package_is_named_up_front_and_fails_only_its_claim(self) -> None:
        self.snippets(
            C_1__R='library(notARealPackage123)\nexpect("C-1", a = 1)\ncheck("C-1", a = 1)\n',
            C_2__R='expect("C-2", b = 2, tol = 0)\ncheck("C-2", b = 2)\n',
        )
        log, rc = self.build_and_run()
        self.assertIn("These R packages are used below but not installed: notARealPackage123", log)
        self.assertIn('install.packages("notARealPackage123")', log)
        self.assertRegex(log, r"\[C-2\s+b\s*\] OBSERVED  2  PASS")
        self.assertEqual(self.summary(log)[2], 1)

    def test_writing_beside_the_deposit_and_recording_under_another_id_are_fails(self) -> None:
        self.snippets(
            C_1__R='writeLines("x", file.path(PKG_DIR, "new.txt"))\nexpect("C-1", a = 1)\ncheck("C-1", a = 1)\n',
            C_2__R='expect("C-1", b = 2)\n',
        )
        log, rc = self.build_and_run()
        self.assertIn("deposit unmodified: NO -- INVESTIGATE", log)
        self.assertIn("FAIL  runtime error: the C-2 block recorded an outcome under C-1", log)
        self.assertGreaterEqual(self.summary(log)[2], 2)

    def test_check_claims_script_passes_a_clean_script_and_fails_a_broken_one(self) -> None:
        self.snippets(C_1__R='expect("C-1", a = 2, tol = 0)\ncheck("C-1", a = 1 + 1)\n')
        self.build_and_run()
        ok = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("claims script runs on its own: 1 checks, 1 PASS, 0 FAIL", ok.stdout)
        self.snippets(C_1__R='expect("C-1", a = 3, tol = 0)\ncheck("C-1", a = 1 + 1)\n')
        self.build_and_run()
        bad = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertIn("does not run cleanly on its own", bad.stdout)
        self.assertIn("OBSERVED  2  FAIL", bad.stdout)

    def test_a_check_that_reads_the_audits_working_files_does_not_run_on_its_own(self) -> None:
        (self.audit / "work").mkdir()
        (self.audit / "work" / "operator_check.txt").write_text("42\n")
        self.snippets(C_1__R='expect("C-1", v = 42, tol = 0)\n'
                             'check("C-1", v = as.numeric(readLines(file.path(AUDIT_DIR, "work", "operator_check.txt"))))\n',
                      C_2__R='expect("C-2", has_paper = TRUE)\n'
                             'check("C-2", has_paper = file.exists(file.path(AUDIT_DIR, "paper", "paper.txt")))\n')
        log, rc = self.build_and_run()
        self.assertEqual(self.summary(log), (2, 2, 0), "it passes from the audit folder, as the notebook did")
        bad = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertIn("C-1", bad.stdout)
        self.assertNotRegex(bad.stdout, r"C-2\s.*FAIL", "the article is there for a replicator")

    def test_a_failing_prelude_is_recorded_and_the_claims_still_run(self) -> None:
        self.snippets(_prelude__R='helper <- function() 1\nlibrary(notARealPackage123)\nlate <- function() 2\n',
                      C_1__R='expect("C-1", h = 1, tol = 0)\ncheck("C-1", h = helper())\n',
                      C_2__R='expect("C-2", l = 2, tol = 0)\ncheck("C-2", l = late())\n')
        log, rc = self.build_and_run()
        self.assertEqual(rc, 0, log)
        self.assertNotIn("Execution halted", log)
        self.assertRegex(log, r"\[prelude\s+-\s*\] FAIL  runtime error: there is no package called")
        self.assertRegex(log, r"\[C-1\s+h\s*\] OBSERVED  1  PASS", "what the prelude defined before the error stays")
        self.assertIn("the shared helpers (_prelude.R) stopped with an error", log)
        self.assertGreaterEqual(self.summary(log)[2], 2, "the prelude and C-2 (late() never defined) both FAIL")

    def test_a_deposit_file_name_with_accents_is_compared_in_any_locale(self) -> None:
        (self.audit / "pkg" / "DATA Example 4 donn\u00e9es.csv").write_text("x\n1\n")
        self.snippets(C_1__R='expect("C-1", a = 2, tol = 0)\ncheck("C-1", a = 1 + 1)\n')
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0)
        for loc in ("C", "en_US.UTF-8"):
            env = {k: v for k, v in os.environ.items() if not k.startswith(("I4R_", "LC_", "LANG"))}
            env.update(I4R_PKG_DIR=str(self.audit / "pkg"), LC_ALL=loc, LANG=loc)
            done = subprocess.run(["Rscript", "--vanilla", str(self.audit / "claims_script.R")], cwd=self.cwd, env=env,
                                  capture_output=True, text=True, timeout=300)
            self.assertIn("deposit unmodified: yes", done.stdout, f"{loc}: {done.stdout[-300:]} {done.stderr[-300:]}")

    def test_helper_files_beside_the_snippets_travel_with_the_script(self) -> None:
        (self.audit / "verification_snippets" / "py").mkdir(parents=True, exist_ok=True)
        (self.audit / "verification_snippets" / "py" / "core.py").write_text(
            'import sys\nprint(sum(int(x) for x in sys.argv[1:]))  # ]-" and ]--" stay inside\n')
        (self.audit / "verification_snippets" / "table.csv").write_text("a,b\n1,2\n")
        self.snippets(C_1__R='expect("C-1", total = 6, rows = 1, tol = 0)\n'
                             'out <- system2("python3", c(file.path(SNIPS_DIR, "py", "core.py"), "1", "2", "3"), stdout = TRUE)\n'
                             'check("C-1", total = as.numeric(out), rows = nrow(read.csv(file.path(SNIPS_DIR, "table.csv"))))\n')
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        done = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)   # the replicator layout has no snippets folder

    def test_observed_of_reaches_back_to_an_earlier_claim(self) -> None:
        self.snippets(C_1__R='expect("C-1", n = 2, tol = 0)\ncheck("C-1", n = nrow(read_deposit("data.csv")))\n',
                      C_2__R='expect("C-2", same_n = 2, tol = 0)\ncheck("C-2", same_n = observed_of("C-1", "n"))\n')
        log, rc = self.build_and_run()
        self.assertEqual(self.summary(log), (2, 2, 0), log)

    def test_the_deposits_own_file_wins_over_a_same_named_file_beside_it(self) -> None:
        (self.audit / "data.csv").write_text("x,y\n100,200\n")   # beside pkg/, i.e. in AUDIT_DIR, the working dir
        self.snippets(C_1__R='expect("C-1", mean_x = 2, tol = 0)\ncheck("C-1", mean_x = mean(read_deposit("data.csv")$x))\n')
        log, rc = self.build_and_run()
        self.assertEqual(self.summary(log), (1, 1, 0), log)

    def test_values_print_as_2_7_9_printed_them(self) -> None:
        self.snippets(C_1__R='expect("C-1", tiny = 1e-07, tol = 0)\ncheck("C-1", tiny = 1e-07)\n')
        log, rc = self.build_and_run()
        self.assertRegex(log, r"\[C-1\s+tiny\s*\] EXPECTED  1e-07")

    def test_only_packages_the_code_loads_are_named(self) -> None:
        self.snippets(_prelude__R='# rempsyc::nice_table() was tried\nok <- requireNamespace("jmv", quietly = TRUE)\n'
                                  'msg <- "see TOSTER::TOSTER for the equivalence test"\nlibrary(stats)\n',
                      C_1__R='expect("C-1", a = 1, tol = 0)\ncheck("C-1", a = utils::head(1))\n')
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        need = [l for l in (self.audit / "claims_script.R").read_text().splitlines() if l.startswith(".I4R_NEED <- ")]
        self.assertEqual(need, ['.I4R_NEED <- c()'])

    def test_a_c_locale_caller_gets_the_same_verdict(self) -> None:
        self.snippets(C_1__R='expect("C-1", minus = "\u2212", n = 1, tol = 0)\n'
                             'check("C-1", minus = "\u2212", n = nchar("\u2212"))\n')
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0)
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        done = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_script_built_before_2_7_10_is_named_as_such(self) -> None:
        (self.audit / "claims_script.R").write_text('cat("\\n1 checks, 1 PASS, 0 FAIL\\n")\nclaims_summary <- function() 0\n')
        done = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("built before 2.7.10", done.stdout)
        self.assertTrue((self.audit / "work" / "claims_check.log").is_file(), "the run's output is kept")
        self.assertIn("I4R_AUDIT_DIR", done.stdout, "the printed command reproduces the check's layout")

    def test_a_script_with_no_code_backed_claim_passes(self) -> None:
        findings = [dict(FINDINGS[0])]
        (self.audit / "comment.json").write_text(json.dumps({"findings": findings}))
        (self.audit / "claims_map.tsv").write_text("F1\tnone\n")
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        done = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("no code-backed claim", done.stdout)

    def test_check_claims_script_setup_errors_are_exit_2(self) -> None:
        self.assertEqual(subprocess.run(["bash", str(CHECK)], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True).returncode, 2,
                         "no claims script")
        (self.audit / "claims_script.do").write_text("* Stata\n")
        other = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True)
        self.assertEqual(other.returncode, 0)
        self.assertIn("is not R", other.stdout)


if __name__ == "__main__":
    unittest.main()
