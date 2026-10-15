#!/usr/bin/env python3
"""select_subset.py [--pool P] [--stratify proportional|balanced] [--n 25] [--seed S] [--audit-ids FILE] [--write]

Choose the papers the baseline runs on, from published audits whose Phase-1 snapshot is on disk (config/baseline.json
"pools"): "shipped" = Claude roles on Sonnet 5.5 at xhigh with GPT-6.1 Sol critics, the shipped full-pipeline
condition; "sonnet55-xhigh" = either critic model; "sonnet55" = either effort too. A seeded random draw stratified by
whether the deposit holds code: "proportional" keeps the pool's mix, "balanced" takes half and half where the pool
allows. --audit-ids takes an explicit list (one audit id per line) instead. Prints the plan; --write saves
baseline_comments/SUBSET.json. No model call.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import pathlib
import random
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from baseline_common import CONFIG, BaselineError, find_tree, utc_now, write_json  # noqa: E402
import sources  # noqa: E402


def eligible(paper: dict, rule: dict) -> bool:
    return (paper["claude_model"] == rule["claude_model"] and paper["claude_effort"] in rule["claude_effort"]
            and paper["critic_model"] in rule["critic_model"] and paper["snapshot_sha256"] is not None)


def allocate(strata: dict[bool, list], n: int, balanced: bool = False) -> dict[bool, int]:
    total = sum(len(v) for v in strata.values())
    if balanced:
        quota = {k: n / len(strata) for k in strata}
    else:
        quota = {k: n * len(v) / total for k, v in strata.items()}
    take = {k: min(len(strata[k]), int(q)) for k, q in quota.items()}
    order = sorted(strata, key=lambda k: quota[k] - int(quota[k]), reverse=True)
    while sum(take.values()) < n:
        for k in order:
            if sum(take.values()) < n and take[k] < len(strata[k]):
                take[k] += 1
    return take


def draw(papers: list[dict], n: int, seed: int, balanced: bool = False) -> list[dict]:
    strata: dict[bool, list] = collections.defaultdict(list)
    for p in sorted(papers, key=lambda p: p["audit_id"]):
        strata[p["code_present"]].append(p)
    if n > len(papers):
        raise BaselineError(f"only {len(papers)} eligible papers for a subset of {n}")
    rng = random.Random(seed)
    chosen = []
    for key, k in sorted(allocate(strata, n, balanced).items()):
        chosen += rng.sample(strata[key], k)
    return sorted(chosen, key=lambda p: p["audit_id"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=CONFIG["subset"]["n"])
    ap.add_argument("--seed", type=int, default=CONFIG["subset"]["seed"])
    ap.add_argument("--pool", default=CONFIG["subset"]["pool"], choices=sorted(CONFIG["subset"]["pools"]))
    ap.add_argument("--stratify", default=CONFIG["subset"]["stratify"], choices=["proportional", "balanced"])
    ap.add_argument("--audit-ids", type=pathlib.Path)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--replace", action="store_true", help="replace a SUBSET.json that baseline runs already used "
                                                          "(the old file is kept as SUBSET.<time>.json)")
    args = ap.parse_args(argv)
    tree = find_tree()
    papers = sources.published(tree)
    rule = CONFIG["subset"]["pools"][args.pool]
    if args.audit_ids:
        wanted = [l.strip() for l in args.audit_ids.read_text().splitlines() if l.strip() and not l.startswith("#")]
        by_id = {p["audit_id"]: p for p in papers}
        missing = [a for a in wanted if a not in by_id or by_id[a]["snapshot_sha256"] is None]
        if missing:
            raise BaselineError(f"not a published audit with a Phase-1 snapshot: {', '.join(missing)}")
        chosen, method = [by_id[a] for a in wanted], f"explicit list ({args.audit_ids.name})"
        pool_ids = sorted(wanted)
    else:
        pool_papers = [p for p in papers if eligible(p, rule)]
        chosen = draw(pool_papers, args.n, args.seed, args.stratify == "balanced")
        method = (f"seeded random draw (seed {args.seed}) of {args.n} from the {len(pool_papers)} audits of pool "
                  f"'{args.pool}' ({rule['note']}), stratified by code_present ({args.stratify})")
        pool_ids = sorted(p["audit_id"] for p in pool_papers)
    pool = len(pool_ids)
    by_code = collections.Counter(p["code_present"] for p in chosen)
    print(f"{len(papers)} published audits; {pool} in the pool; {len(chosen)} chosen "
          f"({by_code[True]} with code, {by_code[False]} without)")
    print(f"versions: {dict(collections.Counter(p['protocol_version'] for p in chosen))}")
    print(f"journals: {dict(collections.Counter(p['journal'] for p in chosen))}")
    if args.write:
        out = tree / CONFIG["output_dir"]
        out.mkdir(exist_ok=True)
        target = out / "SUBSET.json"
        used = any(out.glob("*/*/run.json")) or (out / "RUNLOG.jsonl").exists()
        if used and not args.replace:
            raise BaselineError("baseline runs already exist; changing the papers now changes the experiment. Pass "
                                "--replace to keep the old SUBSET.json aside and write a new one")
        if target.exists():
            target.rename(out / f"SUBSET.{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.json")
        write_json(target, {
            "schema_version": "i4r-baseline-subset-1", "created_at": utc_now(), "method": method,
            "pool_name": None if args.audit_ids else args.pool, "rule": None if args.audit_ids else rule,
            "stratify": None if args.audit_ids else args.stratify, "seed": None if args.audit_ids else args.seed,
            "n": len(chosen), "pool_size": pool, "pool_audit_ids": pool_ids,
            "pool_sha256": hashlib.sha256("\n".join(pool_ids).encode()).hexdigest(), "papers": chosen,
        })
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaselineError as exc:
        print(f"select_subset: {exc}", file=sys.stderr)
        sys.exit(1)
