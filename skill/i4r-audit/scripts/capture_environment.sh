#!/bin/bash
# capture_environment.sh <audit-dir>
# Records the software actually installed at audit time into run.json's `environment` field.
# Captured LIVE, at init — versions reconstructed later describe a different machine state.
set -uo pipefail
DIR="${1:?audit dir}"
[ -f "$DIR/run.json" ] || { echo "FATAL: $DIR/run.json not found — run init_run.sh first"; exit 1; }

RV=$(R --version 2>/dev/null | head -1 || true)
PYV=$(python3 --version 2>/dev/null || true)
PD=$(command -v pandoc >/dev/null && pandoc --version | head -1 || echo "")
SOF=$(command -v soffice || echo /Applications/LibreOffice.app/Contents/MacOS/soffice)
LOV=$([ -x "$SOF" ] && "$SOF" --version 2>/dev/null | head -1 || echo "")
CODEX=/Applications/ChatGPT.app/Contents/Resources/codex
CXV=$([ -x "$CODEX" ] && "$CODEX" --version 2>/dev/null | head -1 || echo "")
OSV=$( (sw_vers 2>/dev/null | awk '{print $2}' | paste -sd' ' -) || uname -sr)

RPKG=$(Rscript --no-init-file -e 'pk <- c("haven","lavaan","psych","rmarkdown","knitr","readxl","jsonlite"); for (p in pk) cat(p, "=", if (requireNamespace(p, quietly=TRUE)) as.character(packageVersion(p)) else "absent", "\n")' 2>/dev/null || true)
PPKG=$(python3 -c '
import importlib.metadata as m
for p in ("pyreadstat","pandas","scipy","numpy","statsmodels","jsonschema","openpyxl"):
    try: print(p, "=", m.version(p))
    except Exception: print(p, "= absent")' 2>/dev/null || true)

# Values cross into Python as ENVIRONMENT VARIABLES, never interpolated into source: R's own
# version banner contains double quotes (-- "[Not] Part in a Rumble") and breaks any quoted
# literal it is pasted into.
export I4R_ENV_OS="$OSV" I4R_ENV_R="$RV" I4R_ENV_RPKG="$RPKG" I4R_ENV_PY="$PYV"
export I4R_ENV_PPKG="$PPKG" I4R_ENV_PANDOC="$PD" I4R_ENV_LO="$LOV" I4R_ENV_CODEX="$CXV"
python3 - "$DIR/run.json" <<'PYBLOCK'
import json, sys, datetime, os, socket
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
json.dump(r, open(p, "w"), indent=2, ensure_ascii=False)
print("environment recorded:", ", ".join(k for k, v in r["environment"].items() if v))
PYBLOCK
