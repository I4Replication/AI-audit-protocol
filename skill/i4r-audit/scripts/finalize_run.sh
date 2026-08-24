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
#   --stopping-reason R       converged | cap | aborted. Required if run.json does not have one:
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
DEFAULT_REGISTRY="${REGISTRY_DIR:-/Users/example/Library/CloudStorage/Dropbox-I4R/Discipline_tree/registry}"

die () { echo "FATAL: finalize_run.sh: $*" >&2; exit 2; }
now_utc () { date -u +%Y-%m-%dT%H:%M:%SZ; }

# =====================================================================================
# The validator. Draft 2020-12, the subset these two schemas use: type, required,
# properties, additionalProperties, items, $ref into $defs, enum, const, pattern,
# minimum/maximum, minLength, minItems, and a light format check for date/date-time.
#
# It ships with its own controls (--controls), which finalize runs BEFORE touching any
# real file. A validator that accepts everything passes every document; the only way to
# know it is working is to hand it documents it must reject.
# =====================================================================================
VALIDATOR="$HERE/.validate_json.py"

write_validator () {
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
  # $1 run.json  $2 pkg dir (may not exist)  $3 stopping reason ("" to keep)  $4 detail
  python3 - "$1" "$2" "$3" "$4" <<'PYD'
import hashlib, json, pathlib, sys, datetime

run_p, pkg_p, stop, detail = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
R = json.loads(pathlib.Path(run_p).read_text())

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

# ---- token and cost coverage: always reported WITH its denominator
expected = len(R.get("model_assignments", []))
with_tok = sum(1 for t in R.get("tokens", []) if t.get("total_tokens") is not None)
tot_tok = sum(t["total_tokens"] for t in R.get("tokens", []) if t.get("total_tokens") is not None)
tot_usd = sum(c["amount"] for c in R.get("costs", []) if c.get("amount") is not None)
R["token_accounting"] = {
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
    lines, n = [], 0
    for f in sorted(p.rglob("*")):
        if f.is_file() and not f.is_symlink():
            try: b = f.read_bytes()
            except OSError: continue
            n += 1
            lines.append(hashlib.sha256(b).hexdigest() + "  " + f.relative_to(p).as_posix())
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
critics = [m for m in ma if m.get("role_class") in ("critic", "synthesizer")]
auditors = {m["role"] for m in ma if m.get("role_class") == "auditor"}
delivered = (R.get("delivery") or {}).get("delivered_at")
viol = []

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

all_five = (len(auditors) >= 5) if ma else None
if all_five is False:
    viol.append("fan-out ran %d auditor role(s), not 5" % len(auditors))

crit_indep = None
if critics:
    crit_indep = all(m.get("differs_from_auditor_family") for m in critics)
    for m in critics:
        if not m.get("differs_from_auditor_family"):
            viol.append("round %s: %s ran on the auditor family (%s) -- that review was not "
                        "adversarial" % (m.get("round"), m.get("role"), m.get("model_family")))

no_swap = None
if ma:
    swaps = [m for m in ma if m.get("replaces_model_id") and m.get("intended") is False]
    no_swap = not swaps
    for m in swaps:
        viol.append("role %s changed model from %s to %s mid-run without an intended change"
                    % (m.get("role"), m.get("replaces_model_id"), m.get("model_id")))

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
if R.get("stopping_reason") == "cap" and not R.get("unreviewed_sections"):
    warn.append("stopped on the cap but unreviewed_sections is empty -- SKILL.md requires "
                "listing the sections no critic has reviewed")

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
_pos = ["ABC_XY_sub_E_999.mat", "data/P999.csv", "sub_999", "participant 999", "CYC_P999"]
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

file_to_registry () {
  # $1 registry root  $2 run.json  $3 comment.json
  python3 - "$1" "$2" "$3" <<'PYR'
import json, pathlib, shutil, sys, datetime
reg = pathlib.Path(sys.argv[1]); runp = pathlib.Path(sys.argv[2]); comp = pathlib.Path(sys.argv[3])
R = json.loads(runp.read_text()); C = json.loads(comp.read_text())
aid = R["audit_id"]
d = reg / aid
d.mkdir(parents=True, exist_ok=True)
shutil.copyfile(runp, d / "run.json")
shutil.copyfile(comp, d / "comment.json")
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
idx.write_text("\n".join(keep) + "\n")

# record where it went, in the registry copy and in the working copy
for target in (d / "run.json", runp):
    obj = json.loads(target.read_text())
    obj["registry"] = {"registry_dir": str(d), "filed_at": now, "index_row_appended": True}
    target.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
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
# --no-verification exists because a gate nobody can bypass gets bypassed by not running the
# script. It records the waiver IN the run record rather than letting the absence go unremarked.
verification_gate () {
  local DIRN="$1" COMMENT="$2" NB="" STATUS=""
  for c in "$DIRN/verification.Rmd" "$DIRN/report/verification.Rmd" \
           "$DIRN/verification/verification.Rmd"; do
    [ -f "$c" ] && { NB="$c"; break; }
  done
  if [ -z "$NB" ]; then
    if [ "${ALLOW_NO_VERIFICATION:-0}" = "1" ]; then
      echo "  WAIVED: no verification.Rmd. The waiver is recorded in run.json; the comment's"
      echo "          numbers are not independently re-checkable by anyone who did not run this"
      echo "          audit, and no artefact will exist once this working directory is deleted."
      python3 - "$1/run.json" <<'PYW'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); r = json.loads(p.read_text())
n = (r.get("notes") or "").rstrip()
msg = ("Filed with ALLOW_NO_VERIFICATION=1: no verification notebook was produced, so no claim in "
       "this comment has a re-runnable check. Regenerating one later requires REPORT_final.md, "
       "which lives only in the working directory.")
if msg not in n:
    r["notes"] = (n + "\n\n" + msg).strip()
    p.write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
PYW
      return 0
    fi
    echo "  FAIL: no verification.Rmd under $DIRN. Every numerical claim in the comment must be"
    echo "        re-checkable by a human who did not run the audit -- all of them, not only the"
    echo "        conclusion-changing ones. Run:"
    echo "            bash scripts/make_verification_notebook.sh $DIRN"
    echo "        then write the check bodies into verification_snippets/ until the stub count is"
    echo "        zero. To file without one anyway, set ALLOW_NO_VERIFICATION=1; the waiver is"
    echo "        written into run.json so the gap is on the record rather than invisible."
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

  # The knit's own exit status, if it was run. Absent is a warning; failed is fatal.
  for c in "$DIRN/verification_status.txt" "$DIRN/report/verification_status.txt"; do
    [ -f "$c" ] && { STATUS="$c"; break; }
  done
  if [ -z "$STATUS" ]; then
    echo "  WARN: no verification_status.txt -- the notebook exists but there is no evidence it was"
    echo "        ever knitted. Render it and let the exit status propagate."
  elif [ "$(tr -d '[:space:]' < "$STATUS")" != "0" ]; then
    echo "  FAIL: the last knit exited $(tr -d '[:space:]' < "$STATUS"): at least one claim did not"
    echo "        reproduce. Resolve or reclassify it before filing."
    return 1
  else
    echo "  ok: the notebook knitted with every claim reproducing"
  fi
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

  local RUN DIRN
  if [ -d "$TARGET" ]; then RUN="$TARGET/run.json"; else RUN="$TARGET"; fi
  [ -f "$RUN" ] || die "$RUN not found"
  DIRN=$(dirname "$RUN")
  [ -n "$COMMENT" ] || COMMENT="$DIRN/comment.json"
  [ -n "$PKGD" ] || PKGD="$DIRN/pkg"
  [ -f "$SCHEMAS/run.schema.json" ] || die "no run.schema.json under $SCHEMAS"
  [ -f "$SCHEMAS/comment.schema.json" ] || die "no comment.schema.json under $SCHEMAS"
  if [ ! -f "$COMMENT" ]; then
    die "$COMMENT not found. finalize validates the PAIR: the process record is only half the
       registry entry, and a run with no comment record cannot be pooled by the meta-paper."
  fi

  local existing_stop
  existing_stop=$(jq -r '.stopping_reason // ""' "$RUN")
  if [ -z "$STOP" ] && [ -z "$existing_stop" ]; then
    die "no stopping reason. Pass --stopping-reason converged|cap|aborted.
       It cannot be inferred: stopping at the round cap and converging produce identical
       round counts and mean opposite things."
  fi
  case "${STOP:-$existing_stop}" in
    converged|cap|aborted) ;;
    *) die "stopping reason must be converged, cap or aborted (got '${STOP:-$existing_stop}')" ;;
  esac

  local fail=0 rc out

  echo "== 0. validator self-test =="
  write_validator; if [ $? -ne 0 ]; then die "could not write the validator"; fi
  python3 "$VALIDATOR" --controls
  if [ $? -ne 0 ]; then
    echo "ABORTING: the validator cannot be trusted, so neither can a clean result from it." >&2
    rm -f "$VALIDATOR"; exit 1
  fi

  echo "== 1. deriving run.json =="
  out=$(derive_run "$RUN" "$PKGD" "$STOP" "$DETAIL")
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
  python3 "$VALIDATOR" "$COMMENT" "$SCHEMAS/comment.schema.json"
  if [ $? -ne 0 ]; then fail=1; fi

  echo "== 4. cross-checking the pair =="
  cross_check "$RUN" "$COMMENT"
  if [ $? -ne 0 ]; then fail=1; fi

  echo "== 4b. verification notebook =="
  verification_gate "$DIRN" "$COMMENT"
  if [ $? -ne 0 ]; then fail=1; fi

  if [ "$fail" -ne 0 ]; then
    echo ""
    echo "FINALIZE FAILED — nothing was filed to the registry. Fix the errors above and re-run."
    rm -f "$VALIDATOR"
    exit 1
  fi

  if [ "$NOREG" -eq 1 ]; then
    echo "== 5. registry: skipped (--no-registry) =="
  else
    echo "== 5. filing into the registry =="
    mkdir -p "$REGISTRY"
    if [ $? -ne 0 ]; then echo "  FAIL: cannot create $REGISTRY"; rm -f "$VALIDATOR"; exit 1; fi
    file_to_registry "$REGISTRY" "$RUN" "$COMMENT"
    if [ $? -ne 0 ]; then echo "  FAIL: filing failed"; rm -f "$VALIDATOR"; exit 1; fi
  fi

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

  # Every fixture gets a minimal passing notebook so the cases below keep testing what they were
  # written to test rather than all failing at the new 4b gate. The gate itself is case 14.
  nb () { printf 'expect("C-1", n = 1)\ncheck("C-1", n = 1)\n' > "$1/verification.Rmd"
          echo 0 > "$1/verification_status.txt"; }

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
  mkdir -p "$T/h"; cp "$S/EXAMPLE_run.json" "$T/h/run.json"; nb "$T/h"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["enumeration_scheme"]="PLACEHOLDER-v0"
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/h/comment.json"
  "$SELF" "$T/h" --no-registry > "$T/out9.txt" 2>&1
  grep -qi "comparable with scheme-coded records" "$T/out9.txt"; grc=$?
  if [ "$grc" -ne 0 ]; then
    echo "  FAIL: a PLACEHOLDER-v0 record was not warned about"; fails=1
  else echo "  ok: PLACEHOLDER-v0 still warns about non-comparability"; fi

  # 4g. NEGATIVE CONTROL: Rule 2c -- 'unverifiable' with no cause is not a finding
  mkdir -p "$T/i"; cp "$S/EXAMPLE_run.json" "$T/i/run.json"; nb "$T/i"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
for x in o["findings"]:
    if x["impact"] == "unverifiable": x["unverifiable_reason"] = None
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/i/comment.json"
  "$SELF" "$T/i" --no-registry > "$T/out10.txt" 2>&1
  rc=$?
  grep -q "no unverifiable_reason" "$T/out10.txt"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: an 'unverifiable' finding with no recorded cause was accepted (exit $rc)"; fails=1
  else echo "  ok: Rule 2c enforced -- unverifiable with no cause blocks finalize"; fi

  # 4h. NEGATIVE CONTROL: a role cannot both find independently and agree after exposure
  mkdir -p "$T/j"; cp "$S/EXAMPLE_run.json" "$T/j/run.json"; nb "$T/j"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
p=o["findings"][0]["provenance"]; p["agreed_after_seeing"]=p["independently_found_by"][:1]
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/j/comment.json"
  "$SELF" "$T/j" --no-registry > "$T/out11.txt" 2>&1
  rc=$?
  grep -q "cannot be both" "$T/out11.txt"; grc=$?
  if [ "$rc" -eq 0 ] || [ "$grc" -ne 0 ]; then
    echo "  FAIL: a role counted as both independent finder and after-the-fact agreer (exit $rc)"; fails=1
  else echo "  ok: manufactured corroboration blocked"; fi

  # 4i. the package_locus quasi-identifier warning fires on a participant-shaped path
  mkdir -p "$T/k"; cp "$S/EXAMPLE_run.json" "$T/k/run.json"; nb "$T/k"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
o["findings"][0]["package_locus"]=["data/CYC_P07_session.csv"]
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/k/comment.json"
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
  if [ "$n" != "675/5/2" ]; then
    echo "  FAIL: human effort / settled facts = '$n', expected '675/5/2'"; fails=1
  else echo "  ok: person-minutes totalled over 5 of 5 phases; SETTLED.md round recorded"; fi

  # 5. NEGATIVE CONTROL: an invalid comment.json must block, and must not file
  mkdir -p "$T/b"; cp "$S/EXAMPLE_run.json" "$T/b/run.json"; nb "$T/b"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["findings"][0]["remedy_class"]="magic"
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/b/comment.json"
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
  mkdir -p "$T/d"; cp "$S/EXAMPLE_run.json" "$T/d/run.json"; nb "$T/d"
  python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); o["audit_id"]="a-different-audit"
json.dump(o, open(sys.argv[2],"w"))' "$S/EXAMPLE_comment.json" "$T/d/comment.json"
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
  mkdir -p "$T/f"; cp "$S/EXAMPLE_run.json" "$T/f/run.json"; nb "$T/f"
  "$SELF" "$T/f" --no-registry > "$T/out7.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 2 ]; then echo "  FAIL: a missing comment.json should refuse with exit 2, got $rc"; fails=1
  else echo "  ok: a missing comment.json is refused (exit 2)"; fi

  # 10. the end-to-end path: init -> log_event -> finalize on a fresh audit
  mkdir -p "$T/g/pkg"; printf 'a,b\n1,2\n' > "$T/g/pkg/d.csv"; nb "$T/g"; nb "$T/f"; nb "$T/e"; nb "$T/d"; nb "$T/c"; nb "$T/b"; nb "$T/k"; nb "$T/j"; nb "$T/i"; nb "$T/h"
  bash "$HERE/init_run.sh" "$T/g" --audit-id e2e-1 --doi 10.5555/e2e \
       --auditor-model claude-x --auditor-family claude --auditor-provider anthropic \
       --protocol-version p --gate-set-version g > /dev/null 2>&1
  rc=$?
  export RUN_JSON="$T/g/run.json"
  bash "$HERE/log_event.sh" phase_end phase=intake > /dev/null 2>&1
  for ph in fan_out adjudication critique release; do
    bash "$HERE/log_event.sh" phase_start phase=$ph > /dev/null 2>&1
    bash "$HERE/log_event.sh" phase_end   phase=$ph > /dev/null 2>&1
  done
  bash "$HERE/log_event.sh" round_start round=1 at=2026-01-01T10:00:00Z > /dev/null 2>&1
  bash "$HERE/log_event.sh" round_end   round=1 at=2026-01-01T11:00:00Z > /dev/null 2>&1
  bash "$HERE/log_event.sh" round_result round=1 blocking_items=1 comment_defects=1 \
       paper_defects=0 integrated_at=2026-01-01T12:00:00Z consistency_check_run=true \
       consistency_check_exit=0 character=wording_only > /dev/null 2>&1
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
    "changes_conclusion":False,
    "provenance":{"origin_role":"auditor_text_vs_data",
      "independently_found_by":["auditor_text_vs_data","auditor_reproduction"]}}],
  "rollup":{"impact_max":"value_wrong","findings_total":1,
    "count_by_remedy_class":{"one_line_code":0,"one_number":1,"reanalysis":0,"not_fixable":0}},
  "timing":{"publication_date":"2025-01-01","audit_date":"2026-01-01"},
  "author_response":{"status":"not_notified"}}, open(p,"w"))
PYMIN
  "$SELF" "$T/g" --stopping-reason converged --registry "$T/reg" > "$T/out8.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  FAIL: the end-to-end init->log->finalize path exited $rc"; cat "$T/out8.txt"; fails=1
  else echo "  ok: end-to-end init -> log_event -> finalize validates clean"; fi
  n=$(python3 -c 'import json,sys
o=json.load(open(sys.argv[1])); print(o["compliance"]["compliance_pass"])' "$T/g/run.json" 2>/dev/null)
  if [ "$n" != "True" ]; then
    echo "  FAIL: a fully compliant synthetic run reported compliance_pass=$n"; sed -n '/VIOLATION/p' "$T/out8.txt"; fails=1
  else echo "  ok: a compliant run reports compliance_pass true (the vector is not always-false)"; fi
  n=$(wc -l < "$T/reg/index.jsonl" | tr -d ' ')
  if [ "$n" != "2" ]; then echo "  FAIL: index should now hold 2 audits, holds $n"; fails=1
  else echo "  ok: index accumulates across audits (2 rows)"; fi

  # 14. the 4b gate must BLOCK, stubs must fail, and the waiver must be recorded not silent
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
  if [ "$rc" -ne 0 ]; then
    echo "  FAIL: ALLOW_NO_VERIFICATION=1 did not permit filing (exit $rc)"; fails=1
  elif ! grep -q "ALLOW_NO_VERIFICATION=1" "$T/v/run.json"; then
    echo "  FAIL: the waiver was honoured but never written into run.json -- a silent gap"; fails=1
  else echo "  ok: the waiver is permitted AND recorded in run.json"; fi

  rm -rf "$T"
  if [ "$fails" -ne 0 ]; then echo "SELF-TEST FAILED"; return 1; fi
  echo "SELF-TEST PASSED"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
main "$@"
exit $?
