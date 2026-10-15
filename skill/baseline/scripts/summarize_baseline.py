#!/usr/bin/env python3
"""summarize_baseline.py [--json FILE]: baseline results beside the full pipeline's, paper by paper.

For every subset paper it reads the baseline's current run (baseline_comments/<journal>/<paper>/run.json and
findings.json), every baseline run ever made for it (baseline_comments/RUNLOG.jsonl, failed and superseded runs
included), and the full pipeline's metadata/<journal>/<paper>/{run.json, comment.json} for the SAME audit id, and
reports the same quantities for both. Resources count every run and every call that started inference; calls and
hours cover every model, cost and tokens Claude's only (the GPT critics' cost is not recorded), and cost is also given
as if every prompt-cache write had the full runs' 1-hour lifetime. Outcomes are reported over every paper attempted,
over the papers with a comment, and over those within the page cap. Counts only; no title or author.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import statistics as st
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import baseline_common as bc  # noqa: E402
from run_baseline import final_dir, load_subset, out_root  # noqa: E402

# what the agent was asked and how its calls ran: a different prompt or cache lifetime is a different condition
CONDITION_KEYS = ("requested_model", "reasoning_effort", "max_budget_usd", "timeout_seconds", "prompt_template_sha256",
                  "prompt_cache_ttl")
PRICES = {k: v for k, v in bc.CONFIG.get("cache_write_usd_per_mtok", {}).items() if isinstance(v, dict)}


def at_1h_cache(cost, writes: dict | None, model: str = "claude-sonnet-5-5"):
    """The cost as if every prompt-cache write had a 1-hour lifetime, the full runs' usual one (None if unknown)."""
    price = PRICES.get(model)
    if not isinstance(cost, (int, float)) or not writes or not price:
        return None
    return round(cost + (writes.get("5m") or 0) * (price["1h"] - price["5m"]) / 1e6, 4)


def envelope_cache_writes(audit: pathlib.Path, invocation: dict) -> dict | None:
    """A pipeline call's cache writes by lifetime, from its raw CLI envelope (run.json does not record them)."""
    art = (invocation.get("model_identity_evidence") or {}).get("artifact")
    if not art:
        return None
    try:
        text = (audit / art).read_text(errors="replace")
        try:
            env = json.loads(text)
        except json.JSONDecodeError:
            env = [x for x in (json.loads(l) for l in text.splitlines() if l.strip()) if x.get("type") == "result"][-1]
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return bc.cache_writes(env) if isinstance(env, dict) else None


def page_cap(meta: pathlib.Path) -> dict:
    """The published comment's body pages and cap, from its document build manifest."""
    try:
        pages = json.loads((meta / "document_build_manifest.json").read_text()).get("pages") or {}
    except (OSError, ValueError, AttributeError):
        return {"body_pages": None, "over_page_cap": None}
    body, cap, abstract = pages.get("body_pages"), pages.get("body_page_cap"), pages.get("abstract_ending_page")
    over = (body > cap or abstract != 1) if isinstance(body, int) and isinstance(cap, int) else None
    return {"body_pages": body, "over_page_cap": over}


def finding_mix(f: list[dict]) -> dict:
    return {
        "findings": len(f), "changes_conclusion": sum(1 for x in f if x.get("changes_conclusion")),
        "disclosure": sum(1 for x in f if x.get("disclosure_flag")),
        "unverifiable": sum(1 for x in f if x.get("impact") == "unverifiable"),
        "unverifiable_reasons": dict(collections.Counter(x.get("unverifiable_reason") for x in f
                                                         if x.get("impact") == "unverifiable")),
        "paper_only": sum(1 for x in f if x.get("paper_only") is True),
        "impact": dict(collections.Counter(x.get("impact") for x in f)),
    }


def pipeline_side(tree: pathlib.Path, paper: dict) -> dict:
    meta = tree / "metadata" / paper["journal"] / paper["metadata_folder"]
    audit = tree / "runs" / paper["journal"] / paper["runs_folder"] / "audit"
    run = json.loads((meta / "run.json").read_text())
    if run.get("audit_id") != paper["audit_id"]:
        return {"source_changed": run.get("audit_id")}
    comment = json.loads((meta / "comment.json").read_text())
    f = comment.get("findings") or []
    started = [x for x in run.get("invocations") or [] if x.get("inference_start_status", "started") == "started"]
    claude = [x for x in started if x.get("model_family") == "claude"]
    other = [x for x in started if x.get("model_family") != "claude"]
    costs = [(x.get("provider_cost") or {}).get("amount") for x in claude]
    toks = [(x.get("usage") or {}).get("total_tokens") for x in claude]
    cost = sum(costs) if costs and all(isinstance(c, (int, float)) for c in costs) else None
    splits = [envelope_cache_writes(audit, x) for x in claude]
    writes = ({"5m": sum(s["5m"] for s in splits), "1h": sum(s["1h"] for s in splits)}
              if splits and all(splits) else None)
    return {
        **finding_mix(f), "has_comment": True,
        "reproduces_headline": (comment.get("package") or {}).get("reproduces_headline"),
        # calls and hours cover every model (Claude roles and GPT critics); cost and tokens are Claude's only
        "model_calls": len(started), "claude_calls": len(claude), "other_model_calls": len(other),
        "other_model_cost_known": all(isinstance((x.get("provider_cost") or {}).get("amount"), (int, float))
                                      for x in other),
        "rounds": len(run.get("rounds") or []),
        "model_hours": ((run.get("durations") or {}).get("provider_active_interval_union_seconds") or 0) / 3600,
        "claude_cost_usd": cost, "cache_writes": writes, "claude_cost_usd_1h_cache": at_1h_cache(cost, writes),
        "claude_tokens": sum(toks) if toks and all(isinstance(t, (int, float)) for t in toks) else None,
        **page_cap(meta),
    }


def baseline_side(folder: pathlib.Path, all_runs: list[dict]) -> dict | None:
    try:
        run = json.loads((folder / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        run = None
    try:
        rec = json.loads((folder / "findings.json").read_text())
        f = [x for x in rec.get("findings") or [] if isinstance(x, dict)]
        repro = (rec.get("package") or {}).get("reproduces_headline")
    except (OSError, json.JSONDecodeError, AttributeError):
        f, repro = [], None
    if run is None and not all_runs:
        return None
    t = (run or {}).get("totals") or {}
    pages = ((run or {}).get("render") or {}).get("pages") or {}
    condition = {k: ((run or {}).get("condition") or {}).get(k) for k in CONDITION_KEYS}
    runs = [r for r in all_runs if not run or {k: (r.get("condition") or {}).get(k) for k in CONDITION_KEYS} == condition]

    def total(key):
        vals = [r.get(key) for r in runs if isinstance(r.get(key), (int, float))]
        return sum(vals) if vals else None
    unknown_cost = sum(r.get("calls_with_unknown_cost") or 0 for r in runs)
    split = [(r.get("cache_writes_5m"), r.get("cache_writes_1h")) for r in runs]
    writes = ({"5m": sum(a for a, _ in split), "1h": sum(b for _, b in split)}
              if split and all(isinstance(a, int) and isinstance(b, int) for a, b in split) else None)
    kept = ((run or {}).get("outputs") or {}).get("kept") or []
    return {
        "status": ((run or {}).get("outcome") or {}).get("status"), "valid": ((run or {}).get("validation") or {}).get("ok"),
        **finding_mix(f), "has_comment": "REPORT_final.md" in kept,
        "reproduces_headline": repro, "rounds": 1,
        "runs": len(runs), "runs_other_condition": len(all_runs) - len(runs),
        "attempts": total("model_calls"), "model_calls": total("calls_with_inference"),
        "claude_calls": total("calls_with_inference"), "other_model_calls": 0,
        # one call at a time, so its wall time is the union of its provider-active intervals (the pipeline's model_hours)
        "model_hours": (total("elapsed_seconds") or 0) / 3600 or None,
        "claude_cost_usd": None if unknown_cost else total("cost_usd"), "calls_with_unknown_cost": unknown_cost,
        "cache_writes": writes,
        "claude_cost_usd_1h_cache": None if unknown_cost else at_1h_cache(total("cost_usd"), writes),
        "claude_tokens": total("total_tokens"),
        "current_run_cost_usd": t.get("cost_usd"), "single_model": t.get("single_model"),
        "observed_models": t.get("observed_models"), "permission_denials": t.get("permission_denials"),
        "body_pages": pages.get("body_pages"), "over_page_cap": pages.get("over_page_cap"),
    }


def med(values):
    v = [x for x in values if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(st.median(v), 2) if v else None


def side_summary(rows: list[dict], side: str) -> dict:
    reasons = collections.Counter()
    for r in rows:
        reasons.update(r[side]["unverifiable_reasons"])
    findings_total = sum(r[side]["findings"] for r in rows)
    return {
        "papers": len(rows),
        "findings_median": med(r[side]["findings"] for r in rows),
        "comments_with_conclusion_change": sum(1 for r in rows if r[side]["changes_conclusion"]),
        "comments_with_disclosure": sum(1 for r in rows if r[side]["disclosure"]),
        "unverifiable_findings": sum(r[side]["unverifiable"] for r in rows), "unverifiable_reasons": dict(reasons),
        "paper_only_share": round(sum(r[side]["paper_only"] for r in rows) / findings_total, 3) if findings_total else None,
        "reproduces_headline": dict(collections.Counter(r[side]["reproduces_headline"] for r in rows)),
        "over_page_cap": sum(1 for r in rows if r[side].get("over_page_cap") is True),
        "page_cap_unknown": sum(1 for r in rows if r[side].get("over_page_cap") is None),
        "papers_with_unknown_cost": sum(1 for r in rows if r[side].get("claude_cost_usd") is None),
        "papers_with_unknown_other_model_cost": sum(1 for r in rows if r[side].get("other_model_calls")
                                                    and not r[side].get("other_model_cost_known")),
        "model_calls_median": med(r[side]["model_calls"] for r in rows),   # every model, calls that started inference
        "claude_calls_median": med(r[side].get("claude_calls", r[side]["model_calls"]) for r in rows),
        "model_hours_median": med(r[side]["model_hours"] for r in rows),   # every model
        "claude_cost_usd_median": med(r[side]["claude_cost_usd"] for r in rows),
        "claude_cost_usd_1h_cache_median": med(r[side].get("claude_cost_usd_1h_cache") for r in rows),
        "papers_with_5m_cache_writes": sum(1 for r in rows if (r[side].get("cache_writes") or {}).get("5m")),
        "claude_tokens_median": med(r[side]["claude_tokens"] for r in rows),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", type=pathlib.Path)
    args = ap.parse_args(argv)
    tree = bc.find_tree()
    subset = load_subset(tree)
    runlog = collections.defaultdict(list)
    log = out_root(tree) / "RUNLOG.jsonl"
    if log.is_file():
        for line in log.read_text().splitlines():
            if line.strip():
                entry = json.loads(line)
                runlog[entry["audit_id"]].append(entry)
    rows, skipped = [], []
    for paper in subset["papers"]:
        base = baseline_side(final_dir(tree, paper), runlog.get(paper["audit_id"], []))
        if base is None:
            continue
        pipe = pipeline_side(tree, paper)
        if "source_changed" in pipe:
            skipped.append({"audit_id": paper["audit_id"], "reason": f"metadata now holds audit {pipe['source_changed']}"})
            continue
        rows.append({"audit_id": paper["audit_id"], "code_present": paper["code_present"], "baseline": base,
                     "pipeline": pipe})
    with_comment = [r for r in rows if r["baseline"]["has_comment"]]
    within_cap = [r for r in with_comment if r["baseline"].get("over_page_cap") is False]
    summary = {
        "subset": subset["method"], "papers": len(subset["papers"]), "papers_run": len(rows),
        "baseline_status": dict(collections.Counter(r["baseline"]["status"] for r in rows)),
        "baseline_runs_total": sum(r["baseline"]["runs"] for r in rows), "skipped": skipped,
        "all_papers_run": {"baseline": side_summary(rows, "baseline"), "pipeline": side_summary(rows, "pipeline")},
        "papers_with_a_baseline_comment": {"baseline": side_summary(with_comment, "baseline"),
                                           "pipeline": side_summary(with_comment, "pipeline")},
        "baseline_comments_within_the_page_cap": {"baseline": side_summary(within_cap, "baseline"),
                                                  "pipeline": side_summary(within_cap, "pipeline")},
    }
    print(json.dumps(summary, indent=1))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
