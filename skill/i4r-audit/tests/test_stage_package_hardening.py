#!/usr/bin/env python3
"""Focused controls for release-path, privacy, and package hardening."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


SKILL = Path(__file__).resolve().parents[1]
STAGE = SKILL / "scripts/stage_release.sh"
BUILDER = SKILL / "scripts/build_package.py"
PRIVACY = SKILL / "scripts/check_release_privacy.py"
GENERATOR = SKILL / "scripts/generate_finding_artifacts.py"

_privacy_spec = importlib.util.spec_from_file_location("i4r_release_privacy", PRIVACY)
assert _privacy_spec and _privacy_spec.loader
privacy = importlib.util.module_from_spec(_privacy_spec)
sys.modules[_privacy_spec.name] = privacy
_privacy_spec.loader.exec_module(privacy)


def write(path: Path, content: str | bytes = "fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run(*command: str | Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )


def preflight_audit(root: Path) -> Path:
    """Build enough intake state to reach symlink preflight, not generation."""
    audit = root / "audit"
    mandatory = (
        "report/REPORT_final.md",
        "report/LEDGER.md",
        "report/ADJUDICATION.md",
        "report/01_reproduction.md",
        "report/02_data_integrity.md",
        "report/03_text_vs_data.md",
        "report/04_code_methods.md",
        "report/05_privacy_archive.md",
        "verification.Rmd",
        "verification_status.txt",
        "verification_summary.csv",
        "verification_manifest.json",
    )
    for relative in mandatory:
        write(audit / relative)
    write(audit / "claims_script.R")
    return audit


def write_pdf(path: Path, *, author: str, text: str = "sanitized fixture") -> None:
    """Write a tiny standards-compliant one-page PDF without test dependencies."""
    escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    escaped_author = author.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 120 Td ({escaped_text}) Tj ET\n".encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
        f"<< /Author ({escaped_author}) >>".encode("ascii"),
    )
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    write(path, bytes(payload))


def release_fixture(base: Path) -> Path:
    """Create the smallest projection-bound audit accepted by staging."""
    audit = base / "Discipline/Journal/Paper/audit"
    report = "# Independent audit\n\n## 1. Finding\n\nSanitized evidence [claim 1].\n"
    write(audit / "report/REPORT_final.md", report)
    write(audit / "report/ADJUDICATION.md", "# Adjudication\n\nAccepted.\n")
    for name in (
        "01_reproduction.md",
        "02_data_integrity.md",
        "03_text_vs_data.md",
        "04_code_methods.md",
        "05_privacy_archive.md",
    ):
        write(audit / f"report/{name}", f"# {name}\n\nComplete.\n")
    write(audit / "comment.json", json.dumps({"audit_id": "hardening-fixture", "findings": []}) + "\n")
    write(audit / "work/analysis.py", "print('sanitized fixture')\n")
    evidence = audit / "work/check_primary.txt"
    snippet = audit / "work/check_primary.R"
    write(evidence, "expected=2 observed=2\n")
    write(snippet, "stopifnot(1 + 1 == 2)\n")
    finding = json.loads((SKILL / "schemas/fixtures/v2_finding_row.json").read_text())
    finding.update(
        {
            "claim": "The sanitized arithmetic fixture reproduces exactly.",
            "source_location": {"path": "report/REPORT_final.md", "locator": "Section 1"},
            "claim_ref": "Section 1",
            "report_section": "Section 1",
            "settled": None,
        }
    )
    finding["evidence"].update(
        {
            "source_hashes": [{"path": "work/check_primary.txt", "sha256": digest(evidence)}],
            "rerun_command": "Rscript work/check_primary.R",
            "expected": 2,
            "observed": 2,
            "tolerance_basis": "Exact integer comparison.",
        }
    )
    finding["dependencies"]["artifact_paths"] = ["work/check_primary.txt", "work/check_primary.R"]
    finding["verification_snippet"] = {
        "kind": "code",
        "path": "work/check_primary.R",
        "basis": "The bounded arithmetic check reproduces exactly.",
    }
    write(audit / "work/findings.jsonl", json.dumps(finding, sort_keys=True) + "\n")
    write(audit / "package_manifest.json", '{"files": []}\n')
    (audit / "pkg").mkdir(parents=True)
    generated = run(sys.executable, GENERATOR, audit, "--apply")
    if generated.returncode != 0:
        raise AssertionError(generated.stdout + generated.stderr)
    write(audit / "report/REPORT_v1.md", (audit / "report/REPORT_final.md").read_text())
    write(audit / "verification_status.txt", "0\n")
    write(audit / "verification_summary.csv", "claim_id,key,verdict,detail\nC-1,fixture,PASS,sanitized\n")
    manifest: dict[str, object] = {
        "schema_version": "2.0.0",
        "status": 0,
        "status_artifact": {
            "path": "verification_status.txt",
            "sha256": digest(audit / "verification_status.txt"),
        },
        "render": {
            "transaction_id": "hardening-render-1",
            "started_at": "2026-08-27T12:00:00Z",
            "ended_at": "2026-08-27T12:00:01Z",
            "exit_status": 0,
            "mode": "executed",
        },
    }
    for label, relative in {
        "report": "report/REPORT_final.md",
        "comment": "comment.json",
        "summary": "verification_summary.csv",
        "notebook": "verification.Rmd",
    }.items():
        path = audit / relative
        manifest[label] = {"path": relative, "sha256": digest(path), "bytes": path.stat().st_size}
    write(audit / "verification_manifest.json", json.dumps(manifest, indent=2) + "\n")
    return audit


class StagePathAndSymlinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-stage-hardening-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_release_target_must_be_disjoint_after_canonicalization(self) -> None:
        audit = self.root / "outer/audit"
        audit.mkdir(parents=True)
        alias = self.root / "audit-alias"
        alias.symlink_to(audit, target_is_directory=True)
        targets = [
            audit / "release",
            audit / "child/..",
            alias / "release",
            audit.parent,
        ]
        case_alias = audit.parent / audit.name.upper()
        if case_alias.exists():
            targets.append(case_alias / "case-release")
        for index, target in enumerate(targets):
            with self.subTest(index=index, target=target):
                result = run("/bin/bash", STAGE, audit, target)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be disjoint", result.stdout + result.stderr)
        self.assertFalse((audit / "release").exists())

    def test_symlinked_mandatory_claims_and_source_inputs_fail_preflight(self) -> None:
        outside = self.root / "outside.txt"
        write(outside)
        cases = ("mandatory", "claims", "source", "source_directory")
        for case in cases:
            with self.subTest(case=case):
                base = self.root / case
                audit = preflight_audit(base)
                if case == "mandatory":
                    (audit / "report/LEDGER.md").unlink()
                    (audit / "report/LEDGER.md").symlink_to(outside)
                elif case == "claims":
                    (audit / "claims_script.R").unlink()
                    (audit / "claims_script.R").symlink_to(outside)
                elif case == "source":
                    (audit / "work").mkdir()
                    (audit / "work/analysis.qmd").symlink_to(outside)
                else:
                    outside_source = self.root / "outside-source"
                    outside_source.mkdir(exist_ok=True)
                    write(outside_source / "analysis.qmd", "1 + 1\n")
                    (audit / "work").symlink_to(outside_source, target_is_directory=True)
                release = base / "release"
                result = run("/bin/bash", STAGE, audit, release)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("symlinked", (result.stdout + result.stderr).lower())

    def test_qmd_source_is_staged_and_scanned(self) -> None:
        audit = release_fixture(self.root / "clean")
        # PDF is optional to staging and has separate fail-closed tests below.
        for pdf in (audit / "report").glob("*.pdf"):
            pdf.unlink()
        write(audit / "work/reproducible.qmd", "---\ntitle: fixture\n---\n1 + 1\n")
        release = self.root / "clean-release"
        result = run("/bin/bash", STAGE, audit, release)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((release / "work/reproducible.qmd").is_file())

    def test_statistical_source_private_path_blocks_staging(self) -> None:
        audit = release_fixture(self.root / "leaky")
        for pdf in (audit / "report").glob("*.pdf"):
            pdf.unlink()
        private = "/" + "home/realperson/private/input"
        write(audit / "work/reproduce.sas", f"%include '{private}';\n")
        release = self.root / "leaky-release"
        result = run("/bin/bash", STAGE, audit, release)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("absolute path(s) naming a real account", result.stdout + result.stderr)
        self.assertTrue((release / "RELEASE_REJECTED").is_file())


class PrivacyInspectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-privacy-hardening-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_every_required_source_suffix_detects_private_paths(self) -> None:
        private = "/" + "Us" + "ers/realperson/private/input"
        required = {".rmd", ".qmd", ".ipynb", ".do", ".sps", ".sas", ".jl", ".m"}
        self.assertTrue(required.issubset(privacy.TEXT_SUFFIXES))
        for suffix in sorted(required):
            with self.subTest(suffix=suffix):
                path = self.root / f"leak{suffix}"
                write(path, private + "\n")
                issues = privacy.inspect_file(path, path.name)
                self.assertTrue(any(issue.category == "private_path" for issue in issues))

        encoded = self.root / "encoded.qmd"
        write(encoded, "%2F" + "Us" + "ers%2Frealperson%2Fprivate%2Finput\n")
        self.assertTrue(any(issue.category == "private_path" for issue in privacy.inspect_file(encoded)))

    def test_docx_metadata_is_scanned_and_malformed_documents_fail_closed(self) -> None:
        private = "/" + "home/realperson/private/input"
        document = self.root / "metadata.docx"
        with zipfile.ZipFile(document, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            midpoint = len(private) // 2
            archive.writestr(
                "word/document.xml",
                f"<document><text>{private[:midpoint]}</text><text>{private[midpoint:]}</text></document>",
            )
            archive.writestr("docProps/core.xml", "<core><source>sanitized</source></core>")
        issues = privacy.inspect_file(document, document.name)
        self.assertTrue(any(issue.category == "private_path" for issue in issues))

        metadata_document = self.root / "core-metadata.docx"
        encoded_private = private.replace("/", "&#47;")
        with zipfile.ZipFile(metadata_document, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document>sanitized</document>")
            archive.writestr("docProps/core.xml", f"<core><source>{encoded_private}</source></core>")
        metadata_issues = privacy.inspect_file(metadata_document, metadata_document.name)
        self.assertTrue(any(issue.category == "private_path" for issue in metadata_issues))

        malformed = self.root / "malformed.docx"
        write(malformed, b"not a zip")
        with self.assertRaises(privacy.InspectionError):
            privacy.inspect_file(malformed, malformed.name)

    def test_malformed_pdf_fails_closed(self) -> None:
        malformed = self.root / "malformed.pdf"
        write(malformed, b"%PDF invalid\n")
        with self.assertRaises(privacy.InspectionError):
            privacy.inspect_file(malformed, malformed.name)

    def test_pdf_metadata_is_extracted_and_scanned(self) -> None:
        document = self.root / "metadata.pdf"
        private = "/" + "Us" + "ers/realperson/private/input"
        write_pdf(document, author=private)
        issues = privacy.inspect_file(document, document.name)
        self.assertTrue(any(issue.category == "private_path" for issue in issues))


class PackageBuilderHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-package-hardening-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "mini-skill"
        scripts = self.source / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(BUILDER, scripts / BUILDER.name)
        shutil.copy2(PRIVACY, scripts / PRIVACY.name)
        write(self.source / "VERSION", "2.0.0\n")
        write(self.source / "config/pipeline-policy-v2.json", "{}\n")
        write(self.source / "SKILL.md", "# Clean fixture\n")
        # This stale source artifact must never become a false manifest entry.
        write(self.source / "PACKAGE_MANIFEST.json", '{"stale": true}\n')
        self.builder = scripts / BUILDER.name

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_has_no_self_entry_and_concurrent_builds_use_unique_temps(self) -> None:
        output = self.root / "mini-skill.zip"
        processes = [
            subprocess.Popen(
                [sys.executable, str(self.builder), "--output", str(output)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertFalse(list(self.root.glob(".mini-skill.zip.*.tmp")))
        check = run(sys.executable, self.builder, "--check", "--output", output)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read("mini-skill/PACKAGE_MANIFEST.json"))
        self.assertFalse(
            any(row["path"].endswith("/PACKAGE_MANIFEST.json") for row in manifest["entries"])
        )

    def test_output_inside_source_and_private_qmd_are_rejected(self) -> None:
        inside_paths = [self.source / "forbidden.zip"]
        case_alias = self.source.parent / self.source.name.upper()
        if case_alias.exists():
            inside_paths.append(case_alias / "case-forbidden.zip")
        for inside in inside_paths:
            with self.subTest(inside=inside):
                result = run(sys.executable, self.builder, "--output", inside)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside source root", result.stdout + result.stderr)
                self.assertFalse(inside.exists())

        private = "/" + "Us" + "ers/realperson/private/input"
        write(self.source / "private.qmd", private + "\n")
        result = run(sys.executable, self.builder, "--output", self.root / "leaky.zip")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private absolute paths", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
