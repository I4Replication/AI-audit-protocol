"""2.7.11: a published claims script names what it needs from outside R, and runs in a C locale.

Found 2026-10-30 by re-running every published claims script on its own, outside the checker's environment:
- checks call pdftotext/pdfimages, python3 with numpy and scipy, and macOS's textutil at run time, and the script
  named none of them: with Poppler 21.03 (the audit ran 26.07) one script FAILs several checks, without scipy another
  FAILs many with "subscript out of bounds", and on a machine without textutil a third FAILs a few with "error in
  running command";
- in a C locale R reads the script's non-ASCII literals and the deposit's text as bytes it cannot match: 27 of 64
  scripts built by 2.7.10 record FAILs, nine because the shared prelude stops, after which most claims fail;
- a manual check names the audit's own working files (phase1/quorum.json, report/0N_*.md), which a replicator does
  not have;
- the header told the replicator to put pkg/ beside the working folder; the code reads pkg/ inside it.
The 2.7.11 harness is frozen per audit (publication.claims_script_harness in pipeline-policy.snapshot.json): an audit
initialised before 2.7.11 froze no leaf and its claims script is rebuilt byte for byte as 2.7.10 built it.
Every test builds the condition on disk and runs the real builder and R (the checker where it applies); each fails
on 2.7.10.
"""
from __future__ import annotations

import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
BUILDER = SKILL / "scripts" / "make_claims_script.sh"
CHECK = SKILL / "scripts" / "check_claims_script.sh"
HAS_R = shutil.which("Rscript") is not None
DASH = "−"   # a non-ASCII character, written literally into the R sources below


def finding(n: int, **extra) -> dict:
    row = {"id": f"F{n}", "claim": f"Claim number {n}.", "impact": "value_wrong", "confidence": "CONFIRMED",
           "section": f"Section {n + 1}", "evidence": {"operation": f"operation {n}"}}
    row.update(extra)
    return row


@unittest.skipUnless(HAS_R, "R is not installed")
class ClaimsScriptPortabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-claims-")
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.audit = self.tmp / "audit"
        (self.audit / "verification_snippets").mkdir(parents=True)
        (self.audit / "pkg").mkdir()
        (self.audit / "paper").mkdir()
        (self.audit / "paper" / "paper.txt").write_text("The mean of x is 2.0.\n")
        (self.audit / "pkg" / "data.csv").write_text("x,y\n1,2\n3,4\n")
        self.cwd = self.tmp / "elsewhere"
        self.cwd.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.freeze({"claims_script_harness": "portable_1"})   # what init_run.sh freezes from the 2.7.11 policy

    def freeze(self, publication: dict | None) -> None:
        snapshot = self.audit / "pipeline-policy.snapshot.json"
        if publication is None:
            snapshot.unlink(missing_ok=True)
        else:
            snapshot.write_text(json.dumps({"snapshot_version": "1", "policy": {"publication": publication}}))

    # ---- helpers ------------------------------------------------------------------------------------------------
    def write(self, snippets: dict, tail: dict | None = None) -> None:
        """snippets: {"_prelude": text, "C-1": text, ...}; tail: {n: operation} for claims with no code."""
        findings, rows = [], []
        for name, text in snippets.items():
            (self.audit / "verification_snippets" / f"{name}.R").write_text(text, encoding="utf-8")
            if name.startswith("C-"):
                n = int(name[2:])
                findings.append(finding(n)); rows.append(f"F{n}\tsnippet:verification_snippets/{name}.R\n")
        for n, operation in (tail or {}).items():
            findings.append(finding(n, evidence={"operation": operation})); rows.append(f"F{n}\tnone\n")
        findings.sort(key=lambda f: int(f["id"][1:]))
        (self.audit / "comment.json").write_text(json.dumps({"findings": findings}))
        (self.audit / "claims_map.tsv").write_text("".join(rows))

    def env(self, path_first: list[pathlib.Path] = (), **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("I4R_", "LC_", "LANG"))}
        env["PATH"] = os.pathsep.join([str(p) for p in path_first] + [env.get("PATH", "")])
        env.update(extra)
        return env

    def build(self, env: dict | None = None) -> str:
        built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True,
                               env=env or self.env())
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        return built.stdout

    def run_script(self, env: dict, cwd: pathlib.Path | None = None) -> str:
        env = {**env, "I4R_PKG_DIR": str(self.audit / "pkg")} if cwd is None else env
        done = subprocess.run(["Rscript", "--vanilla", str(self.audit / "claims_script.R")], cwd=cwd or self.cwd,
                              env=env, capture_output=True, text=True, timeout=300)
        return done.stdout + done.stderr

    def summary(self, log: str) -> tuple[int, int, int]:
        lines = [l for l in log.splitlines() if l.endswith(" FAIL") and " checks, " in l]
        self.assertTrue(lines, log)
        n, p, f = (int(x) for x in lines[-1].replace(" checks,", "").replace(" PASS,", "").replace(" FAIL", "").split())
        return n, p, f

    def record_environment(self, pdftotext: str = "", python: str = "", packages: dict | None = None) -> None:
        """The audit's own records of its machine: Phase 1's environment fingerprint and run.json's environment."""
        snap = "ab" * 32
        (self.audit / "phase1" / "snapshots" / snap / "evidence").mkdir(parents=True, exist_ok=True)
        (self.audit / "phase1" / "quorum.json").write_text(json.dumps({"source_snapshot_sha256": snap}))
        (self.audit / "phase1" / "snapshots" / snap / "evidence" / "environment_fingerprint.json").write_text(
            json.dumps({"tools": {"pdftotext": f"pdftotext version {pdftotext}" if pdftotext else None,
                                  "python": f"Python {python}" if python else None}}))
        (self.audit / "run.json").write_text(json.dumps({"environment": {
            "python": f"Python {python}" if python else None, "python_packages": packages or {}}}))

    def fake_tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    # ---- the gate: an audit initialised before 2.7.11 keeps 2.7.10's script ---------------------------------------
    def test_an_audit_that_froze_no_harness_gets_the_2_7_10_script(self) -> None:
        self.record_environment(pdftotext="26.08.0")
        self.write({"_prelude": f'TXT <- function() system2("pdftotext", c("-raw", "x.pdf", "-"), stdout = TRUE)\n',
                    "C-1": 'expect("C-1", two = 2, tol = 0)\ncheck("C-1", two = 1 + 1)\n'},
                   tail={2: "See the manifest bound in phase1/quorum.json"})
        builds = []
        for publication in (None, {}, {"primary_artifact_count": 6}):   # no snapshot; snapshots without the leaf
            self.freeze(publication)
            self.build()
            builds.append((self.audit / "claims_script.R").read_bytes())
        self.assertEqual(len(set(builds)), 1)
        legacy = builds[0].decode()
        self.assertIn("(or a pkg/\n## folder beside the working directory)", legacy, "2.7.10's header, unchanged")
        for added in (".I4R_TOOLS", "Sys.setlocale", "getRversion", "NOT PUBLISHED", "outside programs"):
            self.assertNotIn(added, legacy)
        self.freeze({"claims_script_harness": "portable_1"})
        self.build()
        self.assertIn(".I4R_TOOLS", (self.audit / "claims_script.R").read_text())

    def test_a_harness_the_release_does_not_ship_is_refused(self) -> None:
        self.write({"C-1": 'expect("C-1", two = 2, tol = 0)\ncheck("C-1", two = 1 + 1)\n'})
        for value in ("notebook_setup_1", "portable_9"):
            self.freeze({"claims_script_harness": value})
            built = subprocess.run(["bash", str(BUILDER), str(self.audit)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 1, built.stdout + built.stderr)
            self.assertIn("not a packaged claims-script harness", built.stdout)
            self.assertFalse((self.audit / "claims_script.R").exists())

    # ---- B6: a C locale --------------------------------------------------------------------------------------------
    def test_a_c_locale_replicator_gets_the_verdict_the_notebook_got(self) -> None:
        self.write({
            "_prelude": f'CLEAN <- gsub("{DASH}", "-", "x\\u2212y")\nlate <- function() 2\n',
            "C-1": f'expect("C-1", s = "x-y")\ncheck("C-1", s = CLEAN)\n',
            "C-2": 'expect("C-2", l = 2, tol = 0)\ncheck("C-2", l = late())\n',
            "C-3": f'expect("C-3", n = 3, tol = 0)\ncheck("C-3", n = nchar("a{DASH}b"))\n',
        })
        self.build()
        listed = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()
        loc = next((l for l in listed if l.lower().replace("-", "") in ("en_us.utf8", "c.utf8")), "C.UTF-8")
        utf8 = self.run_script(self.env(LC_ALL=loc, LANG=loc))
        self.assertEqual(self.summary(utf8), (3, 3, 0), utf8)
        c = self.run_script(self.env(LC_ALL="C", LANG="C"))
        # 2.7.10: the prelude stops ('pattern' is invalid), late() is never defined, nchar counts bytes: 4 FAIL
        self.assertNotIn("FAIL  runtime error", c)
        self.assertEqual(self.summary(c), (3, 3, 0), c)
        self.assertIn("it now runs in", c)

    # ---- B1: an outside program, older than the audit's ------------------------------------------------------------
    def test_an_older_or_missing_outside_program_is_named_up_front_and_changes_no_verdict(self) -> None:
        self.fake_tool("pdftotext", 'if [ "$1" = "-v" ]; then echo "pdftotext version ${FAKE_POPPLER:-26.07.0}" >&2; exit 0; fi\n'
                                    'echo "the article text"\n')
        self.fake_tool("i4rnosuchtool", 'echo 42\n')
        self.write({
            "_prelude": 'ARTICLE <- function() paste(system2("pdftotext", c("-q", "-raw", "x.pdf", "-"), stdout = TRUE), collapse = " ")\n'
                        'paper_has <- function(s) grepl(s, ARTICLE(), fixed = TRUE)\n',
            "C-1": 'expect("C-1", quoted = TRUE)\ncheck("C-1", quoted = paper_has("article text"))\n',
            "C-2": 'expect("C-2", two = 2, tol = 0)\ncheck("C-2", two = 1 + 1)\n',
            "C-3": 'expect("C-3", v = 42, tol = 0)\ncheck("C-3", v = as.numeric(system2("i4rnosuchtool", stdout = TRUE)))\n',
        })
        self.record_environment(pdftotext="26.07.0")
        self.build(self.env([self.bin], FAKE_POPPLER="21.03.0"))   # the audit's record decides, not this machine
        same = self.run_script(self.env([self.bin], FAKE_POPPLER="26.07.0"))
        self.assertEqual(self.summary(same), (3, 3, 0), same)
        self.assertNotIn("Outside program", same)
        older = self.run_script(self.env([self.bin], FAKE_POPPLER="21.03.0"))
        self.assertRegex(older, r"Outside program pdftotext: 21\.03\.0 is OLDER than the audit's 26\.07\.0.*\(claims C-1\)")
        self.assertEqual(self.summary(older), self.summary(same), "the lines say why; they never change a verdict")
        self.assertNotIn("outside programs missing or older", older, "nothing FAILed, so there is nothing to explain")
        newer_minor = self.run_script(self.env([self.bin], FAKE_POPPLER="26.01.0"))
        self.assertNotIn("Outside program", newer_minor, "an older minor release of the same major is not flagged")
        (self.bin / "i4rnosuchtool").unlink()
        missing = self.run_script(self.env([self.bin], FAKE_POPPLER="26.07.0"))
        self.assertRegex(missing, r"Outside program i4rnosuchtool: MISSING: install i4rnosuchtool.*\(claims C-3\)")
        self.assertIn("outside programs missing or older than the audit's (named at the top): i4rnosuchtool", missing)
        self.assertEqual(self.summary(missing)[2], 2, "C-3's unchecked key and its runtime error, as on 2.7.10")

    def test_the_build_is_the_same_bytes_whatever_this_machine_has(self) -> None:
        # generate_finding_artifacts.py --check binds the claims script's bytes: a rebuild in a shell with another
        # Poppler or Python on PATH must not change them. The builder itself runs under the first python3 on PATH,
        # and Python 3.9 (macOS's /usr/bin/python3) has no sys.stdlib_module_names, so the standard library the
        # declared modules leave out must not be that Python's own list either.
        self.fake_tool("pdftotext", 'echo "pdftotext version ${FAKE_POPPLER:-26.07.0}" >&2\n')
        self.record_environment(pdftotext="26.08.0", python="3.14.2", packages={"numpy": "2.4.0"})
        (self.audit / "verification_snippets" / "core.py").write_text(
            "import json, zoneinfo\nfrom collections import Counter\nimport numpy as np\nprint(json.dumps(1))\n")
        self.write({"_prelude": 'TXT <- function() system2("pdftotext", c("-raw", "x.pdf", "-"), stdout = TRUE)\n'
                                'PY <- function() system2("python3", c("-c", shQuote(r"-(\nimport numpy\nprint(1)\n)-")), stdout = TRUE)\n',
                    "C-1": 'expect("C-1", n = 1, tol = 0)\ncheck("C-1", n = length(TXT()) + 0 * nchar(PY()))\n'})
        pythons = {}
        for candidate in (sys.executable, "/usr/bin/python3", shutil.which("python3") or ""):
            if candidate and os.access(candidate, os.X_OK):
                version = subprocess.run([candidate, "-c", "import sys; print(sys.version_info[:2])"],
                                         capture_output=True, text=True).stdout.strip()
                pythons.setdefault(version, pathlib.Path(candidate))
        builds = []
        for n, (version, python) in enumerate(sorted(pythons.items())):
            first_python = self.tmp / f"python-{n}"
            first_python.mkdir()
            (first_python / "python3").symlink_to(python)
            for poppler, i4r_python in (("21.03.0", None), ("26.07.0", "/usr/bin/python3")):
                extra = {"FAKE_POPPLER": poppler, **({"I4R_PYTHON": i4r_python} if i4r_python else {})}
                self.build(self.env([first_python, self.bin], **extra))
                builds.append((version, poppler, (self.audit / "claims_script.R").read_bytes()))
        self.assertGreaterEqual(len(builds), 2)
        self.assertEqual({data for _, _, data in builds}, {builds[0][2]}, [(v, p) for v, p, _ in builds])
        text = builds[0][2].decode()
        self.assertIn('audit = "26.08.0"', text)
        self.assertIn('modules = c("numpy"), module_versions = c("2.4.0")', text)

    # ---- B2: Python and its modules ------------------------------------------------------------------------------
    @unittest.skipUnless(shutil.which("python3"), "python3 is not installed")
    def test_python_and_the_modules_the_helpers_import_are_declared(self) -> None:
        (self.audit / "verification_snippets" / "py").mkdir()
        (self.audit / "verification_snippets" / "py" / "core.py").write_text(
            "import sys\nimport i4r_no_such_module_2711 as m\nprint(m.answer())\n")
        self.write({
            "_prelude": 'core <- function() system2("python3", shQuote(file.path(SNIPS_DIR, "py", "core.py")), stdout = TRUE)\n',
            "C-1": 'expect("C-1", answer = 42, tol = 0)\ncheck("C-1", answer = as.numeric(core()))\n',
            "C-2": 'expect("C-2", two = 2, tol = 0)\ncheck("C-2", two = 1 + 1)\n',
        })
        self.build()
        log = self.run_script(self.env())
        self.assertRegex(log, r"Outside program python3: MISSING Python module i4r_no_such_module_2711 .*\(claims C-1\)")
        self.assertRegex(log, r"\[C-2\s+two\s*\] OBSERVED  2  PASS")

    # ---- B3: a macOS-only program --------------------------------------------------------------------------------
    def test_a_macos_only_program_is_named_on_another_system_or_when_absent(self) -> None:
        self.write({
            "C-1": 'expect("C-1", words = 2, tol = 0)\n'
                   'txt <- system2("textutil", c("-convert", "txt", "-stdout", shQuote(file.path(PKG_DIR, "data.csv"))), stdout = TRUE)\n'
                   'check("C-1", words = length(txt))\n',
            "C-2": 'expect("C-2", two = 2, tol = 0)\ncheck("C-2", two = 1 + 1)\n',
        })
        self.build()
        env = self.env()
        if platform.system() == "Darwin":   # hide /usr/bin/textutil behind a PATH that has everything else
            shim = self.tmp / "usrbin"; shim.mkdir()
            for entry in pathlib.Path("/usr/bin").iterdir():
                if entry.name != "textutil":
                    (shim / entry.name).symlink_to(entry)
            env["PATH"] = os.pathsep.join(p if p != "/usr/bin" else str(shim) for p in env["PATH"].split(os.pathsep)
                                          if "textutil" not in p)
            if shutil.which("textutil", path=env["PATH"]):
                self.skipTest("textutil is on PATH outside /usr/bin")
        log = self.run_script(env)
        self.assertRegex(log, r"Outside program textutil: (MISSING: install textutil \(part of macOS\)|"
                              r"runs on macOS only and this system is \w+).*\(claims C-1\)")
        self.assertRegex(log, r"\[C-2\s+two\s*\] OBSERVED  2  PASS")

    # ---- B4: the header describes the lookup the code does ----------------------------------------------------------
    def test_the_layout_the_header_describes_runs(self) -> None:
        self.write({"C-1": 'expect("C-1", mean_x = 2, has_paper = TRUE, tol = 0)\n'
                           'check("C-1", mean_x = mean(read_deposit("data.csv")$x), '
                           'has_paper = file.exists(file.path("paper", "paper.txt")))\n'})
        self.build()
        header = (self.audit / "claims_script.R").read_text()
        self.assertNotIn("folder beside the working directory", header, "2.7.10's wording: the code never read ../pkg")
        self.assertIn("from the folder that holds them both", header)
        # what the header says: pkg/ and paper/ side by side, run from the folder that holds them, no variables set
        log = self.run_script(self.env(), cwd=self.audit)
        self.assertEqual(self.summary(log), (2, 2, 0), log)

    # ---- B5: a manual check that names the audit's own files -------------------------------------------------------
    def test_a_manual_check_naming_the_audits_working_files_is_marked_and_reported(self) -> None:
        (self.audit / "pkg" / "scripts").mkdir()
        (self.audit / "pkg" / "scripts" / "fit.R").write_text("1\n")
        self.write({"C-1": 'expect("C-1", two = 2, tol = 0)\ncheck("C-1", two = 1 + 1)\n'},
                   tail={2: "See the evidence manifest bound in phase1/quorum.json and report/03_text_vs_data.md; "
                            "compare scripts/fit.R with Table 2 of paper/paper.pdf"})
        out = self.build()
        script = (self.audit / "claims_script.R").read_text()
        tail = " ".join(l.lstrip("#").strip() for l in script.split("CLAIMS WITH NO CODE", 1)[1].splitlines())
        self.assertIn("NOT PUBLISHED: phase1/quorum.json, report/03_text_vs_data.md are the audit's own working", tail)
        self.assertNotIn("scripts/fit.R are", tail, "a path that exists in the deposit is the deposit's")
        self.assertNotIn("scripts/fit.R is", tail)
        self.assertIn("NOTE: claim 2's manual check names the audit's working files", out)
        done = subprocess.run(["bash", str(CHECK), str(self.audit)], capture_output=True, text=True,
                              env={**self.env(), "I4R_CLAIMS_LOG_DIR": "none"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    # ---- amend_claims_script.py builds in a scratch stage: it must build the harness the audit froze ------------------
    def test_the_amend_tools_scratch_build_is_the_audits_own_build(self) -> None:
        self.record_environment(pdftotext="26.08.0")
        self.write({"_prelude": 'TXT <- function() system2("pdftotext", c("-raw", "x.pdf", "-"), stdout = TRUE)\n',
                    "C-1": 'expect("C-1", n = 1, tol = 0)\ncheck("C-1", n = length(TXT()))\n'},
                   tail={2: "See the manifest bound in phase1/quorum.json"})
        sys.path.insert(0, str(SKILL / "scripts"))
        self.addCleanup(sys.path.remove, str(SKILL / "scripts"))
        import amend_claims_script
        for n, publication in enumerate(({"claims_script_harness": "portable_1"}, None)):
            self.freeze(publication)
            self.build()
            own = (self.audit / "claims_script.R").read_bytes()
            scratch = self.tmp / f"amend-{n}"
            scratch.mkdir()
            staged = amend_claims_script.build(self.audit, scratch, "R")
            self.assertEqual(staged.read_bytes(), own, publication)
            self.assertEqual(b'audit = "26.08.0"' in own, publication is not None)

    # ---- an older R than the syntax the checks use -------------------------------------------------------------------
    def test_checks_that_need_r_4_1_say_so_first(self) -> None:
        # the native pipe and the \(x) lambda shorthand both need R 4.1
        for check in ('c(1, 1) |> sum()', 'sum(vapply(c(1, 1), \\(x) x, numeric(1)))'):
            with self.subTest(check=check):
                self.write({"C-1": f'expect("C-1", two = 2, tol = 0)\ncheck("C-1", two = {check})\n'})
                self.build()
                script = (self.audit / "claims_script.R").read_text()
                first_code = [l for l in script.splitlines() if l and not l.startswith("#")]
                self.assertTrue(any('getRversion() < "4.1.0"' in l for l in first_code[:16]), first_code[:16])
                self.assertEqual(self.summary(self.run_script(self.env())), (1, 1, 0))


class InitFreezesTheHarness(unittest.TestCase):
    """The leaf reaches an audit only through init_run.sh, which freezes the shipped policy into the snapshot; a
    leftover snapshot with a harness this skill does not build is refused there, before anything is written."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-init-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        sys.path.insert(0, str(SKILL / "tests"))
        self.addCleanup(sys.path.remove, str(SKILL / "tests"))
        import test_270_policy_and_init as pinit
        self.pinit = pinit

    def harness_frozen(self, audit: pathlib.Path):
        policy = json.loads((audit / self.pinit.SNAPSHOT).read_text(encoding="utf-8"))["policy"]
        return policy["publication"].get("claims_script_harness")

    def test_a_new_audit_freezes_the_shipped_harness(self) -> None:
        audit = self.pinit.make_audit(self.root / "new" / "audit")
        result = self.pinit.init(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.harness_frozen(audit), "portable_1")
        # and the builder reads it: a claims script built for this audit carries the 2.7.11 harness
        (audit / "verification_snippets").mkdir()
        (audit / "verification_snippets" / "C-1.R").write_text('expect("C-1", two = 2, tol = 0)\ncheck("C-1", two = 1 + 1)\n')
        (audit / "comment.json").write_text(json.dumps({"findings": [finding(1)]}))
        (audit / "claims_map.tsv").write_text("F1\tsnippet:verification_snippets/C-1.R\n")
        built = subprocess.run(["bash", str(BUILDER), str(audit)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertIn("from the folder that holds them both", (audit / "claims_script.R").read_text())

    def test_a_leftover_freezing_an_unknown_harness_is_refused_untouched(self) -> None:
        for value in ("portable_9", "notebook_setup_1", 1):
            with self.subTest(value=value):
                policy = self.pinit.shipped_policy()
                policy["publication"]["claims_script_harness"] = value
                data = self.pinit.envelope_bytes(policy)
                audit = self.pinit.make_audit(self.root / f"odd-{value}" / "audit")
                (audit / self.pinit.SNAPSHOT).write_bytes(data)
                result = self.pinit.init(audit)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("freezes a claims-script harness this skill does not build", result.stderr)
                self.assertEqual((audit / self.pinit.SNAPSHOT).read_bytes(), data)
                self.assertFalse((audit / "run.json").exists())

    def test_a_leftover_without_the_leaf_is_reused_and_keeps_the_2_7_10_harness(self) -> None:
        # negative control: a 2.7.11 snapshot with only the harness leaf removed (a hand-edited one; a real 2.7.10
        # leftover also lacks the reasoning-token leaf, so init refuses it) is not refused and keeps 2.7.10's harness
        policy = self.pinit.shipped_policy()
        self.assertEqual(policy["publication"].pop("claims_script_harness"), "portable_1")
        data = self.pinit.envelope_bytes(policy)
        audit = self.pinit.make_audit(self.root / "old" / "audit")
        (audit / self.pinit.SNAPSHOT).write_bytes(data)
        result = self.pinit.init(audit)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((audit / self.pinit.SNAPSHOT).read_bytes(), data)
        self.assertIsNone(self.harness_frozen(audit))


class DriftPinsTheHarness(unittest.TestCase):
    """config/check_policy_drift.py pins the shipped leaf to a literal and joins scripts/claims_harness.py to it."""

    def drift(self, relative: str, old: str, new: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="i4r-2711-drift-") as name:
            tree = pathlib.Path(name) / "i4r-audit"
            shutil.copytree(SKILL, tree, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"), symlinks=True)
            if relative:
                path = tree / relative
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count(old), 1, (relative, old))
                path.write_text(text.replace(old, new), encoding="utf-8")
            return subprocess.run([sys.executable, str(tree / "config" / "check_policy_drift.py")], capture_output=True,
                                  text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)

    def test_the_leaf_and_the_module_are_pinned(self) -> None:
        self.assertEqual(self.drift("", "", "").returncode, 0)
        cases = [
            ("config/pipeline-policy-v2.json", '"claims_script_harness": "portable_1"',
             '"claims_script_harness": "portable_2"', "publication policy is not"),
            ("config/pipeline-policy-v2.json", ',\n    "claims_script_harness": "portable_1"', "",
             "publication policy is not"),
            ("scripts/claims_harness.py", 'SHIPPED_HARNESS = "portable_1"', 'SHIPPED_HARNESS = "portable_2"',
             "SHIPPED_HARNESS is 'portable_2'"),
            ("scripts/claims_harness.py", "(LEGACY_HARNESS, SHIPPED_HARNESS)", "(SHIPPED_HARNESS,)",
             "no longer holds the 2.7.10 harness"),
        ]
        for relative, old, new, message in cases:
            with self.subTest(file=relative, new=new):
                result = self.drift(relative, old, new)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(message, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
