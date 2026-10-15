#!/usr/bin/env python3
"""The Claude routes the skill can run, and how a served model is matched to its route (2.7.0, 2.7.3).

Every Claude role (the coordinator, the five Phase-1 auditors, their capability probe and the
post-fan-out verifier) takes its route from the audit's own frozen
``pipeline-policy.snapshot.json``, never from ``config/pipeline-policy-v2.json`` after init. 2.7.0
moves NEW audits to Claude Sonnet 5.5; an audit initialised earlier keeps Sonnet 5 for its whole
life, so its packets, probe keys, receipts, prompts and verifier re-admission are byte-identical to
2.6.10's. This module is the single allow-set of routes both generations need, following the
``validate_v2.LIVE_ROUND_CAPS`` precedent:

- ``SONNET_5`` is the legacy route, spawned with the CLI alias ``sonnet``. Its served-model check is
  the loose pre-2.7.0 regex, unchanged, so an in-flight audit is judged exactly as before.
- ``SONNET_5_5`` is spawned with the full model id ``claude-sonnet-5-5``, never an alias: on Claude
  CLI 2.1.206 the alias ``sonnet`` still serves claude-sonnet-5. Its served-model check is exact, so
  a call the CLI routed to another Sonnet is refused instead of being recorded as 5.5.

2.7.3 lowers NEW audits' Claude effort one degree, from ``max`` to ``xhigh`` (the CLI's levels are low,
medium, high, xhigh and max): ``SONNET_5_5_XHIGH`` is Sonnet 5.5, spawned exactly as ``SONNET_5_5``, at
``--effort xhigh``, and is what a new audit freezes (``SHIPPED``). On 2.7.0 to 2.7.2 Sonnet 5.5 at max wrote
2.3 to 2.9 times the output tokens of Sonnet 5 at max, about 72.5% of it thinking, so each Claude stage
cost about twice as much at the same list price, and one coordinator response overflowed the CLI's
64k-token per-response cap. The effort is part of the route, so it is frozen per audit like the model:
an audit initialised on 2.7.0 to 2.7.2 keeps ``SONNET_5_5`` (max) and one initialised earlier keeps
``SONNET_5``, each for its whole life. A prompt names the route's own effort ("at xhigh effort");
for a max route, and for a packet with no model block, it renders the 2.7.2 bytes exactly
(``effort_of``). The served-model checks key on ``requested_model`` alone and are unchanged.

Stdlib only. The post-delta receipt's CODE_PATHS files must never import this module.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

ROUTE_KEYS = ("requested_model", "spawn_alias", "provider", "reasoning_effort")

SONNET_5: dict[str, str] = {
    "requested_model": "sonnet-5", "spawn_alias": "sonnet",
    "provider": "anthropic", "reasoning_effort": "max",
}
SONNET_5_5: dict[str, str] = {
    "requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5",
    "provider": "anthropic", "reasoning_effort": "max",
}
# 2.7.3: the same model and spawn, one effort degree lower.
SONNET_5_5_XHIGH: dict[str, str] = {
    "requested_model": "sonnet-5-5", "spawn_alias": "claude-sonnet-5-5",
    "provider": "anthropic", "reasoning_effort": "xhigh",
}
# Every route a frozen snapshot may carry, oldest first. SHIPPED is what a new audit freezes; the
# drift check pins config/pipeline-policy-v2.json to it.
LIVE_ROUTES: tuple[dict[str, str], ...] = (SONNET_5, SONNET_5_5, SONNET_5_5_XHIGH)
SHIPPED: dict[str, str] = SONNET_5_5_XHIGH
# The effort every route ran at before 2.7.3, and the one a packet with no model block (a pre-2.7.0
# packet) or no effort in it names.
LEGACY_EFFORT = "max"

DISPLAY_NAMES = {"sonnet-5": "Claude Sonnet 5", "sonnet-5-5": "Claude Sonnet 5.5"}
SHORT_NAMES = {"sonnet-5": "Sonnet 5", "sonnet-5-5": "Sonnet 5.5"}
# The bare model id each route is served as, for a prompt that names one (the coordinator's
# "never a model name such as ..." example) and for test fixtures.
MODEL_IDS = {"sonnet-5": "claude-sonnet-5", "sonnet-5-5": "claude-sonnet-5-5"}

# The pre-2.7.0 check, byte for byte (run_coordinator.sonnet5). It also matches claude-sonnet-5-5;
# that looseness is kept for SONNET_5 audits only, which were always judged by it.
_LEGACY_SONNET_5 = re.compile(r"(?:^|[-_.])sonnet[-_.]?5(?:$|[-_.])")
# A served Sonnet 5.5 id: the bare id the CLI reports, optionally with a date suffix or a context tag.
_SONNET_5_5 = re.compile(r"^claude-sonnet-5-5(?:-\d{8})?(?:\[[0-9a-z]+\])?$")
# Any Sonnet id at all, for telling a helper model (Haiku) from a second Sonnet in one call.
_ANY_SONNET = re.compile(r"(?:^|[-_.])sonnet(?:$|[-_.])")

# Environment variables that silently remap what the CLI serves for a model alias or the default
# model. A SONNET_5 audit spawns the alias `sonnet`, so an exported ANTHROPIC_DEFAULT_SONNET_MODEL
# on an operator's machine would move it to another model while its rows still say sonnet-5. They
# are removed from every Claude spawn. The environment is not part of any identity.
REMAPPING_ENV = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL")

# 2.7.4: the CLI's per-response output cap. Claude CLI 2.1.206 sends every request with max_tokens set to
# its catalog default for the served model (64,000 for claude-sonnet-5, and for claude-sonnet-5-5, which it
# canonicalises to that row by prefix), unless CLAUDE_CODE_MAX_OUTPUT_TOKENS names another value, which it
# clamps to the row's upper limit (128,000). A response that reaches max_tokens is cut; the CLI resumes it
# in-context three times and then fails the call ("Claude's response exceeded the 64000 output token
# maximum"). The coordinator's findings.jsonl alone is about 55,000 tokens on a 50-row registry, so a
# single Write of it at max effort cannot fit under 64,000. A Sonnet 5.5 route is spawned with the cap at
# the upper limit, whatever the operator exported; other routes get the operator's environment unchanged,
# as in 2.7.3. Like REMAPPING_ENV, the cap enters no packet, prompt, key, receipt or snapshot, and the CLI's
# envelope field modelUsage.<id>.maxOutputTokens reports the catalog default whatever the cap is.
OUTPUT_CAP_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
SONNET_5_5_OUTPUT_CAP = 128_000
OUTPUT_CAPS: dict[str, int] = {"sonnet-5-5": SONNET_5_5_OUTPUT_CAP}


class RouteError(ValueError):
    """A snapshot route the packaged runtime cannot dispatch."""


def project(route: Mapping[str, Any] | None) -> dict[str, Any]:
    """The four route keys of a policy row (extra keys such as must_equal_auditor_family ignored)."""
    route = route if isinstance(route, Mapping) else {}
    return {key: route.get(key) for key in ROUTE_KEYS}


def live_route(route: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The LIVE_ROUTES entry this row names, or None."""
    observed = project(route)
    for candidate in LIVE_ROUTES:
        if observed == candidate:
            return dict(candidate)
    return None


def require_live_route(route: Mapping[str, Any] | None, label: str) -> dict[str, str]:
    found = live_route(route)
    if found is None:
        allowed = "; ".join(describe(candidate) for candidate in LIVE_ROUTES)
        raise RouteError(f"{label} is not a packaged Claude route: {project(route)} (allowed: {allowed})")
    return found


def describe(route: Mapping[str, Any]) -> str:
    return (f"{SHORT_NAMES.get(str(route.get('requested_model')), route.get('requested_model'))} "
            f"({route.get('requested_model')}, spawned as --model {route.get('spawn_alias')}) / "
            f"{route.get('reasoning_effort')}")


def display_name(requested_model: str | None) -> str:
    """The model sentence a prompt names. A packet without a model block is a pre-2.7.0 packet."""
    return DISPLAY_NAMES.get(requested_model or "sonnet-5", DISPLAY_NAMES["sonnet-5"])


def short_name(requested_model: str | None) -> str:
    return SHORT_NAMES.get(requested_model or "sonnet-5", SHORT_NAMES["sonnet-5"])


def model_id(requested_model: str | None) -> str:
    return MODEL_IDS.get(requested_model or "sonnet-5", MODEL_IDS["sonnet-5"])


def effort_of(packet: Mapping[str, Any] | None) -> str:
    """The effort a prompt names for a packet: its model block's reasoning_effort (2.7.3).

    A packet with no model block (pre-2.7.0) or no effort in it is a max-effort packet, so every prompt
    built for a max route renders exactly the bytes 2.7.2 rendered ("at max effort").
    """
    model = packet.get("model") if isinstance(packet, Mapping) else None
    value = model.get("reasoning_effort") if isinstance(model, Mapping) else None
    return value if isinstance(value, str) and value else LEGACY_EFFORT


def requested_model_of(packet: Mapping[str, Any] | None) -> str | None:
    """The requested_model of a packet's model block; None when it has none (a pre-2.7.0 fixture)."""
    model = packet.get("model") if isinstance(packet, Mapping) else None
    value = model.get("requested_model") if isinstance(model, Mapping) else None
    return value if isinstance(value, str) else None


def is_sonnet_5_legacy(model_id: str) -> bool:
    """run_coordinator.sonnet5, unchanged."""
    return bool(_LEGACY_SONNET_5.search(model_id.lower()))


def serves(requested_model: str | None, model_id: str) -> bool:
    """Whether a served model id is the primary model of this route."""
    if requested_model == "sonnet-5-5":
        return bool(_SONNET_5_5.match(model_id.lower()))
    return is_sonnet_5_legacy(model_id)


def primaries(requested_model: str | None, model_ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """(primaries, conflicting Sonnet ids) among a call's served model ids.

    For SONNET_5_5 a served Sonnet that is not 5.5 is a conflict, never a helper: the call did work
    on a model the route does not name. For SONNET_5 the conflict list is always empty and the
    primaries are what the pre-2.7.0 check counted, so two loose matches still refuse as before.
    """
    ids = [str(model) for model in model_ids]
    found = [model for model in ids if serves(requested_model, model)]
    if requested_model != "sonnet-5-5":
        return found, []
    conflicts = [model for model in ids if model not in found and _ANY_SONNET.search(model.lower())]
    return found, conflicts


def output_cap(route: Mapping[str, Any] | None) -> int | None:
    """The packaged per-response output cap of a live route, or None (the CLI's own default)."""
    found = live_route(route)
    return OUTPUT_CAPS.get(found["requested_model"]) if found is not None else None


def spawn_env(
    env: Mapping[str, str] | None = None, *, route: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """The environment a Claude CLI subprocess runs with: the operator's, minus REMAPPING_ENV, and
    (2.7.4) with the route's packaged output cap when it has one. Without a route (`claude --version`)
    or on a route with no cap it is exactly the 2.7.3 environment."""
    source = os.environ if env is None else env
    result = {key: value for key, value in source.items() if key not in REMAPPING_ENV}
    cap = output_cap(route)
    if cap is not None:
        result[OUTPUT_CAP_ENV] = str(cap)
    return result
