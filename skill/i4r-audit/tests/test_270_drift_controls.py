#!/usr/bin/env python3
"""2.7.0: two defects of config/check_policy_drift.py found in review, and their repair.

* ``NegativeControlsNameTheirPins`` (DD-1). A negative control passed on ANY error from its detector. The
  Sonnet 5 Phase-1 control reverted the auditors, the verifier and the capability-probe leaf together, so
  the probe pin alone satisfied it: a pin on the auditors or the verifier loosened to an allow-set of live
  routes (the tempting mistake once the runners dispatch both Sonnet routes) went unnoticed. Each control
  now names the pin message(s) that must reject it; the probe leaf has a control of its own, and so does
  the critic route (a revert to gpt-5.6-terra). Every pin is loosened in turn on the real checker, and
  exactly the controls that name it must fail.
* ``PrintedInitCommandGuard`` (DD-2). The docs guard for the printed ``init_run.sh --auditor-model``
  read the raw text with ``--auditor-model(?:\\s+|=)([A-Za-z0-9._-]+)``, so a quoted value, CHEATSHEET's
  ``&nbsp;``, a value wrapped in HTML tags and a backslash-newline continuation all passed whatever model
  they named. Each form is planted ON DISK in a copy of this tree and the real checker must refuse it by
  name; the same form naming the shipped model passing is the negative control, and so is the 2.7.0
  release-candidate regex missing every form.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
CHECKER = SKILL / "config" / "check_policy_drift.py"
IGNORED = shutil.ignore_patterns(
    ".git", ".pytest_cache", "__pycache__", "_history*", "CHEATSHEET.pdf", ".DS_Store", "*.pyc")
# The guard's pattern before this fix, read against the raw document text.
RAW_GUARD = re.compile(r"--auditor-model(?:\s+|=)([A-Za-z0-9._-]+)")


def load_checker(path: pathlib.Path = CHECKER):
    spec = importlib.util.spec_from_file_location(f"i4r_270_dd_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------ DD-1
class NegativeControlsNameTheirPins(unittest.TestCase):
    checker = load_checker()
    claude = checker.SHIPPED_CLAUDE_LABEL
    sol = checker.SHIPPED_CRITIC_MODEL
    # Every pin a control names, the detector that emits it, and the controls it must reject: loosening
    # the pin must fail exactly these, each by label.
    PINS = [
        ("check_policy_shape", "round target/cap is not 2/7", {"round target", "round cap"}),
        ("check_policy_shape", "round-cap amendment contract drifted", {"round-cap amendment maximum"}),
        # 2.7.3 (tests/test_273_sonnet_effort.py): the effort reverted to max everywhere, the verifier alone and the
        # probe leaf alone are three more controls, named by the Claude pins below.
        ("check_policy_shape", f"coordinator policy is not {claude}",
         {"coordinator restored to Sonnet 5", "Claude effort restored to max"}),
        ("check_policy_shape", f"one or more phase-1 auditors is not assigned to {claude}",
         {"Phase-1 route restored to Sonnet 5", "Claude effort restored to max"}),
        ("check_policy_shape", "auditor routing is", {"Phase-1 route restored to Sonnet 5", "auditor routing"}),
        ("check_policy_shape", f"post-fan-out verifier policy is not {claude} on the Phase-1 auditor family",
         {"Phase-1 route restored to Sonnet 5", "post-fan-out verifier family", "Claude effort restored to max",
          "post-fan-out verifier left at max"}),
        ("check_policy_shape", "post-fan-out verifier route differs from the Phase-1 auditors' route",
         {"post-fan-out verifier family", "post-fan-out verifier left at max"}),
        ("check_policy_shape", f"the Phase-1 capability probe is not {claude}",
         {"Phase-1 capability probe restored to Sonnet 5", "Claude effort restored to max",
          "Phase-1 capability probe left at max"}),
        ("check_policy_shape", f"critic policy is not {sol} / high", {"critic route restored to gpt-5.6-terra"}),
        ("check_policy_shape", f"targeted-adjudicator policy is not {sol} / max",
         {"critic route restored to gpt-5.6-terra"}),
        ("check_policy_shape", f"critique model probe is not {sol} / high",
         {"critic route restored to gpt-5.6-terra"}),
        # 2.7.1 (tests/test_271_probe_bound.py): the probe bound's two leaves, each pinned apart.
        ("check_policy_shape", "the Phase-1 probe bound phase_1.runtime.probe_max_normalized_tokens is",
         {"Phase-1 probe bound restored to 150000"}),
        ("check_policy_shape", "the capability-probe leaf maximum_normalized_tokens is",
         {"Phase-1 probe bound restored to 150000"}),
        # 2.7.2 (tests/test_272_phase1_timeout.py): the Phase-1 timeout's two leaves, each pinned apart.
        ("check_policy_shape", "the Phase-1 timeout phase_1.runtime.timeout_seconds is",
         {"Phase-1 timeout restored to 2700"}),
        ("check_policy_shape", "the Phase-1 leaf provider_timeout_seconds is", {"Phase-1 timeout restored to 2700"}),
        # 2.7.4 (tests/test_274_critique_timeout.py, tests/test_274_claim_change_rule.py): the critique ceiling and the
        # later-round claim-change rule, each pinned to a literal.
        ("check_policy_shape", "the critique ceiling critique.provider_timeout_seconds is",
         {"critique ceiling restored to 2700"}),
        ("check_policy_shape",
         "the later-round claim-change rule critique.later_rounds.routing_manifest.claim_change_rule is",
         {"claim-change rule removed"}),
        ("check_policy_shape", "later-round extra specialists are not restricted", {"later-round routing"}),
        ("check_verification_and_release", "verification policy drifted", {"verification waiver"}),
        ("check_finding_projection", "finding source-of-truth projection policy drifted", {"finding registry"}),
        ("check_local_execution", "local batch/cache/manifest/atomic execution policy drifted",
         {"document cache"}),
    ]

    def run_controls(self, detector: str | None = None, dropped: str | None = None) -> list[str]:
        """check_negative_controls on the shipped policy, with one detector's ``dropped`` message removed."""
        module = load_checker()
        if detector is not None:
            strict = getattr(module, detector)

            def loosened(candidate: dict, errors: list[str]) -> None:
                raised: list[str] = []
                strict(candidate, raised)
                errors.extend(error for error in raised if not error.startswith(dropped))
            setattr(module, detector, loosened)
        found: list[str] = []
        self.assertEqual(module.check_negative_controls(module.load_policy(), found), 20)
        return found

    @staticmethod
    def failed_labels(found: list[str]) -> set[str]:
        labels = set()
        for error in found:
            match = re.match(r"negative control was not rejected(?: by its pin)?: (.+?)(?:: no error starts .*)?$",
                             error, flags=re.DOTALL)
            labels.add(match.group(1) if match else f"<unparsed: {error}>")
        return labels

    def test_the_unmodified_checker_passes_all_twenty_controls(self) -> None:
        # Negative control for every loosening below.
        self.assertEqual(self.run_controls(), [])

    def test_each_loosened_pin_fails_exactly_the_controls_that_name_it(self) -> None:
        covered = set()
        for detector, message, expected in self.PINS:
            with self.subTest(pin=message):
                found = self.run_controls(detector, message)
                self.assertEqual(self.failed_labels(found), expected, found)
                covered |= expected
        # every control is named by at least one pin above
        self.assertEqual(len(covered), 20)

    def test_a_loosened_auditor_pin_is_seen_although_other_pins_still_fire(self) -> None:
        # The defect: the Phase-1 control still raises the alias and verifier errors (and, before the split,
        # the probe error), so "some error" was enough. Now the auditor model pin itself must fire.
        message = f"one or more phase-1 auditors is not assigned to {self.claude}"
        found = self.run_controls("check_policy_shape", message)
        # 2.7.3: the effort revert names the auditor pin too, and is seen the same way
        self.assertEqual(self.failed_labels(found), {"Phase-1 route restored to Sonnet 5", "Claude effort restored to max"})
        found = sorted(found, key=lambda error: "Claude effort restored to max" in error)
        self.assertTrue(found[0].startswith(
            "negative control was not rejected by its pin: Phase-1 route restored to Sonnet 5: no error starts "
            f"{message!r}"), found[0])
        self.assertIn("auditor routing is", found[0], "other pins still rejected it: the old rule would pass")
        self.assertIn("post-fan-out verifier policy is not", found[0])

    def test_the_phase_1_control_no_longer_touches_the_probe_leaf(self) -> None:
        # Loosening every Claude pin but the probe's leaves the Phase-1 control with nothing to catch it
        # (its revert no longer includes the probe leaf), and the probe control still passes.
        module = load_checker()
        strict = module.check_policy_shape
        prefixes = (f"one or more phase-1 auditors is not assigned to {self.claude}", "auditor routing is",
                    f"post-fan-out verifier policy is not {self.claude}")

        def loosened(candidate: dict, errors: list[str]) -> None:
            raised: list[str] = []
            strict(candidate, raised)
            errors.extend(error for error in raised if not error.startswith(prefixes))
        module.check_policy_shape = loosened
        found: list[str] = []
        module.check_negative_controls(module.load_policy(), found)
        self.assertIn("negative control was not rejected: Phase-1 route restored to Sonnet 5", found)
        self.assertNotIn("Phase-1 capability probe restored to Sonnet 5", self.failed_labels(found))

    def test_the_critic_route_revert_is_rejected_on_disk_by_each_pin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="i4r-270-dd1-") as directory:
            tree = pathlib.Path(directory) / "i4r-audit"
            shutil.copytree(SKILL, tree, ignore=IGNORED, symlinks=True)
            path = tree / "config" / "pipeline-policy-v2.json"
            original = path.read_bytes()
            cases = [
                ("critics", lambda p: p["models"]["critics"].update(requested_model="gpt-5.6-terra"),
                 [f"critic policy is not {self.sol} / high"]),
                ("adjudicator", lambda p: p["models"]["targeted_adjudicator"].update(requested_model="gpt-5.6-terra"),
                 [f"targeted-adjudicator policy is not {self.sol} / max"]),
                ("probe", lambda p: p["critique"]["model_probe"].update(requested_model="gpt-5.6-terra"),
                 [f"critique model probe is not {self.sol} / high, the critics' route"]),
                ("probe effort", lambda p: p["critique"]["model_probe"].update(reasoning_effort="max"),
                 [f"critique model probe is not {self.sol} / high, the critics' route"]),
                ("probe cache contract", lambda p: p["critique"]["model_probe"].update(reuse_across_rounds=False),
                 ["model probe is not cached once per audit/model/effort"]),
            ]
            for label, mutate, expected in cases:
                with self.subTest(drift=label):
                    policy = json.loads(original)
                    mutate(policy)
                    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
                    result = subprocess.run([sys.executable, str(tree / "config/check_policy_drift.py")],
                                            text=True, capture_output=True, timeout=120)
                    path.write_bytes(original)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    for message in expected:
                        self.assertIn(message, result.stdout)
                    # the probe's route and its cache contract are pinned apart, so each drift names only itself
                    if label.startswith("probe"):
                        other = ("critique model probe is not" if label == "probe cache contract"
                                 else "model probe is not cached")
                        self.assertNotIn(other, result.stdout)
            result = subprocess.run([sys.executable, str(tree / "config/check_policy_drift.py")],
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("twenty planted drifts were rejected", result.stdout)


# ------------------------------------------------------------------------------ DD-2
# SKILL.md's own printed command, which a form planted in SKILL.md replaces: appending would break its word cap.
SKILL_FLAG = "--auditor-model sonnet-5-5 \\\n  --auditor-family"
FORMS = [
    # (label, text naming MODEL, document it is planted in: appended, or in place of SKILL_FLAG in SKILL.md)
    ("a double-quoted value", '<pre>init_run.sh "$AUDIT_DIR" --auditor-model "MODEL"</pre>', "CHEATSHEET.html"),
    ("a single-quoted value", "\n    --auditor-model='MODEL' \\\n", "INSTALL.md"),
    ("CHEATSHEET's &nbsp;", "<code>--auditor-model&nbsp;MODEL</code>", "CHEATSHEET.html"),
    ("a numeric no-break space", "<code>--auditor-model&#160;MODEL</code>", "CHEATSHEET.html"),
    ("the value in its own tags", "<code>--auditor-model</code> <b>MODEL</b>", "CHEATSHEET.html"),
    ("a backslash-newline continuation", "--auditor-model \\\n  MODEL --auditor-family", "SKILL.md"),
    ("a CRLF continuation", "\r\n  --auditor-model \\\r\n  MODEL\r\n", "INSTALL.md"),
    ("entity-encoded dashes", "<p>&#45;&#45;auditor-model MODEL</p>", "CHEATSHEET.html"),
]


class PrintedInitCommandGuard(unittest.TestCase):
    checker = load_checker()
    shipped = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))[
        "models"]["coordinator"]["requested_model"]

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="i4r-270-dd2-")
        cls.tree = pathlib.Path(cls.temporary.name) / "i4r-audit"
        shutil.copytree(SKILL, cls.tree, ignore=IGNORED, symlinks=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def planted(self, document: str, text: str) -> tuple[int, str]:
        path = self.tree / document
        original = path.read_bytes()
        try:
            if document == "SKILL.md":
                current = original.decode("utf-8")
                self.assertEqual(current.count(SKILL_FLAG), 1)
                path.write_bytes(current.replace(SKILL_FLAG, text).encode("utf-8"))
            else:
                path.write_bytes(original + text.encode("utf-8"))
            result = subprocess.run([sys.executable, str(self.tree / "config/check_policy_drift.py")],
                                    text=True, capture_output=True, timeout=120,
                                    env={**os.environ, "LC_ALL": "C"})
            return result.returncode, result.stdout + result.stderr
        finally:
            path.write_bytes(original)

    def test_each_form_is_read_as_its_model(self) -> None:
        for label, text, _ in FORMS:
            with self.subTest(form=label):
                self.assertEqual(self.checker.printed_auditor_models(text.replace("MODEL", "sonnet-5")), ["sonnet-5"])
                # the defect: the raw-text pattern missed every form
                self.assertEqual(RAW_GUARD.findall(text.replace("MODEL", "sonnet-5")), [])

    def test_placeholders_and_prose_are_not_read_as_a_model(self) -> None:
        # Negative controls: stripping only named HTML tags keeps a Markdown placeholder from joining the flag
        # to the next word, and a value that is not a literal model is not one.
        for text in ("init_run.sh <audit-dir> --auditor-model <model> --auditor-family claude",
                     "pass --auditor-model <model> only to override the policy",
                     "refuses a `--auditor-model` value in SKILL.md",
                     '--auditor-model "$SHIPPED_MODEL"',
                     "--auditor-model $(jq -r .models.coordinator.requested_model policy.json)",
                     "no flag here: auditor-model sonnet-5"):
            with self.subTest(text=text):
                self.assertEqual(self.checker.printed_auditor_models(text), [])
        self.assertEqual(self.checker.printed_auditor_models(
            "--auditor-model sonnet-5-5 \\\n  --auditor-model 'sonnet-5' --auditor-model=sonnet-5-5"),
            ["sonnet-5-5", "sonnet-5"])

    def test_the_shipped_documents_print_only_the_shipped_model(self) -> None:
        for document in ("SKILL.md", "INSTALL.md", "CHEATSHEET.html"):
            with self.subTest(document=document):
                named = self.checker.printed_auditor_models((SKILL / document).read_text(encoding="utf-8"))
                self.assertTrue(set(named) <= {self.shipped}, named)
        self.assertIn(self.shipped, self.checker.printed_auditor_models((SKILL / "SKILL.md").read_text()))

    def test_each_form_naming_an_old_model_is_refused_on_disk(self) -> None:
        for label, text, document in FORMS:
            with self.subTest(form=label):
                code, output = self.planted(document, text.replace("MODEL", "sonnet-5"))
                self.assertEqual(code, 1, output)
                self.assertIn(f"{document} prints init_run.sh --auditor-model sonnet-5, but the shipped policy "
                              f"freezes {self.shipped}", output)
                # negative control: the same form naming the shipped model passes
                code, output = self.planted(document, text.replace("MODEL", self.shipped))
                self.assertEqual(code, 0, output)
                self.assertIn("POLICY DRIFT CHECK OK", output)


if __name__ == "__main__":
    unittest.main()
