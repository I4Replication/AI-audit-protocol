"""2.7.11: a deposit whose read-only mode the synced folder stripped no longer stops finalize, staging or publication.

Intake makes pkg/ read-only and the post-delta archive gate refuses a writable deposit ("archive package directory is
not read-only: pkg/."). Dropbox strips that mode (10 of 11 audits on 2026-09-25, finalized ones included) while every
byte still hashes to package_manifest.json, so finalize, stage and publish refused until someone ran chmod by hand,
and stage_release.sh named the wrong remedy: rebuild the packet and re-run the gates, which on a finalized audit
journals a gate event and stales its receipt. scripts/refreeze_pkg.py gives the mode back only when every file matches
the manifest, and the three entry points run it first. Each test builds the condition on disk and runs the real code;
on 2.7.10 refreeze_pkg.py does not exist and publication refuses the writable deposit.
"""
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
REFREEZE = SKILL / "scripts" / "refreeze_pkg.py"
STAGE = SKILL / "scripts" / "stage_release.sh"
PUBLISH = SKILL / "scripts" / "publish_completed.sh"
FIXTURE_ENV = {"I4R_PUBLISH_ANY_ROOT": "1", "I4R_CREATE_REGISTRY": "1", "I4R_RESERVE_GRACE": "0", "LC_ALL": "C"}


def writable(package: pathlib.Path) -> list[str]:
    return sorted(p.relative_to(package).as_posix() or "." for p in [package, *package.rglob("*"), ]
                  if stat.S_IMODE(p.stat().st_mode) & 0o222)


class Refreeze(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-refreeze-")
        self.addCleanup(tmp.cleanup)
        self.audit = pathlib.Path(tmp.name) / "audit"
        package = self.audit / "pkg"
        (package / "data").mkdir(parents=True)
        (package / "data" / "a.csv").write_text("x\n1\n")
        (package / "readme.txt").write_text("deposit\n")
        rows = []
        for path in sorted(package.rglob("*")):
            if path.is_file():
                rows.append({"path": path.relative_to(package).as_posix(), "size": path.stat().st_size,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "extension": path.suffix.lower().lstrip(".") or "<none>"})
        tree = hashlib.sha256("".join(f"{r['path']}\0{r['sha256']}\n" for r in rows).encode()).hexdigest()
        (self.audit / "package_manifest.json").write_text(json.dumps({
            "schema_version": "2.0.0", "algorithm": "sha256", "package_sha256": tree, "file_count": len(rows),
            "file_types": {"csv": 1, "txt": 1}, "files": rows}))
        (self.audit / "run.events.jsonl").write_text('{"seq": 1, "type": "init", "payload": {}}\n')
        (self.audit / "run.json").write_text('{"protocol_version": "2.7.10"}\n')
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+rw", str(package)]))

    def outside_pkg(self) -> dict:
        """Every path outside pkg/ with its bytes, mode and mtime: the helper must leave all of it as it is."""
        return {p.relative_to(self.audit).as_posix(): (p.read_bytes() if p.is_file() else None,
                                                        p.stat().st_mode, p.stat().st_mtime_ns)
                for p in [self.audit, *self.audit.rglob("*")] if "pkg" not in p.relative_to(self.audit).parts}

    def run_refreeze(self) -> subprocess.CompletedProcess:
        return subprocess.run(["python3", str(REFREEZE), str(self.audit)], capture_output=True, text=True, timeout=60)

    def test_a_stripped_deposit_gets_its_mode_back_and_no_byte_changes(self) -> None:
        before = {p: p.read_bytes() for p in (self.audit / "pkg").rglob("*") if p.is_file()}
        audit_before = self.outside_pkg()
        self.assertTrue(writable(self.audit / "pkg"))
        done = self.run_refreeze()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("pkg/: 4 entries made read-only again", done.stdout)
        self.assertEqual(writable(self.audit / "pkg"), [])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(self.outside_pkg(), audit_before)   # no journal event, no file written outside pkg/
        again = self.run_refreeze()   # nothing left to do: silent
        self.assertEqual((again.returncode, again.stdout), (0, ""))

    def test_a_deposit_that_differs_from_its_manifest_keeps_its_modes(self) -> None:
        (self.audit / "pkg" / "readme.txt").write_text("changed\n")
        modes = writable(self.audit / "pkg")
        done = self.run_refreeze()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("pkg/readme.txt no longer matches package_manifest.json: modes left as they are", done.stdout)
        self.assertEqual(writable(self.audit / "pkg"), modes)

    def test_an_extra_file_keeps_the_modes(self) -> None:
        (self.audit / "pkg" / "new.txt").write_text("added\n")
        done = self.run_refreeze()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("pkg/new.txt is not in package_manifest.json", done.stdout)
        self.assertIn("new.txt", writable(self.audit / "pkg"))

    def test_an_unreadable_file_keeps_the_modes(self) -> None:
        (self.audit / "pkg" / "readme.txt").chmod(0o200)
        modes = writable(self.audit / "pkg")
        done = self.run_refreeze()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("pkg/readme.txt cannot be read", done.stdout)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(writable(self.audit / "pkg"), modes)

    def test_an_interrupt_during_the_gate_check_puts_every_mode_back(self) -> None:
        # the real helper with the gate's check replaced by one that is interrupted after every mode was changed
        driver = ("import pathlib, sys; sys.path.insert(0, sys.argv[1]); import run_post_delta_gates, refreeze_pkg\n"
                  "def interrupted(audit): raise KeyboardInterrupt\n"
                  "run_post_delta_gates.verify_archive = interrupted\n"
                  "refreeze_pkg.refreeze(pathlib.Path(sys.argv[2]))\n")
        modes = writable(self.audit / "pkg")
        audit_before = self.outside_pkg()
        done = subprocess.run([sys.executable, "-B", "-c", driver, str(SKILL / "scripts"), str(self.audit)],
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("KeyboardInterrupt", done.stderr)
        self.assertEqual(writable(self.audit / "pkg"), modes)
        self.assertEqual(self.outside_pkg(), audit_before)

    def test_when_the_archive_gate_still_refuses_every_mode_goes_back(self) -> None:
        manifest = json.loads((self.audit / "package_manifest.json").read_text())
        manifest["package_sha256"] = "0" * 64   # the rows match, the summary does not: only the gate's check sees it
        (self.audit / "package_manifest.json").write_text(json.dumps(manifest))
        modes = writable(self.audit / "pkg")
        done = self.run_refreeze()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("package tree hash is stale", done.stdout)
        self.assertEqual(writable(self.audit / "pkg"), modes)


class PublicationOfAStrippedDeposit(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_release_pipeline import make_audit
        tmp = tempfile.TemporaryDirectory(prefix="i4r-2711-refreeze-e2e-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("I4R_")}, **FIXTURE_ENV}
        self.audit = make_audit(self.root, paper="Paper-Stripped", layout="new")
        os.chmod(self.audit / "pkg", 0o755)   # what the synced folder does to intake's 0555

    def test_staging_gives_the_deposit_its_mode_back(self) -> None:
        staged = subprocess.run(["/bin/bash", str(SKILL / "scripts" / "stage_release.sh"), str(self.audit),
                                 str(self.root / "staged")], capture_output=True, text=True, env=self.env, timeout=600)
        self.assertEqual(staged.returncode, 0, staged.stdout[-3000:] + staged.stderr[-3000:])
        self.assertIn("pkg/: 1 entry made read-only again", staged.stdout)
        self.assertEqual(stat.S_IMODE((self.audit / "pkg").stat().st_mode) & 0o222, 0)

    def test_publication_gives_the_deposit_its_mode_back(self) -> None:
        audit, root, env = self.audit, self.root, self.env
        published = subprocess.run(["/bin/bash", str(PUBLISH), str(audit), str(root / "completed")],
                                   capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        self.assertIn("pkg/: 1 entry made read-only again", published.stdout)
        self.assertEqual(stat.S_IMODE((audit / "pkg").stat().st_mode) & 0o222, 0)

    def test_a_deposit_the_helper_leaves_alone_gets_the_mode_remedy_not_a_rebuild(self) -> None:
        # NF1: 2.7.10 answered this refusal with "Rebuild its packet and gates", which journals a gate event on a
        # finalized audit and stales its receipt
        package = self.audit / "pkg"
        (package / "sub").mkdir()
        (package / "link").symlink_to("sub")   # an internal symlink: the gate admits it, the helper leaves it alone
        journal = (self.audit / "run.events.jsonl").read_bytes()
        staged = subprocess.run(["/bin/bash", str(STAGE), str(self.audit), str(self.root / "staged")],
                                capture_output=True, text=True, env=self.env, timeout=600)
        out = staged.stdout + staged.stderr
        self.assertEqual(staged.returncode, 1, out[-3000:])
        # Review EDGE-6: the gate admits an internal symlink, so the message names the hand remedy, not a refusal
        self.assertIn("pkg/link is a symlink, which refreeze_pkg does not handle: modes left as they are "
                      "(if the deposit is unchanged, run chmod -R a-w pkg/ by hand)", out)
        self.assertNotIn("the archive gate refuses both", out)
        self.assertIn("not read-only: pkg/", out)
        self.assertIn("chmod -R a-w", out)
        self.assertIn("Do not rebuild the packet or re-run the gates for this", out)
        self.assertNotIn("Rebuild its packet and gates", out)
        self.assertNotEqual(stat.S_IMODE(package.stat().st_mode) & 0o222, 0)   # modes left as they were
        self.assertEqual((self.audit / "run.events.jsonl").read_bytes(), journal)
        published = subprocess.run(["/bin/bash", str(PUBLISH), str(self.audit), str(self.root / "completed")],
                                   capture_output=True, text=True, env=self.env, timeout=600)
        out = published.stdout + published.stderr
        self.assertEqual(published.returncode, 1, out[-3000:])
        self.assertIn("the synced folder strips pkg/'s read-only mode: if every file still matches", out)


if __name__ == "__main__":
    unittest.main()
