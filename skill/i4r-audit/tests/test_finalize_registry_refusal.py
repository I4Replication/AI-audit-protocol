"""finalize_run.sh must not file into the skill-local registry in production (2.4.11).

The fallback was announced but taken. On the seventh production audit, run from a stale copy,
the announcement scrolled past and the row landed in the skill checkout; re-filing and moving
the Completed-reports set aside were done by hand. Outside a fixture the fallback is a refusal.
"""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
FINALIZE = SKILL / "scripts" / "finalize_run.sh"


def run(audit: pathlib.Path, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = {k: v for k, v in os.environ.items() if k not in ("REGISTRY_DIR", "I4R_REGISTRY_DIR",
                                                                     "I4R_ALLOW_SKILL_LOCAL_REGISTRY")}
    environment.update(env or {})
    return subprocess.run(["/bin/bash", str(FINALIZE), str(audit), "--stopping-reason", "target_complete", *extra],
                          text=True, capture_output=True, timeout=120, env=environment)


class SkillLocalRegistryIsRefused(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="i4r-reg-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def audit(self, *parts: str) -> pathlib.Path:
        audit = self.root.joinpath(*parts)
        audit.mkdir(parents=True)
        (audit / "run.json").write_text(json.dumps({"schema_version": "2.1.0"}) + "\n")
        return audit

    def test_outside_a_tree_the_fallback_is_a_refusal_with_the_remedy(self) -> None:
        result = run(self.audit("loose", "audit"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("refusing to file into the skill-local registry", result.stderr)
        self.assertIn("--registry <tree>/registry", result.stderr)
        self.assertIn("--no-registry", result.stderr)
        self.assertFalse((SKILL.parent / "registry" / "index.jsonl.tmp").exists())

    def test_the_fixture_override_restores_the_old_behaviour(self) -> None:
        result = run(self.audit("loose2", "audit"), env={"I4R_ALLOW_SKILL_LOCAL_REGISTRY": "1"})
        self.assertNotIn("refusing to file into the skill-local registry", result.stderr)
        self.assertIn("skill-local", result.stderr)   # still announced

    def test_no_registry_and_explicit_registry_are_not_refused(self) -> None:
        for extra in (("--no-registry",), ("--registry", str(self.root / "reg"))):
            with self.subTest(extra=extra):
                result = run(self.audit("loose3-" + extra[0].strip("-"), "audit"), *extra)
                self.assertNotIn("refusing to file into the skill-local registry", result.stderr)

    def test_inside_a_tree_the_programme_registry_is_derived_and_announced(self) -> None:
        (self.root / "tree" / "registry").mkdir(parents=True)
        for parts in (("tree", "Journal", "Paper", "audit"),            # legacy
                      ("tree", "runs", "Journal", "Paper2", "audit")):  # 2.5.0
            with self.subTest(layout=parts):
                result = run(self.audit(*parts))
                self.assertNotIn("refusing to file into the skill-local registry", result.stderr)
                self.assertIn("programme registry, derived from the audit directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
