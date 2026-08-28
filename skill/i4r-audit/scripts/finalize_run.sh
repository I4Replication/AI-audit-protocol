#!/bin/bash
# finalize_run.sh <audit-dir|run.json> [options]      (or: finalize_run.sh --self-test)
#
# Called ONCE, at the close of an audit, after the comment has been delivered. It:
#   1. self-tests its own JSON Schema validator against known-good and known-bad documents,
#      and refuses to run if the validator fails to REJECT the bad ones;
#   2. computes the derived fields of run.json -- durations, rounds_run, token coverage,
#      package-at-close, and the compliance vector;
#   3. computes the derived fields of comment.json -- the rollup counts, days_elapsed and
#      the author-response intervals;
#   4. validates BOTH files against their schemas and exits non-zero if either fails;
#   5. cross-checks the pair (same audit_id, same DOI);
#   6. files the validated pair into the internal registry and appends the index row.
#
# Options:
#   --comment <path>          default <audit-dir>/comment.json
#   --stopping-reason R       target_complete | converged | cap_with_unresolved | aborted.
#                             it is a fact about the run, not something a script can infer, and
#                             "five rounds complete" is not the same claim as "converged".
#   --stopping-detail TEXT
#   --schemas DIR             default: ../schemas beside this script
#   --registry DIR            default: $REGISTRY_DIR, else the I4R programme registry
#   --no-registry             derive and validate only; do not file
#   --pkg-dir DIR             default <audit-dir>/pkg; re-scanned to detect writes into pkg/
#   --self-test
#
# Exit status: 0 all good; 1 a validation or consistency check failed; 2 refused (usage,
# missing file, missing stopping reason). Every step checks its own status explicitly.
set -uo pipefail

SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
DEFAULT_SCHEMAS="$HERE/../schemas"
DEFAULT_REGISTRY="${REGISTRY_DIR:-${I4R_REGISTRY_DIR:-$HERE/../../registry}}"

die () { echo "FATAL: finalize_run.sh: $*" >&2; exit 2; }
now_utc () { date -u +%Y-%m-%dT%H:%M:%SZ; }
FINALIZE_TX=""
cleanup_finalize_tx () {
  if [ -n "${FINALIZE_TX:-}" ] && [ -d "$FINALIZE_TX" ]; then rm -rf "$FINALIZE_TX"; fi
  [ -f "${VALIDATOR:-}" ] && rm -f "$VALIDATOR"
}
trap cleanup_finalize_tx EXIT
trap 'cleanup_finalize_tx; exit 129' HUP
trap 'cleanup_finalize_tx; exit 130' INT
trap 'cleanup_finalize_tx; exit 143' TERM

# =====================================================================================
# The validator. Draft 2020-12, the subset these two schemas use: type, required,
# properties, additionalProperties, items, $ref into $defs, enum, const, pattern,
# minimum/maximum, minLength, minItems, and a light format check for date/date-time.
#
# It ships with its own controls (--controls), which finalize runs BEFORE touching any
# real file. A validator that accepts everything passes every document; the only way to
# know it is working is to hand it documents it must reject.
# =====================================================================================
# Per-process path: concurrent audits must never overwrite or delete each other's validator.
VALIDATOR="${TMPDIR:-/tmp}/i4r-validate-json.$$.${RANDOM:-0}.py"

write_validator () {
  cp "$HERE/validate_json_schema.py" "$VALIDATOR"
  return $?
  cat > "$VALIDATOR" <<'PYV'
import json, re, sys, datetime

DTRE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

def tname(v):
    if v is None: return "null"
    if isinstance(v, bool): return "boolean"
    if isinstance(v, int): return "integer"
    if isinstance(v, float): return "number"
    if isinstance(v, str): return "string"
    if isinstance(v, list): return "array"
    if isinstance(v, dict): return "object"
    return "unknown"

def type_ok(v, t):
    if t == "integer": return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":  return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean": return isinstance(v, bool)
    if t == "string":  return isinstance(v, str)
    if t == "object":  return isinstance(v, dict)
    if t == "array":   return isinstance(v, list)
    if t == "null":    return v is None
    return True

def resolve(ref, root):
    if not ref.startswith("#/"):
        raise ValueError("only local refs are supported: " + ref)
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node

def validate(inst, schema, root, path="", errs=None):
    if errs is None: errs = []
    if not isinstance(schema, dict): return errs
    if "$ref" in schema:
        validate(inst, resolve(schema["$ref"], root), root, path, errs)
        schema = {k: v for k, v in schema.items() if k != "$ref"}
    for sub in schema.get("allOf", []):
        validate(inst, sub, root, path, errs)
    if "oneOf" in schema:
        matches = [sub for sub in schema["oneOf"] if not validate(inst, sub, root, path, [])]
        if len(matches) != 1:
            errs.append("%s: expected exactly one oneOf branch, matched %d" % (path or "/", len(matches)))
    if "anyOf" in schema and not any(not validate(inst, sub, root, path, []) for sub in schema["anyOf"]):
        errs.append("%s: did not match any anyOf branch" % (path or "/"))
    if "not" in schema and not validate(inst, schema["not"], root, path, []):
        errs.append("%s: matched forbidden 'not' schema" % (path or "/"))
    if "if" in schema:
        branch = schema.get("then") if not validate(inst, schema["if"], root, path, []) else schema.get("else")
        if branch is not None: validate(inst, branch, root, path, errs)
    if "type" in schema:
        ts = schema["type"]
        ts = ts if isinstance(ts, list) else [ts]
        if not any(type_ok(inst, t) for t in ts):
            errs.append("%s: expected type %s, got %s" % (path or "/", "|".join(ts), tname(inst)))
            return errs                      # further checks would be noise
    if "enum" in schema and inst not in schema["enum"]:
        errs.append("%s: %r is not one of %r" % (path or "/", inst, schema["enum"]))
    if "const" in schema and inst != schema["const"]:
        errs.append("%s: %r != const %r" % (path or "/", inst, schema["const"]))
    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            errs.append("%s: %r does not match /%s/" % (path or "/", inst[:60], schema["pattern"]))
        if "minLength" in schema and len(inst) < schema["minLength"]:
            errs.append("%s: shorter than minLength %d" % (path or "/", schema["minLength"]))
        f = schema.get("format")
        if f == "date-time" and not DTRE.match(inst):
            errs.append("%s: %r is not an RFC 3339 date-time" % (path or "/", inst))
        if f == "date":
            try: datetime.date.fromisoformat(inst)
            except Exception: errs.append("%s: %r is not an ISO date" % (path or "/", inst))
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errs.append("%s: %r < minimum %r" % (path or "/", inst, schema["minimum"]))
        if "maximum" in schema and inst > schema["maximum"]:
            errs.append("%s: %r > maximum %r" % (path or "/", inst, schema["maximum"]))
    if isinstance(inst, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in inst:
                errs.append("%s: missing required property '%s'" % (path or "/", r))
        ap = schema.get("additionalProperties", True)
        if "minProperties" in schema and len(inst) < schema["minProperties"]:
            errs.append("%s: fewer than minProperties %d" % (path or "/", schema["minProperties"]))
        for k, v in inst.items():
            if k in props:
                validate(v, props[k], root, path + "/" + k, errs)
            elif ap is False:
                errs.append("%s: unexpected property '%s'" % (path or "/", k))
            elif isinstance(ap, dict):
                validate(v, ap, root, path + "/" + k, errs)
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errs.append("%s: fewer than minItems %d" % (path or "/", schema["minItems"]))
        if "items" in schema:
            for i, v in enumerate(inst):
                validate(v, schema["items"], root, "%s/%d" % (path, i), errs)
        if schema.get("uniqueItems"):
            enc = [json.dumps(v, sort_keys=True, ensure_ascii=False) for v in inst]
            if len(enc) != len(set(enc)):
                errs.append("%s: array items are not unique" % (path or "/"))
    return errs

def check(instpath, schemapath):
    with open(schemapath) as f: schema = json.load(f)
    with open(instpath) as f: inst = json.load(f)
    return validate(inst, schema, schema)

# ---------------------------------------------------------------- controls
CONTROL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["a", "b", "c"],
    "properties": {
        "a": {"type": "string", "pattern": "^x"},
        "b": {"type": ["integer", "null"], "minimum": 0},
        "c": {"type": "string", "enum": ["one", "two"]},
        "d": {"type": "array", "items": {"$ref": "#/$defs/leaf"}},
        "e": {"type": "string", "format": "date-time"},
    },
    "$defs": {"leaf": {"type": "object", "additionalProperties": False,
                       "required": ["k"], "properties": {"k": {"type": "boolean"}}}},
}
GOOD = {"a": "xyz", "b": 3, "c": "one", "d": [{"k": True}], "e": "2026-07-14T09:00:00Z"}
BAD = [
    ("missing required",     {"a": "xyz", "b": 3}),
    ("wrong type",           {"a": "xyz", "b": "three", "c": "one"}),
    ("unknown property",     {"a": "xyz", "b": 3, "c": "one", "z": 1}),
    ("bad enum",             {"a": "xyz", "b": 3, "c": "three"}),
    ("bad pattern",          {"a": "abc", "b": 3, "c": "one"}),
    ("below minimum",        {"a": "xyz", "b": -1, "c": "one"}),
    ("null where not allowed", {"a": None, "b": 3, "c": "one"}),
    ("bad nested via $ref",  {"a": "xyz", "b": 3, "c": "one", "d": [{"k": "yes"}]}),
    ("extra nested prop",    {"a": "xyz", "b": 3, "c": "one", "d": [{"k": True, "j": 1}]}),
    ("bad date-time",        {"a": "xyz", "b": 3, "c": "one", "e": "14 July 2026"}),
    ("boolean as integer",   {"a": "xyz", "b": True, "c": "one"}),
]

def controls():
    bad_controls_passed = []
    e = validate(GOOD, CONTROL_SCHEMA, CONTROL_SCHEMA)
    if e:
        print("  FAIL: the validator rejects a known-GOOD document: %s" % e[:3])
        return 1
    for label, doc in BAD:
        if not validate(doc, CONTROL_SCHEMA, CONTROL_SCHEMA):
            bad_controls_passed.append(label)
    if bad_controls_passed:
        print("  FAIL: the validator ACCEPTED these known-bad documents: %s" % bad_controls_passed)
        return 1
    print("  ok: validator self-test (1 good accepted, %d bad rejected, including "
          "booleans-as-integers and violations nested behind a $ref)" % len(BAD))
    return 0

if __name__ == "__main__":
    if sys.argv[1] == "--controls":
        sys.exit(controls())
    errors = check(sys.argv[1], sys.argv[2])
    if errors:
        print("  FAIL: %s does not validate against %s (%d error%s):"
              % (sys.argv[1], sys.argv[2], len(errors), "" if len(errors) == 1 else "s"))
        for e in errors[:25]:
            print("     " + e)
        if len(errors) > 25:
            print("     ... and %d more" % (len(errors) - 25))
        sys.exit(1)
    print("  ok: %s validates against %s" % (sys.argv[1].split("/")[-1], sys.argv[2].split("/")[-1]))
    if sys.argv[1].endswith("run.json") and not json.load(open(sys.argv[1])).get("environment"):
        print("  WARN: run.json carries no environment record — capture_environment.sh was never run;")
        print("        software versions for this audit are now unrecoverable (pre-2026-08-24 audits only)")
    sys.exit(0)
PYV
  return $?
}

# =====================================================================================
# Derivation
# =====================================================================================
derive_run () {
  # $1 run.json  $2 pkg dir  $3 stopping reason  $4 detail  $5 audit root  $6 policy
  python3 - "$1" "$2" "$3" "$4" "$5" "$6" <<'PYD'
import collections, hashlib, json, pathlib, sys, datetime

run_p, pkg_p, stop, detail, audit_root, policy_p = sys.argv[1:7]
R = json.loads(pathlib.Path(run_p).read_text())
audit_root = pathlib.Path(audit_root).resolve()
live_policy = json.loads(pathlib.Path(policy_p).read_text())
policy, policy_snapshot_error = live_policy, None
snapshot_path = audit_root / "pipeline-policy.snapshot.json"
if snapshot_path.is_file():
    try:
        envelope=json.loads(snapshot_path.read_text()); candidate=envelope["policy"]
        canonical=json.dumps(candidate,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
        if envelope.get("policy_sha256") != hashlib.sha256(canonical).hexdigest():
            raise ValueError("hash mismatch")
        policy=candidate
    except Exception as exc: policy_snapshot_error="invalid audit-scoped policy snapshot: %s" % exc
elif R.get("schema_version") == "2.0.0":
    policy_snapshot_error="audit-scoped pipeline policy snapshot is missing"

def ts(x):
    if not x: return None
    try: return datetime.datetime.fromisoformat(x.replace("Z", "+00:00"))
    except Exception: return None

def dur(a, b):
    A, B = ts(a), ts(b)
    if A is None or B is None: return None
    return int((B - A).total_seconds())

warn = []

# ---- close the clock
if stop: R["stopping_reason"] = stop
if detail: R["stopping_reason_detail"] = detail
if not R.get("ended_at"):
    R["ended_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
R["wall_clock_seconds"] = dur(R.get("started_at"), R.get("ended_at"))

# ---- phases
for name, ph in R.get("phases", {}).items():
    ph["wall_clock_seconds"] = dur(ph.get("started_at"), ph.get("ended_at"))
    ph["ran"] = bool(ph.get("started_at")) and bool(ph.get("ended_at"))
R["active_seconds"] = sum(p["wall_clock_seconds"] or 0 for p in R.get("phases", {}).values()) or None

# ---- rounds
rounds = R.get("rounds", [])
for r in rounds:
    r["wall_clock_seconds"] = dur(r.get("started_at"), r.get("ended_at"))
    if (r.get("blocking_items") is not None and r.get("comment_defects") is not None
            and r.get("paper_defects") is not None):
        r["unclassified_defects"] = (r["blocking_items"] - r["comment_defects"] - r["paper_defects"])
        if r["unclassified_defects"] < 0:
            warn.append("round %s: comment_defects + paper_defects exceed blocking_items"
                        % r.get("round"))
R["rounds_run"] = len(rounds)

# ---- canonical invocations and token coverage.  Cache-only records remain in run.json;
# release accounting counts provider calls only.
ledger_path = audit_root / "invocations.jsonl"
all_invocations, invocations, ledger_errors = [], [], []
if ledger_path.is_file():
    for n, line in enumerate(ledger_path.read_text(errors="replace").splitlines(), 1):
        if not line.strip(): continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict): raise ValueError("row is not an object")
        except Exception:
            ledger_errors.append("invocations.jsonl line %d is invalid" % n); continue
        all_invocations.append(row)
        if row.get("provider_call") is True: invocations.append(row)
R["invocations"] = all_invocations
ids = [x.get("invocation_id") for x in all_invocations]
if any(not x for x in ids) or len(ids) != len(set(ids)):
    ledger_errors.append("invocation ledger has missing or duplicate invocation IDs")
    expected = len(invocations); with_tok = 0; tot_tok = 0
elif invocations:
    expected = len(invocations)
    def usage(row):
        tok = row.get("usage") or {}
        if not isinstance(tok, dict): return None
        fields=("input_tokens","cached_input_tokens","output_tokens","reasoning_tokens","total_tokens")
        if tok.get("obtainable") is not True or any(not isinstance(tok.get(k), int) for k in fields):
            return None
        return int(tok["total_tokens"])
    observed = [usage(x) for x in invocations]
    with_tok = sum(x is not None for x in observed); tot_tok = sum(x or 0 for x in observed)
else:
    # V1-only fallback: exact role/round joins, never raw row counts. V2 compliance below
    # treats a missing canonical ledger as a violation.
    assignments = R.get("model_assignments", [])
    def key(x): return (x.get("role_class"), x.get("role"), x.get("round"), x.get("attempt", 1))
    akeys = [key(x) for x in assignments]
    token_by_key = {}
    for t in R.get("tokens", []):
        k = key(t)
        if k in token_by_key: warn.append("duplicate legacy token row for %r" % (k,))
        token_by_key[k] = t.get("total_tokens")
    if R.get("schema_version") == "2.0.0":
        expected = 0; with_tok = 0; tot_tok = 0
    else:
        expected = len(set(akeys)); matched = [token_by_key.get(k) for k in set(akeys)]
        with_tok = sum(x is not None for x in matched); tot_tok = sum(x or 0 for x in matched)
tot_usd = sum(c["amount"] for c in R.get("costs", []) if c.get("amount") is not None)
R["token_accounting"] = {
    "invocations_expected": expected or None,
    "invocations_with_tokens": with_tok,
    "role_rounds_expected": expected or None,
    "role_rounds_with_tokens": with_tok,
    "coverage_fraction": round(with_tok / expected, 4) if expected else None,
    "total_tokens_observed": tot_tok or None,
    "total_cost_usd_observed": round(tot_usd, 4) if tot_usd else None,
}

# ---- brief corrections
bc = R.setdefault("brief_corrections", {"count": 0, "items": []})
bc["count"] = len(bc.get("items", []))

# ---- human effort: self-reported, and reported WITH the number of phases it covers, because
# a total over two of five phases is not a total.
he = R.get("human_effort")
if he:
    pm = he.get("person_minutes_by_phase") or {}
    vals = [v for v in pm.values() if v is not None]
    he["person_minutes_total"] = sum(vals) if vals else None
    he["phases_recorded"] = len(vals)
    if vals and len(vals) < 5:
        warn.append("human_effort covers %d of 5 phases; person_minutes_total (%d) is a "
                    "partial figure and must not be reported as the audit's total effort"
                    % (len(vals), sum(vals)))
    if not vals:
        warn.append("no human effort recorded for any phase: the programme's AI-assisted vs "
                    "human-only comparison has no numerator for this audit")

# ---- the settled-facts lever
sf = R.get("settled_facts") or {}
if sf.get("used") is None:
    warn.append("settled_facts.used is null: the protocol's largest claimed cost lever "
                "(~40-50% of critic time) cannot be evaluated for this audit")
elif sf.get("used") and sf.get("generated_after_round") is None:
    warn.append("SETTLED.md was used but generated_after_round is null; without it the "
                "before/after saving cannot be estimated")
if sf.get("spot_check_failures"):
    warn.append("%s spot-check(s) of a SETTLED.md fact FAILED -- something asserted as settled "
                "was wrong, which is a finding about the audit, not a statistic"
                % sf["spot_check_failures"])

# ---- the package as it stands at close: did anything write into pkg/?
pkg = R.get("package") or {}
p = pathlib.Path(pkg_p)
if p.is_dir():
    lines, n, reused = [], 0, False
    manifest_path = audit_root / "package_manifest.json"
    if manifest_path.is_file():
        try:
            intake = json.loads(manifest_path.read_text())
            cached = intake.get("files") or []
            current = {f.relative_to(p).as_posix(): f for f in p.rglob("*") if f.is_file() and not f.is_symlink()}
            if len(current) == len(cached) and all(
                item.get("path") in current and current[item["path"]].stat().st_size == item.get("size") and
                current[item["path"]].stat().st_mtime_ns == item.get("mtime_ns") for item in cached):
                lines = [item["sha256"] + "  " + item["path"] for item in sorted(cached, key=lambda x:x["path"])]
                n, reused = len(lines), True
        except Exception:
            reused = False
    if not reused:
        for f in sorted(p.rglob("*")):
            if f.is_file() and not f.is_symlink():
                try:
                    h = hashlib.sha256()
                    with f.open("rb") as fh:
                        for block in iter(lambda: fh.read(1024 * 1024), b""): h.update(block)
                except OSError: continue
                n += 1
                lines.append(h.hexdigest() + "  " + f.relative_to(p).as_posix())
    man = hashlib.sha256("\n".join(lines).encode()).hexdigest() if lines else None
    pkg["file_count_at_close"] = n
    pkg["manifest_sha256_at_close"] = man
    if pkg.get("manifest_sha256") is not None:
        pkg["pkg_unchanged"] = (man == pkg["manifest_sha256"])
        if not pkg["pkg_unchanged"]:
            warn.append("pkg/ CHANGED during the audit (%s files at intake, %s at close) -- "
                        "suspect your own scripts before the authors"
                        % (pkg.get("file_count"), n))
R["package"] = pkg or None

# ---- records retained: only auto-filled when nothing was logged, so a deliberate
# retention record is never overwritten by a guess.
if not R.get("records_retained"):
    base = pathlib.Path(R.get("audit_dir") or pathlib.Path(run_p).parent)
    STD = [("report_final", "report/REPORT_final.md"), ("ledger", "report/LEDGER.md"),
           ("adjudication_log", "report/ADJUDICATION.md"), ("auditor_reports", "report"),
           ("critique_rounds", "critique"), ("work_scripts", "work"),
           ("run_json", "run.json"), ("event_journal", "run.events.jsonl"),
           ("package_manifest", "work/pkg_manifest.txt")]
    out = []
    for name, rel in STD:
        q = base / rel
        if q.is_dir():
            fs = [x for x in q.rglob("*") if x.is_file()]
            out.append({"artifact": name, "path": rel, "present": len(fs) > 0,
                        "count": len(fs), "bytes": sum(x.stat().st_size for x in fs),
                        "sha256": None,
                        "retention": "retained" if fs else "never_created", "reason": None})
        elif q.is_file():
            b = q.read_bytes()
            out.append({"artifact": name, "path": rel, "present": True, "count": 1,
                        "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(),
                        "retention": "retained", "reason": None})
        else:
            out.append({"artifact": name, "path": rel, "present": False, "count": 0,
                        "bytes": None, "sha256": None, "retention": "never_created",
                        "reason": "not present at close; recorded so that 'destroyed' and "
                                  "'never existed' stay distinguishable"})
    R["records_retained"] = out
R["records_retained_checked_at"] = R["ended_at"]

# ---- the compliance vector
ma = R.get("model_assignments", [])
delivered = (R.get("delivery") or {}).get("delivered_at")
viol = list(ledger_errors)
if policy_snapshot_error: viol.append(policy_snapshot_error)
if R.get("schema_version") == "2.0.0" and not R.get("normalization"):
    expected_target=(policy.get("critique") or {}).get("round_target")
    expected_cap=(policy.get("critique") or {}).get("round_cap")
    if R.get("round_target") != expected_target or R.get("round_cap") != expected_cap:
        viol.append("run round target/cap %s/%s differs from immutable policy %s/%s" %
                    (R.get("round_target"),R.get("round_cap"),expected_target,expected_cap))
    if sf.get("used") is not True or sf.get("generated_after_round") != 1:
        viol.append("settled facts were not generated immediately after and injected from round 1")
    if sf.get("path") != "SETTLED.md":
        viol.append("settled-facts event does not bind canonical SETTLED.md")
    settled_json=audit_root/"SETTLED.json"; settled_md=audit_root/"SETTLED.md"
    if not settled_json.is_file() or not settled_md.is_file():
        viol.append("canonical SETTLED.json/SETTLED.md outputs are missing")
    else:
        try:
            settled_obj=json.loads(settled_json.read_text())
            registry_path=audit_root/"work/findings.jsonl"; report_path=audit_root/"report/REPORT_final.md"
            def file_sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
            facts=settled_obj.get("facts")
            if (settled_obj.get("schema_version") != "2.0.0" or
                    settled_obj.get("invalidated") is not False or not isinstance(facts,list) or
                    settled_obj.get("source_registry_sha256") != file_sha(registry_path) or
                    settled_obj.get("report_sha256") != file_sha(report_path)):
                viol.append("settled cache is not bound to the current canonical registry/report")
            if sf.get("entries") != len(facts):
                viol.append("settled-facts event entry count differs from SETTLED.json")
        except Exception as exc:
            viol.append("settled cache could not be validated: %s" % exc)
    if sf.get("spot_check_failures") not in (0, None):
        viol.append("settled cache has failed spot checks")

phases_ran = {k: bool(v.get("ran")) for k, v in R.get("phases", {}).items()}
for k, v in sorted(phases_ran.items()):
    if not v: viol.append("phase '%s' did not run to completion" % k)

not_integrated, integrated_ok = [], 0
for r in rounds:
    ia = r.get("integrated_at")
    ok = bool(ia) and bool(delivered) and ts(ia) < ts(delivered)
    r["integrated_before_delivery"] = ok if (ia or delivered) else None
    if ok: integrated_ok += 1
    else: not_integrated.append(r.get("round"))
cibd = bool(rounds) and bool(delivered) and not not_integrated
if not delivered:
    viol.append("no delivery timestamp: 'the critique was integrated before delivery' is unfalsifiable")
elif not rounds:
    viol.append("the comment was delivered with no critique round on record")
for n in not_integrated:
    viol.append("round %s: critique not integrated before delivery" % n)

if R.get("schema_version") == "2.0.0":
    # V2 compliance is evidence-backed.  Optional legacy assignments cannot manufacture a
    # quorum: use completed, content-bound canonical invocation records only.
    completed = [x for x in invocations if x.get("status") == "completed"]
    by_role = collections.defaultdict(list)
    for item in completed: by_role[item.get("role")].append(item)
    required_auditors = set(policy["phase_1"]["required_roles"])
    observed_auditors = {x.get("role") for x in completed if x.get("role_class") == "auditor"}
    all_five = observed_auditors == required_auditors
    if not all_five:
        viol.append("canonical auditor quorum differs: missing=%s extra=%s" %
                    (sorted(required_auditors-observed_auditors), sorted(observed_auditors-required_auditors)))
    for role in sorted(required_auditors):
        if len(by_role[role]) != 1:
            viol.append("auditor role %s has %d completed canonical invocations, expected exactly 1" %
                        (role, len(by_role[role])))
    if len(by_role["coordinator"]) != 1:
        viol.append("coordinator has %d completed canonical invocations, expected exactly 1" %
                    len(by_role["coordinator"]))
    probe_records=[x for x in all_invocations if x.get("role") == "model_probe"
                   and x.get("status") == "completed"]
    provider_probes=[x for x in probe_records if x.get("provider_call") is True]
    if not probe_records:
        viol.append("no successful audit-scoped model-capability probe/cache record exists")
    if len(provider_probes) > 1:
        viol.append("model capability was probed %d times; policy permits one provider probe per audit/model" %
                    len(provider_probes))

    def configured(item):
        role=item.get("role")
        if role == "coordinator": return policy["models"]["coordinator"]
        if role in policy["models"]["phase_1_auditors"]:
            return policy["models"]["phase_1_auditors"][role]
        if item.get("role_class") == "adjudicator" or role in {"targeted_adjudicator","critic_synth"}:
            return policy["models"]["targeted_adjudicator"]
        if item.get("role_class") in {"critic","synthesizer"} or role == "model_probe":
            return policy["models"]["critics"]
        return None

    policy_errors=[]
    for item in invocations:
        cfg=configured(item)
        if cfg:
            for field,key in (("requested_model","requested_model"),("provider","provider"),
                              ("reasoning_effort","reasoning_effort")):
                if item.get(field) != cfg[key]:
                    policy_errors.append("%s %s=%r expected %r" %
                                         (item.get("invocation_id"),field,item.get(field),cfg[key]))
        if not item.get("observed_model") or item.get("model_source") != "observed":
            policy_errors.append("%s lacks an observed served-model identity" % item.get("invocation_id"))
        artifact=item.get("output_artifact")
        expected_hash=item.get("output_sha256")
        if item.get("status") == "completed":
            path=(audit_root/str(artifact or "")).resolve()
            try: path.relative_to(audit_root)
            except ValueError:
                policy_errors.append("%s output path escapes audit root" % item.get("invocation_id")); continue
            if not path.is_file():
                policy_errors.append("%s completed output is missing" % item.get("invocation_id"))
            elif expected_hash != hashlib.sha256(path.read_bytes()).hexdigest():
                policy_errors.append("%s completed output hash changed" % item.get("invocation_id"))
    viol.extend("invocation policy: "+error for error in policy_errors)

    round_numbers=[r.get("round") for r in rounds]
    if round_numbers != list(range(1,len(rounds)+1)):
        viol.append("critique rounds are not unique and contiguous from 1")
    specialist_roles={"critic_stats","critic_data","critic_fairness"}
    expected_round_one=set(policy["critique"]["round_1"]["roles"])
    for round_row in rounds:
        number=round_row.get("round")
        actual={x.get("role") for x in completed
                if x.get("role_class") == "critic" and x.get("round") == number}
        declared=set(round_row.get("critics_run") or [])
        expected = expected_round_one if number == 1 else declared
        if number == 1 and actual != expected_round_one:
            viol.append("round 1 critic quorum differs: missing=%s extra=%s" %
                        (sorted(expected_round_one-actual),sorted(actual-expected_round_one)))
        if number and number > 1:
            if "critic_completeness" not in actual or not (actual & specialist_roles):
                viol.append("round %s lacks completeness plus at least one routed specialist" % number)
            if not declared:
                viol.append("round %s does not declare critics_run" % number)
        if declared != actual:
            viol.append("round %s declared/completed critic roles differ: declared=%s completed=%s" %
                        (number,sorted(declared),sorted(actual)))
        for role in actual:
            matches=[x for x in completed if x.get("role")==role and x.get("round")==number]
            if len(matches) != 1:
                viol.append("round %s role %s has %d completed invocations, expected exactly 1" %
                            (number,role,len(matches)))

    # A completed critic record must itself be the canonical, schema-complete result—not a
    # Markdown skeleton or an unrelated output file whose hash happens to match telemetry.
    skill_root=pathlib.Path(policy_p).resolve().parent.parent
    sys.path.insert(0,str(skill_root/"schemas/tools"))
    try: import validate_v2
    except Exception as exc:
        viol.append("critic semantic validator could not load: %s" % exc)
    else:
        for item in completed:
            if item.get("role_class") != "critic": continue
            path=(audit_root/str(item.get("output_artifact") or "")).resolve()
            try:
                result=json.loads(path.read_text())
                validate_v2.validate_critic_result(result)
                if (result.get("audit_id") != R.get("audit_id") or result.get("round") != item.get("round")
                        or result.get("role") != item.get("role") or result.get("status") != "complete"):
                    raise ValueError("audit/round/role/status does not match invocation")
            except Exception as exc:
                viol.append("%s critic result is not a matching complete v2 record: %s" %
                            (item.get("invocation_id"),exc))

    auditor_families={x.get("model_family") for x in completed
                      if x.get("role_class") in {"coordinator","auditor"} and x.get("model_family")}
    critic_calls=[x for x in completed if x.get("role_class") in {"critic","adjudicator","synthesizer"}]
    crit_indep = bool(critic_calls) and all(x.get("model_family") not in auditor_families for x in critic_calls)
    if not crit_indep:
        viol.append("completed critic/adjudicator model family is absent or overlaps the auditor family")
    no_swap = not policy_errors
else:
    critics = [m for m in ma if m.get("role_class") in ("critic", "synthesizer")]
    auditors = {m["role"] for m in ma if m.get("role_class") == "auditor"}
    all_five = (len(auditors) >= 5) if ma else None
    if all_five is False:
        viol.append("fan-out ran %d auditor role(s), not 5" % len(auditors))
    crit_indep = None
    if critics:
        crit_indep = all(m.get("differs_from_auditor_family") for m in critics)
        for m in critics:
            if not m.get("differs_from_auditor_family"):
                viol.append("round %s: %s ran on the auditor family (%s) -- that review was not adversarial" %
                            (m.get("round"),m.get("role"),m.get("model_family")))
    no_swap = None
    if ma:
        swaps=[m for m in ma if m.get("replaces_model_id") and m.get("intended") is False]
        no_swap=not swaps
        for m in swaps:
            viol.append("role %s changed model from %s to %s mid-run without an intended change" %
                        (m.get("role"),m.get("replaces_model_id"),m.get("model_id")))

cc_every = None
if rounds:
    cc_every = all(r.get("consistency_check_run") for r in rounds)
    if not cc_every:
        viol.append("check_consistency.sh was not recorded for every round")

gates = (R.get("compliance") or {}).get("gates_run", [])
for g in gates:
    if g.get("exit_status") not in (0, None):
        later = [h for h in gates if h.get("gate") == g.get("gate")
                 and h.get("round") == g.get("round") and h.get("exit_status") == 0
                 and (h.get("at") or "") > (g.get("at") or "")]
        if not later:
            viol.append("gate '%s' (round %s) exited %s and never passed afterwards"
                        % (g.get("gate"), g.get("round"), g.get("exit_status")))

converged = R.get("stopping_reason") == "converged"
if R.get("schema_version") == "2.0.0":
    stop = R.get("stopping_reason")
    target = R.get("round_target") or 2
    cap = R.get("round_cap") or 5
    changed = set(R.get("changed_claim_ids") or [])
    rereviewed = set(R.get("rereviewed_claim_ids") or [])
    missing_rereview = sorted(changed - rereviewed)
    if stop in ("target_complete", "converged") and R.get("rounds_run", 0) < target:
        viol.append("%s recorded before the required round target (%s < %s)" %
                    (stop, R.get("rounds_run", 0), target))
    if stop == "converged":
        if R.get("rounds_run", 0) <= target:
            viol.append("converged is reserved for a completed round beyond the target")
        if not rounds or rounds[-1].get("factual_corrections") != 0:
            viol.append("converged lacks a final beyond-target round with factual_corrections=0")
    if stop in ("target_complete", "converged") and missing_rereview:
        viol.append("%s recorded before changed claim(s) were re-reviewed: %s" %
                    (stop, ", ".join(missing_rereview)))
    if stop in ("target_complete", "converged"):
        if R.get("unresolved_blocking_item_ids"):
            viol.append("%s recorded with unresolved blocking items: %s" %
                        (stop, ", ".join(R["unresolved_blocking_item_ids"])))
        if R.get("unresolved_conclusion_critical_item_ids"):
            viol.append("%s recorded with unresolved conclusion-critical items: %s" %
                        (stop, ", ".join(R["unresolved_conclusion_critical_item_ids"])))
    if stop in ("target_complete", "converged") and R.get("unreviewed_sections"):
        viol.append("%s recorded with unreviewed sections: %s" %
                    (stop, ", ".join(R["unreviewed_sections"])))
    if stop == "cap_with_unresolved":
        if R.get("rounds_run", 0) < cap:
            viol.append("cap_with_unresolved recorded before the hard round cap (%s < %s)" %
                        (R.get("rounds_run", 0), cap))
        if not (R.get("unreviewed_sections") or R.get("unresolved_blocking_item_ids") or
                R.get("unresolved_conclusion_critical_item_ids")):
            viol.append("cap_with_unresolved recorded without identifying any unresolved scope")

flags = [v for v in [all_five, cibd, crit_indep, no_swap, cc_every] if v is not None]
compliance_pass = all(flags) and all(phases_ran.values()) and not viol

R["compliance"] = {
    "phases_ran": phases_ran,
    "all_five_auditors_ran": all_five,
    "critique_integrated_before_delivery": cibd,
    "rounds_integrated_before_delivery": integrated_ok,
    "rounds_not_integrated": [n for n in not_integrated if n is not None],
    "converged": converged,
    "critics_independent_all_rounds": crit_indep,
    "no_silent_model_swap": no_swap,
    "gates_run": gates,
    "consistency_check_after_every_round": cc_every,
    "compliance_pass": bool(compliance_pass),
    "violations": viol,
}
R["status"] = "aborted" if R.get("stopping_reason") == "aborted" else "complete"

pathlib.Path(run_p + ".tmp").write_text(json.dumps(R, indent=2, ensure_ascii=False) + "\n")
print(json.dumps({"warnings": warn,
                  "compliance_pass": R["compliance"]["compliance_pass"],
                  "violations": viol,
                  "stopping_reason": R.get("stopping_reason"),
                  "rounds_run": R["rounds_run"],
                  "coverage": R["token_accounting"]["coverage_fraction"]}))
PYD
  return $?
}

derive_comment () {
  # $1 comment.json  $2 run.json
  python3 - "$1" "$2" <<'PYC'
import hashlib, json, pathlib, re, sys, datetime

cp, rp = sys.argv[1], sys.argv[2]
C = json.loads(pathlib.Path(cp).read_text())
R = json.loads(pathlib.Path(rp).read_text())
warn = []

def d(x):
    if not x: return None
    try: return datetime.date.fromisoformat(x)
    except Exception: return None

errors = []
f = C.get("findings", []) or []
roll = C.setdefault("rollup", {})

# ---- SCHEME.md v1.0 Field 1: the impact ladder, ranked
RANK = {"none": 0, "unverifiable": 1, "value_wrong": 2, "conclusion_narrowed": 3,
        "conclusion_unsupported": 4, "conclusion_contradicted": 5,
        "artefact_of_construction": 6}

for x in f:
    x["impact_rank"] = RANK.get(x.get("impact"))
    # Rule 2c: "unverifiable with no cause recorded is not a finding". Enforced, not warned.
    if x.get("impact") == "unverifiable":
        if not x.get("unverifiable_reason"):
            errors.append("finding %s is 'unverifiable' with no unverifiable_reason "
                          "(Rule 2c: that is not a finding)" % x.get("id"))
        if x.get("blocked_tier") is None:
            errors.append("finding %s is 'unverifiable' with no blocked_tier -- without it the "
                          "work schedule cannot tell what capacity a completed package would "
                          "release (Rule 2b)" % x.get("id"))
    else:
        for k in ("unverifiable_reason", "blocked_tier"):
            if x.get(k) is not None:
                errors.append("finding %s has %s but impact is '%s', not 'unverifiable'"
                              % (x.get("id"), k, x.get("impact")))
    # verifier count and outcome roll-up
    v = x.setdefault("verification", {"verifiers": []})
    vs = v.get("verifiers") or []
    v["verifiers"] = vs
    v["verifier_count"] = len(vs)
    v["all_reproduced"] = (bool(vs) and all(q.get("outcome") == "reproduced" for q in vs))
    # independent corroboration, kept strictly apart from agreement-after-exposure
    pr = x.setdefault("provenance", {})
    ind = pr.get("independently_found_by")
    if ind is None:
        ind = [pr["origin_role"]] if pr.get("origin_role") else []
        pr["independently_found_by"] = ind
    pr["independent_corroboration_count"] = len(set(ind))
    overlap = set(ind) & set(pr.get("agreed_after_seeing") or [])
    if overlap:
        errors.append("finding %s lists %s as BOTH an independent finder and an after-the-fact "
                      "agreer; a role cannot be both, and counting it as the first would "
                      "manufacture corroboration" % (x.get("id"), sorted(overlap)))

roll["findings_total"] = len(f)
cbr = {"one_line_code": 0, "one_number": 0, "reanalysis": 0, "not_fixable": 0}
for x in f:
    k = x.get("remedy_class")
    if k in cbr: cbr[k] += 1
roll["count_by_remedy_class"] = cbr
imp = {}
for x in f:
    if x.get("impact") is not None: imp[x["impact"]] = imp.get(x["impact"], 0) + 1
roll["count_by_impact"] = imp or None

# ---- Rule 1: the comment takes the maximum impact, and reports the tier OF THAT FINDING
ranked = [x for x in f if x.get("impact_rank") is not None]
if ranked:
    top = max(ranked, key=lambda x: x["impact_rank"])
    roll["impact_max"] = top["impact"]
    roll["impact_max_rank"] = top["impact_rank"]
    roll["impact_max_finding"] = top.get("id")
    # NOT the worst tier in the comment -- the tier of the finding that set the maximum.
    roll["tier_of_max"] = top.get("verification_tier")
else:
    roll["impact_max"] = None; roll["impact_max_rank"] = None
    roll["impact_max_finding"] = None; roll["tier_of_max"] = None

td = {"T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0}
for x in f:
    t = x.get("verification_tier")
    if t in (1, 2, 3, 4, 5): td["T%d" % t] += 1
roll["tier_distribution"] = td
eh = [x.get("expected_verifier_hours") for x in f if x.get("expected_verifier_hours") is not None]
ah = [x.get("actual_verifier_hours") for x in f if x.get("actual_verifier_hours") is not None]
roll["total_expected_verifier_hours"] = round(sum(eh), 4) if eh else None
roll["total_actual_verifier_hours"] = round(sum(ah), 4) if ah else None

# Rule 2d: residual_unverifiable -- the worst thing we found, AND territory we could not enter
roll["residual_unverifiable"] = (any(x.get("impact") == "unverifiable" for x in f)
                                 and (roll["impact_max_rank"] or 0) > 1)
claims = {x.get("claim_ref") for x in f if x.get("claim_ref")}
roll["claims_affected"] = len(claims) if claims else None
# Rule 1.5: paper_only at comment level is a conjunction, never a majority
roll["paper_only"] = (bool(f) and all(x.get("paper_only") for x in f))
roll["n_paper_only"] = sum(1 for x in f if x.get("paper_only"))

roll["n_changing_conclusions"] = sum(1 for x in f if x.get("changes_conclusion"))
roll["n_disclosure_flagged"] = sum(1 for x in f if x.get("disclosure_flag"))
corr = [(x.get("provenance") or {}).get("independent_corroboration_count") or 0 for x in f]
roll["n_multiply_corroborated"] = sum(1 for c in corr if c >= 2)
roll["max_independent_corroboration"] = max(corr) if corr else None
roll["n_human_verified"] = sum(1 for x in f if (x.get("verification") or {}).get("verifier_count"))
roll["n_unverified"] = sum(1 for x in f if not (x.get("verification") or {}).get("verifier_count"))

D = C.setdefault("disclosure", {"flags": [], "urgency": "none"})
D["findings_flagged"] = [x.get("id") for x in f if x.get("disclosure_flag")]
if D.get("urgency") == "immediate":
    warn.append("disclosure.urgency is 'immediate': identifiable individuals are exposed in a "
                "live deposit. This escalates to the coordinator BEFORE the comment is drafted "
                "or circulated, and no worked example may demonstrate the re-identification route.")
if D.get("flags") and D.get("urgency") == "none":
    errors.append("disclosure lists flags %s but urgency is 'none'" % D["flags"])

ids = [x.get("id") for x in f]
if len(ids) != len(set(ids)):
    errors.append("finding ids are not unique: %s" % ids)

# ---- package_locus quasi-identifier scan. Self-tested in both directions before it is
# trusted: a detector that cannot fire certifies nothing, and one that fires on ordinary
# text gets switched off. Controls use out-of-range ids so they can never be real data.
IDPAT = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,3}_?P\d+(?![A-Za-z0-9])"
                   r"|(?<![A-Za-z0-9])P\d{1,3}(?![A-Za-z0-9])"
                   r"|sub_[A-Z]_\d+|sub_\d+"
                   r"|(?:participant|subject)[ _]?\d{1,3}", re.I)
_pos = ["MSTE_AC_sub_E_999.mat", "data/P999.csv", "sub_999", "participant 999", "CYC_P999"]
_neg = ["code/03_model.R:88", "data/wave2.csv column session_date", "subsequent submissions"]
if not all(IDPAT.search(s) for s in _pos):
    errors.append("the package_locus identifier detector fails a known-present control; "
                  "a clean scan from it would mean nothing")
elif any(IDPAT.search(s) for s in _neg):
    errors.append("the package_locus identifier detector fires on ordinary paths")
else:
    for x in f:
        for L in (x.get("package_locus") or []):
            if IDPAT.search(L):
                warn.append("finding %s: package_locus %r looks like it names a participant. "
                            "A deposit path can itself be a quasi-identifier -- coarsen it to a "
                            "directory and column, or redact before any release."
                            % (x.get("id"), L))

# ---- Rule 3: prohibited vocabulary. WARN, not block, and deliberately so: 'manipulated' is
# ordinary experimental vocabulary in this literature ('the manipulated variable'), so a hard
# gate here would be routinely bypassed, and a bypassed gate protects nothing. Scope is stated
# rather than assumed: the free-text fields a finding actually carries.
BANNED = ["fabricated", "falsified", "manipulated", "fraudulent", "misconduct", "deceptive",
          "dishonest", "deliberate", "intentional", "knowingly", "concealed", "p-hacked",
          "harked", "cherry-picked", "misleading", "careless", "sloppy", "negligent"]
BANRE = re.compile(r"\b(" + "|".join(re.escape(b) for b in BANNED) + r")\b", re.I)
scanned = []
for x in f:
    for k in ("claim", "remedy_description", "changes_conclusion_which"):
        if x.get(k): scanned.append(("finding %s .%s" % (x.get("id"), k), x[k]))
    ev = x.get("evidence") or {}
    if ev.get("tolerance_note"): scanned.append(("finding %s .evidence.tolerance_note" % x.get("id"), ev["tolerance_note"]))
if roll.get("overall_rationale"): scanned.append(("rollup.overall_rationale", roll["overall_rationale"]))
if D.get("statement"): scanned.append(("disclosure.statement", D["statement"]))
if not BANRE.search("the data were fabricated"):
    errors.append("the Rule 3 vocabulary detector does not fire on a known-present instance")
for where, text in scanned:
    for m in set(w.lower() for w in BANRE.findall(text)):
        warn.append("Rule 3: %s uses the word %r, which asserts intent, motive or competence. "
                    "State the observable and the discrepancy instead. (If this is ordinary "
                    "experimental vocabulary, e.g. a manipulated variable, ignore.)" % (where, m))

C.setdefault("assertions_withheld", {
    "intent": "not_assessed", "misconduct": "not_assessed",
    "note": "This programme reports only what the artifacts show. It makes no finding as to "
            "cause, intent, or good faith, and recommends no editorial action. "
            "'consistent_with' fields locate findings against published editorial standards "
            "and are descriptive only."})

t = C.setdefault("timing", {})
if not t.get("audit_start_date") and R.get("started_at"):
    t["audit_start_date"] = R["started_at"][:10]
if not t.get("audit_date") and R.get("ended_at"):
    t["audit_date"] = R["ended_at"][:10]
pub, aud = d(t.get("publication_date")), d(t.get("audit_date"))
t["days_elapsed"] = (aud - pub).days if (pub and aud) else None
if t["days_elapsed"] is not None and t["days_elapsed"] < 0:
    warn.append("days_elapsed is negative: the audit date precedes publication")

ar = C.setdefault("author_response", {"status": "not_notified"})
n = d(ar.get("notified_at"))
firsts = [d(ar.get(k)) for k in ("acknowledged_at", "disputed_at", "correction_submitted_at")]
firsts = [x for x in firsts if x]
ar["days_to_first_response"] = (min(firsts) - n).days if (n and firsts) else None
outs = [d(ar.get(k)) for k in ("corrected_at", "retracted_at", "no_response_declared_at")]
outs = [x for x in outs if x]
ar["days_to_outcome"] = (min(outs) - n).days if (n and outs) else None
if ar.get("status") not in ("not_notified",) and not ar.get("notified_at"):
    warn.append("author_response.status is '%s' but notified_at is empty -- the response "
                "intervals cannot be computed without it" % ar.get("status"))

C.setdefault("run_ref", {})
C["run_ref"]["run_json_path"] = C["run_ref"].get("run_json_path") or "./run.json"
C["run_ref"]["run_json_sha256"] = hashlib.sha256(pathlib.Path(rp).read_bytes()).hexdigest()
C["run_ref"]["protocol_version"] = R.get("protocol_version")

if str(C.get("enumeration_scheme", "")).upper().startswith("PLACEHOLDER"):
    warn.append("enumeration_scheme is '%s': this record predates SCHEME.md, its impact values "
                "come from a provisional enumeration, and it is NOT comparable with "
                "scheme-coded records. Recode it against SCHEME.md."
                % C.get("enumeration_scheme"))
elif not C.get("enumeration_scheme"):
    errors.append("enumeration_scheme is empty; a finding's codes are only interpretable "
                  "against the scheme in force when they were assigned")

pathlib.Path(cp + ".tmp").write_text(json.dumps(C, indent=2, ensure_ascii=False) + "\n")
print(json.dumps({"warnings": warn, "errors": errors, "findings": len(f),
                  "impact_max": roll.get("impact_max"),
                  "tier_of_max": roll.get("tier_of_max"),
                  "residual_unverifiable": roll.get("residual_unverifiable"),
                  "changes_conclusions": roll["n_changing_conclusions"],
                  "days_elapsed": t["days_elapsed"]}))
sys.exit(3 if errors else 0)
PYC
  return $?
}

cross_check () {
  python3 - "$1" "$2" <<'PYX'
import json, pathlib, sys
R = json.loads(pathlib.Path(sys.argv[1]).read_text())
C = json.loads(pathlib.Path(sys.argv[2]).read_text())
bad = []
if R.get("audit_id") != C.get("audit_id"):
    bad.append("audit_id differs: run.json '%s' vs comment.json '%s' -- the registry cannot "
               "join the pair" % (R.get("audit_id"), C.get("audit_id")))
rd, cd = R.get("paper_doi"), (C.get("bibliographic") or {}).get("doi")
if rd and cd and rd != cd:
    bad.append("DOI differs: run.json '%s' vs comment.json '%s'" % (rd, cd))
if not rd and not cd:
    print("  WARN: neither file carries a DOI")
rp = (R.get("package") or {}).get("file_count")
cp = (C.get("package") or {}).get("file_count")
if rp is not None and cp is not None and rp != cp:
    print("  WARN: package file_count differs (run %s, comment %s) -- the package may have "
          "changed under the audit" % (rp, cp))
if bad:
    for b in bad: print("  FAIL: " + b)
    sys.exit(1)
print("  ok: the pair agrees on audit_id and DOI")
PYX
  return $?
}

rebind_verification_manifest () {
  # The notebook ran against the original scientific finding records. finalize derives only
  # deterministic metadata (ranks, counts, rollups) into the transaction comment. Bind that exact
  # derived file while retaining the pre-finalize hash as an audit trail.
  python3 - "$1" "$2" "$3" <<'PYREBIND'
import datetime, hashlib, json, os, pathlib, sys, tempfile
source, comment, output = map(pathlib.Path, sys.argv[1:])
manifest=json.loads(source.read_text())
previous=(manifest.get("comment") or {}).get("sha256")
digest=hashlib.sha256(comment.read_bytes()).hexdigest()
manifest["pre_finalize_comment_sha256"]=previous
manifest["comment"]["sha256"]=digest
manifest["recorded_at"]=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
manifest["binding_note"]="comment hash rebound by finalize_run.sh after deterministic metadata derivation"
fd,tmp=tempfile.mkstemp(prefix=output.name+".",dir=output.parent)
with os.fdopen(fd,"w",encoding="utf-8") as handle:
    json.dump(manifest,handle,indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
os.replace(tmp,output)
PYREBIND
}

commit_pair () {
  # $1/$2/$3 validated transaction files; $4/$5/$6 working destinations. The marker and
  # rollback copies make the unavoidable three-renames operation recoverable.
  python3 - "$1" "$2" "$3" "$4" "$5" "$6" <<'PYCMT'
import hashlib, json, os, pathlib, shutil, sys, tempfile
src_run, src_comment, src_manifest, dst_run, dst_comment, dst_manifest = map(pathlib.Path, sys.argv[1:])
marker = dst_run.parent / ".finalize-commit.json"
if marker.exists(): raise SystemExit("an unrecovered finalization transaction already exists")
tx=pathlib.Path(tempfile.mkdtemp(prefix=".finalize-commit.",dir=dst_run.parent))
backup=tx/"backup"; next_dir=tx/"next"; backup.mkdir(); next_dir.mkdir()
for source,name in ((dst_run,"run.json"),(dst_comment,"comment.json"),(dst_manifest,"verification_manifest.json")):
    shutil.copyfile(source,backup/name)
for source,name in ((src_run,"run.json"),(src_comment,"comment.json"),(src_manifest,"verification_manifest.json")):
    shutil.copyfile(source,next_dir/name)
    with (next_dir/name).open("rb") as handle: os.fsync(handle.fileno())
state = {"state":"prepared", "transaction_dir":tx.name,
         "targets":["run.json","comment.json","verification_manifest.json"],
         "run_sha256":hashlib.sha256(src_run.read_bytes()).hexdigest(),
         "comment_sha256":hashlib.sha256(src_comment.read_bytes()).hexdigest(),
         "manifest_sha256":hashlib.sha256(src_manifest.read_bytes()).hexdigest()}
fd, mt = tempfile.mkstemp(prefix=marker.name + ".", dir=marker.parent)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(state, fh); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
os.replace(mt, marker)
try:
    os.replace(next_dir/"comment.json", dst_comment)
    os.replace(next_dir/"verification_manifest.json", dst_manifest)
    os.replace(next_dir/"run.json", dst_run)
except Exception:
    for destination,name in ((dst_run,"run.json"),(dst_comment,"comment.json"),(dst_manifest,"verification_manifest.json")):
        os.replace(backup/name,destination)
    raise
else:
    marker.unlink(); shutil.rmtree(tx)
print("  ok: validated run/comment/verification binding committed as one recoverable transaction")
PYCMT
}

recover_commit_pair () {
  python3 - "$1" <<'PYRECOVER'
import json,os,pathlib,re,shutil,sys,tempfile
root=pathlib.Path(sys.argv[1]).resolve(); marker=root/".finalize-commit.json"
if not marker.exists(): raise SystemExit(0)
if marker.is_symlink() or not marker.is_file(): raise SystemExit("unsafe finalization recovery marker")
state=json.loads(marker.read_text()); name=state.get("transaction_dir")
if not isinstance(name,str) or not re.fullmatch(r"\.finalize-commit\.[A-Za-z0-9._-]+",name):
    raise SystemExit("unsafe finalization transaction directory")
tx=root/name; backup=tx/"backup"
try: tx.resolve().relative_to(root)
except ValueError: raise SystemExit("finalization transaction escapes audit root")
expected=["run.json","comment.json","verification_manifest.json"]
if state.get("targets") != expected or not backup.is_dir():
    raise SystemExit("finalization recovery backup is missing or malformed")
for target in expected:
    source=backup/target
    if source.is_symlink() or not source.is_file(): raise SystemExit(f"finalization recovery lacks {target}")
for target in expected:
    source=backup/target; destination=root/target
    fd,tmp=tempfile.mkstemp(prefix=destination.name+".recover.",dir=root)
    with os.fdopen(fd,"wb") as out:
        out.write(source.read_bytes()); out.flush(); os.fsync(out.fileno())
    os.replace(tmp,destination)
shutil.rmtree(tx); marker.unlink()
print("recovered and rolled back an interrupted finalization transaction",file=sys.stderr)
PYRECOVER
}

file_to_registry () {
  # $1 registry root  $2 run.json  $3 comment.json
  python3 - "$1" "$2" "$3" <<'PYR'
import fcntl, json, os, pathlib, shutil, sys, datetime, tempfile
reg = pathlib.Path(sys.argv[1]); runp = pathlib.Path(sys.argv[2]); comp = pathlib.Path(sys.argv[3])
R = json.loads(runp.read_text()); C = json.loads(comp.read_text())
aid = R["audit_id"]
d = reg / aid
reg.mkdir(parents=True, exist_ok=True)
lock = (reg / ".registry.lock").open("a+")
fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
d.mkdir(parents=True, exist_ok=True)
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def atomic_copy(source, target):
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
        with open(tmp, "rb") as fh: os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

atomic_copy(runp, d / "run.json")
atomic_copy(comp, d / "comment.json")

row = {
    "audit_id": aid,
    "filed_at": now,
    "doi": R.get("paper_doi"),
    "journal": (C.get("bibliographic") or {}).get("journal"),
    "year": (C.get("bibliographic") or {}).get("year"),
    "field": (C.get("bibliographic") or {}).get("field"),
    "design": (C.get("design") or {}).get("type"),
    "audit_date": (C.get("timing") or {}).get("audit_date"),
    "days_elapsed": (C.get("timing") or {}).get("days_elapsed"),
    "findings_total": (C.get("rollup") or {}).get("findings_total"),
    "n_changing_conclusions": (C.get("rollup") or {}).get("n_changing_conclusions"),
    "reproduces": (C.get("rollup") or {}).get("reproduces"),
    "runs_as_shipped": (C.get("package") or {}).get("runs_as_shipped"),
    "rounds_run": R.get("rounds_run"),
    "stopping_reason": R.get("stopping_reason"),
    "compliance_pass": (R.get("compliance") or {}).get("compliance_pass"),
    "wall_clock_seconds": R.get("wall_clock_seconds"),
    "token_coverage": (R.get("token_accounting") or {}).get("coverage_fraction"),
    "protocol_version": R.get("protocol_version"),
    "gate_set_version": R.get("gate_set_version"),
    "enumeration_scheme": C.get("enumeration_scheme"),
    "safe_to_publish_run": R.get("safe_to_publish"),
    "safe_to_publish_comment": C.get("safe_to_publish"),
}

# Append, but never twice for the same audit: re-finalizing must update the row, not add a
# second one that a naive count would treat as a second audit.
idx = reg / "index.jsonl"
keep = []
if idx.exists():
    for line in idx.read_text().splitlines():
        line = line.strip()
        if not line: continue
        try: o = json.loads(line)
        except Exception:
            keep.append(line); continue
        if o.get("audit_id") != aid: keep.append(line)
keep.append(json.dumps(row, ensure_ascii=False))
fd, tmp = tempfile.mkstemp(prefix=idx.name + ".", dir=idx.parent)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("\n".join(keep) + "\n"); fh.flush(); os.fsync(fh.fileno())
os.replace(tmp, idx)
fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
lock.close()
print("  ok: filed into %s (index now holds %d audit%s)"
      % (d, len(keep), "" if len(keep) == 1 else "s"))
PYR
  return $?
}

# The verification notebook is the audit's only claim to being checkable by anyone who did not run
# it. Phase 3 has always said to ship one. Prose did not make it happen: thirteen audits filed
# without a notebook, and by the time anyone looked for them the working directories had been
# deleted and the code behind every published number was gone. So it is a gate, not advice.
#
verification_gate () {
  local DIRN="$1" COMMENT="$2" NB="" STATUS="" SUMMARY="" REPORT="" MANIFEST=""
  for c in "$DIRN/verification.Rmd" "$DIRN/report/verification.Rmd" \
           "$DIRN/verification/verification.Rmd"; do
    [ -f "$c" ] && { NB="$c"; break; }
  done
  if [ -z "$NB" ]; then
    echo "  FAIL: no verification.Rmd under $DIRN. Every numerical claim in the comment must be"
    echo "        re-checkable by a human who did not run the audit -- all of them, not only the"
    echo "        conclusion-changing ones. Run:"
    echo "            bash scripts/make_verification_notebook.sh $DIRN"
    echo "        then write the check bodies into verification_snippets/ until the stub count is"
    echo "        zero. Accuracy-safe v2 has no unverified-publication waiver."
    return 1
  fi
  echo "  ok: $NB"

  # A notebook full of TODO stubs is worse than none, because it looks finished.
  # grep -c always prints a count and exits 1 when that count is zero, so a `|| echo 0` fallback
  # appends a SECOND line and the arithmetic test then dies on "0\n0". Take grep's stdout alone.
  #
  # MATCH THE MARKER THE GENERATOR ACTUALLY WRITES. The first version of this gate grepped for
  # 'TODO: write the check', a string make_verification_notebook.sh never emits, so it counted 0
  # stubs in a notebook that was 21 stubs and nothing else -- the gate would have passed exactly
  # the artefact it exists to reject. The generator writes '## TODO -- STUB.' per claim and a
  # stop("TODO: no check written for <id>") inside each stub chunk; anchor on the former.
  local stubs
  # `TODO . STUB` was one wildcard wide and the generator writes an EM DASH while a hand-planted
  # test fixture wrote two ASCII hyphens, so the pattern matched the real file and missed the test
  # -- an encoding-fragile detector one character from a false clean. `TODO.*STUB` is agnostic to
  # whatever sits between the words, and the stop() marker is a second independent anchor.
  # Anchor TIGHTLY on the two markers the generator writes for a real stub, and on nothing else.
  # `TODO.*STUB` was too loose: the notebook's own summary chunk contains the line
  #   cat(sprintf("claims still a TODO stub : %d%s\n", length(STUB_CLAIMS), ...
  # which matches TODO...STUB via the VARIABLE NAME, so a fully completed notebook was reported as
  # carrying a stub. A gate that blocks clean work is as broken as one that passes dirty work; both
  # get switched off. The real markers are a section comment at line start and the stop() call.
  stubs=$(grep -c '^## TODO\|stop("TODO: no check written' "$NB" 2>/dev/null)
  stubs=${stubs:-0}
  if [ "$stubs" -gt 0 ]; then
    echo "  FAIL: $stubs unfinished claim stub(s) in the notebook. A stub is a FAIL, not a silence"
    echo "        -- it reads as a completed check to anyone skimming the knitted output."
    return 1
  fi

  # The knit's own exit status is mandatory. An unexecuted notebook proves nothing.
  for c in "$DIRN/verification_status.txt" "$DIRN/report/verification_status.txt"; do
    [ -f "$c" ] && { STATUS="$c"; break; }
  done
  if [ -z "$STATUS" ]; then
    echo "  FAIL: no verification_status.txt -- the notebook was not proven to have executed"
    return 1
  elif [ "$(tr -d '[:space:]' < "$STATUS")" != "0" ]; then
    echo "  FAIL: the last knit exited $(tr -d '[:space:]' < "$STATUS"): at least one claim did not"
    echo "        reproduce. Resolve or reclassify it before filing."
    return 1
  else
    echo "  ok: the notebook knitted with every claim reproducing"
  fi

  for c in "$DIRN/verification_summary.csv" "$DIRN/report/verification_summary.csv" \
           "$DIRN/verification/verification_summary.csv"; do
    [ -f "$c" ] && { SUMMARY="$c"; break; }
  done
  for c in "$DIRN/report/REPORT_final.md" "$DIRN/REPORT_final.md"; do
    [ -f "$c" ] && { REPORT="$c"; break; }
  done
  MANIFEST="$DIRN/verification_manifest.json"
  if [ -z "$SUMMARY" ] || [ -z "$REPORT" ] || [ ! -f "$MANIFEST" ]; then
    echo "  FAIL: verification_summary.csv, verification_manifest.json and REPORT_final.md are required"
    echo "        Run scripts/run_verification.sh after the final report/comment edits."
    return 1
  fi
  python3 - "$REPORT" "$COMMENT" "$SUMMARY" "$NB" "$MANIFEST" "$DIRN" "$STATUS" <<'PYVJOIN'
import collections, csv, hashlib, json, pathlib, re, sys
report, comment, summary, notebook, manifest, root, status_file = map(pathlib.Path, sys.argv[1:])
def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024*1024), b""): h.update(block)
    return h.hexdigest()
manifest_obj=json.loads(manifest.read_text())
bad = False
for label, path in (("report",report),("comment",comment),("summary",summary),("notebook",notebook)):
    expected=(manifest_obj.get(label) or {}).get("sha256")
    if expected != digest(path):
        print(f"  FAIL: verification manifest does not match current {label}"); bad=True
if manifest_obj.get("status") != 0:
    print("  FAIL: verification manifest status is not zero"); bad=True
status_artifact=manifest_obj.get("status_artifact") or {}
status_path=(root/status_artifact.get("path", "")).resolve()
try: status_path.relative_to(root.resolve())
except ValueError:
    print("  FAIL: verification status-artifact path escapes the audit root"); bad=True
else:
    if (status_path != status_file.resolve() or not status_path.is_file()
            or status_artifact.get("sha256") != digest(status_path)):
        print("  FAIL: verification manifest does not match the executed status artifact"); bad=True
render=manifest_obj.get("render") or {}
if render.get("exit_status") != 0 or render.get("mode") != "executed":
    print("  FAIL: verification manifest lacks a successful render transaction"); bad=True
pkg=root/"package_manifest.json"
if pkg.is_file() and manifest_obj.get("package_manifest_sha256") != digest(pkg):
    print("  FAIL: verification manifest does not match current package manifest"); bad=True
text = report.read_text(errors="replace")
report_ids = re.findall(r"\[claim\s+(\d+)\]", text, re.I)
comment_obj = json.loads(comment.read_text())
comment_ids = []
for i, finding in enumerate(comment_obj.get("findings") or [], 1):
    raw = str(finding.get("id") or finding.get("finding_id") or "")
    m = re.search(r"(\d+)$", raw)
    if not m:
        print(f"  FAIL: comment finding {i} has no numeric stable ID: {raw!r}")
        raise SystemExit(1)
    comment_ids.append(m.group(1))
with summary.open(newline="", errors="replace") as fh:
    rows = list(csv.DictReader(fh))
if not rows:
    print("  FAIL: verification summary is empty")
    raise SystemExit(1)
claim_col = "claim" if "claim" in rows[0] else "claim_id" if "claim_id" in rows[0] else None
if not claim_col:
    print("  FAIL: verification summary has no claim/claim_id column")
    raise SystemExit(1)
verified_ids, verified_seen, verified_pairs, failed = [], set(), set(), []
allowed_verdicts = {"PASS", "FAIL", "JUDGEMENT", "UNVERIFIABLE"}
for row_number, row in enumerate(rows, 2):
    raw = str(row.get(claim_col) or "")
    m = re.fullmatch(r"C-(\d+)", raw, re.I)
    if not m or int(m.group(1)) < 1:
        print(f"  FAIL: verification summary row {row_number} has an invalid claim ID: {raw!r}")
        bad = True
        continue
    claim_id = str(int(m.group(1)))
    if claim_id not in verified_seen:
        verified_ids.append(claim_id); verified_seen.add(claim_id)
    key = str(row.get("key") or "").strip()
    if not key:
        print(f"  FAIL: verification summary row {row_number} has an empty check key")
        bad = True
    pair = (claim_id, key)
    if pair in verified_pairs:
        print(f"  FAIL: verification summary repeats claim/key pair C-{claim_id}/{key}")
        bad = True
    verified_pairs.add(pair)
    verdict = str(row.get("verdict") or "").strip().upper()
    if verdict not in allowed_verdicts:
        print(f"  FAIL: verification summary row {row_number} has an invalid verdict: {verdict!r}")
        bad = True
    if verdict == "FAIL": failed.append(raw)
for label, values in (("report claim tags", report_ids), ("comment finding IDs", comment_ids)):
    dup = sorted(k for k, n in collections.Counter(values).items() if n != 1)
    if dup:
        print(f"  FAIL: {label} are not unique: {dup}"); bad = True
if report_ids != comment_ids:
    print("  FAIL: report claim tags and comment finding IDs do not match in exact canonical order")
    print("        only report:", sorted(set(report_ids)-set(comment_ids)))
    print("        only comment:", sorted(set(comment_ids)-set(report_ids)))
    print("        report order:", report_ids)
    print("        comment order:", comment_ids); bad = True
if report_ids != verified_ids:
    print("  FAIL: verification-summary claims do not exactly join report/comment claims")
    print("        only report:", sorted(set(report_ids)-set(verified_ids)))
    print("        only summary:", sorted(set(verified_ids)-set(report_ids)))
    print("        report order:", report_ids)
    print("        summary order:", verified_ids); bad = True
manifest_ids = manifest_obj.get("claim_ids")
expected_manifest_ids = [f"C-{claim_id}" for claim_id in report_ids]
if manifest_ids != expected_manifest_ids:
    print("  FAIL: verification-manifest claim IDs do not exactly join report/comment claims")
    print("        expected:", expected_manifest_ids)
    print("        observed:", manifest_ids); bad = True
if failed:
    print("  FAIL: verification summary contains failed claims:", failed); bad = True
if bad: raise SystemExit(1)
print(f"  ok: {len(report_ids)} report/comment findings are unique and have notebook outcomes")
PYVJOIN
  [ $? -eq 0 ] || return 1
  return 0
}

# =====================================================================================
main () {
  command -v jq >/dev/null;      if [ $? -ne 0 ]; then die "jq is required"; fi
  command -v python3 >/dev/null; if [ $? -ne 0 ]; then die "python3 is required"; fi

  local TARGET="" COMMENT="" STOP="" DETAIL="" SCHEMAS="$DEFAULT_SCHEMAS"
  local REGISTRY="$DEFAULT_REGISTRY" NOREG=0 PKGD=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --comment) COMMENT="${2:-}"; shift 2 ;;
      --stopping-reason) STOP="${2:-}"; shift 2 ;;
      --stopping-detail) DETAIL="${2:-}"; shift 2 ;;
      --schemas) SCHEMAS="${2:-}"; shift 2 ;;
      --registry) REGISTRY="${2:-}"; shift 2 ;;
      --no-registry) NOREG=1; shift ;;
      --pkg-dir) PKGD="${2:-}"; shift 2 ;;
      -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
      -*) die "unknown option $1" ;;
      *) if [ -z "$TARGET" ]; then TARGET="$1"; else die "unexpected argument $1"; fi; shift ;;
    esac
  done
  [ -n "$TARGET" ] || die "usage: finalize_run.sh <audit-dir|run.json> [options]"

  local RUN DIRN ORIG_RUN ORIG_COMMENT
  if [ -d "$TARGET" ]; then RUN="$TARGET/run.json"; else RUN="$TARGET"; fi
  [ -f "$RUN" ] || die "$RUN not found"
  DIRN=$(dirname "$RUN")
  [ -n "$COMMENT" ] || COMMENT="$DIRN/comment.json"
  [ -n "$PKGD" ] || PKGD="$DIRN/pkg"
  [ -f "$SCHEMAS/run.schema.json" ] || die "no run.schema.json under $SCHEMAS"
  [ -f "$SCHEMAS/comment.schema.json" ] || die "no comment.schema.json under $SCHEMAS"
  recover_commit_pair "$DIRN" || die "could not recover an interrupted metadata transaction"
  if [ ! -f "$COMMENT" ]; then
    die "$COMMENT not found. finalize validates the PAIR: the process record is only half the
       registry entry, and a run with no comment record cannot be pooled by the meta-paper."
  fi

  # Derive and validate in a private transaction. A failed finalization must not set ended_at,
  # rewrite rollups, or leave only one half of the metadata pair updated.
  ORIG_RUN="$RUN"; ORIG_COMMENT="$COMMENT"
  FINALIZE_TX=$(mktemp -d "$DIRN/.finalize-tx.XXXXXX") || die "cannot create finalization transaction"
  cp "$ORIG_RUN" "$FINALIZE_TX/run.json" || die "cannot stage run.json"
  cp "$ORIG_COMMENT" "$FINALIZE_TX/comment.json" || die "cannot stage comment.json"
  RUN="$FINALIZE_TX/run.json"; COMMENT="$FINALIZE_TX/comment.json"

  local existing_stop
  existing_stop=$(jq -r '.stopping_reason // ""' "$RUN")
  if [ -z "$STOP" ] && [ -z "$existing_stop" ]; then
    die "no stopping reason. Pass --stopping-reason target_complete|converged|cap_with_unresolved|aborted.
       It cannot be inferred: stopping at the round cap and converging produce identical
       round counts and mean opposite things."
  fi
  case "${STOP:-$existing_stop}" in
    target_complete|converged|cap_with_unresolved|aborted) ;;
    *) die "invalid stopping reason '${STOP:-$existing_stop}'" ;;
  esac

  local fail=0 rc out

  echo "== 0. validator self-test =="
  write_validator; if [ $? -ne 0 ]; then die "could not write the validator"; fi
  python3 "$VALIDATOR" --controls
  if [ $? -ne 0 ]; then
    echo "ABORTING: the validator cannot be trusted, so neither can a clean result from it." >&2
    rm -f "$VALIDATOR"; exit 1
  fi

  if jq -e '.schema_version == "2.0.0"' "$RUN" >/dev/null 2>&1; then
    echo "== 0b. canonical finding projections =="
    python3 "$HERE/generate_finding_artifacts.py" "$DIRN" --check || {
      echo "FINALIZE FAILED — findings registry projections are missing or divergent." >&2
      rm -f "$VALIDATOR"; exit 1
    }
    echo "== 0c. immutable critique-round projections =="
    python3 "$HERE/validate_round_projections.py" "$DIRN" --require-post-delta-gates || {
      echo "FINALIZE FAILED — critique routes/results are not bound to immutable runtime projections." >&2
      rm -f "$VALIDATOR"; exit 1
    }
    echo "== 0d. canonical invocation telemetry =="
    python3 "$HERE/summarize_invocations.py" "$DIRN" --check >/dev/null || {
      echo "FINALIZE FAILED — Phase-1 fan-out evidence or canonical invocation telemetry is incomplete." >&2
      rm -f "$VALIDATOR"; exit 1
    }
  fi

  echo "== 1. deriving run.json =="
  out=$(derive_run "$RUN" "$PKGD" "$STOP" "$DETAIL" "$DIRN" "$HERE/../config/pipeline-policy-v2.json")
  rc=$?
  if [ "$rc" -ne 0 ]; then echo "  FAIL: derivation failed (exit $rc)"; rm -f "$RUN.tmp" "$VALIDATOR"; exit 1; fi
  mv -f "$RUN.tmp" "$RUN"
  if [ $? -ne 0 ]; then die "could not write the derived run.json"; fi
  printf '%s' "$out" | python3 -c 'import json,sys
o=json.load(sys.stdin)
print("  ok: rounds_run=%s stopping_reason=%s token_coverage=%s" % (o["rounds_run"],o["stopping_reason"],o["coverage"]))
print("  compliance_pass: %s" % o["compliance_pass"])
for v in o["violations"]: print("     VIOLATION: " + v)
for w in o["warnings"]:   print("     WARN: " + w)'

  echo "== 2. deriving comment.json =="
  out=$(derive_comment "$COMMENT" "$RUN")
  rc=$?
  # Exit 3 means the file was derived and written but breaks a SCHEME.md classification rule.
  # The file is kept so the errors can be read against it; the run still fails at the gate.
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
    echo "  FAIL: derivation failed (exit $rc)"; rm -f "$COMMENT.tmp" "$VALIDATOR"; exit 1
  fi
  mv -f "$COMMENT.tmp" "$COMMENT"
  if [ $? -ne 0 ]; then die "could not write the derived comment.json"; fi
  printf '%s' "$out" | python3 -c 'import json,sys
o=json.load(sys.stdin)
print("  ok: findings=%s impact_max=%s tier_of_max=%s residual_unverifiable=%s" %
      (o["findings"], o["impact_max"], o["tier_of_max"], o["residual_unverifiable"]))
print("      changing_conclusions=%s days_elapsed=%s" % (o["changes_conclusions"], o["days_elapsed"]))
for e in o.get("errors", []): print("     SCHEME ERROR: " + e)
for w in o["warnings"]: print("     WARN: " + w)'
  if [ "$rc" -eq 3 ]; then fail=1; fi

  echo "== 3. schema validation =="
  python3 "$VALIDATOR" "$RUN" "$SCHEMAS/run.schema.json"
  if [ $? -ne 0 ]; then fail=1; fi
  if jq -e '.schema_version == "2.0.0"' "$RUN" >/dev/null 2>&1; then
    python3 "$SCHEMAS/tools/validate_v2.py" run "$RUN"
    if [ $? -ne 0 ]; then fail=1; fi
  fi
  python3 "$VALIDATOR" "$COMMENT" "$SCHEMAS/comment.schema.json"
  if [ $? -ne 0 ]; then fail=1; fi

  echo "== 4. cross-checking the pair =="
  cross_check "$RUN" "$COMMENT"
  if [ $? -ne 0 ]; then fail=1; fi

  echo "== 4b. verification notebook =="
  verification_gate "$DIRN" "$ORIG_COMMENT"
  if [ $? -ne 0 ]; then fail=1; fi

  if [ "$fail" -ne 0 ]; then
    echo ""
    echo "FINALIZE FAILED — nothing was filed to the registry. Fix the errors above and re-run."
    rm -f "$VALIDATOR"
    exit 1
  fi

  echo "== 4c. atomic metadata commit =="
  local TX_MANIFEST="$FINALIZE_TX/verification_manifest.json"
  rebind_verification_manifest "$DIRN/verification_manifest.json" "$COMMENT" "$TX_MANIFEST"
  if [ $? -ne 0 ]; then echo "  FAIL: could not bind verification to derived comment"; exit 1; fi
  commit_pair "$RUN" "$COMMENT" "$TX_MANIFEST" "$ORIG_RUN" "$ORIG_COMMENT" "$DIRN/verification_manifest.json"
  if [ $? -ne 0 ]; then echo "  FAIL: metadata transaction could not be committed"; exit 1; fi
  RUN="$ORIG_RUN"; COMMENT="$ORIG_COMMENT"
  verification_gate "$DIRN" "$ORIG_COMMENT"
  if [ $? -ne 0 ]; then echo "  FAIL: post-commit verification binding check failed"; exit 1; fi

  if [ "$NOREG" -eq 1 ]; then
    echo "== 5. registry: skipped (--no-registry) =="
    python3 "$HERE/finalization_receipt.py" create "$DIRN" || {
      echo "  FAIL: could not create the no-registry finalization receipt"; exit 1; }
  else
    echo "== 5. filing into the registry =="
    mkdir -p "$REGISTRY"
    if [ $? -ne 0 ]; then echo "  FAIL: cannot create $REGISTRY"; rm -f "$VALIDATOR"; exit 1; fi
    python3 "$HERE/finalization_receipt.py" create "$DIRN" --registry "$REGISTRY"
    if [ $? -ne 0 ]; then echo "  FAIL: filing/receipt transaction failed"; rm -f "$VALIDATOR"; exit 1; fi
  fi
  python3 "$HERE/finalization_receipt.py" check "$DIRN" || {
    echo "  FAIL: finalization receipt did not verify immediately after creation"; exit 1; }

  rm -f "$VALIDATOR"
  echo ""
  echo "run.json and comment.json are valid and complete."
  local cp
  cp=$(jq -r '.compliance.compliance_pass' "$RUN")
  if [ "$cp" != "true" ]; then
    echo "NOTE: compliance_pass is false. The audit deviated from protocol; that does not"
    echo "      make the findings wrong, but it must be stated in the release notes."
  fi
  return 0
}

# ------------------------------------------------------------------ self-test
self_test () {
  local T rc grc fails n S
  S="$DEFAULT_SCHEMAS"
  [ -f "$S/EXAMPLE_run.json" ] || die "self-test needs $S/EXAMPLE_run.json"
  [ -f "$S/EXAMPLE_comment.json" ] || die "self-test needs $S/EXAMPLE_comment.json"
  T=$(mktemp -d "${TMPDIR:-/tmp}/finalize_selftest.XXXXXX") || die "no scratch dir"
  fails=0
  echo "== finalize_run.sh self-test in $T =="

  # Every completed fixture gets a content-bound passing verification bundle so the cases below
  # test their intended invariant. The strict missing/stub/waiver controls are at the end.
  nb () {
    [ -f "$1/comment.json" ] || { echo "self-test fixture lacks comment.json: $1" >&2; return 1; }
    python3 - "$1" <<'PYNB'
import csv, json, pathlib, sys
root=pathlib.Path(sys.argv[1]); findings=json.loads((root/"comment.json").read_text()).get("findings") or []
(root/"report").mkdir(exist_ok=True)
report=[]
for i, finding in enumerate(findings, 1):
    report += [f"## {i}. {finding.get('claim') or 'Synthetic finding'} [claim {i}]", "", "Verified fixture text.", ""]
(root/"report/REPORT_final.md").write_text("\n".join(report) or "# Report with no findings\n")
(root/"verification.Rmd").write_text("---\ntitle: Finalize self-test verification\noutput: html_document\n---\n\nAll fixture outcomes are recorded below.\n")
with (root/"verification_summary.fixture.csv").open("w", newline="") as handle:
    writer=csv.writer(handle); writer.writerow(["claim","key","verdict"])
    for i, _ in enumerate(findings, 1): writer.writerow([f"C-{i}","fixture","PASS"])
PYNB
    mkdir -p "$1/.verification-test-bin"
    printf '%s\n' '#!/bin/sh' \
      'cp verification_summary.fixture.csv verification_summary.csv' \
      'printf '\''0\n'\'' > verification_status.txt' 'exit 0' > "$1/.verification-test-bin/Rscript"
    chmod +x "$1/.verification-test-bin/Rscript"
    PATH="$1/.verification-test-bin:$PATH" bash "$HERE/run_verification.sh" "$1" >/dev/null
    rm -f "$1/verification_summary.fixture.csv"; rm -rf "$1/.verification-test-bin"
  }

  rebind_nb () {
    python3 - "$1" <<'PYREBINDTEST'
import csv,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); findings=json.loads((root/"comment.json").read_text()).get("findings") or []
with (root/"verification_summary.fixture.csv").open("w",newline="") as handle:
    writer=csv.writer(handle); writer.writerow(["claim","key","verdict"])
    for i,_ in enumerate(findings,1): writer.writerow([f"C-{i}","fixture","PASS"])
PYREBINDTEST
    mkdir -p "$1/.verification-test-bin"
    printf '%s\n' '#!/bin/sh' \
      'cp verification_summary.fixture.csv verification_summary.csv' \
      'printf '\''0\n'\'' > verification_status.txt' 'exit 0' > "$1/.verification-test-bin/Rscript"
    chmod +x "$1/.verification-test-bin/Rscript"
    PATH="$1/.verification-test-bin:$PATH" bash "$HERE/run_verification.sh" "$1" >/dev/null
    rm -f "$1/verification_summary.fixture.csv"; rm -rf "$1/.verification-test-bin"
  }

  mkdir -p "$T/a"
  cp "$S/EXAMPLE_run.json" "$T/a/run.json"
  cp "$S/EXAMPLE_comment.json" "$T/a/comment.json"
  nb "$T/a"

  # 1. the shipped examples must finalize and validate cleanly
  "$SELF" "$T/a" --registry "$T/reg" > "$T/out1.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then echo "  FAIL: finalizing the example pair exited $rc"; cat "$T/out1.txt"; fails=1
  else echo "  ok: the shipped examples derive, validate and file (exit 0)"; fi

  # 2. registry filing
  if [ ! -f "$T/reg/20260714-nakamura-9f3ac1/run.json" ] || \
     [ ! -f "$T/reg/20260714-nakamura-9f3ac1/comment.json" ]; then
    echo "  FAIL: the pair was not filed into the registry"; fails=1
  else echo "  ok: pair filed into the registry"; fi
  n=$(wc -l < "$T/reg/index.jsonl" 2>/dev/null | tr -d ' ')
  if [ "$n" != "1" ]; then echo "  FAIL: index has $n rows, expected 1"; fails=1
  else echo "  ok: index has one row"; fi

  # 3. re-finalizing must UPDATE the index row, not add a second one
  nb "$T/a"
  "$SELF" "$T/a" --registry "$T/reg" > "$T/out2.txt" 2>&1
  n=$(wc -l < "$T/reg/index.jsonl" | tr -d ' ')
  if [ "$n" != "1" ]; then echo "  FAIL: re-finalize duplicated the index row ($n rows)"; fails=1
  else echo "  ok: re-finalize updates the index row in place"; fi

  # 4. derived fields actually got computed
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
print("%s/%s/%s" % (o["rounds_run"], o["phases"]["fan_out"]["wall_clock_seconds"], o["compliance"]["compliance_pass"]))' "$T/a/run.json")
  if [ "$n" != "5/3132/False" ]; then
    echo "  FAIL: derived run fields = '$n', expected '5/3132/False'"; fails=1
  else echo "  ok: durations and the compliance vector derived (and the example's two real"
       echo "      protocol breaches correctly make compliance_pass false)"; fi
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
print("%s/%s/%s" % (o["rollup"]["findings_total"], o["rollup"]["count_by_remedy_class"]["reanalysis"], o["timing"]["days_elapsed"]))' "$T/a/comment.json")
  if [ "$n" != "6/2/308" ]; then
    echo "  FAIL: derived comment fields = '$n', expected '6/2/308'"; fails=1
  else echo "  ok: rollup counts and days_elapsed derived"; fi

  # 4b. SCHEME.md v1.0 Rule 1: the maximum, and the tier OF THE FINDING THAT SET IT --
  # not the worst tier in the comment, which here is T4 on a lower-ranked finding.
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))["rollup"]
print("%s/%s/%s/%s/%s" % (o["impact_max"], o["impact_max_finding"], o["tier_of_max"],
                          o["residual_unverifiable"], o["paper_only"]))' "$T/a/comment.json")
  if [ "$n" != "conclusion_contradicted/F3/3/True/False" ]; then
    echo "  FAIL: Rule 1 roll-up = '$n', expected 'conclusion_contradicted/F3/3/True/False'"; fails=1
  else echo "  ok: impact_max by rank, tier_of_max from that finding (T3, not the comment's T4),"
       echo "      residual_unverifiable true, comment-level paper_only false (conjunction)"; fi

  # 4c. corroboration: independent discovery counted, post-exposure agreement NOT counted
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
f={x["id"]: x for x in o["findings"]}
print("%s/%s/%s" % (f["F3"]["provenance"]["independent_corroboration_count"],
                    f["F4"]["provenance"]["independent_corroboration_count"],
                    o["rollup"]["n_multiply_corroborated"]))' "$T/a/comment.json")
  if [ "$n" != "1/3/3" ]; then
    echo "  FAIL: corroboration counts = '$n', expected '1/3/3' -- F3 has two agreers after"
    echo "        exposure and they must NOT be counted as independent finders"; fails=1
  else echo "  ok: independent corroboration counted; agreement-after-seeing excluded from it"; fi

  # 4d. verifier counts and the tier hour totals
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
print("%s/%s/%s" % (o["rollup"]["n_human_verified"], o["rollup"]["n_unverified"],
                    o["rollup"]["total_expected_verifier_hours"]))' "$T/a/comment.json")
  if [ "$n" != "4/2/32.75" ]; then
    echo "  FAIL: verification roll-up = '$n', expected '4/2/32.75'"; fails=1
  else echo "  ok: verifier counts and expected verifier-hours rolled up"; fi

  # 4e. a real scheme version must NOT trigger the non-comparability warning.
  # Matched case-insensitively and on a substring that excludes the leading "not": the message
  # capitalises NOT for emphasis, so a case-sensitive "not comparable" matched neither the
  # positive nor the negative control -- this assertion passed vacuously until it was paired
  # with 4f, which failed loudly and exposed both.
  grep -qi "comparable with scheme-coded records" "$T/out1.txt"; grc=$?
  if [ "$grc" -eq 0 ]; then
    echo "  FAIL: a record coded against SCHEME.md v1.0 still got the placeholder warning"; fails=1
  else echo "  ok: no placeholder warning for a scheme-coded record"; fi

  # 4f. NEGATIVE CONTROL: a PLACEHOLDER record must still be warned about
  mkdir -p "$T/h"; cp "$S/EXAMPLE_run.json" "$T/h/run.json"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["enumeration_scheme"]="PLACEHOLDER-v0"
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/h/comment.json"
  nb "$T/h"
  "$SELF" "$T/h" --no-registry > "$T/out9.txt" 2>&1
  grep -qi "comparable with scheme-coded records" "$T/out9.txt"; grc=$?
  if [ "$grc" -ne 0 ]; then
    echo "  FAIL: a PLACEHOLDER-v0 record was not warned about"; fails=1
  else echo "  ok: PLACEHOLDER-v0 still warns about non-comparability"; fi

  # 4g. NEGATIVE CONTROL: Rule 2c -- 'unverifiable' with no cause is not a finding
  mkdir -p "$T/i"; cp "$S/EXAMPLE_run.json" "$T/i/run.json"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
for x in o["findings"]:
    if x["impact"] == "unverifiable": x["unverifiable_reason"] = None
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/i/comment.json"
  nb "$T/i"
  "$SELF" "$T/i" --no-registry > "$T/out10.txt" 2>&1
  rc=$?
  grep -q "no unverifiable_reason" "$T/out10.txt"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: an 'unverifiable' finding with no recorded cause was accepted (exit $rc)"; fails=1
  else echo "  ok: Rule 2c enforced -- unverifiable with no cause blocks finalize"; fi

  # 4h. NEGATIVE CONTROL: a role cannot both find independently and agree after exposure
  mkdir -p "$T/j"; cp "$S/EXAMPLE_run.json" "$T/j/run.json"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
p=o["findings"][0]["provenance"]; p["agreed_after_seeing"]=p["independently_found_by"][:1]
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/j/comment.json"
  nb "$T/j"
  "$SELF" "$T/j" --no-registry > "$T/out11.txt" 2>&1
  rc=$?
  grep -q "cannot be both" "$T/out11.txt"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: a role counted as both independent finder and after-the-fact agreer (exit $rc)"; fails=1
  else echo "  ok: manufactured corroboration blocked"; fi

  # 4i. the package_locus quasi-identifier warning fires on a participant-shaped path
  mkdir -p "$T/k"; cp "$S/EXAMPLE_run.json" "$T/k/run.json"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
o["findings"][0]["package_locus"]=["data/CYC_P07_session.csv"]
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/k/comment.json"
  nb "$T/k"
  "$SELF" "$T/k" --no-registry > "$T/out12.txt" 2>&1
  grep -q "looks like it names a participant" "$T/out12.txt"; grc=$?
  if [ "$grc" -ne 0 ]; then
    echo "  FAIL: a participant-shaped package_locus drew no warning"; fails=1
  else echo "  ok: participant-shaped package_locus warns before release"; fi

  # 4j. human effort and the settled-facts lever survive the round trip
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
print("%s/%s/%s" % (o["human_effort"]["person_minutes_total"],
                    o["human_effort"]["phases_recorded"],
                    o["settled_facts"]["generated_after_round"]))' "$T/a/run.json")
  if [ "$n" != "675/5/1" ]; then
    echo "  FAIL: human effort / settled facts = '$n', expected '675/5/1'"; fails=1
  else echo "  ok: person-minutes totalled over 5 of 5 phases; SETTLED.md round recorded"; fi

  # 5. NEGATIVE CONTROL: an invalid comment.json must block, and must not file
  mkdir -p "$T/b"; cp "$S/EXAMPLE_run.json" "$T/b/run.json"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["findings"][0]["remedy_class"]="magic"
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/b/comment.json"
  nb "$T/b"
  "$SELF" "$T/b" --registry "$T/reg2" > "$T/out3.txt" 2>&1
  rc=$?
  # Capture grep's status into a variable. Writing `[ "$rc" -eq 0 ] || [ $? -ne 0 ]` reads the
  # status of the FIRST test, not of grep -- a nonzero exit for any reason would then pass this
  # control, which is how a gate ends up asserting nothing.
  grep -q "not one of" "$T/out3.txt"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: an out-of-enum remedy_class was accepted, or failed for the wrong reason (exit $rc)"; cat "$T/out3.txt"; fails=1
  else echo "  ok: invalid comment.json blocks finalize, with the enum error named (exit $rc)"; fi
  if [ -d "$T/reg2/20260714-nakamura-9f3ac1" ]; then
    echo "  FAIL: a rejected pair was filed to the registry anyway"; fails=1
  else echo "  ok: nothing filed when validation fails"; fi

  # 6. NEGATIVE CONTROL: an unknown field in run.json must block
  mkdir -p "$T/c"; cp "$S/EXAMPLE_comment.json" "$T/c/comment.json"; nb "$T/c"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["total_cost"]=42
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_run.json" "$T/c/run.json"
  "$SELF" "$T/c" --no-registry > "$T/out4.txt" 2>&1
  rc=$?
  grep -q "unexpected property 'total_cost'" "$T/out4.txt"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: an undeclared top-level field was accepted (exit $rc)"; cat "$T/out4.txt"; fails=1
  else echo "  ok: undeclared fields in run.json block finalize"; fi

  # 7. NEGATIVE CONTROL: a mismatched pair must block
  mkdir -p "$T/d"; cp "$S/EXAMPLE_run.json" "$T/d/run.json"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["audit_id"]="a-different-audit"
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/d/comment.json"
  nb "$T/d"
  "$SELF" "$T/d" --no-registry > "$T/out5.txt" 2>&1
  rc=$?
  grep -q "audit_id differs" "$T/out5.txt"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: a mismatched run/comment pair was accepted (exit $rc)"; cat "$T/out5.txt"; fails=1
  else echo "  ok: a mismatched pair blocks finalize"; fi

  # 8. NEGATIVE CONTROL: no stopping reason must refuse with the refusal code
  mkdir -p "$T/e"; cp "$S/EXAMPLE_comment.json" "$T/e/comment.json"; nb "$T/e"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["stopping_reason"]=None
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_run.json" "$T/e/run.json"
  "$SELF" "$T/e" --no-registry > "$T/out6.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ]; then echo "  FAIL: a missing stopping reason should refuse with exit 2, got $rc"; fails=1
  else echo "  ok: a missing stopping reason is refused, never guessed (exit 2)"; fi

  # 9. NEGATIVE CONTROL: a missing comment.json must refuse
  mkdir -p "$T/f"; cp "$S/EXAMPLE_run.json" "$T/f/run.json"
  "$SELF" "$T/f" --no-registry > "$T/out7.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ]; then echo "  FAIL: a missing comment.json should refuse with exit 2, got $rc"; fails=1
  else echo "  ok: a missing comment.json is refused (exit 2)"; fi

  # 10. the end-to-end path: init -> log_event -> finalize on a fresh audit
  mkdir -p "$T/g/pkg"; printf 'a,b\n1,2\n' > "$T/g/pkg/d.csv"
  bash "$HERE/init_run.sh" "$T/g" --audit-id e2e-1 --doi 10.5555/e2e \
       --auditor-model sonnet-5 --auditor-family claude --auditor-provider anthropic \
       --protocol-version p --gate-set-version g > /dev/null 2>&1
  rc=$?
  export RUN_JSON="$T/g/run.json"
  bash "$HERE/log_event.sh" phase_end phase=intake > /dev/null 2>&1
  for ph in fan_out adjudication critique release; do
    bash "$HERE/log_event.sh" phase_start phase=$ph > /dev/null 2>&1
    bash "$HERE/log_event.sh" phase_end   phase=$ph > /dev/null 2>&1
  done
  bash "$HERE/log_event.sh" round_start round=1 at=2026-01-01T10:00:00Z \
       critics_run=critic_stats,critic_data,critic_fairness,critic_completeness > /dev/null 2>&1
  bash "$HERE/log_event.sh" round_end   round=1 at=2026-01-01T11:00:00Z > /dev/null 2>&1
  bash "$HERE/log_event.sh" round_result round=1 blocking_items=1 comment_defects=1 \
       paper_defects=0 integrated_at=2026-01-01T12:00:00Z consistency_check_run=true \
       consistency_check_exit=0 character=wording_only factual_corrections=1 \
       report_version_reviewed=report/REPORT_v1.md > /dev/null 2>&1
  bash "$HERE/log_event.sh" round_start round=2 at=2026-01-01T12:05:00Z \
       critics_run=critic_fairness,critic_completeness > /dev/null 2>&1
  bash "$HERE/log_event.sh" round_end   round=2 at=2026-01-01T12:20:00Z > /dev/null 2>&1
  bash "$HERE/log_event.sh" round_result round=2 blocking_items=0 comment_defects=0 \
       paper_defects=0 integrated_at=2026-01-01T12:30:00Z consistency_check_run=true \
       consistency_check_exit=0 character=wording_only factual_corrections=0 \
       report_version_reviewed=report/REPORT_final.md > /dev/null 2>&1
  bash "$HERE/log_event.sh" review_scope changed_claim_ids:='["F2"]' rereviewed_claim_ids:='["F2"]' \
       unresolved_blocking_item_ids:='[]' unresolved_conclusion_critical_item_ids:='[]' \
       unreviewed_sections:='[]' > /dev/null 2>&1
  bash "$HERE/log_event.sh" delivery artifact_path=report/c.docx delivered_at=2026-01-01T13:00:00Z > /dev/null 2>&1
  unset RUN_JSON
  python3 - "$T/g/comment.json" <<'PYMIN'
import json, sys
p = sys.argv[1]
json.dump({"schema_version":"1.0.0","audit_id":"e2e-1","safe_to_publish":False,
  "enumeration_scheme":"v1.0",
  "disclosure":{"flags":[],"urgency":"none"},
  "bibliographic":{"doi":"10.5555/e2e","journal":"J","year":2025,"field":"econ"},
  "design":{"type":"rct"},
  "sample":{"n":10,"unit_of_observation":"participant","human_subjects":True},
  "reported_statistics":{"main_result_significant":True,"power_analysis_present":False,
    "effect_sizes_reported":True,"intervals_reported":True,"multiplicity_correction_present":False},
  "package":{"data_present":True,"code_present":True,"runs_as_shipped":"yes"},
  "governance":{"coi_statement_present":True,"funding_statement_present":True,"pii_present":False},
  "findings":[{"id":"F1","claim":"The abstract reports N = 120; every table reports 102.",
    "impact":"value_wrong","verification_tier":1,"paper_only":True,
    "expected_verifier_hours":0.25,"disclosure_flag":False,"remedy_class":"one_number",
    "changes_conclusion":False,"confidence":"CONFIRMED",
    "evidence":{"artifact":"paper, abstract and tables","operation":"compare the printed sample sizes",
      "expected":120,"observed":102,"tolerance_note":None},
    "provenance":{"origin_role":"auditor_text_vs_data",
      "independently_found_by":["auditor_text_vs_data","auditor_reproduction"]}}],
  "rollup":{"impact_max":"value_wrong","findings_total":1,
    "count_by_remedy_class":{"one_line_code":0,"one_number":1,"reanalysis":0,"not_fixable":0}},
  "timing":{"publication_date":"2025-01-01","audit_date":"2026-01-01"},
  "author_response":{"status":"not_notified"}}, open(p,"w"))
PYMIN
  nb "$T/g"
  # Build the v2 runtime evidence against the current contracts, using genuine settled
  # promotion and the production diff builder rather than a hand-authored packet.
  python3 - "$T/g" "$S/fixtures/v2_finding_row.json" \
      "$S/fixtures/v2_critic_result.json" "$HERE/.." <<'PYCURRENTV2'
import copy, hashlib, json, os, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
finding_fixture = json.loads(pathlib.Path(sys.argv[2]).read_text())
critic_fixture = json.loads(pathlib.Path(sys.argv[3]).read_text())
skill = pathlib.Path(sys.argv[4]).resolve()
scripts = skill / "scripts"
sys.path.insert(0, str(scripts))
import test_run_critique_runtime as critique_runtime

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)

def write_json(path, value):
    write(path, json.dumps(value, sort_keys=True) + "\n")

def input_sha(round_no):
    return hashlib.sha256(f"e2e-current-input-{round_no}".encode()).hexdigest()

def critic_id(round_no, role):
    return f"e2e-1:r{round_no}:critic_{role}:{input_sha(round_no)[:16]}"

before_report = root / "report/REPORT_v1.md"
final_report = root / "report/REPORT_final.md"
write(before_report, "# Independent audit\n\n## 1. Finding\n\nDraft sample-size wording [claim 1].\n")
write(final_report, """# Independent audit

## Abstract

This sanitized fixture verifies the exact structured sample-size correction.

## 1. Introduction

The bounded example exercises the current release protocol.

## 2. Finding

The integrated comment distinguishes the reported and observed sample sizes.

## 3. Conclusion

The fixture supports only the implementation claim tested here.

## References

AI-Games protocol. AI-vertical audit guidance.
""")
source = root / "work/check_primary.txt"
snippet = root / "work/check_primary.py"
draft_evidence = root / "work/draft_review.txt"
write(source, "expected=120 observed=102\n")
write(snippet, "assert 102 != 120\n")
write(draft_evidence, "The pre-integration wording was reviewed and superseded.\n")
write_json(root / "EVIDENCE_HASHES.json", {
    path.relative_to(root).as_posix(): {"sha256": sha(path), "size": path.stat().st_size}
    for path in (source, snippet, draft_evidence)
})

finding = copy.deepcopy(finding_fixture)
finding.update({
    "claim": "The abstract reports N = 120; every table reports 102.",
    "source_location": {"path": "report/REPORT_final.md", "locator": "Section 1"},
    "claim_ref": "Section 1", "report_section": "Section 1",
})
finding["evidence"].update({
    "source_hashes": [{"path": "work/check_primary.txt", "sha256": sha(source)}],
    "rerun_command": "python work/check_primary.py", "expected": 120, "observed": 102,
    "tolerance_basis": "Exact integer comparison.",
})
finding["dependencies"].update({
    "environment_fingerprint": "sha256:e2e-current-environment",
    "finding_ids": [], "artifact_paths": ["work/check_primary.txt", "work/check_primary.py"],
})
finding["verification_snippet"] = {
    "kind": "code", "path": "work/check_primary.py", "basis": "Exact comparison.",
}
finding["settled"] = {"approved": True, "valid_for_report_hash": sha(final_report)}
excluded = copy.deepcopy(finding)
excluded.update({
    "id": "F2", "claim": "The superseded draft wording is excluded after integration.",
    "domain": "framing", "status": "rejected", "coordinator_approved": False,
    "ledger_disposition": {"status": "EXCLUDED", "target": None,
                           "reason": "Superseded wording retained as a tested negative."},
    "verifications": [], "settled": None,
})
excluded["dependencies"]["finding_ids"] = ["F1"]
before_excluded = copy.deepcopy(excluded)
before_excluded["claim"] = "Draft wording remained under review before Round-1 integration."

def write_registries():
    write(root / "work/findings_round_1.jsonl", "".join(
        json.dumps(row, sort_keys=True) + "\n" for row in (finding, before_excluded)))
    write(root / "work/findings.jsonl", "".join(
        json.dumps(row, sort_keys=True) + "\n" for row in (finding, excluded)))

write_registries()
subprocess.run([sys.executable, str(scripts / "generate_finding_artifacts.py"),
                str(root), "--apply"], check=True, stdout=subprocess.DEVNULL)

package_file = root / "pkg/d.csv"
package_hash = sha(package_file)
package_tree = hashlib.sha256(f"d.csv\0{package_hash}\n".encode()).hexdigest()
write_json(root / "package_manifest.json", {
    "schema_version": "2.0.0", "algorithm": "sha256",
    "package_sha256": package_tree, "file_count": 1, "file_types": {"csv": 1},
    "files": [{"path": "d.csv", "size": package_file.stat().st_size,
               "sha256": package_hash, "extension": "csv"}],
})
os.chmod(package_file, 0o444)
os.chmod(root / "pkg", 0o555)
policy_hash = sha(root / "pipeline-policy.snapshot.json")
evidence_hash = sha(root / "EVIDENCE_HASHES.json")
report_hashes = {1: sha(before_report), 2: sha(final_report)}

def make_critic(round_no, role):
    result = copy.deepcopy(critic_fixture)
    finding_id = "F1" if round_no == 1 else "F2"
    result.update({
        "audit_id": "e2e-1", "invocation_id": critic_id(round_no, role),
        "round": round_no, "role": f"critic_{role}",
        "report_sha256": report_hashes[round_no],
        "covered_claim_ids": ["F1", "F2"] if round_no == 1 else ["F2"],
        "conflicts": [], "new_issues": [],
        "settled_spot_checks": [] if round_no == 1 else [{
            "fact_id": "F1", "status": "passed",
            "evidence": "Rechecked the bound primary evidence hash.",
        }],
    })
    result["input_hashes"] = {
        "snapshot_sha256": input_sha(round_no),
        "report_sha256": report_hashes[round_no],
        "prompt_sha256": sha(skill / f"prompts/role_{role}.txt"),
        "policy_sha256": policy_hash,
        "evidence_manifest_sha256": evidence_hash,
    }
    result["coverage"] = {
        "assigned_checks": [f"critic_{role}"], "completed_checks": [f"critic_{role}"],
        "skipped_checks": [],
    }
    item = copy.deepcopy(critic_fixture["items"][0])
    item.update({
        "item_id": f"{role}-{finding_id}", "finding_id": finding_id,
        "claim": finding["claim"] if finding_id == "F1" else excluded["claim"],
        "evidence": {
            "artifact_refs": [{"path": "work/check_primary.txt", "sha256": sha(source)}],
            "operation": "compare the bounded rerun with the printed value",
            "result": "The exact comparison reproduces the discrepancy.",
            "reproducible_script": {"path": "work/check_primary.py", "sha256": sha(snippet)},
        },
    })
    if round_no == 2:
        item["evidence"] = {
            "artifact_refs": [{"path": "work/draft_review.txt", "sha256": sha(draft_evidence)}],
            "operation": "check the final ledger disposition against the integrated report",
            "result": "The superseded wording remains excluded.",
            "reproducible_script": None,
        }
    result["items"] = [item]
    if round_no == 1:
        draft_item = copy.deepcopy(item)
        draft_item.update({
            "item_id": f"{role}-F2", "finding_id": "F2",
            "claim": before_excluded["claim"],
            "evidence": {
                "artifact_refs": [{"path": "work/draft_review.txt",
                                   "sha256": sha(draft_evidence)}],
                "operation": "review the draft wording before integration",
                "result": "The draft wording is explicitly accounted for as superseded.",
                "reproducible_script": None,
            },
        })
        result["items"].append(draft_item)
    result["overall"] = {
        "publishable": True, "blocking_count": 0,
        "summary": "Synthetic critic completed every assigned check.",
    }
    output = root / f"critique/round_{round_no}/critic_{role}.json"
    write_json(output, result)
    return output, result

critics = {}
for round_no, roles in ((1, ("stats", "data", "fairness", "completeness")),
                        (2, ("fairness", "completeness"))):
    for role in roles:
        critics[(round_no, role)] = make_critic(round_no, role)

static_roles = [
    ("coordinator", "coordinator", "sonnet-5", "anthropic", "claude"),
    ("auditor_reproduction", "auditor", "sonnet-5", "anthropic", "claude"),
    ("auditor_data_integrity", "auditor", "sonnet-5", "anthropic", "claude"),
    ("auditor_text_vs_data", "auditor", "sonnet-5", "anthropic", "claude"),
    ("auditor_code_methods", "auditor", "sonnet-5", "anthropic", "claude"),
    ("auditor_privacy_archive", "auditor", "sonnet-5", "anthropic", "claude"),
    ("model_probe", "other", "gpt-5.6-terra", "openai", "gpt"),
]
phase_one_reports = {
    "auditor_reproduction": "report/01_reproduction.md",
    "auditor_data_integrity": "report/02_data_integrity.md",
    "auditor_text_vs_data": "report/03_text_vs_data.md",
    "auditor_code_methods": "report/04_code_methods.md",
    "auditor_privacy_archive": "report/05_privacy_archive.md",
}
ledger = []

def invocation(inv_id, role, role_class, round_no, requested, provider, family,
               output, in_hash="a" * 64, prompt_hash="b" * 64,
               cache="write", started="2026-01-01T09:00:00Z",
               ended="2026-01-01T09:01:00Z"):
    relative = output.relative_to(root).as_posix()
    observed = ("claude-sonnet-5-20260801" if requested == "sonnet-5" else
                "claude-opus-5-20260801" if provider == "anthropic" else
                "gpt-5.6-terra-2026-08-01")
    ledger.append({
        "invocation_id": inv_id, "role": role, "role_class": role_class,
        "round": round_no, "attempt": 1, "status": "completed", "provider": provider,
        "provider_call": True, "cache_usage": cache, "session_id": f"e2e-session-{len(ledger)+1}",
        "requested_model": requested, "observed_model": observed, "model_family": family,
        "model_source": "observed",
        "reasoning_effort": ("max" if role_class in {"coordinator", "auditor", "adjudicator"}
                              or role == "critic_synth" else "high"),
        "started_at": started, "ended_at": ended, "elapsed_seconds": 60,
        "input_artifact_sha256": in_hash, "prompt_sha256": prompt_hash,
        "output_artifact": relative, "output_sha256": sha(output),
        "usage": {"obtainable": True, "input_tokens": 10, "output_tokens": 5,
                  "cached_input_tokens": 0, "reasoning_tokens": 1, "total_tokens": 16,
                  "source": "api_response"},
        "error": None, "error_class": None, "legacy_source": None,
    })

for index, (role, role_class, requested, provider, family) in enumerate(static_roles, 1):
    relative = phase_one_reports.get(role)
    output = root / relative if relative else root / f"work/{role}.json"
    if relative:
        write(output, f"# {role}\n\nComplete canonical Phase-1 record.\n")
        auditor_index = list(phase_one_reports).index(role)
        started = f"2026-01-01T09:00:0{auditor_index}Z"
        ended = f"2026-01-01T09:01:0{auditor_index}Z"
    else:
        write_json(output, {"role": role, "status": "complete"})
        started, ended = "2026-01-01T09:00:00Z", "2026-01-01T09:01:00Z"
    invocation(f"e2e-static-{index}", role, role_class, 0, requested, provider, family,
               output, cache="probe_cache" if role == "model_probe" else "write",
               started=started, ended=ended)
for (round_no, role), (output, result) in critics.items():
    invocation(result["invocation_id"], f"critic_{role}", "critic", round_no,
               "gpt-5.6-terra", "openai", "gpt", output,
               in_hash=result["input_hashes"]["snapshot_sha256"],
               prompt_hash=result["input_hashes"]["prompt_sha256"],
               started="2026-01-01T10:05:00Z" if round_no == 1 else "2026-01-01T12:06:00Z",
               ended="2026-01-01T10:06:00Z" if round_no == 1 else "2026-01-01T12:07:00Z")

finding["verifications"] = [
    {"reviewer_id": "auditor_reproduction", "invocation_id": "e2e-static-2",
     "evidence_record": {"path": "report/01_reproduction.md",
                         "sha256": sha(root / "report/01_reproduction.md")},
     "model_family": "claude", "outcome": "confirmed"},
    {"reviewer_id": "critic_stats", "invocation_id": critic_id(1, "stats"),
     "evidence_record": {"path": "critique/round_1/critic_stats.json",
                         "sha256": sha(root / "critique/round_1/critic_stats.json")},
     "model_family": "gpt", "outcome": "confirmed"},
]
# Canonical report projection is deterministic; iterate the report-hash binding to
# its fixed point after replacing placeholder verification records with real ones.
for _ in range(3):
    finding["settled"]["valid_for_report_hash"] = sha(final_report)
    write_registries()
    subprocess.run([sys.executable, str(scripts / "generate_finding_artifacts.py"),
                    str(root), "--apply"], check=True, stdout=subprocess.DEVNULL)
    if finding["settled"]["valid_for_report_hash"] == sha(final_report):
        break
else:
    raise RuntimeError("canonical report projection did not reach a stable hash")
report_hashes[2] = sha(final_report)
# Round-2 critic identities were created before the canonical Appendix existed;
# rebuild those results against the stable report, then bind the ledger to all bytes.
for role in ("fairness", "completeness"):
    critics[(2, role)] = make_critic(2, role)
for row in ledger:
    if row["round"] == 2 and row["role"].startswith("critic_"):
        role = row["role"].removeprefix("critic_")
        output, result = critics[(2, role)]
        row.update({"input_artifact_sha256": result["input_hashes"]["snapshot_sha256"],
                    "prompt_sha256": result["input_hashes"]["prompt_sha256"],
                    "output_sha256": sha(output)})
    else:
        row["output_sha256"] = sha(root / row["output_artifact"])
write(root / "invocations.jsonl", "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger))

def seed_round(round_no, roles, report, diff=None):
    round_dir = root / f"critique/round_{round_no}"
    in_hash = input_sha(round_no); report_hash = sha(report)
    snapshot = round_dir / "snapshots" / in_hash; source_dir = snapshot / "source"
    write(source_dir / "REPORT_TO_REVIEW.md", report.read_text())
    for relative, origin in (
        ("context/PIPELINE_POLICY.json", root / "pipeline-policy.snapshot.json"),
        ("context/EVIDENCE_HASHES.json", root / "EVIDENCE_HASHES.json"),
        ("context/work/findings.jsonl", root / ("work/findings_round_1.jsonl" if round_no == 1 else "work/findings.jsonl")),
    ):
        target = source_dir / relative; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(origin.read_bytes())
    for relative in ("work/check_primary.txt", "work/check_primary.py", "work/draft_review.txt"):
        target = snapshot / relative; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / relative).read_bytes())
    if round_no > 1:
        for name in ("SETTLED.json", "SETTLED.md"):
            target = source_dir / "context" / name; target.write_bytes((root / name).read_bytes())
    for role in roles:
        target = snapshot / f"rendered/prompt_{role}.txt"; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((skill / f"prompts/role_{role}.txt").read_bytes())
    diff_target = None
    if diff is not None:
        diff_target = source_dir / "context/DIFF_MANIFEST.json"
        diff_target.write_bytes(diff.read_bytes())
    assignments = {role: ([] if round_no == 1 else ["F2"]) for role in roles}
    routing = {"routing_version": "i4r-round-routing-1", "round": round_no,
               "roles": roles, "reason": "current v2 self-test route",
               "diff_manifest_present": diff_target is not None,
               "diff_manifest_sha256": sha(diff_target) if diff_target else None,
               "claim_assignment_mode": "unscoped_round_1" if round_no == 1 else "exact_changed_claims",
               "claim_assignments": assignments}
    write_json(source_dir / "ROUTING.json", routing)
    files = {p.relative_to(snapshot).as_posix(): sha(p)
             for p in sorted(snapshot.rglob("*")) if p.is_file()}
    write_json(snapshot / "MANIFEST.json", {
        "snapshot_version": "i4r-runtime-snapshot-3", "input_sha256": in_hash,
        "report_sha256": report_hash, "round": round_no, "model": "gpt-5.6-terra",
        "effort": "high", "roles": roles, "audit_id": "e2e-1", "files_sha256": files,
    })
    invocation_name = f"selftest-current-round-{round_no}"
    projection = round_dir / "projections" / invocation_name
    write_json(projection / "ROUTING.json", routing)
    result_rows = []
    claim_assessments = {}
    critic_outputs = []
    for role in roles:
        original = root / f"critique/round_{round_no}/critic_{role}.json"
        copied = projection / f"critic_{role}.json"; copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(original.read_bytes())
        result = json.loads(copied.read_text())
        result_rows.append((role, result))
        claim_assessments[role] = [item["finding_id"] for item in result["items"]]
        markdown = projection / f"critic_{role}.md"
        write(markdown, f"# critic_{role}\n\nSynthetic complete projection.\n\n<!-- I4R_CRITIC_COMPLETE -->\n")
        sidecar = projection / f"{role}.complete.json"
        write_json(sidecar, {
            "completion_version": "i4r-runtime-completion-2", "audit_id": "e2e-1",
            "input_sha256": in_hash, "report_sha256": report_hash, "round": round_no,
            "role": role, "attempts": 1, "completion_marker": True,
            "output_sha256": sha(copied), "markdown_sha256": sha(markdown),
        })
        critic_outputs.append({
            "role": f"critic_{role}",
            "json_path": copied.relative_to(root).as_posix(), "json_sha256": sha(copied),
            "markdown_path": markdown.relative_to(root).as_posix(),
            "markdown_sha256": sha(markdown),
            "completion_path": sidecar.relative_to(root).as_posix(),
            "completion_sha256": sha(sidecar),
        })
    merge = critique_runtime.build_claim_merge(
        result_rows,
        critique_runtime.load_registry_file(source_dir / "context/work/findings.jsonl"),
        [], audit_id="e2e-1", round_number=round_no,
        input_sha=in_hash, report_sha=report_hash,
    )
    merge_path = projection / "CLAIM_MERGE.json"
    write_json(merge_path, merge)
    write_json(projection / "adjudication_packet.json", {
        "packet_version": "i4r-targeted-adjudication-packet-2", "audit_id": "e2e-1",
        "round": round_no, "input_sha256": in_hash, "report_sha256": report_hash,
        "policy": "auto", "triggers": [], "claim_merge_sha256": sha(merge_path),
        "unresolved_rows": [], "critic_outputs": critic_outputs,
    })
    projection_files = {p.relative_to(projection).as_posix(): sha(p)
                        for p in sorted(projection.rglob("*")) if p.is_file()}
    write_json(projection / "MANIFEST.json", {
        "projection_version": "i4r-round-projection-1", "invocation_id": invocation_name,
        "audit_id": "e2e-1", "round": round_no, "input_sha256": in_hash,
        "report_sha256": report_hash, "roles": roles, "adjudication": "no_triggers",
        "target_input_sha256": None, "claim_assessments": claim_assessments,
        "rereviewed_claim_ids": list(dict.fromkeys(
            claim_id for role in roles for claim_id in claim_assessments[role])),
        "claim_merge_sha256": sha(merge_path), "files_sha256": projection_files,
    })
    write(round_dir / "CURRENT_PROJECTION", invocation_name + "\n")
    write_json(round_dir / "ROUND_STATUS.json", {
        "status_version": "i4r-round-status-2", "audit_id": "e2e-1",
        "invocation_id": invocation_name, "input_sha256": in_hash,
        "report_sha256": report_hash, "round": round_no, "outcome": "complete",
        "exit_code": 0, "expected_roles": roles, "completed_roles": roles,
        "failed_roles": [], "quorum_met": True,
    })

seed_round(1, ["stats", "data", "fairness", "completeness"], before_report)
event_env = os.environ.copy(); event_env["RUN_JSON"] = str(root / "run.json")
subprocess.run(["bash", str(scripts / "log_event.sh"), "settled", "used=true",
                "generated_after_round=1", "path=SETTLED.md", "entries=1",
                "spot_check_failures=0"], check=True, env=event_env,
               stdout=subprocess.DEVNULL)
subprocess.run(["bash", str(scripts / "make_settled.sh"), str(root)], check=True,
               stdout=subprocess.DEVNULL)
diff = root / "critique/DIFF_MANIFEST_round_2.json"
diff_command = [sys.executable, str(scripts / "make_diff_packet.py"), str(root),
                "--round", "2", "--before-report", str(before_report),
                "--after-report", str(final_report), "--before-findings",
                str(root / "work/findings_round_1.jsonl"), "--after-findings",
                str(root / "work/findings.jsonl"), "--output", str(diff)]
subprocess.run(diff_command, check=True, stdout=subprocess.DEVNULL)
packet = json.loads(diff.read_text())
if packet["changed_claim_ids"] != ["F2"] or packet["recommended_roles"] != ["fairness"]:
    raise RuntimeError(f"unexpected generated diff route: {packet}")
if [row["fact_id"] for row in packet["settled_fact_references"]] != ["F1"]:
    raise RuntimeError("generated diff omitted the F1 settled dependency")
seed_round(2, ["fairness", "completeness"], final_report, diff)
subprocess.run([sys.executable, str(scripts / "run_post_delta_gates.py"), str(root),
                "--round", "2", "--report", str(final_report)], check=True,
               stdout=subprocess.DEVNULL)
PYCURRENTV2
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  FAIL: could not build the current generated v2 round fixture"; fails=1
  fi
  rebind_nb "$T/g"
  "$SELF" "$T/g" --stopping-reason target_complete --registry "$T/reg" > "$T/out8.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  FAIL: the end-to-end init->log->finalize path exited $rc"; cat "$T/out8.txt"; fails=1
  else echo "  ok: end-to-end init -> log_event -> finalize validates clean"; fi
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); print(o["compliance"]["compliance_pass"])' "$T/g/run.json" 2>/dev/null)
  if [ "$n" != "True" ]; then
    echo "  FAIL: a fully compliant synthetic run reported compliance_pass=$n"; sed -n '/VIOLATION/p' "$T/out8.txt"; fails=1
  else echo "  ok: a compliant run reports compliance_pass true (the vector is not always-false)"; fi

  # NEGATIVE CONTROLS: five completed rows are not evidence of a parallel fan-out unless
  # all five intervals overlap and each result is bound to its canonical role report.
  cp "$T/g/invocations.jsonl" "$T/g/invocations.phase1-good.jsonl"
  python3 - "$T/g/invocations.jsonl" <<'PYSEQUENTIAL'
import json, pathlib, sys
path=pathlib.Path(sys.argv[1]); rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
auditors=sorted((row for row in rows if row.get("role_class") == "auditor"), key=lambda row: row["role"])
assert len(auditors) == 5
for index,row in enumerate(auditors):
    row["started_at"]=f"2026-01-01T09:{index * 2:02d}:00Z"
    row["ended_at"]=f"2026-01-01T09:{index * 2 + 1:02d}:00Z"
    row["elapsed_seconds"]=60
path.write_text("".join(json.dumps(row,sort_keys=True)+"\n" for row in rows))
PYSEQUENTIAL
  python3 "$HERE/summarize_invocations.py" "$T/g" --check \
      --json "$T/sequential.json" --markdown "$T/sequential.md" > "$T/sequential.out" 2>&1
  rc=$?
  grep -q "Phase-1 five-way concurrency has no positive common overlap" "$T/sequential.out"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: five sequential auditor invocations passed the Phase-1 concurrency gate"; fails=1
  else echo "  ok: sequential auditor invocations cannot masquerade as a five-way fan-out"; fi
  mv "$T/g/invocations.phase1-good.jsonl" "$T/g/invocations.jsonl"

  cp "$T/g/invocations.jsonl" "$T/g/invocations.phase1-good.jsonl"
  python3 - "$T/g" <<'PYMISROUTED'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); path=root/"invocations.jsonl"
rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
row=next(item for item in rows if item.get("role") == "auditor_data_integrity")
row["output_artifact"]="report/01_reproduction.md"
row["output_sha256"]=hashlib.sha256((root/row["output_artifact"]).read_bytes()).hexdigest()
path.write_text("".join(json.dumps(item,sort_keys=True)+"\n" for item in rows))
PYMISROUTED
  python3 "$HERE/summarize_invocations.py" "$T/g" --check \
      --json "$T/misrouted.json" --markdown "$T/misrouted.md" > "$T/misrouted.out" 2>&1
  rc=$?
  grep -q "expected canonical output_artifact 'report/02_data_integrity.md'" "$T/misrouted.out"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: a shared/misrouted auditor output passed the Phase-1 routing gate"; fails=1
  else echo "  ok: every Phase-1 auditor is bound to its canonical role-specific report"; fi
  mv "$T/g/invocations.phase1-good.jsonl" "$T/g/invocations.jsonl"

  n=$(wc -l < "$T/reg/index.jsonl" | tr -d ' ')
  if [ "$n" != "2" ]; then echo "  FAIL: index should now hold 2 audits, holds $n"; fails=1
  else echo "  ok: index accumulates across audits (2 rows)"; fi

  # 14. the 4b gate must BLOCK missing and stub notebooks; no environment waiver may bypass it.
  mkdir -p "$T/v"; cp "$S/EXAMPLE_run.json" "$T/v/run.json"
  cp "$S/EXAMPLE_comment.json" "$T/v/comment.json"          # deliberately NO notebook
  "$SELF" "$T/v" --no-registry > "$T/out14.txt" 2>&1
  if [ $? -eq 0 ]; then
    echo "  FAIL: a pair with no verification.Rmd was accepted; the 4b gate is advisory"; fails=1
  else echo "  ok: the verification gate blocks a pair with no notebook (non-zero exit)"; fi

  # plant the marker make_verification_notebook.sh ACTUALLY emits, not an invented one -- the first
  # version of this case planted a string the generator never writes, so it tested nothing
  nb "$T/v"; printf '## TODO -- STUB. This claim has no check yet. It is NOT verified.\n' >> "$T/v/verification.Rmd"
  "$SELF" "$T/v" --no-registry > "$T/out14b.txt" 2>&1
  if [ $? -eq 0 ]; then
    echo "  FAIL: a notebook carrying a TODO stub was accepted as complete"; fails=1
  else echo "  ok: an unfinished stub fails the gate"; fi

  rm -f "$T/v/verification.Rmd" "$T/v/verification_status.txt"
  ALLOW_NO_VERIFICATION=1 "$SELF" "$T/v" --no-registry > "$T/out14c.txt" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "  FAIL: ALLOW_NO_VERIFICATION=1 bypassed the mandatory verification gate"; fails=1
  else echo "  ok: the obsolete ALLOW_NO_VERIFICATION variable cannot bypass verification"; fi

  # Post-delta fixtures deliberately contain read-only immutable snapshots and
  # a read-only package tree. Restore owner write permission only for cleanup.
  chmod -R u+w "$T" 2>/dev/null || true
  rm -rf "$T"
  if [ "$fails" -ne 0 ]; then echo "SELF-TEST FAILED"; return 1; fi
  echo "SELF-TEST PASSED"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
main "$@"
exit $?
