#!/bin/bash
# setup_workspace.sh <audit-dir> <paper.pdf> <package.zip|dir>
set -euo pipefail
DIR="${1:?audit dir}"; PAPER="${2:?paper pdf}"; PKG="${3:?package zip or dir}"
[ -f "$PAPER" ] || { echo "FATAL: paper not found: $PAPER" >&2; exit 2; }
[ -d "$PKG" ] || [ -f "$PKG" ] || { echo "FATAL: package not found: $PKG" >&2; exit 2; }

# A populated intake directory can retain files that no longer exist in the source package.
# Failing closed is cheaper than auditing a hybrid of two deposits.
if [ "${I4R_ALLOW_EXISTING:-0}" != "1" ] && [ -d "$DIR/pkg" ] &&
   [ -n "$(find "$DIR/pkg" -mindepth 1 -print -quit 2>/dev/null)" ]; then
  echo "FATAL: $DIR/pkg is not empty; choose a fresh audit directory" >&2
  echo "       Set I4R_ALLOW_EXISTING=1 only for an explicitly documented recovery." >&2
  exit 2
fi

# ---- automatic reservation (2026-08-24): the audit claims its own paper before touching it ----
# The paper dir is the audit dir's parent; the discipline tree is two levels up. If the audit is
# running OUTSIDE the shared tree (fixtures, tmp dirs, one-off jobs) there is no reservations
# root to write into, so the claim degrades to a warning. Inside the tree, a refusal ABORTS:
# someone else is already on this paper. I4R_SKIP_RESERVE=1 bypasses (say why in run notes).
if [ "${I4R_SKIP_RESERVE:-0}" != "1" ]; then
  # the paper dir may not exist yet on a fresh workspace; a failed cd must not kill the run
  PDIR="$(cd "$(dirname "$DIR")" 2>/dev/null && pwd || true)"
  RROOT="${PDIR:+$(dirname "$(dirname "$PDIR")")}"
  RESV="$(cd "$(dirname "$0")" && pwd)/reserve_paper.sh"
  # the discipline root is recognised by its markers, not by merely having subdirectories —
  # otherwise a tmp-dir fixture two levels deep would scatter reservations into random parents
  if [ -x "$RESV" ] && { [ -d "$RROOT/reservations" ] || [ -d "$RROOT/Completed reports" ]; }; then
    set +e; OUT="$("$RESV" claim "$PDIR" 2>&1)"; RC=$?; set -e
    echo "$OUT" | sed 's/^/  [reserve] /'
    if [ $RC -ne 0 ]; then
      echo "FATAL: could not reserve this paper — someone else holds it or a conflict is unresolved."
      echo "       Pick another paper, or resolve per the message above. (I4R_SKIP_RESERVE=1 overrides.)"
      exit 1
    fi
  else
    echo "  [reserve] outside the shared tree — no reservation written (fixtures/tmp are exempt)"
  fi
fi

mkdir -p "$DIR"/{pkg,paper,work,report,critique}
cp "$PAPER" "$DIR/paper/paper.pdf"
if [ -d "$PKG" ]; then cp -R "$PKG"/. "$DIR/pkg/"; else unzip -q -o "$PKG" -d "$DIR/pkg"; fi
# flatten a single wrapper directory
inner=$(find "$DIR/pkg" -mindepth 1 -maxdepth 1 -type d | head -1)
if [ "$(find "$DIR/pkg" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = "1" ] && [ -n "$inner" ]; then
  # `mv "$inner"/*` misses dotfiles. Move every immediate entry explicitly.
  find "$inner" -mindepth 1 -maxdepth 1 -exec mv {} "$DIR/pkg/" \;
  rmdir "$inner"
fi
command -v pdftotext >/dev/null && pdftotext -layout "$DIR/paper/paper.pdf" "$DIR/paper/paper.txt" 2>/dev/null || true

# One streaming inventory is reused by init, verification, staging and finalization.  The package
# becomes read-only only after hashing; audit-created work belongs under work/.
python3 - "$DIR/pkg" "$DIR/package_manifest.json" <<'PY'
import collections, hashlib, json, os, pathlib, sys, tempfile
root, out = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2])
rows, types = [], collections.Counter()
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    stat = path.stat(); rel = path.relative_to(root).as_posix()
    ext = path.suffix.lower().lstrip(".") or "<none>"; types[ext] += 1
    rows.append({"path": rel, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                 "sha256": h.hexdigest(), "extension": ext})
tree = hashlib.sha256("".join(f"{r['path']}\0{r['sha256']}\n" for r in rows).encode()).hexdigest()
payload = {"schema_version": "2.0.0", "algorithm": "sha256", "package_sha256": tree,
           "file_count": len(rows), "file_types": dict(sorted(types.items())), "files": rows}
fd, tmp = tempfile.mkstemp(prefix=out.name + ".", dir=out.parent)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2, ensure_ascii=False); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.replace(tmp, out)
PY
chmod -R a-w "$DIR/pkg"
echo "=== workspace: $DIR ==="
echo "package entries: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["file_count"])' "$DIR/package_manifest.json")"
echo "paper text:      $( [ -f "$DIR/paper/paper.txt" ] && wc -l < "$DIR/paper/paper.txt" || echo 'EXTRACTION FAILED — read the PDF directly')"
echo ""
echo "--- file types ---"
python3 - "$DIR/package_manifest.json" <<'PY'
import json, sys
d=json.load(open(sys.argv[1]))
for ext, n in sorted(d["file_types"].items(), key=lambda x: (-x[1], x[0]))[:12]:
    print(f"{n:6d} {ext}")
PY
echo ""
echo "--- candidate analysis code ---"
find "$DIR/pkg" -type f \( -name '*.R' -o -name '*.r' -o -name '*.qmd' -o -name '*.Rmd' -o -name '*.py' -o -name '*.do' -o -name '*.sps' -o -name '*.ipynb' \) | head -20
echo ""
echo "NOTE: verify these counts yourself before briefing auditors. A wrong brief propagates."
