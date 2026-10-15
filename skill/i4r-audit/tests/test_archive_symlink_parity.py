#!/usr/bin/env python3
"""The archive gate and the manifest builder must agree about symlinks.

`phase1_runtime.source_files` builds `package_manifest.json` under a three-way rule: an escaping
symlink is fatal, a dangling one is skipped, an internal one is followed. `verify_archive` checks
`pkg/` against that manifest and used to reject *any* symlink, so a deposit phase 1 had already
accepted failed here instead -- and this gate runs after the whole run's provider spend, so the
refusal landed at the end of an audit rather than at preflight.

These tests build real symlinks on disk and compare the two implementations' actual verdicts.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gates = _load("run_post_delta_gates")
phase1 = _load("phase1_runtime")


def freeze(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def thaw(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            path.chmod(0o755 if path.is_dir() else 0o644)
        except OSError:
            pass


def write_manifest(root: Path) -> list[str]:
    """Write the manifest exactly as phase 1 would, and return the pkg paths it declares."""
    package = root / "pkg"
    rows, types = [], {}
    for relative, path in phase1.source_files(root):
        if not relative.startswith("pkg/"):
            continue
        inner = Path(relative).relative_to("pkg").as_posix()
        extension = path.suffix.lower().lstrip(".") or "<none>"
        types[extension] = types.get(extension, 0) + 1
        rows.append({
            "path": inner,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "extension": extension,
        })
    rows.sort(key=lambda item: (package / item["path"]).as_posix())
    tree = hashlib.sha256(
        "".join(f"{r['path']}\0{r['sha256']}\n" for r in rows).encode("utf-8")
    ).hexdigest()
    (root / "package_manifest.json").write_text(json.dumps({
        "schema_version": "2.0.0",
        "files": rows,
        "package_sha256": tree,
        "file_count": len(rows),
        "file_types": dict(sorted(types.items())),
    }), encoding="utf-8")
    return [row["path"] for row in rows]


class ArchiveSymlinkParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="i4r-archive-symlink-")
        # macOS puts temp dirs behind /var -> /private/var, and the gate refuses any input
        # that resolves outside the audit root, so resolve the root itself first.
        self.root = Path(self._tmp.name).resolve() / "audit"
        (self.root / "pkg" / "data").mkdir(parents=True)
        (self.root / "paper").mkdir()
        (self.root / "paper" / "paper.txt").write_text("paper\n", encoding="utf-8")
        (self.root / "pkg" / "data" / "real.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.root / "pkg" / "analysis.R").write_text("print(1)\n", encoding="utf-8")
        self.outside = Path(self._tmp.name).resolve() / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("secret\n", encoding="utf-8")

    def tearDown(self) -> None:
        thaw(self.root)
        self._tmp.cleanup()

    def check(self, expect_paths: list[str]) -> str:
        declared = write_manifest(self.root)
        self.assertEqual(sorted(declared), sorted(expect_paths))
        freeze(self.root / "pkg")
        return gates.verify_archive(self.root)

    def test_plain_package_passes(self) -> None:
        summary = self.check(["analysis.R", "data/real.csv"])
        self.assertIn("2 files", summary)

    def test_internal_file_symlink_is_followed_by_both(self) -> None:
        (self.root / "pkg" / "copy.csv").symlink_to(Path("data") / "real.csv")
        summary = self.check(["analysis.R", "copy.csv", "data/real.csv"])
        self.assertIn("3 files", summary)

    def test_internal_directory_symlink_is_skipped_by_both(self) -> None:
        # The link itself contributes nothing; its target's files are already enumerated.
        (self.root / "pkg" / "alias").symlink_to(Path("data"), target_is_directory=True)
        summary = self.check(["analysis.R", "data/real.csv"])
        self.assertIn("2 files", summary)

    def test_dangling_symlink_is_skipped_by_both(self) -> None:
        # A real deposit ships conf.yaml -> src/conf.yaml with no src/ in the archive. That is
        # broken packaging by the depositor, not a sandbox breach.
        (self.root / "pkg" / "conf.yaml").symlink_to(Path("src") / "conf.yaml")
        summary = self.check(["analysis.R", "data/real.csv"])
        self.assertIn("2 files", summary)

    def test_escaping_symlink_is_fatal_in_both(self) -> None:
        (self.root / "pkg" / "escape.txt").symlink_to(self.outside / "secret.txt")
        with self.assertRaises(phase1.Phase1Error):
            phase1.source_files(self.root)
        # Build a manifest without it so the gate is judged on the symlink alone, not on a
        # manifest mismatch, and confirm the gate independently refuses.
        (self.root / "package_manifest.json").write_text(json.dumps({
            "schema_version": "2.0.0", "files": [], "package_sha256": "",
            "file_count": 0, "file_types": {},
        }), encoding="utf-8")
        freeze(self.root / "pkg")
        with self.assertRaises(gates.GateError) as caught:
            gates.verify_archive(self.root)
        self.assertIn("escaping pkg", str(caught.exception))

    def test_escaping_directory_symlink_is_fatal(self) -> None:
        (self.root / "pkg" / "out").symlink_to(self.outside, target_is_directory=True)
        (self.root / "package_manifest.json").write_text(json.dumps({
            "schema_version": "2.0.0", "files": [], "package_sha256": "",
            "file_count": 0, "file_types": {},
        }), encoding="utf-8")
        freeze(self.root / "pkg")
        with self.assertRaises(gates.GateError) as caught:
            gates.verify_archive(self.root)
        self.assertIn("escaping pkg", str(caught.exception))

    def test_writable_file_is_still_refused(self) -> None:
        """Negative control: loosening the symlink rule must not loosen the freeze check."""
        write_manifest(self.root)
        freeze(self.root / "pkg")
        (self.root / "pkg").chmod(0o755)
        (self.root / "pkg" / "analysis.R").chmod(0o644)
        with self.assertRaises(gates.GateError) as caught:
            gates.verify_archive(self.root)
        self.assertIn("read-only", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
