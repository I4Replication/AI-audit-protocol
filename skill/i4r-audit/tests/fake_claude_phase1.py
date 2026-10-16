#!/usr/bin/env python3
"""Concurrent-safe Claude CLI double for the packaged Phase-1 runner.

It also plays the 2.6.4 post-fan-out verifier (scripts/run_verifier.py), which runs on the same
runtime: a packet whose ROLE_PACKET.json role is ``verifier_<12 hex>`` gets a contract-perfect
verifier output unless a ``verifier_*`` scenario asks for a specific defect. FAKE_PHASE1_TARGET
``verifier`` targets whichever verifier batch role is running, so the generic scenarios
(``transient_always``, ``auth_failure``) apply to it too.

The 2.6.4 review scenarios (tests/test_264_verifier_runtime.py) break the verifier's FIRST sample
only, unless named otherwise, and rebind every hash so the defect is the only one:
``verifier_evidence_unbound`` (verdict evidence names a file absent from the deposit with a null
hash), ``verifier_binding_unbound`` (the same in the evidence manifest's source_bindings),
``verifier_evidence_unhashable`` (a list as evidence source_path), ``verifier_contradicting_section``
(a second "## Hypotheses tested" naming H1 REFUTED), ``verifier_repair_flip`` (an honest REFUTED
with a bad report hash, whose focused repair flips H1 to CONFIRMED), and ``verifier_refuted_invalid``
(EVERY sample: H1 REFUTED plus a stray file, so no sample is ever admitted).

2.7.0: the fake serves the model the route asks for. ``--model sonnet`` (a Sonnet 5 audit, every
audit initialised before 2.7.0) is served ``claude-sonnet-5-20260801``, exactly what every earlier
fixture recorded; ``--model claude-sonnet-5-5`` (a Sonnet 5.5 audit) is served ``claude-sonnet-5-5``.
tests/test_270_claude_routes.py's logging wrapper renames the served ids in the envelope per
``I4R_TEST_SERVED``, a map keyed on the legacy id; while that map names a rename, this fake serves
the legacy id on every route, so the wrapper alone decides what the call was served.

2.7.1: ``FAKE_PHASE1_PROBE_TOTAL_TOKENS=<n>`` makes the capability probe report usage whose normalized
total (input + cache read + cache creation + output, run_phase1.usage_by_model) is exactly ``n``, so a
test can run a probe over one audit's frozen bound and under another's. Unset, the probe reports what
it always did (a total of 170).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import sys
import time


# The primary model id served for each --model the runner spawns (see the module docstring).
LEGACY_SERVED = "claude-sonnet-5-20260801"
SERVED_BY_MODEL = {"sonnet": LEGACY_SERVED, "claude-sonnet-5-5": "claude-sonnet-5-5"}
RENAME_MAP_ENV = "I4R_TEST_SERVED"
PROBE_TOTAL_ENV = "FAKE_PHASE1_PROBE_TOTAL_TOKENS"
# What this process serves; main() sets it from argv before any envelope is written.
SERVED_MODEL = LEGACY_SERVED


def served_model(argv: list[str]) -> str:
    """The id this call is served: the route's own, or the legacy id under a rename map."""
    if json.loads(os.environ.get(RENAME_MAP_ENV) or "{}"):
        return LEGACY_SERVED
    requested = option_values(argv, "--model")
    return SERVED_BY_MODEL.get(requested[0], LEGACY_SERVED) if requested else LEGACY_SERVED


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(path: pathlib.Path) -> str:
    return digest_bytes(path.read_bytes())


def state_event(kind: str, **fields: object) -> int:
    path_value = os.environ.get("FAKE_PHASE1_STATE")
    if not path_value:
        return 1
    path = pathlib.Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        events = [json.loads(line) for line in handle if line.strip()]
        count = sum(
            1 for row in events
            if row.get("kind") == "start" and row.get("role") == fields.get("role")
        ) + 1
        row = {"kind": kind, "at": time.time(), "pid": os.getpid(), **fields}
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return count


def option_values(argv: list[str], option: str) -> list[str]:
    try:
        index = argv.index(option) + 1
    except ValueError:
        return []
    values = []
    while index < len(argv) and not argv[index].startswith("--"):
        values.append(argv[index])
        index += 1
    return values


def role_and_packet(argv: list[str], prompt: str) -> tuple[str, pathlib.Path | None, pathlib.Path | None]:
    dirs = [pathlib.Path(value) for value in option_values(argv, "--add-dir")]
    for directory in dirs:
        packet = directory / "ROLE_PACKET.json"
        if packet.is_file():
            value = json.loads(packet.read_text(encoding="utf-8"))
            return value["role"], packet, directory
    for directory in dirs:
        prior = directory / "prior_output/result.json"
        if prior.is_file():
            value = json.loads(prior.read_text(encoding="utf-8"))
            return value["role"], prior, directory
    return "phase1_capability_probe", None, dirs[0] if dirs else None


def valid_role_output(
    cwd: pathlib.Path,
    role: str,
    packet_path: pathlib.Path | None,
    *,
    broken: str | None = None,
    mutation: str | None = None,
) -> None:
    if packet_path is None:
        raise RuntimeError("missing role packet")
    if packet_path.name == "result.json":
        prior_result = json.loads(packet_path.read_text(encoding="utf-8"))
        source_snapshot = prior_result["source_snapshot_sha256"]
        source_root = packet_path.parent
        prior_manifest = json.loads((source_root / "evidence/manifest.json").read_text(encoding="utf-8"))
        scientific = prior_result["scientific_fields"]
        shutil.copytree(source_root / "evidence", cwd / "evidence")
        shutil.copyfile(source_root / "REPORT.md", cwd / "REPORT.md")
        if mutation == "claim":
            scientific["findings"][0]["claim"] += " Mutated during repair."
        elif mutation == "evidence_result":
            scientific["findings"][0]["evidence"][0]["result"] += " Mutated during repair."
        elif mutation == "impact":
            scientific["findings"][0]["impact"] += " Mutated during repair."
        elif mutation == "remedy":
            scientific["findings"][0]["remedy"] += " Mutated during repair."
        elif mutation == "report_prose":
            report = cwd / "REPORT.md"
            report.write_text(
                report.read_text(encoding="utf-8").replace(
                    "bounded review", "mutated bounded review", 1,
                ),
                encoding="utf-8",
            )
        elif mutation == "evidence_artifact":
            check = cwd / "evidence/aggregate_check.json"
            check.write_bytes(check.read_bytes() + b" ")
            prior_manifest["artifacts"][0].update({
                "sha256": digest(check), "bytes": check.stat().st_size,
            })
            (cwd / "evidence/manifest.json").write_text(
                json.dumps(prior_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        report = cwd / "REPORT.md"
        manifest_path = cwd / "evidence/manifest.json"
        result = {
            "schema_version": "i4r-phase1-role-result-2",
            "role": role,
            "status": "complete",
            "completion_marker": True,
            "source_snapshot_sha256": source_snapshot,
            "report": {"path": "REPORT.md", "sha256": digest(report)},
            "evidence_manifest": {
                "path": "evidence/manifest.json", "sha256": digest(manifest_path),
            },
            "scientific_fields": scientific,
        }
        (cwd / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        return
    else:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        source_snapshot = packet["source_snapshot_sha256"]
        binding = next(row for row in packet["source_manifest"] if row["path"].startswith("pkg/"))
        scientific = {
            "passed_check_ids": [f"{role}:inventory", f"{role}:source-read"],
            "findings": [{
                "id": f"{role}:F1", "status": "LIKELY",
                "claim": "One synthetic fixture discrepancy requires bounded reconciliation.",
                "evidence": [{
                    "source_path": binding["path"],
                    "source_sha256": binding["sha256"],
                    "result": "The source-bound aggregate check retained one discrepancy.",
                }],
                "impact": "The discrepancy may affect one bounded public statement.",
                "remedy": "Reconcile the source and update the affected statement.",
                "conclusion_changing": role in {"auditor_reproduction", "auditor_text_vs_data"},
                "disclosure_critical": role == "auditor_privacy_archive",
            }],
            "open_question_ids": [f"{role}:Q1"],
        }
    evidence = cwd / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    check = evidence / "aggregate_check.json"
    check.write_text(json.dumps({
        "schema_version": "fake-phase1-check-1",
        "role": role,
        "aggregate_only": True,
        "rows_emitted": False,
    }, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "i4r-phase1-role-result-2",
        "role": role,
        "source_snapshot_sha256": source_snapshot,
        "source_bindings": [{"path": binding["path"], "sha256": binding["sha256"]}],
        "artifacts": [{
            "path": "aggregate_check.json", "sha256": digest(check), "bytes": check.stat().st_size,
        }],
    }
    manifest_path = evidence / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    headings = [
        ("Result summary", "The bounded review completed and retained both affirmative and negative checks."),
        ("Brief corrections", "None. The immutable packet and raw source hashes agreed."),
        ("Checks performed and passed", "The source inventory, raw read path, and aggregate evidence binding passed."),
        ("Findings", "LIKELY — one synthetic fixture issue remains for coordinator review. Evidence is bounded; a benign file-format explanation was tested; impact is limited; the remedy is to reconcile the source."),
        ("Tested hypotheses not confirmed", "A second candidate explanation was checked against the raw source and was not confirmed."),
        ("Open questions", "The coordinator should determine whether the bounded discrepancy affects public wording."),
        ("Evidence inventory", "The aggregate check JSON and its closed manifest are rerunnable records."),
    ]
    body = [f"# {role}", ""]
    for heading, paragraph in headings:
        body.extend([f"## {heading}", "", paragraph, ""])
    body.extend([
        f"<!-- source:{binding['path']}@{binding['sha256']} -->", "",
        (
            "The audit inspected the frozen source rather than accepting a transcribed claim. "
            "It records exact artifact hashes, bounded operations, aggregate outcomes, negative "
            "controls, and a proportionate next step. No intent is inferred, and uncertainty is "
            "kept explicit. "
        ) * 7,
        "",
        f"<!-- i4r-role-complete:{role} -->",
        "",
    ])
    report = cwd / "REPORT.md"
    report.write_text("\n".join(body), encoding="utf-8")
    result = {
        "schema_version": "i4r-phase1-role-result-2",
        "role": role,
        "status": "complete",
        "completion_marker": True,
        "source_snapshot_sha256": source_snapshot,
        "report": {"path": "REPORT.md", "sha256": digest(report)},
        "evidence_manifest": {"path": "evidence/manifest.json", "sha256": digest(manifest_path)},
        "scientific_fields": scientific,
    }
    if broken == "result_report_hash":
        result["report"]["sha256"] = "0" * 64
    (cwd / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


VERIFIER_RESULT_VERSION = "i4r-verifier-result-1"
VERIFIER_MARKER = "<!-- i4r-role-complete:verifier -->"


def is_verifier(role: str) -> bool:
    return role.startswith("verifier_")


def matches_target(role: str, target: str) -> bool:
    return role == target or (target == "verifier" and is_verifier(role))


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verifier_rehash(cwd: pathlib.Path) -> None:
    """Rebind the manifest artifact rows and result.json's report/manifest hashes after an edit."""
    evidence = cwd / "evidence"
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"] = [
        {"path": path.relative_to(evidence).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
        for path in sorted(evidence.rglob("*")) if path.is_file() and path.name != "manifest.json"
    ]
    write_json(evidence / "manifest.json", manifest)
    result = json.loads((cwd / "result.json").read_text(encoding="utf-8"))
    result["report"] = {"path": "REPORT.md", "sha256": digest(cwd / "REPORT.md")}
    result["evidence_manifest"] = {"path": "evidence/manifest.json", "sha256": digest(evidence / "manifest.json")}
    write_json(cwd / "result.json", result)


def verifier_defect(cwd: pathlib.Path, scenario: str, count: int) -> None:
    """Apply one review scenario's defect to a contract-perfect verifier output."""
    first = count == 1
    result_path = cwd / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    verdicts = result["scientific_fields"]["verdicts"]
    report = cwd / "REPORT.md"
    if scenario == "verifier_evidence_unbound" and first:
        for verdict in verdicts:
            verdict["evidence"] = [{"source_path": "pkg/NOT_IN_THE_DEPOSIT.csv", "source_sha256": None,
                                    "result": "fabricated observation, never joined to the snapshot"}]
        write_json(result_path, result)
    elif scenario == "verifier_evidence_unhashable" and first:
        row = verdicts[0]["evidence"][0]
        row["source_path"] = [row["source_path"]]
        write_json(result_path, result)
    elif scenario == "verifier_binding_unbound" and first:
        manifest = json.loads((cwd / "evidence/manifest.json").read_text(encoding="utf-8"))
        manifest["source_bindings"] = [{"path": "pkg/NOT_IN_THE_DEPOSIT.csv", "sha256": None}]
        write_json(cwd / "evidence/manifest.json", manifest)
    elif scenario == "verifier_contradicting_section" and first:
        report.write_text(report.read_text(encoding="utf-8")
                          + "\n## Hypotheses tested\n\n### H1: REFUTED\n\nOn reflection the table "
                          "contradicts the claim.\n", encoding="utf-8")
    elif scenario in {"verifier_repair_flip", "verifier_refuted_invalid"} and (
            first or scenario == "verifier_refuted_invalid"):
        verdicts[0]["verdict"] = "REFUTED"
        write_json(result_path, result)
        report.write_text(report.read_text(encoding="utf-8").replace(": CONFIRMED", ": REFUTED", 1),
                          encoding="utf-8")
    else:
        return
    verifier_rehash(cwd)
    if scenario == "verifier_repair_flip":
        # One structural slip, so the honest REFUTED is sent to a focused repair.
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["report"]["sha256"] = "0" * 64
        write_json(result_path, result)
    if scenario == "verifier_refuted_invalid":
        # An unrepairable defect: the sample is never admitted and stays under phase1/roles.
        (cwd / "scratch_notes.txt").write_text("left behind\n", encoding="utf-8")


def verifier_repair_output(cwd: pathlib.Path, prior: pathlib.Path, scenario: str) -> None:
    """A focused repair: copy the frozen surface, fix only the structural bindings."""
    source_root = prior.parent
    prior_result = json.loads(prior.read_text(encoding="utf-8"))
    shutil.copytree(source_root / "evidence", cwd / "evidence")
    shutil.copyfile(source_root / "REPORT.md", cwd / "REPORT.md")
    result = dict(prior_result)
    if scenario == "verifier_repair_mutate_verdict":
        # The frozen surface: a repair that flips a verdict must be refused, never promoted.
        verdict = result["scientific_fields"]["verdicts"][0]
        verdict["verdict"] = "REFUTED" if verdict["verdict"] != "REFUTED" else "CONFIRMED"
    if scenario == "verifier_repair_flip":
        # The attack: flip the honest REFUTED to CONFIRMED and rewrite the heading to match, so
        # only the byte-frozen projection can tell.
        result["scientific_fields"]["verdicts"][0]["verdict"] = "CONFIRMED"
        report = cwd / "REPORT.md"
        report.write_text(report.read_text(encoding="utf-8").replace(": REFUTED", ": CONFIRMED", 1),
                          encoding="utf-8")
    result["report"] = {"path": "REPORT.md", "sha256": digest(cwd / "REPORT.md")}
    result["evidence_manifest"] = {
        "path": "evidence/manifest.json", "sha256": digest(cwd / "evidence/manifest.json"),
    }
    write_json(cwd / "result.json", result)


def verifier_output(cwd: pathlib.Path, role: str, packet_path: pathlib.Path, scenario: str, count: int) -> None:
    """A contract-perfect verifier output for the packet, or one with the scenario's defect."""
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    hypotheses = packet["hypotheses"]
    evidence = cwd / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    verdicts = []
    artifacts = []
    blocks = []
    bindings: dict[str, str] = {}
    for index, hypothesis in enumerate(hypotheses):
        source = hypothesis["source_hashes"][0]
        bindings[source["path"]] = source["sha256"]
        name = f"{hypothesis['hypothesis_id'].lower()}_check.py"
        script = evidence / name
        script.write_text(
            "import hashlib, pathlib\n"
            f"print(hashlib.sha256(pathlib.Path({source['path']!r}).read_bytes()).hexdigest())\n",
            encoding="utf-8",
        )
        artifacts.append({"path": name, "sha256": digest(script), "bytes": script.stat().st_size})
        word = "CONFIRMED"
        if index == 0 and scenario == "verifier_refuted":
            word = "REFUTED"
        if index == 0 and scenario == "verifier_unverifiable":
            word = "UNVERIFIABLE"
        rerun: dict[str, object] = {"artifact": f"evidence/{name}", "command": f"python3 evidence/{name}"}
        if word == "UNVERIFIABLE":
            rerun = {"artifact": None, "command": None}
        verdicts.append({
            "hypothesis_id": hypothesis["hypothesis_id"],
            "claim_sha256": hypothesis["claim_sha256"],
            "verdict": word,
            "evidence": [{
                "source_path": source["path"], "source_sha256": source["sha256"],
                "result": "The frozen source was read and compared with the claim as written.",
            }],
            "rerun": rerun,
            "basis": "The deposited source bears directly on every element of the claim."
            if word != "UNVERIFIABLE" else "The deposit lacks the raw channel the claim relies on.",
        })
        blocks.extend([
            f"### {hypothesis['hypothesis_id']}: {word}", "",
            "The check script compared the frozen source with the claim as written.",
            f"<!-- source:{source['path']}@{source['sha256']} -->", "",
        ])
    if scenario == "verifier_extra_hypothesis":
        verdicts.append(dict(verdicts[0], hypothesis_id="H99", claim_sha256="e" * 64))
    if scenario == "verifier_missing_hypothesis" and count == 1 and len(verdicts) > 1:
        verdicts.pop()
    manifest = {
        "schema_version": VERIFIER_RESULT_VERSION,
        "role": role,
        "source_snapshot_sha256": packet["source_snapshot_sha256"],
        "source_bindings": [{"path": path, "sha256": value} for path, value in sorted(bindings.items())],
        "artifacts": artifacts,
    }
    write_json(evidence / "manifest.json", manifest)
    body = [
        "# Post-fan-out verification", "",
        "## Result summary", "", "Every hypothesis was tested against the frozen deposit.", "",
        "## Hypotheses tested", "", *blocks,
        "## Evidence inventory", "", "One rerunnable check script per hypothesis, listed in the manifest.", "",
        VERIFIER_MARKER, "",
    ]
    (cwd / "REPORT.md").write_text("\n".join(body), encoding="utf-8")
    result = {
        "schema_version": VERIFIER_RESULT_VERSION,
        "role": role,
        "status": "complete",
        "completion_marker": True,
        "source_snapshot_sha256": packet["source_snapshot_sha256"],
        "hypotheses_sha256": packet["hypotheses_sha256"],
        "report": {"path": "REPORT.md", "sha256": digest(cwd / "REPORT.md")},
        "evidence_manifest": {"path": "evidence/manifest.json", "sha256": digest(evidence / "manifest.json")},
        "scientific_fields": {"verdicts": verdicts},
    }
    if scenario in {"verifier_repairable", "verifier_repair_mutate_verdict"} and count == 1:
        # A structural slip only: the report hash in result.json is wrong.
        result["report"]["sha256"] = "0" * 64
    write_json(cwd / "result.json", result)
    verifier_defect(cwd, scenario, count)


def envelope(payload: object, *, error: bool = False) -> dict[str, object]:
    # The "denials" scenario models a real CLI session in which the sandbox refused one
    # shell command and the model recovered. The runner must record it, not fail the role.
    denials: list[dict[str, object]] = []
    if os.environ.get("FAKE_PHASE1_SCENARIO") == "denials":
        denials = [{
            "tool_name": "Bash",
            "tool_use_id": "toolu_fake_denied",
            "tool_input": {"command": "cp ./scratch/*.json ./evidence/ && ls evidence"},
        }]
    return {
        "type": "result",
        "subtype": "error" if error else "success",
        "is_error": error,
        "session_id": f"fake-phase1-{os.getpid()}",
        "result": payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True),
        "modelUsage": {
            SERVED_MODEL: {
                "inputTokens": 100,
                "outputTokens": 40,
                "cacheReadInputTokens": 20,
                "cacheCreationInputTokens": 10,
                "reasoningTokens": 5,
            }
        },
        "num_turns": 4,
        "total_cost_usd": 0.01,
        "duration_api_ms": 100,
        "permission_denials": denials,
    }


def rejected_envelope(message: str) -> dict[str, object]:
    """Provider rejection before an inference starts: deliberately no usage/model evidence."""
    return {
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": message,
        "permission_denials": [],
    }


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print("2.1.206 (Fake Claude Code)")
        return 0
    global SERVED_MODEL
    SERVED_MODEL = served_model(argv)
    prompt = sys.stdin.read()
    cwd = pathlib.Path.cwd()
    role, packet_path, source = role_and_packet(argv, prompt)
    scenario = os.environ.get("FAKE_PHASE1_SCENARIO", "success")
    target = os.environ.get("FAKE_PHASE1_TARGET", "auditor_reproduction")
    count = state_event("start", role=role, argv=argv, cwd=str(cwd), scenario=scenario,
                        output_cap=os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
                        shell_cwd_pin=os.environ.get("CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"))
    if role == "phase1_capability_probe":
        if scenario == "probe_failure":
            print(json.dumps(envelope("probe rejected", error=True), separators=(",", ":")))
            state_event("end", role=role, returncode=1)
            return 1
        dirs = [pathlib.Path(value) for value in option_values(argv, "--add-dir")]
        nonce_path = next(path / "nonce.txt" for path in dirs if (path / "nonce.txt").is_file())
        nonce = nonce_path.read_text(encoding="utf-8")
        # The Write tool is not seatbelt-confined and takes a relative path under cwd, so it
        # succeeds even when every sandbox rule is inert. Model that faithfully: probe.ok is
        # always written, while the two Bash-produced artefacts appear only when the emitted
        # allowRead/allowWrite rules are actually expressible as filesystem rules. Without
        # this the fake reports a healthy sandbox no matter what settings it was handed.
        (cwd / "probe.ok").write_text(nonce, encoding="utf-8")
        sandbox_rules_live = True
        try:
            settings_value = json.loads(
                pathlib.Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8")
            )
            filesystem = settings_value["sandbox"]["filesystem"]
            rules = filesystem.get("allowRead", []) + filesystem.get("allowWrite", [])
            sandbox_rules_live = not any(set("?*[]{}\\") & set(rule) for rule in rules)
        except (ValueError, KeyError, OSError):
            sandbox_rules_live = False
        if sandbox_rules_live:
            evidence = cwd / "evidence"
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "bash.ok").write_text(nonce, encoding="utf-8")
            contract = next(
                (d / "PROBE_CONTRACT.json" for d in dirs if (d / "PROBE_CONTRACT.json").is_file()),
                None,
            )
            if contract is not None:
                (cwd / "packet_copy.json").write_bytes(contract.read_bytes())
        tool_hash = None
        for directory in dirs:
            contract = directory / "PROBE_CONTRACT.json"
            if contract.is_file():
                settings = pathlib.Path(argv[argv.index("--settings") + 1])
                value = json.loads(settings.read_text(encoding="utf-8"))
                abstract = {
                    "safe_mode": True,
                    "bare": False,
                    "network": {"allowedDomains": [], "deniedDomains": ["*"]},
                    "tools": ["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
                    "bash_allowlist": value["i4rBashAllowlist"],
                    "no_session_persistence": True,
                    "plugins": False,
                    "mcp_servers": {},
                }
                tool_hash = digest_bytes(json.dumps(
                    abstract, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8"))
        payload = {
            "status": "complete",
            "nonce_sha256": digest_bytes(nonce.encode("utf-8")),
            "tool_contract_sha256": tool_hash,
            "output_file": "probe.ok",
        }
        body = envelope(payload)
        if os.environ.get(PROBE_TOTAL_ENV):
            counts = body["modelUsage"][SERVED_MODEL]
            counts["inputTokens"] = int(os.environ[PROBE_TOTAL_ENV]) - (
                counts["cacheReadInputTokens"] + counts["cacheCreationInputTokens"] + counts["outputTokens"])
        print(json.dumps(body, separators=(",", ":")))
        state_event("end", role=role, returncode=0)
        return 0
    if scenario in {"transient_once", "transient_always"} and matches_target(role, target) and (
        scenario == "transient_always" or count == 1
    ):
        print(json.dumps(rejected_envelope("429 overloaded"), separators=(",", ":")))
        print("HTTP 429 overloaded", file=sys.stderr)
        state_event("end", role=role, returncode=1)
        return 1
    if scenario in {"output_cap_once", "output_cap_always"} and matches_target(role, target) and (
        scenario == "output_cap_always" or count == 1
    ):
        # 2.7.4: an accepted call whose response the CLI cut at its per-response cap, four times.
        body = envelope("API Error: Claude's response exceeded the "
                        f"{os.environ.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS') or 64000} output token maximum. "
                        "To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment "
                        "variable.")
        body.update(subtype="success", is_error=True, terminal_reason="api_error")
        print(json.dumps(body, separators=(",", ":")))
        state_event("end", role=role, returncode=1)
        return 1
    if scenario == "auth_failure" and matches_target(role, target):
        print(json.dumps(rejected_envelope("authentication failed"), separators=(",", ":")))
        print("authentication failed", file=sys.stderr)
        state_event("end", role=role, returncode=1)
        return 1
    if is_verifier(role):
        if packet_path is not None and packet_path.name == "result.json":
            verifier_repair_output(cwd, packet_path, scenario)
        elif packet_path is not None:
            verifier_output(cwd, role, packet_path, scenario, count)
        print(json.dumps(envelope("complete"), separators=(",", ":")))
        state_event("end", role=role, returncode=0)
        return 0
    if scenario == "slow_parallel":
        time.sleep(0.25)
    broken = None
    repair_scenario = scenario == "repairable_output" or scenario.startswith("repair_mutate_")
    is_repair = "Perform one format-only repair" in prompt
    # Only the role's first sample is broken; a fresh attempt after a rejected repair is valid,
    # as it was on every such case in audit A34.
    if repair_scenario and role == target and not is_repair and count == 1:
        broken = "result_report_hash"
    mutation = scenario.removeprefix("repair_mutate_") if (
        scenario.startswith("repair_mutate_") and role == target and is_repair
    ) else None
    if scenario == "no_output" and role == target:
        # A complete, paid-for provider call that produced no output files at all. The bare
        # Phase1Error from the missing REPORT.md used to escape run_role's
        # `except OutputValidationError` and reach no recorder.
        print(json.dumps(envelope("complete"), separators=(",", ":")))
        state_event("end", role=role, returncode=0)
        return 0
    if scenario in ("snapshot_mode_drift", "snapshot_content_tamper") and role == target:
        # A valid sample, and the frozen source changed during the call.
        #   snapshot_mode_drift     the File Provider shape: a raw file gets its write bit back,
        #                           every byte unchanged (what 2.6.7 failed the paid attempt
        #                           over). The runner re-freezes it and keeps the paid output.
        #                           One chmod, so a concurrent check can never see half of it.
        #   snapshot_content_tamper a genuine content change: the post-call check must fail the
        #                           attempt as one environment failure, with no retry.
        valid_role_output(cwd, role, packet_path)
        raw = next(p for p in [pathlib.Path(v) for v in option_values(argv, "--add-dir")]
                   if p.name == "raw")
        frozen = next(p for p in sorted(raw.rglob("*")) if p.is_file())
        frozen.chmod(0o644)
        if scenario == "snapshot_content_tamper":
            with frozen.open("ab") as handle:
                handle.write(b"provider-side tamper\n")
        print(json.dumps(envelope("complete"), separators=(",", ":")))
        state_event("end", role=role, returncode=0)
        return 0
    if scenario == "snapshot_planted_file" and role == target:
        # 2.6.8 fix A: the File Provider gives a frozen raw/ directory its write bit back and a
        # new file lands in it (a sync "(conflicted copy)"). Every listed byte is unchanged, so
        # only an inventory check sees it; a re-freeze must refuse it, never seal it.
        valid_role_output(cwd, role, packet_path)
        raw = next(p for p in [pathlib.Path(v) for v in option_values(argv, "--add-dir")]
                   if p.name == "raw")
        holder = next(p for p in sorted(raw.rglob("*")) if p.is_file()).parent
        holder.chmod(0o755)
        (holder / "data (conflicted copy).csv").write_text("column\nplanted\n")
        print(json.dumps(envelope("complete"), separators=(",", ":")))
        state_event("end", role=role, returncode=0)
        return 0
    if scenario == "result_absolute_path" and role == target:
        # The exact shape from audit A38: REPORT.md stays clean because the
        # auditor described the deposit's hardcoded setwd() paths without quoting them, while
        # the structured finding carries the literal path.
        valid_role_output(cwd, role, packet_path)
        result = json.loads((cwd / "result.json").read_text(encoding="utf-8"))
        fields = result["scientific_fields"]
        if fields.get("findings"):
            fields["findings"][0]["claim"] = (
                "the script hardcodes " + "C:/" + "Users/7350/CloudDrive/Shared"
            )
        (cwd / "result.json").write_text(json.dumps(result), encoding="utf-8")
        print(json.dumps(envelope("complete"), separators=(",", ":")))
        state_event("end", role=role, returncode=0)
        return 0
    if scenario == "invalid_evidence" and role == target:
        valid_role_output(cwd, role, packet_path)
        (cwd / "evidence/aggregate_check.json").write_text("changed\n", encoding="utf-8")
    else:
        valid_role_output(cwd, role, packet_path, broken=broken, mutation=mutation)
    print(json.dumps(envelope("complete"), separators=(",", ":")))
    state_event("end", role=role, returncode=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
