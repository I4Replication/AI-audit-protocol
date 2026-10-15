#!/bin/bash
# capture_environment.sh <audit-dir>
# Measures the software actually installed on this machine and journals it: one environment_captured event,
# written by scripts/lifecycle_journal.py record_environment. run.json's `environment` field is the projection of
# the latest such event, so it survives every later append. Captured LIVE, at init (init_run.sh runs this) —
# versions reconstructed later describe a different machine state, which is why the event is written only here.
# Before 2.7.4 this wrote run.json directly and the next journal append re-projected run.json without it. A legacy
# run.json (not metadata 2.1, so no canonical journal) is still written in place, as log_event.sh writes it.
# Exit 0 recorded; 1 not recorded (no run.json, a finalized audit, a refused or failed journal write).
set -uo pipefail
DIR="${1:?audit dir}"
[ -f "$DIR/run.json" ] || { echo "FATAL: $DIR/run.json not found — run init_run.sh first"; exit 1; }
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/i4r-env.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
SOF=$(command -v soffice || echo /Applications/LibreOffice.app/Contents/MacOS/soffice)
# The same binary run_critique.sh runs: CODEX, then I4R_CODEX_BIN, else the newest working CLI that
# scripts/codex_cli.py finds (the ChatGPT app's bundled copies and every codex on PATH).
if [ -n "${CODEX:-}" ] || [ -n "${I4R_CODEX_BIN:-}" ]; then
  CODEX="${CODEX:-$I4R_CODEX_BIN}"
else
  CODEX=$(python3 "$HERE/codex_cli.py" 2>/dev/null) || CODEX=""
fi

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
python3 - "$DIR" "$HERE" <<'PYBLOCK'
import json, sys, datetime, os, socket, tempfile, pathlib
root = pathlib.Path(sys.argv[1]); sys.path.insert(0, sys.argv[2])
p = root / "run.json"; r = json.load(open(p))
g = lambda k: (os.environ.get(k, "").strip() or None)
def kv(k):
    out = {}
    for ln in (os.environ.get(k, "") or "").strip().split("\n"):
        if "=" in ln:
            a, b = ln.split("=", 1); out[a.strip()] = b.strip()
    return out or None

def publication_refuses(text):
    # run.json is filed with every published comment, and both publication gates refuse ANY e-mail or IP address
    # in it: check_release_privacy.py and check 3c (check_deposit_values.py). The journal is append-only, so an
    # operator string they refuse would block the audit's publication for good. Their own patterns decide.
    import check_deposit_values, check_release_privacy
    return any(pattern.search(text) for pattern in (check_release_privacy._EMAIL, check_deposit_values.EMAIL,
                                                    check_deposit_values.IPV4, check_deposit_values.IPV6))

def provider_refuses(text):
    # run.json is also the coordinator's context/run.json, and the coordinator refuses the whole file when any
    # string in it fails its provider-safety scan: a private path, or a participant-code-shaped token (a host
    # named MBP16 gives the account <user>@MBP16). A later capture cannot help, because run.json's events keep
    # every capture's payload. So the coordinator's own scan decides here too, with no deposit exemption.
    import run_coordinator
    try:
        run_coordinator.validate_provider_visible_string(text, "environment")
    except run_coordinator.CoordinatorError:
        return True
    return False

def operator():
    account = os.environ.get("USER", "?") + "@" + socket.gethostname().split(".")[0]
    owner = os.environ.get("I4R_OWNER") or ""
    if owner and not publication_refuses(owner) and not provider_refuses(owner):
        return owner
    if owner and publication_refuses(owner):
        print(f"note: I4R_OWNER is shaped like an e-mail or IP address, which would block publication of this "
              f"audit's run.json; the environment names the account {account} instead", file=sys.stderr)
    elif owner:
        print(f"note: I4R_OWNER holds a private path or a participant-code-shaped token, which the coordinator's "
              f"provider-safety scan refuses; the environment names the account {account} instead", file=sys.stderr)
    if publication_refuses(account) or provider_refuses(account):
        print("note: the account name fails a publication gate or the coordinator's provider-safety scan; the "
              "environment records no operator (set I4R_OWNER to a name both accept)", file=sys.stderr)
        return None
    return account

environment = {
  "captured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "operator": operator(),
  "os": g("I4R_ENV_OS"), "r": g("I4R_ENV_R"), "r_packages": kv("I4R_ENV_RPKG"),
  "python": g("I4R_ENV_PY"), "python_packages": kv("I4R_ENV_PPKG"),
  "pandoc": g("I4R_ENV_PANDOC"), "libreoffice": g("I4R_ENV_LO"), "codex_cli": g("I4R_ENV_CODEX"),
}
# The tool readings meet the same scan: one the coordinator would refuse is recorded as null, with a note.
for key, value in environment.items():
    if key in ("captured_at", "operator") or value is None:
        continue
    strings = [value] if isinstance(value, str) else [text for pair in value.items() for text in pair]
    if any(provider_refuses(text) for text in strings):
        environment[key] = None
        print(f"note: the {key} reading holds a private path or a participant-code-shaped token, which the "
              f"coordinator's provider-safety scan refuses; the environment records no {key}", file=sys.stderr)
recorded = ", ".join(k for k, v in environment.items() if v)
if r.get("schema_version") == "2.1.0":
    # the rule log_event.sh uses: a metadata-2.1 run.json is a projection of run.events.jsonl
    import lifecycle_journal
    try:
        event = lifecycle_journal.record_environment(root, environment)
    except (OSError, lifecycle_journal.JournalError) as exc:
        print(f"FATAL: the environment was not recorded: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"environment journaled ({lifecycle_journal.ENVIRONMENT_EVENT}, journal seq {event['seq']}):", recorded)
    sys.exit(0)
r["environment"] = environment
fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(r, fh, indent=2, ensure_ascii=False); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.replace(tmp, p)
print("environment recorded:", recorded)
PYBLOCK
