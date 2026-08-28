#!/bin/bash
# capture_environment.sh <audit-dir>
# Records the software actually installed at audit time into run.json's `environment` field.
# Captured LIVE, at init — versions reconstructed later describe a different machine state.
set -uo pipefail
DIR="${1:?audit dir}"
[ -f "$DIR/run.json" ] || { echo "FATAL: $DIR/run.json not found — run init_run.sh first"; exit 1; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/i4r-env.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
SOF=$(command -v soffice || echo /Applications/LibreOffice.app/Contents/MacOS/soffice)
CODEX="${I4R_CODEX_BIN:-/Applications/ChatGPT.app/Contents/Resources/codex}"

# These probes are independent and some package managers start slowly. Run them concurrently.
(R --version 2>/dev/null | head -1 || true) > "$TMP/r" &
(python3 --version 2>/dev/null || true) > "$TMP/python" &
(command -v pandoc >/dev/null && pandoc --version | head -1 || true) > "$TMP/pandoc" &
([ -x "$SOF" ] && "$SOF" --version 2>/dev/null | head -1 || true) > "$TMP/libreoffice" &
([ -x "$CODEX" ] && "$CODEX" --version 2>/dev/null | head -1 || true) > "$TMP/codex" &
((sw_vers 2>/dev/null | awk '{print $2}' | paste -sd' ' -) || uname -sr) > "$TMP/os" &
(Rscript --no-init-file -e 'pk <- c("haven","lavaan","psych","rmarkdown","knitr","readxl","jsonlite"); for (p in pk) cat(p, "=", if (requireNamespace(p, quietly=TRUE)) as.character(packageVersion(p)) else "absent", "\n")' 2>/dev/null || true) > "$TMP/r_packages" &
(python3 -c '
import importlib.metadata as m
for p in ("pyreadstat","pandas","scipy","numpy","statsmodels","jsonschema","openpyxl"):
    try: print(p, "=", m.version(p))
    except Exception: print(p, "= absent")' 2>/dev/null || true) > "$TMP/python_packages" &
wait

RV=$(<"$TMP/r"); PYV=$(<"$TMP/python"); PD=$(<"$TMP/pandoc")
LOV=$(<"$TMP/libreoffice"); CXV=$(<"$TMP/codex"); OSV=$(<"$TMP/os")
RPKG=$(<"$TMP/r_packages"); PPKG=$(<"$TMP/python_packages")

# Values cross into Python as ENVIRONMENT VARIABLES, never interpolated into source: R's own
# version banner contains double quotes (-- "[Not] Part in a Rumble") and breaks any quoted
# literal it is pasted into.
export I4R_ENV_OS="$OSV" I4R_ENV_R="$RV" I4R_ENV_RPKG="$RPKG" I4R_ENV_PY="$PYV"
export I4R_ENV_PPKG="$PPKG" I4R_ENV_PANDOC="$PD" I4R_ENV_LO="$LOV" I4R_ENV_CODEX="$CXV"
python3 - "$DIR/run.json" <<'PYBLOCK'
import json, sys, datetime, os, socket, tempfile
p = sys.argv[1]; r = json.load(open(p))
g = lambda k: (os.environ.get(k, "").strip() or None)
def kv(k):
    out = {}
    for ln in (os.environ.get(k, "") or "").strip().split("\n"):
        if "=" in ln:
            a, b = ln.split("=", 1); out[a.strip()] = b.strip()
    return out or None
r["environment"] = {
  "captured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "operator": os.environ.get("I4R_OWNER") or (os.environ.get("USER", "?") + "@" + socket.gethostname().split(".")[0]),
  "os": g("I4R_ENV_OS"), "r": g("I4R_ENV_R"), "r_packages": kv("I4R_ENV_RPKG"),
  "python": g("I4R_ENV_PY"), "python_packages": kv("I4R_ENV_PPKG"),
  "pandoc": g("I4R_ENV_PANDOC"), "libreoffice": g("I4R_ENV_LO"), "codex_cli": g("I4R_ENV_CODEX"),
}
fd, tmp = tempfile.mkstemp(prefix=os.path.basename(p) + ".", dir=os.path.dirname(p) or ".")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(r, fh, indent=2, ensure_ascii=False); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.replace(tmp, p)
print("environment recorded:", ", ".join(k for k, v in r["environment"].items() if v))
PYBLOCK
