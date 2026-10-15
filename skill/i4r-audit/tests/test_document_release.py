#!/usr/bin/env python3
"""Exact page and transaction regressions for document manifest v3."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


SKILL = Path(__file__).resolve().parents[1]
HELPER = SKILL / "scripts/document_release.py"
BUILDER = SKILL / "scripts/build_docx.sh"
CACHE = SKILL / "scripts/document_build_cache.py"
STAGE = SKILL / "scripts/stage_release.sh"
MANIFEST_SCHEMA = SKILL / "schemas/v3/document-build-manifest.schema.json"
FESSI_BOUNDARY_FIXTURE = SKILL / "tests/fixtures/fessi-document-boundary.json"
FESSI_BOUNDARY_FIXTURE_SHA256 = "8e4648a833670604542cf2dc673ccbbcbe5f75d3a0a952e5b9cbadced3bafa44"

ABSTRACT = (
    "This bounded audit fixture verifies exact rendered pages and keeps its distinctive "
    "ending phrase entirely on the opening report page"
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")


def policy_snapshot(root: Path) -> None:
    policy = json.loads((SKILL / "config/pipeline-policy-v2.json").read_text(encoding="utf-8"))
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    envelope = {
        "snapshot_version": "1",
        "policy_sha256": hashlib.sha256(canonical).hexdigest(),
        "policy": policy,
    }
    write(root / "pipeline-policy.snapshot.json", json.dumps(envelope, indent=2, sort_keys=True) + "\n")


def pdf_escape(value: str) -> bytes:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii")


def multi_page_pdf(pages: list[list[str]]) -> bytes:
    """Generate a dependency-free, text-extractable Letter PDF."""
    count = len(pages)
    page_first = 3
    content_first = page_first + count
    font_number = content_first + count
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [" + " ".join(
            f"{page_first + index} 0 R" for index in range(count)
        ) + f"] /Count {count} >>").encode("ascii"),
    ]
    for index in range(count):
        objects.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
             f"/Contents {content_first + index} 0 R >>").encode("ascii")
        )
    for lines in pages:
        commands = [b"BT /F1 11 Tf 72 740 Td"]
        for index, line in enumerate(lines):
            if index:
                commands.append(b"0 -16 Td")
            commands.append(b"(" + pdf_escape(line) + b") Tj")
        commands.append(b"ET")
        stream = b"\n".join(commands) + b"\n"
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") +
                       b" >>\nstream\n" + stream + b"endstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii") + value + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(payload)


def fessi_boundary_pages(fixture: dict[str, object]) -> list[list[str]]:
    body_pages = int(fixture["diagnostic_body_pages"])
    finding_ids = list(fixture["finding_ids"])
    pages: list[list[str]] = [[
        "Independent audit", "Abstract", *textwrap.wrap(ABSTRACT, width=70),
        "Fessi sanitized boundary surrogate", finding_ids[0],
    ]]
    pages.extend([
        [f"Section {number}. Fessi boundary surrogate", finding_ids[number - 1]]
        for number in range(2, body_pages + 1)
    ])
    pages.extend([
        ["Data and Code Availability", "Sanitized availability statement."],
        ["References", "Sanitized reference entry."],
        ["Appendix A. Summary of findings", *finding_ids],
    ])
    return pages


def audit_fixture(base: Path, *, body_pages: int, back_pages: int,
                  preview: bool = False) -> tuple[Path, Path]:
    audit = base / "audit"
    report = audit / "report"
    report.mkdir(parents=True)
    source = (
        "# Independent audit\n\n## Abstract\n\n" + ABSTRACT +
        "\n\n## 1. Finding\n\nThe bounded release check passes only on exact bytes.\n"
    )
    if back_pages:
        source += "\n## References\n\nReference entry.\n"
    write(report / "REPORT_final.md", source)
    write(report / "front_matter.md", "Independent audit\n")
    write(report / "captions.txt", "")
    policy_snapshot(audit)
    pages: list[list[str]] = [["Independent audit", "Abstract",
                              *textwrap.wrap(ABSTRACT, width=70), "Section 1. Finding"]]
    pages.extend([[f"Section {index + 1}. Body", "Bounded report text."]
                  for index in range(1, body_pages)])
    if back_pages:
        pages.append(["References", "Reference entry."])
        pages.extend([[f"Appendix continuation {index}", "Arbitrary back matter."]
                      for index in range(2, back_pages + 1)])
    if preview:
        pdf = report / "previews/fessi-max14.pdf"
    else:
        pdf = report / "comment.pdf"
    write(pdf, multi_page_pdf(pages))
    return audit, pdf


def create_manifest(audit: Path, pdf: Path, *, preview: bool = False,
                    expected: int = 0) -> subprocess.CompletedProcess[str]:
    relative = pdf.relative_to(audit).as_posix()
    output = (pdf.with_suffix(".manifest.json") if preview
              else audit / "document_build_manifest.json")
    command = [
        sys.executable, str(HELPER), "create-manifest",
        "--audit", str(audit), "--report", str(audit / "report/REPORT_final.md"),
        "--front-matter", str(audit / "report/front_matter.md"),
        "--captions", str(audit / "report/captions.txt"),
        "--pdf", str(pdf), "--pdf-relative", relative,
        "--builder", str(BUILDER), "--cache-helper", str(CACHE),
        "--pandoc-identity", "test-pandoc", "--renderer-identity", "test-libreoffice",
        "--pdfinfo-identity", "test-pdfinfo", "--pdftotext-identity", "test-pdftotext",
        "--build-kind", "preview" if preview else "canonical",
        "--cap", "14" if preview else "10", "--manifest-out", str(output),
        "--diagnostic-out", str(audit / "document_build_diagnostics/last_failure.json"),
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    if result.returncode != expected:
        raise AssertionError(
            f"unexpected exit {result.returncode}, expected {expected}\n{result.stdout}\n{result.stderr}"
        )
    return result


@unittest.skipUnless(shutil.which("pdfinfo") and shutil.which("pdftotext"),
                     "Poppler exact-PDF tools are required")
class DocumentReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="i4r-document-release-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ten_body_pages_pass_and_arbitrary_back_matter_is_excluded(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=10, back_pages=17)
        create_manifest(audit, pdf)
        manifest = json.loads((audit / "document_build_manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], "3.0.0")
        schema_result = subprocess.run(
            [sys.executable, str(SKILL / "scripts/validate_json_schema.py"),
             str(audit / "document_build_manifest.json"), str(MANIFEST_SCHEMA)],
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(schema_result.returncode, 0, schema_result.stdout + schema_result.stderr)
        self.assertEqual(manifest["pages"], {
            "total_pages": 27,
            "body_pages": 10,
            "back_matter_pages": 17,
            "first_back_matter_page": 11,
            "abstract_ending_page": 1,
            "body_page_cap": 10,
        })
        result = subprocess.run(
            [sys.executable, str(HELPER), "verify", str(audit),
             "--builder", str(BUILDER), "--boundary", "test"],
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_data_and_code_availability_starts_uncounted_back_matter(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=10, back_pages=0)
        source = (audit / "report/REPORT_final.md").read_text(encoding="utf-8")
        write(
            audit / "report/REPORT_final.md",
            source
            + "\n## Data and Code Availability\n\nSanitized availability statement.\n"
            + "\n## References\n\nReference entry.\n"
            + "\n## Appendix A. Summary of findings\n\nArbitrary appendix detail.\n",
        )
        pages: list[list[str]] = [[
            "Independent audit", "Abstract", *textwrap.wrap(ABSTRACT, width=70),
            "Section 1. Finding",
        ]]
        pages.extend([[f"Section {index}. Body", "Bounded report text."]
                      for index in range(2, 11)])
        pages.extend([
            ["Data and Code Availability", "Sanitized availability statement."],
            ["References", "Reference entry."],
            ["Appendix A. Summary of findings", "Arbitrary appendix detail."],
        ])
        write(pdf, multi_page_pdf(pages))
        create_manifest(audit, pdf)
        manifest = json.loads((audit / "document_build_manifest.json").read_text())
        self.assertEqual(manifest["pages"]["body_pages"], 10)
        self.assertEqual(manifest["pages"]["first_back_matter_page"], 11)
        self.assertEqual(manifest["pages"]["back_matter_pages"], 3)
        recounted = subprocess.run(
            [sys.executable, str(HELPER), "recount",
             str(audit / "document_build_manifest.json"), str(pdf)],
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(recounted.returncode, 0, recounted.stdout + recounted.stderr)

    def test_case_variant_extra_pdf_and_docx_are_rejected(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=1, back_pages=0)
        create_manifest(audit, pdf)
        cases = {
            "unbound.PDF": pdf.read_bytes(),
            "unbound.DoCx": b"not a release document",
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                extra = audit / "report" / name
                write(extra, content)
                result = subprocess.run(
                    [sys.executable, str(HELPER), "verify", str(audit),
                     "--builder", str(BUILDER), "--boundary", "case-variant"],
                    text=True, capture_output=True, timeout=30,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("extra", (result.stdout + result.stderr).lower())
                extra.unlink()

    def test_eleven_and_fourteen_body_pages_fail_without_a_release_manifest(self) -> None:
        for pages in (11, 14):
            with self.subTest(body_pages=pages):
                case = self.root / f"case-{pages}"
                audit, pdf = audit_fixture(case, body_pages=pages, back_pages=3)
                result = create_manifest(audit, pdf, expected=1)
                self.assertIn(f"{pages} body page(s), cap 10", result.stderr)
                self.assertFalse((audit / "document_build_manifest.json").exists())
                diagnostic = json.loads(
                    (audit / "document_build_diagnostics/last_failure.json").read_text()
                )
                self.assertFalse(diagnostic["release_eligible"])

    def test_ambiguous_back_matter_boundary_fails_closed(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=1, back_pages=1)
        write(pdf, multi_page_pdf([
            ["Independent audit", "Abstract", *textwrap.wrap(ABSTRACT, width=70)],
            ["Body text incorrectly shares this page", "References", "Reference entry."],
        ]))
        result = create_manifest(audit, pdf, expected=1)
        self.assertIn("ambiguous back-matter boundary", result.stderr)
        self.assertFalse((audit / "document_build_manifest.json").exists())

    def test_preview_is_nonrelease_and_cannot_stage(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=14, back_pages=2, preview=True)
        create_manifest(audit, pdf, preview=True)
        preview_manifest = pdf.with_suffix(".manifest.json")
        preview_value = json.loads(preview_manifest.read_text())
        self.assertFalse(preview_value["release_eligible"])
        self.assertEqual(preview_value["page_limit"]["source"], "preview_cli")
        for relative in (
            "report/LEDGER.md", "report/ADJUDICATION.md", "report/01_reproduction.md",
            "report/02_data_integrity.md", "report/03_text_vs_data.md",
            "report/04_code_methods.md", "report/05_privacy_archive.md",
            "verification.Rmd", "verification_status.txt", "verification_summary.csv",
            "verification_manifest.json", "claims_script.R",
        ):
            write(audit / relative, "fixture\n")
        release = self.root / "stage"
        staged = subprocess.run(
            ["/bin/bash", str(STAGE), str(audit), str(release)],
            text=True, capture_output=True, timeout=30,
        )
        self.assertNotEqual(staged.returncode, 0)
        self.assertIn("document_build_manifest.json", staged.stdout + staged.stderr)
        self.assertFalse((release / "report/previews/fessi-max14.pdf").exists())

    def test_failed_rebuild_leaves_prior_passing_bytes_and_manifest_untouched(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=10, back_pages=1)
        create_manifest(audit, pdf)
        manifest_path = audit / "document_build_manifest.json"
        before = (digest(pdf), digest(manifest_path))

        candidate = audit / ".document-build-transactions/candidate/document.pdf"
        pages = [["Independent audit", "Abstract", *textwrap.wrap(ABSTRACT, width=70)]]
        pages.extend([[f"Section {index}. Body"] for index in range(2, 12)])
        pages.append(["References", "Reference entry."])
        write(candidate, multi_page_pdf(pages))
        failed_manifest = candidate.parent / "manifest.json"
        result = subprocess.run([
            sys.executable, str(HELPER), "create-manifest", "--audit", str(audit),
            "--report", str(audit / "report/REPORT_final.md"),
            "--front-matter", str(audit / "report/front_matter.md"),
            "--captions", str(audit / "report/captions.txt"),
            "--pdf", str(candidate), "--pdf-relative", "report/comment.pdf",
            "--builder", str(BUILDER), "--cache-helper", str(CACHE),
            "--pandoc-identity", "test", "--renderer-identity", "test",
            "--pdfinfo-identity", "test", "--pdftotext-identity", "test",
            "--build-kind", "canonical", "--cap", "10", "--manifest-out", str(failed_manifest),
        ], text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((digest(pdf), digest(manifest_path)), before)
        self.assertFalse(failed_manifest.exists())

    def test_failed_transaction_commit_rolls_back_the_prior_pair(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=2, back_pages=1)
        create_manifest(audit, pdf)
        manifest_path = audit / "document_build_manifest.json"
        before = (digest(pdf), digest(manifest_path))

        candidate_dir = audit / ".document-build-transactions/candidate"
        candidate_pdf = candidate_dir / "document.pdf"
        candidate_pages = [
            ["Independent audit", "Abstract", *textwrap.wrap(ABSTRACT, width=70),
             "Changed but still valid body text."],
            ["Section 2. Body", "Second body page."],
            ["References", "Replacement reference entry."],
        ]
        write(candidate_pdf, multi_page_pdf(candidate_pages))
        candidate_manifest = candidate_dir / "document_build_manifest.json"
        result = subprocess.run([
            sys.executable, str(HELPER), "create-manifest", "--audit", str(audit),
            "--report", str(audit / "report/REPORT_final.md"),
            "--front-matter", str(audit / "report/front_matter.md"),
            "--captions", str(audit / "report/captions.txt"),
            "--pdf", str(candidate_pdf), "--pdf-relative", "report/comment.pdf",
            "--builder", str(BUILDER), "--cache-helper", str(CACHE),
            "--pandoc-identity", "test", "--renderer-identity", "test",
            "--pdfinfo-identity", "test", "--pdftotext-identity", "test",
            "--build-kind", "canonical", "--cap", "10",
            "--manifest-out", str(candidate_manifest),
        ], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # The exact commit verifier rejects an extra release-glob PDF after the atomic
        # renames.  The promotion helper must then restore both old visible files.
        write(audit / "report/unbound.pdf", pdf.read_bytes())
        promoted = subprocess.run([
            sys.executable, str(HELPER), "promote", "--audit", str(audit),
            "--manifest-source", str(candidate_manifest),
            "--manifest-target", str(manifest_path),
            "--pdf-source", str(candidate_pdf), "--pdf-target", str(pdf),
        ], text=True, capture_output=True, timeout=30)
        self.assertNotEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
        self.assertIn("extra/proxy PDFs", promoted.stderr)
        self.assertEqual((digest(pdf), digest(manifest_path)), before)

    def test_v2_manifest_is_rejected_at_every_document_boundary(self) -> None:
        audit, pdf = audit_fixture(self.root, body_pages=1, back_pages=0)
        write(audit / "document_build_manifest.json", json.dumps({
            "schema_version": "2.0.0",
            "pdf": {"path": "report/comment.pdf", "sha256": digest(pdf)},
        }) + "\n")
        for command in (
            [sys.executable, str(HELPER), "verify", str(audit), "--builder", str(BUILDER)],
            [sys.executable, str(HELPER), "recount",
             str(audit / "document_build_manifest.json"), str(pdf)],
        ):
            with self.subTest(command=command[2]):
                result = subprocess.run(command, text=True, capture_output=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema 3.0.0", result.stderr)

    def test_fourteen_body_page_pdf_fails_verify_and_independent_recount(self) -> None:
        self.assertEqual(digest(FESSI_BOUNDARY_FIXTURE), FESSI_BOUNDARY_FIXTURE_SHA256)
        fixture = json.loads(FESSI_BOUNDARY_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema_version"], "i4r-fessi-document-boundary-1")
        self.assertFalse(fixture["original_artifact_bundled"])
        self.assertEqual(fixture["finding_ids"], [f"P{number}" for number in range(1, 21)])
        pages = fessi_boundary_pages(fixture)
        pdf_bytes = multi_page_pdf(pages)
        self.assertEqual(hashlib.sha256(pdf_bytes).hexdigest(), fixture["surrogate_pdf_sha256"])

        audit, _ = audit_fixture(self.root, body_pages=1, back_pages=0)
        findings = "\n\n".join(
            f"## {number}. Sanitized finding {claim_id}\n\nPreserved finding {claim_id}."
            for number, claim_id in enumerate(fixture["finding_ids"], 1)
        )
        appendix = "\n".join(f"| {claim_id} | Preserved |" for claim_id in fixture["finding_ids"])
        write(
            audit / "report/REPORT_final.md",
            "# Independent audit\n\n## Abstract\n\n" + ABSTRACT + "\n\n" + findings
            + "\n\n## Data and Code Availability\n\nSanitized availability statement."
            + "\n\n## References\n\nSanitized reference entry."
            + "\n\n## Appendix A. Summary of findings\n\n" + appendix + "\n",
        )
        preview_pdf = audit / "report/previews/fessi-max14.pdf"
        write(preview_pdf, pdf_bytes)
        create_manifest(audit, preview_pdf, preview=True)
        preview_manifest = preview_pdf.with_suffix(".manifest.json")
        preview_value = json.loads(preview_manifest.read_text())
        self.assertEqual(preview_value["pages"]["body_pages"], fixture["diagnostic_body_pages"])
        self.assertEqual(
            preview_value["pages"]["first_back_matter_page"],
            fixture["diagnostic_first_back_matter_page"],
        )
        pdf = audit / "report/comment.pdf"
        write(pdf, preview_pdf.read_bytes())

        # Simulate a stale/forged release record that understates the body boundary.  Both
        # the source-tree verifier and the flattened-candidate recount must inspect exact PDF
        # pages instead of trusting the numeric claim in the manifest.
        manifest = preview_value
        manifest["build_kind"] = "canonical"
        manifest["release_eligible"] = True
        manifest["artifacts"]["pdf"]["path"] = "report/comment.pdf"
        manifest["pages"].update({
            "body_pages": 10,
            "back_matter_pages": 6,
            "first_back_matter_page": 11,
            "body_page_cap": 10,
        })
        manifest["page_limit"] = {
            "value": 10,
            "source": "hash_bound_policy_snapshot",
            "release_enforced": True,
        }
        write(audit / "document_build_manifest.json", json.dumps(manifest, indent=2) + "\n")

        commands = (
            [sys.executable, str(HELPER), "verify", str(audit),
             "--builder", str(BUILDER), "--boundary", "fessi-regression"],
            [sys.executable, str(HELPER), "recount",
             str(audit / "document_build_manifest.json"), str(pdf)],
        )
        for command in commands:
            with self.subTest(boundary=command[2]):
                result = subprocess.run(command, text=True, capture_output=True, timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("body", result.stderr.lower())



class LibreOfficeProfileUrlTest(unittest.TestCase):
    """The LibreOffice profile is passed as a file:// URL. An audit directory named after a paper
    title routinely contains colons and spaces, which are not URL-safe, and LibreOffice aborts on
    such a URL (first production audit: SIGABRT on every canonical build). The builder must never
    derive that URL from the audit or transaction path."""

    def test_libreoffice_profile_url_is_not_derived_from_the_audit_path(self) -> None:
        text = BUILDER.read_text(encoding="utf-8")
        self.assertNotIn('UserInstallation="file://$TX', text)
        self.assertNotIn('UserInstallation="file://$DIR', text)
        self.assertIn('LO_PROFILE=$(mktemp -d "${TMPDIR:-/tmp}/i4r-lo-profile.XXXXXX")', text)
        self.assertIn('-env:UserInstallation="file://$LO_PROFILE"', text)

if __name__ == "__main__":
    unittest.main()
