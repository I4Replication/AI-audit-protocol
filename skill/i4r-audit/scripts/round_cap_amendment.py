#!/usr/bin/env python3
"""Round-cap amendments and reopenings of a finalized audit (2.6.9): the shared rules and the verifier.

    python3 scripts/round_cap_amendment.py <audit-dir>                 # verify; exit 0 consistent, 1 not, 2 unreadable
    python3 scripts/round_cap_amendment.py check-round <audit-dir> <N>  # may round N be prepared or run now?

An audit's round cap is frozen in its pipeline-policy.snapshot.json at init_run.sh, so raising the
programme's cap (5 until 2.6.8, 7 from 2.6.9) changes only audits initialised afterwards. An existing
audit is never re-capped silently. Two tool-only, journaled operator acts extend one:

  * scripts/amend_round_cap.py raises the cap. It rewrites the snapshot with a
    critique.round_cap_amendments history entry, archives the replaced bytes read-only beside it, and
    journals a policy_amendment event (the run's round_cap and snapshot binding follow it);
  * scripts/reopen_audit.py (or amend_round_cap.py --reopen) journals an audit_reopened event, the only
    way a finalized audit runs another round. The close it reopens stays in the journal.

verify() proves afterwards that the snapshot, its archived predecessors and the journal tell one
story. finalize_run.sh writes its failures to compliance.violations (and compliance.policy_amendments_valid),
and validate_round_projections.py raises on them, so publish_completed.sh re-checks them too:

  1. the snapshot's history equals the journal's policy_amendment events, written by the tool;
  2. each archived snapshot exists, hashes as recorded, and differs from its successor only at
     critique.round_cap, critique.stopping.cap_with_unresolved and the history list;
  3. each round's live plan binds the snapshot in force when it was prepared: rounds up to the old cap
     the replaced snapshot, later rounds the amended one. The snapshot in force is the one bound at init,
     rebound by each journaled rewrite (a critic re-pin's metadata_correction, an amendment); each
     amendment starts from the snapshot bound when it was made;
  4. no round above a cap was prepared before the amendment that raised it;
  5. no round was run after a finalize without a later audit_reopened (an error from protocol 2.6.9, or
     once the audit has an amendment or a reopening; a warning for an older record);
  6. the cap recorded at init, raised by each amendment in turn, is the snapshot's cap and run.round_cap;
  7. no round event names a round above the cap in force when it was journaled.

check-round (the driver's gate, and prepare_round's) uses that journaled cap: it proves the snapshot's
envelope and run.json's binding, and refuses a snapshot whose cap differs from the journal's. prospective()
runs verify() on the state an amend or a reopen would leave, before either writes anything.
"""
from __future__ import annotations

import copy
import glob
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import lifecycle_journal  # noqa: E402

SNAPSHOT = "pipeline-policy.snapshot.json"
# The caps a live v2 audit may carry: 2.6.8 and earlier froze 5, 2.6.9 ships 7 (config/check_policy_drift.py pins
# this against critique.round_cap_amendment and against schemas/tools/validate_v2.py).
LIVE_ROUND_CAPS = (5, 7)
AMEND_EVENT = lifecycle_journal.AMEND_EVENT
REOPEN_EVENT = lifecycle_journal.REOPEN_EVENT
AMEND_SOURCE = "amend_round_cap.py"
REOPEN_SOURCES = frozenset({"amend_round_cap.py", "reopen_audit.py"})
LEAF = "critique.round_cap"
# What an amendment may change in the policy: the cap, the stop text that used to name it, and the history list.
AMENDABLE_PATHS = frozenset({
    "critique.round_cap", "critique.stopping.cap_with_unresolved", "critique.round_cap_amendments",
})
ENFORCED_FROM = (2, 6, 9)
PURPOSES = ("continue_critique", "correct_published")
FILING_RECIPIENT = "comments/ and metadata/ (filed publication set)"
_VERSION = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")


class AmendmentError(ValueError):
    """A refused amendment or reopen: nothing was written."""


# ------------------------------------------------------------------------------------------- small helpers
def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def policy_hash(policy: dict[str, Any]) -> str:
    data = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def envelope_bytes(policy: dict[str, Any]) -> bytes:
    """The snapshot envelope exactly as record_invocation.initialize_policy_snapshot writes it."""
    envelope = {"snapshot_version": "1", "policy_sha256": policy_hash(policy), "policy": policy}
    return (json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def load_envelope(path: pathlib.Path) -> dict[str, Any]:
    """A snapshot envelope whose policy_sha256 matches its policy; ValueError otherwise."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} is missing or is not a regular file")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        policy = envelope["policy"]
        if envelope.get("snapshot_version") != "1" or envelope.get("policy_sha256") != policy_hash(policy):
            raise ValueError("policy hash is invalid")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path.name} is not a valid policy snapshot envelope: {exc}") from exc
    return envelope


def diff_paths(left: Any, right: Any, prefix: tuple[str, ...] = ()) -> set[str]:
    """Dotted paths at which two JSON values differ (objects recursively; any other value compared whole)."""
    if isinstance(left, dict) and isinstance(right, dict):
        found: set[str] = set()
        for key in set(left) | set(right):
            if key not in left or key not in right:
                found.add(".".join(prefix + (str(key),)))
            else:
                found |= diff_paths(left[key], right[key], prefix + (str(key),))
        return found
    return set() if left == right else {".".join(prefix) or "<root>"}


def skill_version(skill: pathlib.Path = SKILL) -> str | None:
    try:
        return (skill / "VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def version_tuple(value: Any) -> tuple[int, int, int] | None:
    match = _VERSION.match(str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def scripts_sha256(skill: pathlib.Path = SKILL) -> str | None:
    """init_run.sh's gate_files_digest (toolkit.scripts_sha256): the gate files, sorted, concatenated, hashed."""
    patterns = ("scripts/*.sh", "scripts/*.py", "schemas/*.json", "schemas/v2/*.json", "schemas/v3/*.json",
                "schemas/tools/*.py")
    paths = [path for pattern in patterns for path in skill.glob(pattern) if path.is_file()]
    paths += [path for path in (skill / "config/check_policy_drift.py", skill / "config/pipeline-policy-v2.json")
              if path.is_file()]
    paths += [path for path in skill.glob("prompts/*.txt") if path.is_file()]
    ordered = sorted({str(path) for path in paths}, key=lambda value: value.encode("utf-8"))
    stream = b"".join(pathlib.Path(path).read_bytes() for path in ordered).rstrip(b"\n")   # as $(...) strips
    return hashlib.sha256(stream).hexdigest() if stream else None


def host_toolkit() -> dict[str, Any]:
    def run(command: list[str]) -> str | None:
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=20).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return {"skill_version": skill_version(), "scripts_sha256": scripts_sha256(),
            "host_os": run(["uname", "-sr"]), "bash_version": run(["bash", "-c", "echo $BASH_VERSION"])}


def archive_name(root: pathlib.Path, at: str) -> pathlib.Path:
    """A fresh pipeline-policy.snapshot.superseded-<time>[-N].json beside the snapshot: amend_round_cap.py and
    repin_critic_route.py share it, so two rewrites in the same second never collide on one read-only name."""
    stamp = at.replace(":", "").replace("-", "")
    candidate = root / f"pipeline-policy.snapshot.superseded-{stamp}.json"
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        counter += 1
        candidate = root / f"pipeline-policy.snapshot.superseded-{stamp}-{counter}.json"
    return candidate


def unfinished_rounds(events: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Rounds whose last round_prepared or round_start has no later round_end, round_integrated or round_result:
    {round: {"state": prepared|started, "seq": ...}}. Such a round's frozen snapshot copies the policy snapshot, so a
    rewrite of that snapshot supersedes its plan when it is relaunched."""
    state: dict[int, dict[str, Any]] = {}
    for event in events:
        number = _round_of(event)
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        kind = event.get("type")
        if kind in ("round_prepared", "round_start"):
            state[number] = {"state": "started" if kind == "round_start" or (state.get(number) or {}).get("state")
                             == "started" else "prepared", "seq": event.get("seq")}
        elif kind in ("round_end", "round_integrated", "round_result"):
            state.pop(number, None)
    return state


def read_run(root: pathlib.Path) -> dict[str, Any]:
    try:
        run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AmendmentError(f"run.json is missing or unreadable: {exc}") from exc
    if not isinstance(run, dict):
        raise AmendmentError("run.json is not an object")
    return run


def require_live_21(root: pathlib.Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    if run.get("schema_version") != "2.1.0" or run.get("normalization"):
        raise AmendmentError(
            "only a live metadata-2.1 audit can be amended or reopened (this run is "
            f"{run.get('schema_version')!r}{', normalized from v1' if run.get('normalization') else ''}); a v1 "
            "record cannot be resumed under the current skill: start a fresh audit with new_audit.sh")
    events = lifecycle_journal.load_events(root)
    if not events:
        raise AmendmentError("the audit has no canonical journal (run.events.jsonl)")
    return events


def live_driver_lock(root: pathlib.Path) -> str | None:
    for pid_file in root.glob("critique/round_*/.runtime_lock/pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, OSError):
            continue
        return f"{pid_file.parent.relative_to(root)} (pid {pid})"
    return None


def last_filing(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """publish_completed.sh's last filing delivery: {release_dir, filed_at}, or None when never published."""
    found = None
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "delivery" and payload.get("release_dir") \
                and payload.get("recipient") == FILING_RECIPIENT:
            found = {"release_dir": str(payload["release_dir"]), "filed_at": event.get("at")}
    return found


def _tree_call(function: str, audit: pathlib.Path) -> str | None:
    try:
        result = subprocess.run(
            ["bash", "-c", f'. "$1/tree_layout.sh" && {function} "$2"', "_", str(HERE), str(audit)],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def published_set_in_place(audit: pathlib.Path, release_dir: str) -> list[pathlib.Path]:
    """The filed comments/ and metadata/ folders still in place (finalize's rule, both path spellings)."""
    found: list[pathlib.Path] = []
    for spelling in (audit, audit.resolve()):
        tree = _tree_call("i4r_root_of_audit", spelling)
        if not tree:
            continue
        relative = release_dir.split("/", 1)[1] if release_dir.startswith("comments/") else release_dir
        for candidate in (pathlib.Path(tree) / release_dir, pathlib.Path(tree) / "metadata" / relative):
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)
    return found


def paper_source(audit: pathlib.Path) -> tuple[str | None, str | None]:
    """(journal, paper) from the audit's paper_source.json, written at intake: the paper's TRUE folder name in data/,
    comments/ and metadata/. The runs/ folder drops ?*[]{}\\ from it (new_audit.sh), so a name derived from the audit's
    own path is wrong for such a paper (A22's '?'); publish_completed.sh and status.py read this file for that reason."""
    try:
        source = json.loads((pathlib.Path(audit) / "paper_source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(source, dict):
        return None, None
    journal, paper = source.get("journal"), source.get("paper")
    return (journal if isinstance(journal, str) and journal else None,
            paper if isinstance(paper, str) and paper else None)


def paper_input_dir(audit: pathlib.Path) -> pathlib.Path | None:
    """The audit's data/<Journal>/<Paper> folder: named by paper_source.json first, then by tree_layout's path
    derivation; None when neither names an existing folder under a tree's data/."""
    audit = pathlib.Path(audit)
    journal, paper = paper_source(audit)
    for spelling in (audit, audit.resolve()):
        tree = _tree_call("i4r_root_of_audit", spelling)
        if tree and journal and paper:
            candidate = pathlib.Path(tree) / "data" / journal / paper
            if candidate.is_dir():
                return candidate
    derived = _tree_call("i4r_paper_dir_of_audit", audit)
    if derived and pathlib.Path(derived).parent.parent.name == "data" and pathlib.Path(derived).is_dir():
        return pathlib.Path(derived)
    return None


def claimed_after(audit: pathlib.Path, filed_at: Any) -> bool | None:
    """True when the paper is reserved by a claim made after the filing; None when that cannot be told."""
    paper = paper_input_dir(audit)
    if paper is None:
        return None
    try:
        result = subprocess.run(["bash", str(HERE / "reserve_paper.sh"), "status", str(paper)],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"RESERVED by .* since (\S+)", result.stdout)
    if not match:
        return False
    try:
        # publish releases the reservation after journaling the filing, so a claim held now and stamped no earlier
        # than the filing (to the second) is a re-claim
        return lifecycle_journal.parse_time(match.group(1)) >= lifecycle_journal.parse_time(str(filed_at))
    except (lifecycle_journal.JournalError, ValueError):
        return None


def registry_version(root: pathlib.Path) -> tuple[str | None, Any]:
    """(sha256 of finalization_receipt.json, its registry version) of the close being reopened."""
    path = root / "finalization_receipt.json"
    if not path.is_file():
        return None, None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        version = (receipt.get("registry") or {}).get("version") if isinstance(receipt, dict) else None
    except (OSError, ValueError):
        version = None
    return sha256_file(path), version


# ------------------------------------------------------------------------------------------- the reopen act
def reopen_spec(root: pathlib.Path, events: list[dict[str, Any]], run: dict[str, Any], *, reason: str,
                purpose: str, at: str, source: str, archived_set: pathlib.Path | None = None,
                unconfirmed_claim_ok: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Check that a finalized audit may be reopened now and build its audit_reopened event (nothing is written).

    Returns (event spec, notes). Raises AmendmentError with the remedy when it may not. The payload names the
    superseded finalization receipt's read-only copy (preserve_receipt writes it) and, for a published audit, where
    the filed set was moved (``archived_set``, else found under old_runs/)."""
    if purpose not in PURPOSES:
        raise AmendmentError(f"--purpose must be one of {', '.join(PURPOSES)}")
    if not reason or not reason.strip():
        raise AmendmentError("--reason is required: the journal must say why a finalized audit is reopened")
    closing = lifecycle_journal.unreopened_finalize(events)
    if closing is None:
        raise AmendmentError("the audit is not finalized (or its last finalize was already reopened): there is "
                             "nothing to reopen")
    if (root / ".finalize-commit.json").exists():
        raise AmendmentError("an interrupted finalize left .finalize-commit.json; re-run finalize_run.sh once to "
                             "recover it before reopening")
    notes: list[str] = []
    filing = last_filing(events)
    if filing:
        in_place = published_set_in_place(root, filing["release_dir"])
        if in_place:
            raise AmendmentError(
                "this audit is published and its filed set is still in place (" + ", ".join(
                    str(path) for path in in_place) + "). Move it under old_runs/ first (INSTALL.md, 'Correcting "
                "a published comment', step 1): A=old_runs/<Journal>/<Paper>.published-$(date -u +%Y%m%dT%H%M%SZ); "
                "mkdir -p \"$A\"; mv comments/<Journal>/<Paper> \"$A/comments\"; mv metadata/<Journal>/<Paper> "
                "\"$A/metadata\"; then claim the paper again (step 2)")
        claimed = claimed_after(root, filing["filed_at"])
        if claimed is False:
            raise AmendmentError(
                "this audit is published and its paper is not claimed again since the filing: run bash "
                "scripts/reserve_paper.sh claim \"data/<Journal>/<Paper>\", with the paper's true folder name from "
                "the audit's paper_source.json (INSTALL.md, 'Correcting a published comment', step 2), then reopen")
        if claimed is None and not unconfirmed_claim_ok:
            raise AmendmentError(
                "this audit is published and its paper's re-claim cannot be confirmed: no data/<Journal>/<Paper> "
                "folder was found for it (named by the audit's paper_source.json, else by its runs/ path) under the "
                "audit's tree root, or reserve_paper.sh status could not be read. The claim guards against a "
                "concurrent audit of the same paper once its published set is moved. Claim it again (INSTALL.md, "
                "'Correcting a published comment', step 2); only when the audit sits outside a tree, pass "
                "--unconfirmed-claim to reopen without that check")
        if claimed is None:
            notes.append("--unconfirmed-claim: the paper's re-claim was not confirmed (no data/<Journal>/<Paper> "
                         "folder or no readable reservation); make sure no other audit of this paper runs")
        archived_to, archive_note = published_set_archive(root, filing["release_dir"], archived_set)
        filing = dict(filing, archived_to=archived_to)
        if archive_note:
            notes.append(archive_note)
    receipt_sha, version = registry_version(root)
    payload = {
        "reason": reason.strip(), "purpose": purpose,
        "previous_finalize_seq": closing.get("seq"), "previous_finalized_at": closing.get("at"),
        "previous_stopping_reason": run.get("stopping_reason"), "previous_rounds_run": run.get("rounds_run"),
        "previous_receipt_sha256": receipt_sha, "previous_registry_version": version,
        # the superseded receipt is kept read-only beside the audit: a re-finalize rewrites finalization_receipt.json
        "previous_receipt_archive": ({"name": receipt_archive_name(closing), "sha256": receipt_sha}
                                     if receipt_sha else None),
        "previous_publication": filing, "toolkit": host_toolkit(),
    }
    return {"type": REOPEN_EVENT, "payload": payload, "at": at, "source": source}, notes


RECEIPT = "finalization_receipt.json"


def receipt_archive_name(closing: dict[str, Any]) -> str:
    return f"finalization_receipt.superseded-{closing.get('seq')}.json"


def preserve_receipt(root: pathlib.Path, spec: dict[str, Any]) -> pathlib.Path | None:
    """Write the reopen's superseded-receipt copy (0400) that its payload names; None when the close had no receipt.
    Returns the path when this call created it (the caller removes it if the journal append then fails)."""
    archive = (spec.get("payload") or {}).get("previous_receipt_archive")
    if not isinstance(archive, dict):
        return None
    source, target = root / RECEIPT, root / str(archive["name"])
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != archive.get("sha256"):
        raise AmendmentError(f"{RECEIPT} changed while the reopen was being prepared; run it again")
    if target.exists() or target.is_symlink():
        if target.is_file() and not target.is_symlink() and sha256_file(target) == archive.get("sha256"):
            return None   # an earlier attempt kept the same bytes
        raise AmendmentError(f"{target.name} already exists with other bytes; it names the receipt of the close being "
                             "reopened and is never overwritten")
    with open(target, "xb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    os.chmod(target, 0o400)
    return target


def discard(path: pathlib.Path | None) -> None:
    if path is not None and path.exists():
        os.chmod(path, 0o600)
        path.unlink()


def published_set_archive(audit: pathlib.Path, release_dir: str,
                          given: pathlib.Path | None = None) -> tuple[str | None, str | None]:
    """Where the filed set of the close being reopened now is, relative to the tree root: ``given`` (--archived-set),
    else the newest old_runs/<Journal>/<Paper>.published-* whose metadata/finalization_receipt.json is this audit's
    receipt (the filed copy). Returns (path or None, a note when it could not be told)."""
    tree = None
    for spelling in (audit, audit.resolve()):
        tree = _tree_call("i4r_root_of_audit", spelling)
        if tree:
            break
    root = pathlib.Path(tree).resolve() if tree else None

    def relative(path: pathlib.Path) -> str:
        path = path.resolve()
        if root is not None and (path == root or root in path.parents):
            return path.relative_to(root).as_posix()
        return str(path)
    if given is not None:
        given = pathlib.Path(given)
        if not given.is_dir() or not ((given / "comments").is_dir() or (given / "metadata").is_dir()):
            raise AmendmentError(f"--archived-set {given} is not a folder holding the moved comments/ or metadata/")
        return relative(given), None
    receipt = audit / RECEIPT
    receipt_sha = sha256_file(receipt) if receipt.is_file() else None
    journal, paper = paper_source(audit)
    parts = release_dir.split("/")
    journal = journal or (parts[1] if len(parts) >= 3 else None)
    names = {name for name in (paper, parts[-1] if parts else None, pathlib.Path(audit).resolve().parent.name) if name}
    found: list[pathlib.Path] = []
    if root is not None and journal and receipt_sha:
        base = root / "old_runs" / journal
        for name in names:
            for candidate in base.glob(glob.escape(name) + ".published-*"):
                filed = candidate / "metadata" / RECEIPT
                if candidate.is_dir() and filed.is_file() and sha256_file(filed) == receipt_sha:
                    found.append(candidate)
    if found:
        return relative(sorted(set(found), key=lambda path: path.name)[-1]), None
    return None, ("could not find the moved filed set under old_runs/<Journal>/<Paper>.published-* (its "
                  "metadata/finalization_receipt.json is this audit's receipt); the reopen records no location: pass "
                  "--archived-set <folder> to record it")


# ------------------------------------------------------------------------------------------- the verifier
def _history(policy: dict[str, Any]) -> list[Any]:
    value = (policy.get("critique") or {}).get("round_cap_amendments")
    return value if isinstance(value, list) else []


def _round_of(event: dict[str, Any]) -> Any:
    return (event.get("payload") or {}).get("round")


def enforced(run: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    """Rule 5 is an error from protocol 2.6.9, or once the audit has entered the amend/reopen regime. A record
    initialised earlier (a parseable protocol_version below 2.6.9) could run a round after a finalize, so there it
    is a warning; an unreadable protocol_version cannot prove that and is held to the rule."""
    version = version_tuple(run.get("protocol_version"))
    return version is None or version >= ENFORCED_FROM or any(
        event.get("type") in (AMEND_EVENT, REOPEN_EVENT) for event in events)


def applies(events: list[dict[str, Any]], policy: dict[str, Any] | None) -> bool:
    """compliance.policy_amendments_valid is emitted only for an audit with an amendment or a reopening."""
    return bool(any(event.get("type") in (AMEND_EVENT, REOPEN_EVENT) for event in events)
                or (policy is not None and _history(policy)))


def _envelope_of(name: str, data: bytes | None) -> dict[str, Any]:
    """load_envelope on bytes (the file's, or an overlay's)."""
    if data is None:
        raise ValueError(f"{name} is missing or is not a regular file")
    try:
        envelope = json.loads(data.decode("utf-8"))
        policy = envelope["policy"]
        if envelope.get("snapshot_version") != "1" or envelope.get("policy_sha256") != policy_hash(policy):
            raise ValueError("policy hash is invalid")
    except (UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a valid policy snapshot envelope: {exc}") from exc
    return envelope


def _snapshot_binding(event: dict[str, Any]) -> Any:
    """The snapshot sha256 an event binds the run to (a policy_amendment, or a metadata_correction that sets
    toolkit.pipeline_policy_snapshot_sha256: repin_critic_route.py's rebinding), else None."""
    payload = event.get("payload") or {}
    if event.get("type") == AMEND_EVENT:
        return payload.get("snapshot_sha256")
    if event.get("type") == "metadata_correction":
        toolkit = (payload.get("set") or {}).get("toolkit") if isinstance(payload.get("set"), dict) else None
        if isinstance(toolkit, dict) and toolkit.get("pipeline_policy_snapshot_sha256"):
            return toolkit["pipeline_policy_snapshot_sha256"]
    return None


def verify(root: pathlib.Path, run: dict[str, Any] | None = None,
           events: list[dict[str, Any]] | None = None,
           overlay: dict[str, bytes] | None = None) -> tuple[list[str], list[str]]:
    """(violations, warnings) for the audit's round-cap amendments and reopenings. Empty for an audit with neither
    and a cap equal to the one it was initialised with.

    ``overlay`` maps file names in the audit folder to the bytes they WOULD hold: amend_round_cap.py and
    reopen_audit.py verify the prospective state (their events projected onto the journal, the new snapshot and its
    archive) before they write anything."""
    root = pathlib.Path(root).resolve()
    overlay = dict(overlay or {})
    run = run if run is not None else read_run(root)
    if run.get("schema_version") not in {"2.0.0", "2.1.0"} or run.get("normalization"):
        return [], []
    events = events if events is not None else (
        lifecycle_journal.load_events(root) if (root / lifecycle_journal.JOURNAL_NAME).is_file() else [])

    def read(name: str) -> bytes | None:
        if name in overlay:
            return overlay[name]
        path = root / name
        return path.read_bytes() if path.is_file() and not path.is_symlink() else None

    violations: list[str] = []
    warnings: list[str] = []
    try:
        current_bytes = read(SNAPSHOT)
        policy = _envelope_of(SNAPSHOT, current_bytes)["policy"]
    except ValueError as exc:
        return [f"round-cap amendment check: {exc}"], []
    snapshot_cap = (policy.get("critique") or {}).get("round_cap")
    history = _history(policy)
    amendments = [event for event in events if event.get("type") == AMEND_EVENT]
    reopenings = [event for event in events if event.get("type") == REOPEN_EVENT]

    # (1) the snapshot history and the journal are one list, written by the tool
    for event in amendments:
        if event.get("source") != AMEND_SOURCE:
            violations.append(f"policy_amendment at journal seq {event.get('seq')} was not written by "
                              f"scripts/amend_round_cap.py (source {event.get('source')!r})")
    for event in reopenings:
        if event.get("source") not in REOPEN_SOURCES:
            violations.append(f"audit_reopened at journal seq {event.get('seq')} was not written by "
                              f"scripts/reopen_audit.py or amend_round_cap.py (source {event.get('source')!r})")
    if len(history) != len(amendments):
        violations.append(
            f"the policy snapshot records {len(history)} round-cap amendment(s) but the journal has "
            f"{len(amendments)} policy_amendment event(s): the snapshot or the journal was changed by hand")
    for index, (entry, event) in enumerate(zip(history, amendments)):
        payload = event.get("payload") or {}
        entry = entry if isinstance(entry, dict) else {}
        for key in ("from", "to", "reason", "previous_snapshot_sha256", "previous_policy_sha256",
                    "archived_snapshot"):
            if entry.get(key) != payload.get(key):
                violations.append(f"round-cap amendment {index + 1}: the snapshot history's {key} differs from the "
                                  f"journal's policy_amendment (seq {event.get('seq')})")
        if payload.get("leaf") != LEAF:
            violations.append(f"policy_amendment seq {event.get('seq')} amends {payload.get('leaf')!r}; only "
                              f"{LEAF} may be amended")
        if index > 0 and payload.get("previous_snapshot_sha256") != (amendments[index - 1].get("payload") or {}).get(
                "snapshot_sha256"):
            violations.append(f"round-cap amendment {index + 1} does not start from the snapshot the previous "
                              "amendment wrote")
    current_sha = hashlib.sha256(current_bytes or b"").hexdigest()
    if amendments and (amendments[-1].get("payload") or {}).get("snapshot_sha256") != current_sha:
        violations.append("pipeline-policy.snapshot.json is not the snapshot the last policy_amendment wrote (it "
                          "changed afterwards)")

    # (2) each archived snapshot is the replaced one, and the successor differs only where an amendment may
    successors = [(entry, event) for entry, event in zip(history, amendments)]
    for index, (entry, event) in enumerate(successors):
        payload = event.get("payload") or {}
        name = str(payload.get("archived_snapshot") or "")
        if not re.fullmatch(r"pipeline-policy\.snapshot\.superseded-[0-9TZ]+(?:-[0-9]+)?\.json", name):
            violations.append(f"round-cap amendment {index + 1} names no archived snapshot in the audit folder")
            continue
        archived_bytes = read(name)
        try:
            before = _envelope_of(name, archived_bytes)
        except ValueError as exc:
            violations.append(f"round-cap amendment {index + 1}: archived snapshot {name}: {exc}")
            continue
        if hashlib.sha256(archived_bytes or b"").hexdigest() != payload.get("previous_snapshot_sha256"):
            violations.append(f"round-cap amendment {index + 1}: archived snapshot {name} is not the snapshot it "
                              "replaced (its hash differs)")
        if policy_hash(before["policy"]) != payload.get("previous_policy_sha256"):
            violations.append(f"round-cap amendment {index + 1}: archived snapshot {name}'s policy hash differs "
                              "from the one journaled")
        if index + 1 < len(successors):
            try:
                following = str((successors[index + 1][1].get("payload") or {}).get("archived_snapshot"))
                after_policy = _envelope_of(following, read(following))["policy"]
            except ValueError:
                continue   # reported for that amendment
        else:
            after_policy = policy
        changed = diff_paths(before["policy"], after_policy)
        unexpected = sorted(path for path in changed if path not in AMENDABLE_PATHS)
        if unexpected:
            violations.append(f"round-cap amendment {index + 1} changed the policy beyond the cap: "
                              + ", ".join(unexpected[:5]))
        before_cap = (before["policy"].get("critique") or {}).get("round_cap")
        after_cap = (after_policy.get("critique") or {}).get("round_cap")
        if before_cap != payload.get("from") or after_cap != payload.get("to"):
            violations.append(f"round-cap amendment {index + 1}: the snapshots' caps ({before_cap} -> {after_cap}) "
                              f"differ from the journaled {payload.get('from')} -> {payload.get('to')}")
        if not isinstance(payload.get("to"), int) or not isinstance(payload.get("from"), int) \
                or payload["to"] <= payload["from"]:
            violations.append(f"round-cap amendment {index + 1} does not raise the cap")
        if _history(after_policy)[:len(_history(before["policy"]))] != _history(before["policy"]):
            violations.append(f"round-cap amendment {index + 1} rewrote earlier history entries")

    # (3) and (4): the round plans and the snapshot in force when each was prepared. The snapshot in force is the
    # one the run bound at init, rebound by every journaled rewrite before it: a critic re-pin
    # (repin_critic_route.py's metadata_correction) as well as each amendment.
    initial = lifecycle_journal.initial_run_of(events)
    initial_sha = ((initial or {}).get("toolkit") or {}).get("pipeline_policy_snapshot_sha256") \
        if isinstance(initial, dict) else None
    if amendments:
        def in_force(seq: int) -> Any:
            chosen = initial_sha or (amendments[0].get("payload") or {}).get("previous_snapshot_sha256")
            for event in events:
                bound = _snapshot_binding(event)
                if bound and isinstance(event.get("seq"), int) and event["seq"] < seq:
                    chosen = bound
            return chosen
        for event in amendments:
            if in_force(event.get("seq", 0)) != (event.get("payload") or {}).get("previous_snapshot_sha256"):
                violations.append(f"the policy_amendment at journal seq {event.get('seq')} does not start from the "
                                  "snapshot the run was bound to when it was made")
        live: dict[Any, dict[str, Any]] = {}
        for event in events:
            if event.get("type") == "round_prepared":
                live[_round_of(event)] = event
        for number, event in sorted(live.items(), key=lambda item: str(item[0])):
            bound = ((event.get("payload") or {}).get("plan") or {}).get("policy_sha256")
            if bound != in_force(event.get("seq", 0)):
                violations.append(f"round {number}'s plan (journal seq {event.get('seq')}) does not bind the policy "
                                  "snapshot in force when it was prepared")
        for event in amendments:
            payload = event.get("payload") or {}
            early = sorted({_round_of(prepared) for prepared in events
                            if prepared.get("type") == "round_prepared" and prepared.get("seq", 0) < event.get("seq", 0)
                            and isinstance(_round_of(prepared), int) and isinstance(payload.get("from"), int)
                            and _round_of(prepared) > payload["from"]})
            if early:
                violations.append(f"round(s) {early} were prepared above the cap of {payload.get('from')} before the "
                                  f"amendment (seq {event.get('seq')}) raised it")

    # (5) no round run after a finalize without a reopen
    closed_at = None
    late: list[str] = []
    for event in events:
        if event.get("type") == "finalize":
            closed_at = event.get("seq")
        elif event.get("type") == REOPEN_EVENT:
            closed_at = None
        elif closed_at is not None and event.get("type") in lifecycle_journal.ROUND_RUN_EVENTS:
            late.append(f"{event.get('type')} round {_round_of(event)} (seq {event.get('seq')})")
    if late:
        message = ("round events after a finalize with no audit_reopened between them: " + ", ".join(late[:4])
                   + (f" and {len(late) - 4} more" if len(late) > 4 else ""))
        (violations if enforced(run, events) else warnings).append(message)

    # (6) the cap at init, raised by each amendment in turn, is the snapshot's and the run's
    initial_cap = initial.get("round_cap") if isinstance(initial, dict) else None
    expected = lifecycle_journal.journaled_round_cap(events)
    if amendments and initial_cap is not None and (amendments[0].get("payload") or {}).get("from") != initial_cap:
        violations.append(f"the first round-cap amendment starts from {(amendments[0].get('payload') or {}).get('from')}"
                          f", not the cap of {initial_cap} the audit was initialised with")
    if expected is None and amendments:
        expected = (amendments[-1].get("payload") or {}).get("to")
    if expected is not None and snapshot_cap != expected:
        violations.append(f"the policy snapshot's round cap is {snapshot_cap}, but the audit was initialised with "
                          f"{initial_cap}" + (f" and amended to {expected}" if amendments else "")
                          + ": a cap changes only through scripts/amend_round_cap.py")
    if run.get("round_cap") != snapshot_cap:
        violations.append(f"run.round_cap {run.get('round_cap')} differs from the policy snapshot's cap {snapshot_cap}")
    if snapshot_cap not in LIVE_ROUND_CAPS:
        violations.append(f"the policy snapshot's round cap {snapshot_cap} is not a live cap {list(LIVE_ROUND_CAPS)}")

    # (7) no round event names a round above the cap in force when it was journaled (the cap at init, raised by the
    # amendments before it). A round above the cap was never allowed by any release; a hand-logged or library-appended
    # one is a permanent record of a round the audit had no right to run.
    above: list[str] = []
    for event in events:
        number = _round_of(event)
        if event.get("type") not in lifecycle_journal.ROUND_EVENT_TYPES or isinstance(number, bool) \
                or not isinstance(number, int) or not isinstance(event.get("seq"), int):
            continue
        cap_then = lifecycle_journal.journaled_round_cap(events, before_seq=event["seq"])
        if cap_then is not None and number > cap_then:
            above.append(f"{event.get('type')} round {number} (seq {event['seq']}, cap {cap_then})")
    if above:
        violations.append("round events above the round cap in force when they were journaled (only "
                          "scripts/amend_round_cap.py raises a cap, before any such round): " + ", ".join(above[:4])
                          + (f" and {len(above) - 4} more" if len(above) > 4 else ""))
    return violations, warnings


def prospective(root: pathlib.Path, events: list[dict[str, Any]], specs: list[dict[str, Any]],
                overlay: dict[str, bytes] | None = None) -> list[str]:
    """The violations verify() would report once ``specs`` are journaled after ``events`` and the files in
    ``overlay`` are written: an amend or a reopen proves the state it would leave BEFORE it writes anything, so
    neither its dry run nor its real run can pass its checks and then leave a permanent violation behind."""
    additions: list[dict[str, Any]] = []
    previous = events[-1].get("event_hash") if events else None
    for spec in specs:
        event = lifecycle_journal._new_event(len(events) + len(additions) + 1, previous, str(spec["type"]),
                                             dict(spec.get("payload") or {}), at=spec.get("at"),
                                             source=str(spec.get("source") or "lifecycle_journal.py"))
        additions.append(event)
        previous = event["event_hash"]
    candidate = events + additions
    try:
        run = lifecycle_journal.prove_projectable(candidate)
    except (KeyError, TypeError, ValueError, AttributeError, lifecycle_journal.JournalError) as exc:
        return [f"the journal would not be projectable: {exc}"]
    violations, _ = verify(root, run, candidate, overlay=overlay)
    return violations


def check_round(root: pathlib.Path, round_number: int) -> str | None:
    root = pathlib.Path(root).resolve()
    events = lifecycle_journal.load_events(root) if (root / lifecycle_journal.JOURNAL_NAME).is_file() else []
    return lifecycle_journal.round_refusal(root, events, round_number)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["check-round"]:
        if len(argv) != 3 or not argv[2].isdigit():
            print("usage: round_cap_amendment.py check-round <audit-dir> <round>", file=sys.stderr)
            return 2
        try:
            refusal = check_round(pathlib.Path(argv[1]), int(argv[2]))
        except (OSError, ValueError) as exc:
            print(f"could not read the audit's journal: {exc}", file=sys.stderr)
            return 2
        if refusal:
            print(refusal, file=sys.stderr)
            return 1
        return 0
    if len(argv) != 1 or argv[0].startswith("-"):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    try:
        violations, warnings = verify(pathlib.Path(argv[0]))
    except (OSError, ValueError, AmendmentError) as exc:
        print(f"FATAL: round_cap_amendment.py: {exc}", file=sys.stderr)
        return 2
    for message in warnings:
        print(f"WARNING: {message}", file=sys.stderr)
    for message in violations:
        print(f"VIOLATION: {message}", file=sys.stderr)
    if violations:
        return 1
    print("round-cap amendments and reopenings: consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
