"""A runner-valid post-fan-out verifier record in any on-disk metadata-2.1 test audit (2.6.4).

The Neudorfer audit (20260923-audit-3309cfcf) excluded three true findings (F13-F15) because no
family-A confirmation could be made for a row raised after fan-out; the hand-made records tried
instead (``make_record.py``: a report, a result, an empty evidence manifest and a receipt, all
written by hand) were what the packaged verifier replaced. Since 2.6.4,
``verifier_contract.verify_record`` re-admits every verifier call through
``run_verifier.readmit_record``: the runner's exact receipt, the snapshot ``phase1/quorum.json``
binds, the frozen packet hashing to the receipt's ``packet_sha256`` with the runner's own prompt,
and a promoted tree that passes ``run_verifier.validate_verifier_output``. A test that writes those
files by hand is therefore refused, exactly as a forged record is. This module is the one way the
tests make a record the runner would have made, without running a provider.

    from tests import verifier_fixtures
    info = verifier_fixtures.record_verifier(audit, [claim_a, claim_b], ["CONFIRMED", "REFUTED"],
                                             finding_ids=["F13", "F14"],
                                             started_at=T0, ended_at=T1)

``record_verifier(audit, claims, verdicts=None, *, finding_ids=None, started_at=None,
ended_at=None, invocation_id=None, source_path=None, locator="Table 2; Results", family="claude",
receipt=True, relabel=None, legacy_correction=None, corrected_at=None, tamper=None,
validate_record=True) -> dict``

It works in a real Phase-1 audit (test_264_verifier_runtime) and in an audit with NO real Phase 1
(tests/test_diff_packet.py's fixture as tests/test_264_verifier_confirmation.py builds it, and
tests/test_release_pipeline.make_audit):

1. Snapshot. If ``phase1/quorum.json`` is absent it writes a minimal quorum (status
   ``quorum_complete``, the run.json audit_id, ``quorum_sha256`` over itself) and a snapshot
   manifest ``phase1/snapshots/<sha>/snapshot_manifest.json`` whose ``source_files`` are the
   audit's own ``paper/`` and ``pkg/`` files (``phase1_runtime.source_files``: path, sha256,
   bytes) with no common evidence, hashing to its own ``snapshot_sha256``. It copies no raw
   deposit, so it does not pass ``phase1_runtime.verify_snapshot``; re-admission deliberately
   never re-hashes the deposit. An audit with no readable ``paper/`` file (make_audit has none)
   first gets ``paper/paper.txt`` holding FIXTURE_PAPER_TEXT. An existing quorum is used as is.
2. Packet. The hypotheses are ordered by claim hash and numbered H1..Hn, each citing
   ``source_path`` (default: the first ``paper/`` file of the snapshot) at its snapshot hash; the
   packet is ``run_verifier.make_verifier_packet`` on the verifier route the audit's own
   ``pipeline-policy.snapshot.json`` froze (``audit_route``: Sonnet 5 for an audit initialised
   before 2.7.0, Sonnet 5.5 for one frozen from the 2.7.0 policy), its hash
   ``run_verifier.packet_contract_hash`` with ``build_verifier_prompt``, and it is frozen by
   ``run_verifier.materialize_verifier_packet`` under
   ``phase1/role_packets/<snapshot>/<role>/<packet_sha256>/``. The input hash is
   ``phase1_runtime.role_input_hash`` with the quorum's probe key (FIXTURE_PROBE_KEY for a
   minimal quorum).
3. Output. REPORT.md (the three headings, one ``### Hn: VERDICT`` block per hypothesis, a source
   join, the completion marker), result.json and evidence/manifest.json with one rerun artifact
   per hypothesis (UNVERIFIABLE gets a null rerun and a basis naming what the deposit lacks) are
   written to ``phase1/roles/<role>/<input>/attempt_01_<uuid>/output.partial/``, admitted by
   ``run_verifier.validate_verifier_output`` (the helper raises if its own output is not
   runner-valid), and promoted to ``work/verifier/<role>/<input>/``.
4. Journal. Through the real lifecycle API: ``reserve_invocation`` at round 0 with role_class
   ``verifier`` (its event time is ``started_at``), ``mark_provider_request``,
   ``mark_inference_start``, ``mark_response_received`` (the attempt's claude_stdout.json, at
   ``ended_at``) and ``record_terminal`` with a runner-shaped terminal record on that route
   (observed ``SERVED_BY_REQUESTED[requested_model]``, what tests/fake_claude_phase1.py serves it;
   ``model_identity_evidence`` bound to that stdout file) ending at ``ended_at``. The record is
   first checked with ``record_invocation.validate_record`` (canonical variant only).
5. Receipt. ``run_verifier._receipt`` writes the exact ``RECEIPT_KEYS`` receipt at
   ``phase1/completions/<role>/<input>.json``.

The malformed variants the negative tests need:

* ``receipt=False``: everything but the receipt (the lost-receipt / hand-made shape).
* ``tamper="result" | "report" | "evidence" | "evidence_artifact" | "packet" | "receipt"``: after
  recording, one byte is appended to that file (``receipt`` instead drops its
  ``source_bindings`` key, the hand-receipt shape).
* ``family="gpt"``: the call is journaled on the critic route the audit's snapshot froze
  (``critic_route``: openai / gpt-6.1-sol / high from the 2.7.0 policy, gpt-5.6-terra before; family
  gpt). Record validation is skipped for it.
* ``relabel=(role, role_class)``: the identical call journaled under a legacy name and class
  (the Neudorfer ``operator_verifier``/``other`` and ``auditor_operator_verification``/``auditor``
  shapes); record validation is skipped.
* ``legacy_correction="reason"``: the terminal row is then corrected to ``legacy_record`` through
  ``lifecycle_journal.correct_invocation`` at ``corrected_at`` (default ``ended_at``).

Returned keys: invocation_id, role (the journaled role), batch_role, role_class, family,
input_hash, packet_sha256, snapshot_sha256, claim_sha256s (packet order), hypotheses (the
receipt rows), output_dir, result, result_sha256, report, report_sha256, evidence,
evidence_manifest_sha256, receipt (audit-relative path, or None), attempt, started_at, ended_at;
plus the aliases ``hashes`` (= claim_sha256s) and ``result_sha`` (= result_sha256).

Every scripts module is resolved when the helper runs, never at import: several suites replace
sys.modules entries (phase1_runtime, run_phase1) while the discoverer imports them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import pathlib
import shutil
import sys
import uuid
from typing import Any, Sequence
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

FIXTURE_PROBE_KEY = hashlib.sha256(b"tests/verifier_fixtures.py minimal quorum probe key").hexdigest()
FIXTURE_PAPER_TEXT = (
    "Results. The abstract reports 24 participants; the participant table lists 22.\n"
    "Table 2. Mean bias (bpm): wrist 3.1; arm 0.8.\n"
)
SERVED = "claude-sonnet-5-20260801"
# ROUTES["claude"] is the Sonnet 5 route, and ROUTES["gpt"] the Terra critic route, every audit initialised
# before 2.7.0 froze. A record is journaled on the audit's own snapshot route instead (audit_route,
# critic_route); SERVED_BY_REQUESTED is what each packaged Claude route is served, the ids
# tests/fake_claude_phase1.py serves, and served_critic_model a dated deployment of a critic model.
ROUTES = {
    "claude": {"provider": "anthropic", "requested_model": "sonnet-5", "reasoning_effort": "max",
               "served_model": SERVED},
    "gpt": {"provider": "openai", "requested_model": "gpt-5.6-terra", "reasoning_effort": "high",
            "served_model": "gpt-5.6-terra-2026-08-01"},
}
SERVED_BY_REQUESTED = {"sonnet-5": SERVED, "sonnet-5-5": "claude-sonnet-5-5"}
SERVED_BY_CRITIC_MODEL = {"gpt-5.6-terra": "gpt-5.6-terra-2026-08-01", "gpt-6.1-sol": "gpt-6.1-sol-2026-09-29"}
TAMPERS = ("result", "report", "evidence", "evidence_artifact", "packet", "receipt")


def _module(name: str):
    return importlib.import_module(name)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_phase1(audit: pathlib.Path) -> tuple[pathlib.Path, dict[str, Any], dict[str, Any]]:
    """The quorum's snapshot (dir, manifest) and the quorum, creating a minimal one if absent."""
    runtime, rv = _module("phase1_runtime"), _module("run_verifier")
    audit = pathlib.Path(audit)
    phase_root = audit / "phase1"
    run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
    audit_id = run["audit_id"]
    if not (phase_root / "quorum.json").exists():
        paper = audit / "paper"
        if not paper.is_dir() or not any(path.is_file() for path in paper.rglob("*")):
            paper.mkdir(parents=True, exist_ok=True)
            (paper / "paper.txt").write_text(FIXTURE_PAPER_TEXT, encoding="utf-8")
        rows = [{"path": relative, "sha256": runtime.sha256_file(path), "bytes": path.stat().st_size}
                for relative, path in runtime.source_files(audit)]
        manifest: dict[str, Any] = {
            "schema_version": runtime.SNAPSHOT_VERSION,
            "source_files": rows,
            "common_evidence": {"artifacts": []},
            "fixture": "tests/verifier_fixtures.py minimal snapshot (no raw copy, no common evidence)",
        }
        manifest["snapshot_sha256"] = runtime.sha256_bytes(runtime.canonical_bytes(manifest))
        snapshot_dir = phase_root / "snapshots" / manifest["snapshot_sha256"]
        _write_json(snapshot_dir / "snapshot_manifest.json", manifest)
        quorum: dict[str, Any] = {
            "schema_version": "i4r-phase1-quorum-1",
            "status": "quorum_complete",
            "audit_id": audit_id,
            "source_snapshot_sha256": manifest["snapshot_sha256"],
            "probe_key": FIXTURE_PROBE_KEY,
            "probe_source_invocation_id": None,
            "roles": [],
            "fixture": "tests/verifier_fixtures.py minimal quorum",
        }
        quorum["quorum_sha256"] = runtime.sha256_bytes(runtime.canonical_bytes(quorum))
        _write_json(phase_root / "quorum.json", quorum)
    quorum = rv._quorum(phase_root, audit_id)
    snapshot_dir, manifest = rv._snapshot(phase_root, quorum)
    return snapshot_dir, manifest, quorum


def _output_tree(out: pathlib.Path, role: str, packet: dict[str, Any], verdict_of: dict[str, str]) -> None:
    """A contract-perfect verifier output for ``packet`` with the given verdict per claim hash."""
    rv = _module("run_verifier")
    evidence = out / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    verdicts, artifacts, blocks, bindings = [], [], [], {}
    for hypothesis in packet["hypotheses"]:
        hid = hypothesis["hypothesis_id"]
        source = hypothesis["source_hashes"][0]
        bindings[source["path"]] = source["sha256"]
        word = verdict_of[hypothesis["claim_sha256"]]
        name = f"{hid.lower()}_check.py"
        script = evidence / name
        script.write_text("import hashlib, pathlib\n"
                          f"print(hashlib.sha256(pathlib.Path({source['path']!r}).read_bytes()).hexdigest())\n",
                          encoding="utf-8")
        artifacts.append({"path": name, "sha256": _sha256(script), "bytes": script.stat().st_size})
        verdicts.append({
            "hypothesis_id": hid, "claim_sha256": hypothesis["claim_sha256"], "verdict": word,
            "evidence": [{"source_path": source["path"], "source_sha256": source["sha256"],
                          "result": "The frozen source was read and compared with the claim as written."}],
            "rerun": ({"artifact": None, "command": None} if word == "UNVERIFIABLE"
                      else {"artifact": f"evidence/{name}", "command": f"python3 evidence/{name}"}),
            "basis": ("The deposit lacks the raw channel the claim relies on." if word == "UNVERIFIABLE"
                      else "The deposited source bears directly on every element of the claim."),
        })
        blocks += [f"### {hid}: {word}", "",
                   "The check script compared the frozen source with the claim as written.",
                   f"<!-- source:{source['path']}@{source['sha256']} -->", ""]
    _write_json(evidence / "manifest.json", {
        "schema_version": rv.contract.RESULT_VERSION, "role": role,
        "source_snapshot_sha256": packet["source_snapshot_sha256"],
        "source_bindings": [{"path": path, "sha256": value} for path, value in sorted(bindings.items())],
        "artifacts": artifacts,
    })
    (out / "REPORT.md").write_text("\n".join([
        "# Post-fan-out verification", "",
        "## Result summary", "", "Every hypothesis was tested against the frozen deposit.", "",
        "## Hypotheses tested", "", *blocks,
        "## Evidence inventory", "", "One rerunnable check script per hypothesis, listed in the manifest.", "",
        rv.COMPLETION_MARKER, "",
    ]), encoding="utf-8")
    _write_json(out / "result.json", {
        "schema_version": rv.contract.RESULT_VERSION, "role": role, "status": "complete",
        "completion_marker": True, "source_snapshot_sha256": packet["source_snapshot_sha256"],
        "hypotheses_sha256": packet["hypotheses_sha256"],
        "report": {"path": "REPORT.md", "sha256": _sha256(out / "REPORT.md")},
        "evidence_manifest": {"path": "evidence/manifest.json", "sha256": _sha256(evidence / "manifest.json")},
        "scientific_fields": {"verdicts": verdicts},
    })


def verifier_route(audit: pathlib.Path) -> dict[str, str]:
    """The packaged route run_verifier.py dispatches a verifier on for this audit (the four route
    keys), read from the audit's own frozen snapshot exactly as plan_batch reads it."""
    rv, contract = _module("run_verifier"), _module("verifier_contract")
    route = contract.verifier_route(contract.load_snapshot_policy(pathlib.Path(audit)))
    if route not in rv.PACKAGED_ROUTES:
        raise ValueError(f"the audit's verifier route {route} is not a packaged Claude route")
    return dict(route)


def served_critic_model(requested_model: str) -> str:
    """The dated id a critic model is served as (any model a policy may ship, not only the two named)."""
    return SERVED_BY_CRITIC_MODEL.get(requested_model, f"{requested_model}-2026-09-29")


def critic_route(audit: pathlib.Path) -> dict[str, str]:
    """The critic route this audit's own snapshot froze, with its served id (2.7.0: gpt-6.1-sol when
    frozen from the shipped policy, gpt-5.6-terra for an audit initialised before)."""
    critics = _module("record_invocation").load_policy_snapshot(pathlib.Path(audit))["models"]["critics"]
    return {"provider": critics["provider"], "requested_model": critics["requested_model"],
            "reasoning_effort": critics["reasoning_effort"],
            "served_model": served_critic_model(critics["requested_model"])}


def audit_route(audit: pathlib.Path, family: str = "claude") -> dict[str, str]:
    """The route a ``family`` record on this audit is journaled under, with its served id: the
    snapshot's verifier route for claude, its critic route for gpt."""
    if family != "claude":
        return critic_route(audit)
    route = verifier_route(audit)
    return {"provider": route["provider"], "requested_model": route["requested_model"],
            "reasoning_effort": route["reasoning_effort"],
            "served_model": SERVED_BY_REQUESTED[route["requested_model"]]}


def _usage() -> dict[str, Any]:
    return {"obtainable": True, "input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0,
            "reasoning_tokens": 5, "total_tokens": 125, "source": "api_response"}


def record_verifier(
    audit: pathlib.Path,
    claims: Sequence[str],
    verdicts: Sequence[str] | None = None,
    *,
    finding_ids: Sequence[str] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    invocation_id: str | None = None,
    source_path: str | None = None,
    locator: str = "Table 2; Results",
    family: str = "claude",
    receipt: bool = True,
    relabel: tuple[str, str] | None = None,
    legacy_correction: str | None = None,
    corrected_at: str | None = None,
    tamper: str | None = None,
    validate_record: bool = True,
) -> dict[str, Any]:
    """Record one verifier call exactly as scripts/run_verifier.py would (see the module docstring)."""
    runtime, rv = _module("phase1_runtime"), _module("run_verifier")
    contract, journal = _module("verifier_contract"), _module("lifecycle_journal")
    record_invocation = _module("record_invocation")
    audit = pathlib.Path(audit).resolve()
    claims = list(claims)
    verdicts = list(verdicts) if verdicts is not None else ["CONFIRMED"] * len(claims)
    finding_ids = list(finding_ids) if finding_ids is not None else [f"F{n}" for n in range(1, len(claims) + 1)]
    if not claims or len(verdicts) != len(claims) or len(finding_ids) != len(claims):
        raise ValueError("claims, verdicts and finding_ids must be non-empty and the same length")
    if any(verdict not in contract.VERDICTS for verdict in verdicts):
        raise ValueError(f"verdicts must be among {contract.VERDICTS}")
    if family not in ROUTES:
        raise ValueError(f"family must be one of {sorted(ROUTES)}")
    if tamper is not None and tamper not in TAMPERS:
        raise ValueError(f"tamper must be one of {TAMPERS}")
    started_at = started_at or _now()
    ended_at = ended_at or started_at
    run = json.loads((audit / "run.json").read_text(encoding="utf-8"))
    audit_id = run["audit_id"]
    phase_root = audit / "phase1"
    snapshot_dir, manifest, quorum = ensure_phase1(audit)
    known = runtime.source_hashes(manifest)
    if source_path is None:
        candidates = sorted(path for path in known if path.startswith("paper/")) \
            or sorted(path for path in known if path.startswith("pkg/"))
        if not candidates:
            raise ValueError("the snapshot holds no paper/ or pkg/ source to cite")
        source_path = candidates[0]
    if source_path not in known:
        raise ValueError(f"{source_path} is not a snapshot source")

    by_claim = {claim: (verdict, fid) for claim, verdict, fid in zip(claims, verdicts, finding_ids)}
    if len(by_claim) != len(claims):
        raise ValueError("claims must be distinct")
    ordered = sorted(claims, key=contract.claim_sha256)
    hypotheses = [rv.Hypothesis(
        hypothesis_id=f"H{index}", finding_id=by_claim[claim][1], claim=claim,
        claim_sha256=contract.claim_sha256(claim),
        source_location={"path": source_path, "locator": locator},
        source_hashes=((source_path, known[source_path]),), dropped_source_hashes=(),
    ) for index, claim in enumerate(ordered, 1)]
    batch_role = contract.batch_role(row.claim_sha256 for row in hypotheses)
    packaged = verifier_route(audit)
    packet = rv.make_verifier_packet(batch_role, manifest, packaged, hypotheses)
    prompt = rv.build_verifier_prompt(batch_role, audit_id, packet)
    probe_key = quorum.get("probe_key") if isinstance(quorum.get("probe_key"), str) else FIXTURE_PROBE_KEY
    plan = rv.BatchPlan(
        root=audit, phase_root=phase_root, audit_id=audit_id, route=dict(packaged),
        snapshot=snapshot_dir, snapshot_manifest=manifest, hypotheses=hypotheses, role=batch_role,
        packet=packet, prompt=prompt, packet_hash=rv.packet_contract_hash(packet, prompt),
        probe_key=probe_key, input_hash=runtime.role_input_hash(packet, prompt, probe_key),
    )
    packet_dir = rv.materialize_verifier_packet(plan)
    base = contract.output_dir(batch_role, plan.input_hash)
    target = audit / base
    if target.exists():
        raise ValueError(f"{base} already exists: this batch was already recorded")

    # The attempt, as run_phase1.role_command_and_attempt lays it out.
    attempt_dir = phase_root / "roles" / batch_role / plan.input_hash / f"attempt_01_{uuid.uuid4()}"
    output = attempt_dir / "output.partial"
    output.mkdir(parents=True)
    prompt_hash = runtime.sha256_bytes(prompt.encode("utf-8"))
    _write_json(attempt_dir / "input.json", {
        "schema_version": "i4r-phase1-attempt-input-1", "role": batch_role,
        "input_sha256": plan.input_hash, "source_snapshot_sha256": manifest["snapshot_sha256"],
        "role_packet_sha256": runtime.sha256_file(packet_dir / "packet_manifest.json"),
        "prompt_sha256": prompt_hash, "attempt_kind": "role_inference", "source_invocation_id": None,
    })
    (attempt_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    _output_tree(output, batch_role, packet, {row.claim_sha256: by_claim[row.claim][0] for row in hypotheses})
    validated = rv.validate_verifier_output(output, batch_role, manifest, packet)
    route = audit_route(audit, family)
    stdout = attempt_dir / "claude_stdout.json"
    stdout.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                  "result": "complete", "modelUsage": {route["served_model"]: {}}},
                                 sort_keys=True) + "\n", encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(output, target, symlinks=False)

    journal_role, role_class = relabel or (batch_role, contract.ROLE_CLASS)
    invocation_id = invocation_id or f"inv-{uuid.uuid4().hex}"
    stdout_rel = stdout.relative_to(audit).as_posix()
    result_rel = f"{base}/result.json"
    with mock.patch.object(journal, "utc_now", lambda: started_at):
        journal.reserve_invocation(audit, {
            "invocation_id": invocation_id, "role": journal_role, "role_class": role_class,
            "round": contract.ROUND, "attempt_kind": "role_inference", "provider": route["provider"],
            "requested_model": route["requested_model"], "reasoning_effort": route["reasoning_effort"],
            "input_artifact_sha256": plan.input_hash, "prompt_sha256": prompt_hash,
            "source_invocation_id": None,
        })
    journal.mark_provider_request(audit, invocation_id, status="requested", at=started_at)
    attempt = journal.mark_inference_start(audit, invocation_id, at=started_at)["attempt"]
    journal.mark_response_received(audit, invocation_id, artifact=stdout_rel,
                                   sha256=_sha256(stdout), at=ended_at)
    elapsed = (journal.parse_time(ended_at) - journal.parse_time(started_at)).total_seconds()
    record = {
        "invocation_id": invocation_id, "role": journal_role, "role_class": role_class,
        "round": contract.ROUND, "attempt": attempt, "status": "completed",
        "provider": route["provider"], "provider_call": True, "cache_usage": "none",
        "session_id": f"session-{invocation_id}", "requested_model": route["requested_model"],
        "observed_model": route["served_model"], "observed_model_ids": [route["served_model"]],
        "model_family": family, "model_source": "observed",
        "reasoning_effort": route["reasoning_effort"], "started_at": started_at, "ended_at": ended_at,
        "elapsed_seconds": elapsed, "input_artifact_sha256": plan.input_hash,
        "prompt_sha256": prompt_hash, "output_artifact": result_rel,
        "output_sha256": validated.result_sha256, "usage": _usage(), "error": None,
        "error_class": None, "legacy_source": None, "attempt_kind": "role_inference",
        "provider_request_status": "accepted", "inference_start_status": "started",
        "source_invocation_id": None, "turn_count": 4,
        "provider_cost": {"amount": 0.01, "currency": "USD", "source": "cli_log", "unavailable_reason": None},
        "served_model": route["served_model"], "identity_assurance": "observed",
        "model_identity_evidence": {"source": "cli_log", "raw_ids": [route["served_model"]],
                                    "artifact": stdout_rel, "artifact_sha256": _sha256(stdout)},
        "usage_by_model": [], "unattributed_usage": _usage(), "provider_duration_seconds": elapsed,
    }
    if validate_record and family == "claude" and relabel is None:
        record_invocation.validate_record(record, audit)
    journal.record_terminal(audit, record, synthesize_lifecycle=False)
    receipt_rel = None
    if receipt:
        receipt_rel = contract.completion_receipt_path(batch_role, plan.input_hash)
        value = rv._receipt(plan, invocation_id=invocation_id, attempt=attempt,
                            attempt_kind="role_inference", source_invocation_id=None, validated=validated)
        runtime.atomic_write(audit / receipt_rel, runtime.pretty_bytes(value))
    _write_json(attempt_dir / "status.json", {
        "schema_version": "i4r-phase1-attempt-1", "state": "completed", "invocation_id": invocation_id,
        "attempt": attempt, "attempt_kind": "role_inference", "provider_request_status": "accepted",
        "inference_start_status": "started", "input_artifact_sha256": plan.input_hash,
        "output_sha256": validated.result_sha256, "ended_at": ended_at,
    })
    if legacy_correction is not None:
        journal.correct_invocation(audit, invocation_id, {"status": "legacy_record"},
                                   reason=legacy_correction, at=corrected_at or ended_at)
    if tamper == "receipt":
        if receipt_rel is None:
            raise ValueError("tamper='receipt' needs receipt=True")
        value = json.loads((audit / receipt_rel).read_text(encoding="utf-8"))
        value.pop("source_bindings")
        _write_json(audit / receipt_rel, value)
    elif tamper is not None:
        path = {
            "result": target / "result.json", "report": target / "REPORT.md",
            "evidence": target / "evidence/manifest.json",
            "evidence_artifact": target / "evidence" / f"{packet['hypotheses'][0]['hypothesis_id'].lower()}_check.py",
            "packet": packet_dir / "ROLE_PACKET.json",
        }[tamper]
        mode = path.stat().st_mode
        path.chmod(mode | 0o200)
        with path.open("ab") as handle:
            handle.write(b" ")
        path.chmod(mode)
    claim_hashes = [row.claim_sha256 for row in hypotheses]
    return {
        "invocation_id": invocation_id, "role": journal_role, "batch_role": batch_role,
        "role_class": role_class, "family": family, "input_hash": plan.input_hash,
        "packet_sha256": plan.packet_hash, "snapshot_sha256": manifest["snapshot_sha256"],
        "claim_sha256s": claim_hashes, "hashes": claim_hashes,
        "hypotheses": plan.receipt_hypotheses(), "output_dir": base,
        "result": result_rel, "result_sha256": validated.result_sha256,
        "result_sha": validated.result_sha256,
        "report": f"{base}/REPORT.md", "report_sha256": validated.report_sha256,
        "evidence": f"{base}/evidence/manifest.json",
        "evidence_manifest_sha256": validated.evidence_manifest_sha256,
        "receipt": receipt_rel, "attempt": attempt, "started_at": started_at, "ended_at": ended_at,
    }


def forge_hand_record(
    audit: pathlib.Path, claim: str, *, invocation_id: str = "hand-verifier-1",
    started_at: str | None = None, ended_at: str | None = None,
) -> dict[str, Any]:
    """The reviewers' forge.py record (records-1), journaled through the real lifecycle API.

    Four hand-written files that hash to what a hand-written receipt says: a report reading
    'hand made', a '{}' evidence manifest, a result with no evidence, rerun, report or manifest
    binding, and a receipt without packet_sha256, source_snapshot_sha256 or source_bindings. No
    frozen packet and no quorum snapshot back it. verify_record admitted exactly this before
    2.6.4's re-admission; it must now be refused.
    """
    contract, journal = _module("verifier_contract"), _module("lifecycle_journal")
    audit = pathlib.Path(audit).resolve()
    started_at = started_at or _now()
    ended_at = ended_at or started_at
    digest = contract.claim_sha256(claim)
    role = contract.batch_role([digest])
    input_hash = "c" * 64
    base = contract.output_dir(role, input_hash)

    def put(relative: str, text: str) -> str:
        path = audit / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return _sha256(path)

    report = put(base + "/REPORT.md", "hand made\n")
    manifest = put(base + "/evidence/manifest.json", "{}\n")
    result = put(base + "/result.json", json.dumps({
        "schema_version": contract.RESULT_VERSION, "role": role, "status": "complete",
        "completion_marker": True,
        "scientific_fields": {"verdicts": [{"hypothesis_id": "H1", "claim_sha256": digest,
                                            "verdict": "CONFIRMED"}]}}) + "\n")
    route = audit_route(audit)
    served = route["served_model"]
    with mock.patch.object(journal, "utc_now", lambda: started_at):
        journal.reserve_invocation(audit, {
            "invocation_id": invocation_id, "role": role, "role_class": contract.ROLE_CLASS,
            "round": contract.ROUND, "attempt_kind": "role_inference", "provider": route["provider"],
            "requested_model": route["requested_model"], "reasoning_effort": route["reasoning_effort"],
            "input_artifact_sha256": input_hash, "prompt_sha256": "e" * 64,
        })
    journal.mark_provider_request(audit, invocation_id, status="requested", at=started_at)
    attempt = journal.mark_inference_start(audit, invocation_id, at=started_at)["attempt"]
    elapsed = (journal.parse_time(ended_at) - journal.parse_time(started_at)).total_seconds()
    journal.record_terminal(audit, {
        "invocation_id": invocation_id, "role": role, "role_class": contract.ROLE_CLASS,
        "round": contract.ROUND, "attempt": attempt, "attempt_kind": "role_inference",
        "status": "completed", "provider_call": True, "cache_usage": "none", "session_id": "hand",
        "observed_model": served, "served_model": served, "observed_model_ids": [served],
        "model_family": "claude", "model_source": "observed", "identity_assurance": "observed",
        "started_at": started_at, "ended_at": ended_at, "elapsed_seconds": elapsed,
        "input_artifact_sha256": input_hash, "prompt_sha256": "e" * 64,
        "output_artifact": base + "/result.json", "output_sha256": result, "usage": _usage(),
        "error": None, "error_class": None, "legacy_source": None,
        "provider": route["provider"], "requested_model": route["requested_model"],
        "reasoning_effort": route["reasoning_effort"],
    }, synthesize_lifecycle=False)
    put(contract.completion_receipt_path(role, input_hash), json.dumps({
        "schema_version": contract.COMPLETION_VERSION, "status": "completed", "role": role,
        "input_sha256": input_hash, "invocation_id": invocation_id,
        "result": base + "/result.json", "result_sha256": result,
        "report": base + "/REPORT.md", "report_sha256": report,
        "evidence": base + "/evidence/manifest.json", "evidence_manifest_sha256": manifest,
        "hypotheses": [{"claim_sha256": digest}]}) + "\n")
    return {"invocation_id": invocation_id, "role": role, "input_hash": input_hash,
            "result": base + "/result.json", "result_sha256": result, "claim_sha256": digest}
