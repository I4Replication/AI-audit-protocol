#!/usr/bin/env python3
"""Self-contained tests for document_build_cache.py and build_docx.sh cache integration."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile


HERE = pathlib.Path(__file__).resolve().parent
HELPER = HERE / "document_build_cache.py"
BUILDER = HERE / "build_docx.sh"


def run(command: list[str], *, env: dict[str, str] | None = None,
        expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    if result.returncode != expected:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expected}: {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_executable(path: pathlib.Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def helper_contract_test(scratch: pathlib.Path) -> None:
    root = scratch / "helper-audit"
    root.mkdir()
    inputs = root / "inputs"
    inputs.mkdir()
    files = {}
    for label, content in {
        "report": "report-v1\n", "front_matter": "front-v1\n", "captions": "caption-v1\n",
        "reference_doc": "reference-v1\n", "build_script": "builder-v1\n",
    }.items():
        files[label] = inputs / label
        files[label].write_text(content, encoding="utf-8")

    def key(max_pages: str = "10") -> tuple[str, pathlib.Path]:
        descriptor = root / f"descriptor-{len(list(root.glob('descriptor-*')))}.json"
        command = [sys.executable, str(HELPER), "key", "--descriptor", str(descriptor)]
        for label, path in files.items():
            command += ["--input", f"{label}={path}"]
        command += ["--option", f"max_pages={max_pages}", "--tool", "pandoc=fake-1"]
        result = run(command)
        return result.stdout.strip(), descriptor

    base_key, descriptor = key()
    for label, path in files.items():
        original = path.read_bytes()
        path.write_bytes(original + b"changed\n")
        changed_key, _ = key()
        assert changed_key != base_key, f"{label} content did not invalidate cache key"
        path.write_bytes(original)
    option_key, _ = key("9")
    assert option_key != base_key, "relevant option did not invalidate cache key"
    print("  ok: report/front matter/captions/reference/build script/options all bind the cache key")

    docx = root / "built.docx"
    pdf = root / "built.pdf"
    metadata = root / "validation.json"
    archive = root / "cache.zip"
    docx.write_bytes(b"exact-docx-bytes")
    pdf.write_bytes(b"exact-pdf-bytes")
    metadata.write_text(json.dumps({"status": "passed", "pages": 3, "body_pages": 2}) + "\n")
    run([sys.executable, str(HELPER), "store", "--archive", str(archive), "--key", base_key,
         "--descriptor", str(descriptor), "--docx", str(docx), "--pdf", str(pdf),
         "--metadata", str(metadata)])
    expected_docx, expected_pdf = sha(docx), sha(pdf)
    docx.write_bytes(b"stale-docx")
    pdf.write_bytes(b"stale-pdf")
    run([sys.executable, str(HELPER), "restore", "--archive", str(archive), "--key", base_key,
         "--docx", str(docx), "--pdf", str(pdf), "--root", str(root)])
    assert (sha(docx), sha(pdf)) == (expected_docx, expected_pdf)
    assert not list(root.glob(".*.cache-restore")), "restore left staging files"
    print("  ok: validated cache restores exact DOCX/PDF bytes through atomic file replaces")

    docx.write_bytes(b"sentinel-docx")
    pdf.write_bytes(b"sentinel-pdf")
    sentinel = (sha(docx), sha(pdf))
    archive.write_bytes(b"not a zip")
    run([sys.executable, str(HELPER), "restore", "--archive", str(archive), "--key", base_key,
         "--docx", str(docx), "--pdf", str(pdf), "--root", str(root)], expected=4)
    assert (sha(docx), sha(pdf)) == sentinel, "invalid cache changed visible outputs"
    print("  ok: corrupt cache is rejected before either visible artifact changes")


def make_fake_tools(bin_dir: pathlib.Path, counter: pathlib.Path, abstract: str) -> None:
    bin_dir.mkdir()
    write_executable(bin_dir / "pandoc", r'''
        #!/usr/bin/env python3
        import os, pathlib, sys, zipfile
        args=sys.argv[1:]
        if args == ["--version"]:
            print("pandoc fake-1.0")
            raise SystemExit(0)
        if args[:2] == ["--print-default-data-file", "reference.docx"]:
            sys.stdout.buffer.write(b"fake-reference-docx-v1")
            raise SystemExit(0)
        converted=pathlib.Path(args[0]).read_text(encoding="utf-8")
        forbidden=("I4R-GENERATED-CLAIM-TAGS", "Findings cross-reference",
                   "source-only generated join text")
        if any(value in converted for value in forbidden):
            print("generated claim join leaked into converter input", file=sys.stderr)
            raise SystemExit(9)
        out=pathlib.Path(args[args.index("-o")+1])
        counter=pathlib.Path(os.environ["FAKE_BUILD_COUNTER"])
        with counter.open("a",encoding="utf-8") as handle: handle.write("pandoc\n")
        document=(''' + repr(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<w:body><w:p><w:r><w:t>fake document</w:t></w:r></w:p></w:body></w:document>'
        ) + r''').encode()
        rels=b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>'
        with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml",document)
            archive.writestr("word/_rels/document.xml.rels",rels)
    ''')
    write_executable(bin_dir / "soffice", r'''
        #!/usr/bin/env python3
        import os, pathlib, sys
        args=sys.argv[1:]
        if args == ["--version"]:
            print("LibreOffice fake-1.0")
            raise SystemExit(0)
        outdir=pathlib.Path(args[args.index("--outdir")+1])
        source=pathlib.Path(args[-1])
        counter=pathlib.Path(os.environ["FAKE_BUILD_COUNTER"])
        with counter.open("a",encoding="utf-8") as handle: handle.write("soffice\n")
        (outdir/(source.stem+".pdf")).write_bytes(b"%PDF-1.4\nfake exact pdf\n%%EOF\n")
    ''')
    write_executable(bin_dir / "pdfinfo", r'''
        #!/bin/sh
        if [ "${1:-}" = "-v" ]; then echo "pdfinfo fake-1.0" >&2; exit 0; fi
        echo "Pages: 3"
    ''')
    # The body-page check sees References first on page 3. The abstract check receives the same
    # page-1 text and can find both its head and its last eight words.
    write_executable(bin_dir / "pdftotext", '''
        #!/usr/bin/env python3
        import sys
        if len(sys.argv)>1 and sys.argv[1] == "-v":
            print("pdftotext fake-1.0",file=sys.stderr); raise SystemExit(0)
        print("Audit title Abstract " + ''' + repr(abstract) + ''')
        print("\\fSection 1. Introduction body")
        print("\\fReferences entry")
    ''')


def build_integration_test(scratch: pathlib.Path) -> None:
    audit = scratch / "build-audit"
    report = audit / "report"
    report.mkdir(parents=True)
    abstract = (
        "This concise abstract contains enough words for the page-one matcher and ends with "
        "a distinctive sequence of exactly eight final words"
    )
    source = report / "REPORT_final.md"
    source.write_text(
        "# Audit\n\n## Abstract\n" + abstract +
        "\n\n## 1. Introduction\nBody text.\n\n"
        "<!-- I4R-GENERATED-CLAIM-TAGS:START -->\n"
        "### Findings cross-reference\n\n"
        "- [claim 1] F1: source-only generated join text.\n"
        "<!-- I4R-GENERATED-CLAIM-TAGS:END -->\n\n"
        "## References\nReference entry.\n",
        encoding="utf-8",
    )
    (report / "front_matter.md").write_text("Audit title\n\n", encoding="utf-8")
    (report / "captions.txt").write_text("", encoding="utf-8")
    fake_bin = scratch / "fake-bin"
    counter = scratch / "build-counter.txt"
    counter.write_text("", encoding="utf-8")
    make_fake_tools(fake_bin, counter, abstract)
    env = os.environ.copy()
    env.update({
        "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
        "PANDOC": str(fake_bin / "pandoc"),
        "FAKE_BUILD_COUNTER": str(counter),
    })

    command = ["bash", str(BUILDER), str(audit), "comment"]
    first = run(command, env=env)
    assert "document cache stored:" in first.stdout
    docx, pdf = report / "comment.docx", report / "comment.pdf"
    first_hashes = (sha(docx), sha(pdf))
    first_calls = counter.read_text().splitlines()
    assert first_calls == ["pandoc", "soffice"], first_calls
    print("  ok: generated claim cross-reference block is absent from the published body")
    archives = set((audit / ".cache" / "document-builds").glob("*.zip"))
    assert len(archives) == 1

    docx.unlink(); pdf.unlink()
    second = run(command, env=env)
    assert "document cache hit:" in second.stdout
    assert (sha(docx), sha(pdf)) == first_hashes
    assert counter.read_text().splitlines() == first_calls
    manifest = json.loads((audit / "document_build_manifest.json").read_text())
    assert manifest["docx"]["sha256"] == first_hashes[0]
    assert manifest["pdf"]["sha256"] == first_hashes[1]
    print("  ok: second identical build skips Pandoc/LibreOffice and restores exact bound outputs")

    archive = next(iter(archives))
    before_refresh_cache = archive.read_bytes()
    refreshed = run(["bash", str(BUILDER), "--refresh-cache", str(audit), "comment"], env=env)
    assert "document cache stored:" in refreshed.stdout
    assert counter.read_text().splitlines().count("pandoc") == 2
    assert counter.read_text().splitlines().count("soffice") == 2
    assert zipfile.is_zipfile(archive)
    print("  ok: --refresh-cache bypasses the hit and atomically republishes a valid entry")

    refreshed_cache = archive.read_bytes()
    bypassed = run(["bash", str(BUILDER), "--no-cache", str(audit), "comment"], env=env)
    assert "document cache hit:" not in bypassed.stdout and "document cache stored:" not in bypassed.stdout
    assert archive.read_bytes() == refreshed_cache
    assert counter.read_text().splitlines().count("pandoc") == 3
    assert counter.read_text().splitlines().count("soffice") == 3
    print("  ok: --no-cache bypasses both lookup and storage without altering the cache")

    source.write_text(source.read_text() + "\nA changed source sentence.\n", encoding="utf-8")
    prior_archives = set((audit / ".cache" / "document-builds").glob("*.zip"))
    missed = run(command, env=env)
    assert "document cache stored:" in missed.stdout
    new_archives = set((audit / ".cache" / "document-builds").glob("*.zip")) - prior_archives
    assert len(new_archives) == 1
    changed_archive = next(iter(new_archives))
    assert counter.read_text().splitlines().count("pandoc") == 4
    print("  ok: source change produces a distinct cache key and a real rebuild")

    changed_archive.write_bytes(b"corrupt")
    docx.write_bytes(b"old-visible-docx")
    pdf.write_bytes(b"old-visible-pdf")
    rebuilt = run(command, env=env)
    assert "invalid document cache entry ignored" in rebuilt.stderr
    assert zipfile.is_zipfile(changed_archive)
    assert counter.read_text().splitlines().count("pandoc") == 5
    assert sha(docx) != hashlib.sha256(b"old-visible-docx").hexdigest()
    print("  ok: corrupt matching entry is rejected, rebuilt, and replaced with a valid archive")

    malformed = source.read_text(encoding="utf-8").replace(
        "<!-- I4R-GENERATED-CLAIM-TAGS:END -->", ""
    )
    source.write_text(malformed, encoding="utf-8")
    rejected = run(
        ["bash", str(BUILDER), "--no-cache", str(audit), "comment"],
        env=env,
        expected=1,
    )
    assert "malformed generated claim-tag block" in rejected.stdout
    assert counter.read_text().splitlines().count("pandoc") == 5
    print("  ok: an unbalanced generated claim block fails before document conversion")


def main() -> int:
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="document-cache-selftest."))
    try:
        print(f"== document build cache self-test in {scratch} ==")
        helper_contract_test(scratch)
        build_integration_test(scratch)
        print("SELF-TEST PASSED")
        return 0
    except (AssertionError, OSError, subprocess.SubprocessError) as exc:
        print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
