#!/usr/bin/env python3
"""The staging refusals must name the command that fixes them.

Three orderings in the pre-finalize chain are not obvious, and each costs a full cycle when it
is got wrong. Each announces itself through a refusal whose text, until 2.4.4, said only that
something was stale:

  * the projection must be re-run AFTER approve_settled.py binds settled fields into the registry;
  * the verification notebook must be re-executed after the last report edit;
  * the round-N diff packet describes the round-(N-1) delta.

These tests perturb an audit BUILT HERE with the release-pipeline fixture and assert the refusal
tells the operator what to run. They used to search three directories above the skill for a
finalized production audit and skip when none was found -- so once the production audits were
deleted, the remedy text could be removed with a green suite. The fixture is now part of the
artifact under test.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_release_pipeline import make_audit

SKILL = Path(__file__).resolve().parent.parent


class StagingRefusalsNameTheirRemedy(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-ordering-")
        self.base = Path(self._tmp.name)
        self.work = make_audit(self.base)

    def tearDown(self) -> None:
        for path in [self.base, *self.base.rglob("*")]:
            try:
                path.chmod(0o755 if path.is_dir() else 0o644)
            except OSError:
                pass
        self._tmp.cleanup()

    def stage(self) -> str:
        release = self.base / "release"
        shutil.rmtree(release, ignore_errors=True)
        result = subprocess.run(
            ["bash", str(SKILL / "scripts/stage_release.sh"), str(self.work), str(release)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
        return result.stdout + result.stderr

    def test_the_unperturbed_fixture_stages(self) -> None:
        """Control: the refusals below are about the perturbation, not the fixture."""
        out = self.stage()
        self.assertIn("release staged clean", out, out[-1500:])

    def test_a_registry_change_after_the_projection_names_the_projection_command(self) -> None:
        """This is what approve_settled.py does to a projection taken before it."""
        registry = self.work / "work/findings.jsonl"
        rows = [json.loads(l) for l in registry.read_text().splitlines() if l.strip()]
        disposition = rows[0]["ledger_disposition"]
        disposition["reason"] = (disposition.get("reason") or "") + " (perturbed)"
        registry.write_text(
            "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")
        out = self.stage()
        self.assertIn("generate_finding_artifacts.py", out)
        self.assertIn("--apply", out)
        self.assertIn("approve_settled.py", out)

    def test_a_report_edit_after_verification_names_the_rebuild_tail(self) -> None:
        report = self.work / "report/REPORT_final.md"
        report.write_text(report.read_text() + "\n<!-- perturbed after verification ran -->\n",
                          encoding="utf-8")
        out = self.stage()
        # The document-manifest binding fires before the verification one, so that is the message
        # the operator actually sees; it must name the whole rebuild tail.
        self.assertIn("make_verification_notebook.sh", out)
        self.assertIn("run_verification.sh", out)
        self.assertIn("build_docx.sh", out)


class OrderingIsWrittenDownWhereOperatorsLook(unittest.TestCase):
    """The two orderings that produce no distinctive error until staging."""

    def test_install_md_states_both(self) -> None:
        text = (SKILL / "INSTALL.md").read_text()
        self.assertIn("Re-run the projection after `approve_settled.py`", text)
        self.assertIn("Re-execute the verification notebook after the last report edit", text)

    def test_install_md_explains_the_two_misleading_field_meanings(self) -> None:
        text = (SKILL / "INSTALL.md").read_text()
        self.assertIn("generated_after_round", text)
        self.assertIn("round-**(N-1)** integration delta", text)


if __name__ == "__main__":
    unittest.main()
