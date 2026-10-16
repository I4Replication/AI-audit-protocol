#!/usr/bin/env python3
"""setup_workspace.sh must refuse broken archives without refusing sound ones.

The guard exists for a structurally broken deposit -- one in this tree is truncated 926 bytes
short of its own central directory, and `unzip` on a terminal PROMPTS mid-extract rather than
failing. It was implemented with `unzip -t`, which also inflates every member and so rejected a
second, perfectly extractable deposit over one zero-length entry written as Defl:N. Refusing that
one would have stranded a real paper, which is the opposite of what the guard is for.

Fixtures are built here rather than taken from the tree, so the tests hold on any machine; the
zero-length Defl:N entry reproduces the real A35 deposit's exact defect.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile
import zlib


SKILL = Path(__file__).resolve().parent.parent
SETUP = SKILL / "scripts" / "setup_workspace.sh"
HAVE_ZIPINFO = shutil.which("zipinfo") is not None


def sound_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data/values.csv", "a,b\n1,2\n")
        archive.writestr("analysis.R", "print(1)\n")


def build_zip(path: Path, members: list[tuple[str, bytes, int, int]]) -> None:
    """Write a zip from (name, raw stored bytes, method, declared uncompressed size).

    Hand-rolled because the defect being reproduced -- a member declared Deflate whose stored
    stream is zero bytes long -- is not expressible through zipfile, and editing zipfile's output
    afterwards moves every offset the central directory records.
    """
    local, central, offset = bytearray(), bytearray(), 0
    for name, raw, method, size in members:
        encoded = name.encode("utf-8")
        crc = zlib.crc32(raw if method == 0 else zlib.decompress(raw, -15)) if size else 0
        local += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, method, 0, 0x21,
                             crc, len(raw), size, len(encoded), 0) + encoded + raw
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, method, 0, 0x21,
                               crc, len(raw), size, len(encoded), 0, 0, 0, 0, 0o644 << 16,
                               offset) + encoded
        offset = len(local)
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(members), len(members),
                      len(central), len(local), 0)
    path.write_bytes(bytes(local) + bytes(central) + end)


def deflate(payload: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(payload) + compressor.flush()


def zero_length_deflate_zip(path: Path) -> None:
    """A zero-length member declared Deflate with zero compressed bytes.

    `unzip -t` calls this 'invalid compressed data to inflate' and exits 2; the entry extracts
    to its true content -- nothing -- and the archive is otherwise perfectly readable. This is
    the exact shape of the A35 deposit in this tree.
    """
    body = b"a,b\n1,2\n"
    build_zip(path, [
        ("data/values.csv", deflate(body), 8, len(body)),
        ("data/empty_notes.txt", b"", 8, 0),
    ])


def truncated_zip(path: Path) -> None:
    sound_zip(path)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])


def run_setup(audit: Path, paper: Path, package: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SETUP), str(audit), str(paper), str(package)],
        text=True, capture_output=True, check=False, stdin=subprocess.DEVNULL, timeout=120,
    )


@unittest.skipUnless(HAVE_ZIPINFO, "zipinfo is required for the archive guard")
class IntakeArchiveGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-intake-guard-")
        self.base = Path(self._tmp.name).resolve()
        self.paper = self.base / "paper.pdf"
        self.paper.write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.audit = self.base / "paperdir" / "audit"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_truncated_archive_is_refused_at_intake(self) -> None:
        package = self.base / "truncated.zip"
        truncated_zip(package)
        result = run_setup(self.audit, self.paper, package)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("corrupt or truncated", result.stderr)
        # The refusal must precede the reservation, or the operator is left unwinding one.
        self.assertFalse((self.audit / "pkg").exists())

    def test_zero_length_deflate_entry_does_not_strand_the_paper(self) -> None:
        package = self.base / "zero-length-entry.zip"
        zero_length_deflate_zip(package)
        # Precondition: this fixture is the one `unzip -t` gets wrong and `zipinfo -t` gets right.
        unzip_rc = subprocess.run(["unzip", "-t", "-qq", str(package)], capture_output=True,
                                  stdin=subprocess.DEVNULL).returncode
        zipinfo_rc = subprocess.run(["zipinfo", "-t", str(package)], capture_output=True,
                                    stdin=subprocess.DEVNULL).returncode
        self.assertGreater(unzip_rc, 1, "fixture no longer reproduces the unzip -t false positive")
        self.assertLessEqual(zipinfo_rc, 1, "fixture is structurally broken; it should not be")
        result = run_setup(self.audit, self.paper, package)
        self.assertNotIn("corrupt or truncated", result.stderr)
        self.assertNotEqual(result.returncode, 2, result.stderr)
        # It is still worth saying that a member will extract short.
        self.assertIn("do not inflate cleanly", result.stderr)

    def test_sound_archive_is_silent(self) -> None:
        package = self.base / "sound.zip"
        sound_zip(package)
        result = run_setup(self.audit, self.paper, package)
        self.assertNotIn("corrupt or truncated", result.stderr)
        self.assertNotIn("do not inflate cleanly", result.stderr)

    def test_conflicted_copy_in_the_deposit_is_refused(self) -> None:
        """The scan used to look only at the audit dir, which at intake is empty or absent."""
        package = self.base / "deposit"
        (package / "data").mkdir(parents=True)
        (package / "data" / "values.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (package / "data" / "values (alice's conflicted copy 2026-08-02).csv").write_text(
            "a,b\n9,9\n", encoding="utf-8")
        result = run_setup(self.audit, self.paper, package)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("conflicted copies", result.stderr)
        self.assertIn("values (alice's conflicted copy", result.stderr)

    def test_clean_directory_deposit_is_accepted(self) -> None:
        package = self.base / "clean_deposit"
        (package / "data").mkdir(parents=True)
        (package / "data" / "values.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        result = run_setup(self.audit, self.paper, package)
        self.assertNotIn("conflicted copies", result.stderr)


if __name__ == "__main__":
    unittest.main()
