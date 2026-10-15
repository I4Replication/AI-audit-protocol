"""Provider failures must be classified so the retry policy and the operator see the truth.

Two misclassifications cost real money on audit A34: a dropped connection
labelled ``permanent`` (no retry, attempt burned) and a provider content-safety refusal
labelled ``permanent`` (indistinguishable from a bug, no mitigation shown). Both now have
their own class, and this test pins the real error strings that were observed.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "schemas" / "tools"))
import run_coordinator  # noqa: E402
import run_phase1  # noqa: E402
import validate_v2  # noqa: E402

SAFETY_REFUSAL = (
    "Claude CLI exited 1 | Claude result envelope reports an error | API Error: Sonnet 5 has "
    "safety measures that flagged this message for a cybersecurity topic. To learn about the "
    "Cyber Verification Program and apply for access, visit our help center."
)
CONNECTION_DROP = (
    "Claude CLI exited 1; Claude result envelope reports an error: API Error: Connection "
    "closed mid-response. The response above may be incomplete."
)


class ProviderErrorClassification(unittest.TestCase):
    def test_content_safety_refusal_has_its_own_class(self) -> None:
        self.assertEqual(run_coordinator.classify_error(SAFETY_REFUSAL), "provider_safety_refusal")
        self.assertEqual(run_phase1.classify_failure(SAFETY_REFUSAL), "provider_safety_refusal")

    def test_safety_refusal_is_not_retried_with_identical_content(self) -> None:
        self.assertFalse(run_phase1.retryable("provider_safety_refusal"))

    def test_connection_drop_is_transport_and_retryable(self) -> None:
        self.assertEqual(run_coordinator.classify_error(CONNECTION_DROP), "transport")
        self.assertTrue(run_phase1.retryable("transport"))

    def test_new_class_is_admitted_by_the_record_contract(self) -> None:
        self.assertIn("provider_safety_refusal", validate_v2.ERROR_CLASSES)

    def test_environment_class_is_admitted_everywhere_a_record_is_checked(self) -> None:
        # 2.6.8: a snapshot that changed around a call is recorded as error_class "environment"
        # (one call, no retry), not "schema". The record contract, the invocation-record schema
        # and run.json's invocation projection must all admit it, or the one row it writes would
        # be refused at telemetry or finalize time.
        import json
        self.assertIn("environment", validate_v2.ERROR_CLASSES)
        invocation = json.loads(
            (SKILL / "schemas/v2/invocation-record.schema.json").read_text())
        run = json.loads((SKILL / "schemas/run.schema.json").read_text())
        enums = [invocation["properties"]["error_class"]["enum"]]

        def collect(node: object) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("error_class"), dict) and "enum" in node["error_class"]:
                    enums.append(node["error_class"]["enum"])
                for child in node.values():
                    collect(child)
            elif isinstance(node, list):
                for child in node:
                    collect(child)
        collect(run)
        self.assertEqual(len(enums), 2, "expected one error_class enum per schema")
        for enum in enums:
            self.assertEqual(set(enum) - {None}, set(validate_v2.ERROR_CLASSES))
        # And a real failed row carrying it validates, while an unknown class still does not.
        import copy
        import validate_json_schema  # noqa: E402  (scripts/)
        base = json.loads((SKILL / "schemas/fixtures/v2_invocation_record.json").read_text())
        for error_class, admitted in (("environment", True), ("snapshot_integrity", False)):
            row = copy.deepcopy(base)
            row.update(status="failed", error="snapshot changed", error_class=error_class)
            schema_errors = validate_json_schema.errors_for_value(
                row, SKILL / "schemas/v2/invocation-record.schema.json")
            self.assertEqual(not schema_errors, admitted, schema_errors)
            if admitted:
                validate_v2.validate_invocation(row)
            else:
                with self.assertRaises(validate_v2.ValidationError):
                    validate_v2.validate_invocation(row)
        # The provider-text classifiers never produce it, so the policy's retry lists need not.
        self.assertNotIn("environment", {run_coordinator.classify_error(text) for text in (
            SAFETY_REFUSAL, CONNECTION_DROP, "immutable snapshot source changed",
            "something unexpected happened")})
        self.assertFalse(run_phase1.retryable("environment"))

    def test_generic_fallback_unchanged(self) -> None:
        self.assertEqual(run_coordinator.classify_error("something unexpected happened"), "permanent")


class JournaledErrorRedaction(unittest.TestCase):
    # Attempt 3 of audit A34 journaled an absolute path inside its error string; the
    # run.json projection then failed the provider-safety check as a live coordinator input.
    def test_account_name_with_a_space_is_redacted_whole(self) -> None:
        # `/Users/<Anna Smith>/...` redacted to `<private-path> Smith/...`: the surname survived
        # into run.json and, with the `/Users/` prefix now gone, straight past the release gate
        # as well -- so the leak was one-way and invisible to the very check meant to catch it.
        message = "output must be a regular file: /" + "Users/Anna Smith/Dropbox/audit/report/REPORT_v1.md"
        redacted = run_coordinator.redact_private_paths(message, pathlib.Path("/nowhere"))
        self.assertNotIn("Smith", redacted)
        self.assertIn("<private-path>/Dropbox/audit/report/REPORT_v1.md", redacted)

    def test_a_space_after_an_account_name_does_not_swallow_prose(self) -> None:
        # The separator lookahead is what bounds the space-tolerant alternative.
        message = "/" + "Users/alice is missing"
        self.assertEqual(
            run_coordinator.redact_private_paths(message, pathlib.Path("/nowhere")),
            "<private-path> is missing",
        )

    def test_audit_root_becomes_relative(self) -> None:
        root = pathlib.Path("/" + "Users/someone/Dropbox/audit")
        message = f"required coordinator output REPORT_v1.md must be a regular non-symlink file: {root}/coordinator/x/output.partial/REPORT_v1.md"
        redacted = run_coordinator.redact_private_paths(message, root)
        self.assertIn("file: coordinator/x/output.partial/REPORT_v1.md", redacted)
        self.assertNotIn("Users/", redacted)

    def test_foreign_private_paths_are_masked(self) -> None:
        # Paths assembled so build_package.py's own scan does not read this file as naming an account.
        home = "/" + "home/alice/x"
        tmp = "/" + "private/tmp/y"
        redacted = run_coordinator.redact_private_paths(f"saw {home} and {tmp}", pathlib.Path("/nowhere"))
        self.assertNotIn(home, redacted)
        self.assertNotIn(tmp, redacted)
        self.assertIn("<private-path>", redacted)

    def test_clean_message_is_unchanged(self) -> None:
        self.assertEqual(run_coordinator.redact_private_paths("Claude CLI exited 1", pathlib.Path("/x")), "Claude CLI exited 1")


if __name__ == "__main__":
    unittest.main()
