#!/usr/bin/env python3
"""Workspace certification orchestrator for the drift ecosystem."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Run-snapshot format constants. Mirror the drift-lang toolchain
# contract at tools/drift_deploy/run_snapshot.py. Bump together when
# the toolchain lands a v1 format.
_RUN_SNAPSHOT_FORMAT = "drift-run-snapshot"
_RUN_SNAPSHOT_VERSION = 0

# Sidecar format constants (mirrored from drift-lang).
# A staged package has two JSON sidecars next to its .zdmp:
#   <base>.author-claim                   — signed by the package author
#   <base>.cert-claim.<kid>.json          — signed by the certifier; one
#                                            per cert suite (sorted-first
#                                            wins, matching resolver)
# The outer envelope (`format` + `version`) is stable at v1, but the
# claim *body* schema moved to v2 with the trust-v2 claims (driftc
# 0.33.58+): the body gained artifact_kind / namespaces / required_deps
# (author) and artifact_kind / target / dep_graph (cert). The fields orch
# reads (package_id, version, source_content_id, signatures[0].kid) are
# unchanged, so only the body-schema pins move.
_AUTHOR_CLAIM_FORMAT = "drift-author-claim"
_AUTHOR_CLAIM_VERSION = 1
_AUTHOR_CLAIM_BODY_SCHEMA_VERSION = 2
_CERT_CLAIM_FORMAT = "drift-cert-claim"
_CERT_CLAIM_VERSION = 1
_CERT_CLAIM_BODY_SCHEMA_VERSION = 2

# Strict validators for run-snapshot field shapes. The toolchain's
# loader is strict; orch enforces the same shapes at emit time so
# violations surface at snapshot build, not at gate consume.
_SHA256_HEX_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ED25519_KID_RE = re.compile(r"^ed25519:.+$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RepoConfig:
    name: str
    path: str
    kind: str
    depends_on: list[str]
    commands: dict[str, list[str]]
    requires: list[str] = field(default_factory=list)


@dataclass
class Capability:
    """A declared external capability (cert tool or service).

    The committed ``capabilities`` section declares *policy* only — never host
    facts. Machine facts (paths, host/port, credential env names, instance id)
    come from the host-local ``cert-env.json`` (see ``CertEnv``); the per-run
    ``capabilities.json`` document is the resolved merge that repos read via
    ``DRIFT_CERT_CAPABILITIES``.

    A ``service`` capability is just a shared instance the project connects to;
    the orchestrator models no schemas, locks, or concurrency. Each project
    owns its own sandbox schema(s) against that instance (created/dropped via
    its schema tool, e.g. Mariachi) and may create as many as it needs —
    isolation between projects is by separate schema, so the orchestrator does
    not serialize a shared service across projects.

    Fields:
      - ``id``           : the capability id, e.g. ``"tool:mariachi"``.
      - ``kind``         : ``"tool"`` or ``"service"`` (derived from the id prefix).
      - ``min_version``  : optional tool minimum (preflight-only, not emitted).
      - ``version_argv`` : optional tool version probe argv (``{bin}`` resolved
                           host-locally; preflight-only, not emitted).
    """
    id: str
    kind: str                          # authoritative, derived from the id prefix
    declared_kind: Optional[str] = None  # explicit body `kind`, validated to match
    min_version: Optional[str] = None
    version_argv: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# Trust-v1 cert-suite policy
#
# `drift deploy` emits a v1 cert claim on every invocation. The cert-suite
# policy decides what evidence backs the claim. The orchestrator owns this
# policy because it is the certifier/distributor — project repos must NOT
# specify cert-suite flags in their command recipes.
#
# Two phases:
#   - "stage"   : producer staging (e.g. stage_packages). No standalone
#                 evidence artifact yet → orch passes --cert-suite-no-evidence.
#   - "release" : real release/promotion. Orch computes the evidence digest
#                 from the run's report/log/archive and passes
#                 --cert-suite-evidence-sha256 sha256:<digest>. NOT YET WIRED.
#
# Policy lives at the top of orchestration.json under "cert_suite_policy"
# (action → {phase, suite_id}). A default is applied when the field is
# missing so existing configs continue to work.
# ---------------------------------------------------------------------------

_DEFAULT_CERT_SUITE_POLICY: dict = {
    "stage_packages": {
        "phase": "stage",
        "suite_id": "orch/stage-packages",
    },
}

_CERT_SUITE_FLAGS = (
    "--cert-suite-id",
    "--cert-suite-evidence-sha256",
    "--cert-suite-no-evidence",
)


@dataclass
class OrchestrationConfig:
    schema_version: int
    workspace_root: str
    run_root: str
    state_root: str
    repos: dict[str, RepoConfig]
    environment: dict
    cert_suite_policy: dict = field(default_factory=dict)
    capabilities: dict[str, Capability] = field(default_factory=dict)
    config_name: str = "orchestration"

    @staticmethod
    def load(path: Path) -> "OrchestrationConfig":
        raw = json.loads(path.read_text())
        repos = {}
        stale_affects = []
        for name, r in raw["repos"].items():
            # `affects` was removed from the config model: depends_on is the
            # single source of truth and the downstream-invalidation graph is
            # derived by reversing it (see build_forward_graph). A lingering
            # `affects` key is now meaningless — reject it loudly rather than
            # silently ignore a stale edge.
            if "affects" in r:
                stale_affects.append(name)
            repos[name] = RepoConfig(
                name=name,
                path=r["path"],
                kind=r["kind"],
                depends_on=r.get("depends_on", []),
                commands=r.get("commands", {}),
                requires=r.get("requires", []),
            )
        if stale_affects:
            raise ValueError(
                "`affects` is no longer part of the config model; downstream "
                "invalidation is derived from reversed depends_on. Remove "
                "`affects` from: " + ", ".join(sorted(stale_affects))
            )
        # Derive config_name from the filename, stripping .json.
        config_name = path.stem
        cert_suite_policy = raw.get("cert_suite_policy") or _DEFAULT_CERT_SUITE_POLICY
        capabilities = _parse_capabilities(raw.get("capabilities", {}))
        config = OrchestrationConfig(
            schema_version=raw["schema_version"],
            workspace_root=raw["workspace"]["root"],
            run_root=raw["workspace"]["run_root"],
            state_root=raw["workspace"]["state_root"],
            repos=repos,
            environment=raw.get("environment", {}),
            cert_suite_policy=cert_suite_policy,
            capabilities=capabilities,
            config_name=config_name,
        )
        _validate_repo_recipes(config)
        _validate_dependency_graph(config)
        _validate_capabilities(config)
        return config

    @property
    def lock_filename(self) -> str:
        return f"{self.config_name}.workspace-lock.json"

    @property
    def lock_path(self) -> Path:
        return Path(self.state_root) / self.lock_filename


def _validate_repo_recipes(config: OrchestrationConfig) -> None:
    """Reject repo recipes that try to specify cert-suite flags directly.

    Cert-suite policy is owned by the orchestrator (see
    ``cert_suite_policy`` in orchestration.json). A repo recipe that
    embeds ``--cert-suite-id`` etc. would shadow orch policy and split
    the contract across actors. Fail fast at config load.
    """
    for repo_name, repo in config.repos.items():
        for action, cmd in repo.commands.items():
            if not isinstance(cmd, list):
                continue
            for token in cmd:
                if token in _CERT_SUITE_FLAGS:
                    raise ValueError(
                        f"repo {repo_name!r} command {action!r} contains "
                        f"{token!r}; cert-suite policy is orch-owned. "
                        f"Move it to orchestration.json:cert_suite_policy "
                        f"and remove the flag from the recipe."
                    )


def _validate_dependency_graph(config: OrchestrationConfig) -> None:
    """Reject ``depends_on`` edges that point at an unconfigured repo.

    A ``depends_on`` edge is a provider edge in the *staging* graph: if A
    declares ``A -> B`` the orchestrator must stage B (and B's closure)
    before certifying A. If B is not a configured repo the orchestrator
    cannot stage it, so this is a hard config error — not a silent omission
    that would later surface as a missing-package build failure deep in a
    gate run.

    Every edge is checked here, so the derived downstream-invalidation graph
    (``build_forward_graph``, the reverse of ``depends_on``) is likewise
    guaranteed to reference only configured repos.
    """
    errors: list[str] = []
    for name, repo in config.repos.items():
        for dep in repo.depends_on:
            if dep not in config.repos:
                errors.append(
                    f"  repo {name!r} depends_on unknown repo {dep!r}"
                )
    if errors:
        raise ValueError(
            "invalid depends_on edges — every provider must be a configured "
            "repo:\n" + "\n".join(errors)
        )


_CAPABILITY_KINDS = ("tool", "service")


def _parse_capabilities(raw: dict) -> dict[str, "Capability"]:
    """Parse the committed ``capabilities`` section into Capability objects.

    Ids are ``<kind>:<name>`` (e.g. ``"tool:mariachi"``); kind is derived from
    the prefix. Only behavior policy is read here — host facts live in
    ``cert-env.json``. Structural validity is enforced by
    ``_validate_capabilities`` once the whole config is assembled.
    """
    caps: dict[str, Capability] = {}
    for cap_id, body in raw.items():
        kind = cap_id.split(":", 1)[0] if ":" in cap_id else ""
        body = body or {}
        caps[cap_id] = Capability(
            id=cap_id,
            kind=kind,
            declared_kind=body.get("kind"),
            min_version=body.get("min_version"),
            version_argv=body.get("version_argv"),
        )
    return caps


def _validate_capabilities(config: OrchestrationConfig) -> None:
    """Validate the capability contract and every repo ``requires`` edge.

    Two failure classes, both host-independent and surfaced at config load:
      - structural: each capability id is ``tool:<name>`` / ``service:<name>``
        with a known kind; an explicit ``kind`` field must match the id prefix;
        ``version_argv`` (if present) is a list of strings; ``min_version`` (if
        present) parses as a semver-like string.
      - referential: every repo ``requires`` id names a declared capability —
        same discipline as the unknown-``depends_on`` rejection. (Whether the
        capability is *available on this host* is a separate, host-specific
        check done by the preflight, not here.)
    """
    errors: list[str] = []
    for cap_id, cap in config.capabilities.items():
        if ":" not in cap_id or cap.kind not in _CAPABILITY_KINDS:
            errors.append(
                f"  capability {cap_id!r} must be '<kind>:<name>' with kind in "
                f"{_CAPABILITY_KINDS}"
            )
        # An explicit `kind` in the body must agree with the id prefix, which is
        # authoritative — otherwise `"tool:mariachi": {"kind": "service"}` would
        # silently load as a tool.
        if cap.declared_kind is not None and cap.declared_kind != cap.kind:
            errors.append(
                f"  capability {cap_id!r} declares kind {cap.declared_kind!r} "
                f"but its id prefix says {cap.kind!r}"
            )
        if cap.version_argv is not None and (
            not isinstance(cap.version_argv, list)
            or not all(isinstance(tok, str) for tok in cap.version_argv)
        ):
            errors.append(
                f"  capability {cap_id!r} 'version_argv' must be a list of strings"
            )
        if cap.min_version is not None and (
            not isinstance(cap.min_version, str)
            or _parse_semver(cap.min_version) is None
        ):
            errors.append(
                f"  capability {cap_id!r} 'min_version' is not a semver-like "
                f"string: {cap.min_version!r}"
            )
    for name, repo in config.repos.items():
        for cap_id in repo.requires:
            if cap_id not in config.capabilities:
                errors.append(
                    f"  repo {name!r} requires unknown capability {cap_id!r}"
                )
    if errors:
        raise ValueError(
            "invalid capability configuration:\n" + "\n".join(errors)
        )


def apply_cert_suite_policy(
    command: list[str], action: str, policy: dict,
) -> list[str]:
    """Return *command* with cert-suite flags appended per orch policy.

    *policy* is the map declared at ``orchestration.json:cert_suite_policy``,
    keyed by action name (e.g. ``"stage_packages"``). If *action* has no
    entry, *command* is returned unchanged.
    """
    entry = policy.get(action)
    if not entry:
        return command
    phase = entry.get("phase")
    suite_id = entry.get("suite_id")
    if not suite_id:
        raise ValueError(
            f"cert_suite_policy[{action!r}] missing 'suite_id'"
        )
    if phase == "stage":
        return list(command) + [
            "--cert-suite-id", suite_id,
            "--cert-suite-no-evidence",
        ]
    if phase == "release":
        # Release phase needs a runtime-computed evidence digest from
        # the run's report/log/archive. Not yet wired — the promote path
        # will assemble it when implemented.
        raise NotImplementedError(
            f"cert_suite_policy[{action!r}].phase = 'release' "
            f"requires evidence-digest emission (not yet implemented)"
        )
    raise ValueError(
        f"cert_suite_policy[{action!r}].phase = {phase!r} "
        f"(expected 'stage' or 'release')"
    )


@dataclass
class CertEnv:
    """Host-local resolution of capabilities (``cert-env.json``).

    Supplies machine *facts* only — tool binary paths, service host/port,
    credential env names, instance/lock ids. NEVER committed (it is
    host/CI-specific and points at secrets by name). Keyed by capability id;
    each value is the raw resolution dict for that capability, merged with the
    committed behavior policy by ``build_capabilities_document``.
    """
    resolutions: dict[str, dict]   # capability id -> {bin|host|port|credential_env|...}

    @staticmethod
    def load(path: Path) -> Optional["CertEnv"]:
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError(
                f"cert-env file {path} must be a JSON object mapping "
                f"capability ids to host resolutions"
            )
        return CertEnv(resolutions={k: (v or {}) for k, v in raw.items()})

    def get(self, cap_id: str) -> Optional[dict]:
        return self.resolutions.get(cap_id)


@dataclass
class WorkspaceLock:
    """The last certified workspace snapshot."""
    schema_version: int
    repos: dict[str, str]   # repo name -> commit SHA

    @staticmethod
    def load(path: Path) -> Optional["WorkspaceLock"]:
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        repos = {}
        for name, info in raw.get("repos", {}).items():
            repos[name] = info["commit"]
        return WorkspaceLock(
            schema_version=raw.get("schema_version", 1),
            repos=repos,
        )


@dataclass
class ExecutionPlan:
    candidate_commits: dict[str, str]
    commit_sources: dict[str, str]     # repo -> "submitted" | "certified snapshot"
    changed_repos: list[str]
    involved_repos: list[str]
    validated_repos: list[str]
    steps: list[dict] = field(default_factory=list)
    fasttrack: bool = False


# ---------------------------------------------------------------------------
# Graph operations
# ---------------------------------------------------------------------------

def build_forward_graph(config: OrchestrationConfig) -> dict[str, list[str]]:
    """Build the downstream-invalidation graph: repo -> repos to re-validate
    when it changes.

    ``depends_on`` is the single source of truth. The downstream graph is
    simply its reverse: if consumer X declares ``depends_on`` Y, then a change
    to provider Y must re-validate X, i.e. ``Y -> X`` is a downstream edge.

    Deriving it means a package author declares only its own direct providers
    and never has to track who depends on it. Adding a new consumer touches
    only that consumer's config; upstream provider configs stay untouched and
    can never go stale — which is what an explicit, hand-maintained reverse
    edge list inevitably does at scale.
    """
    graph: dict[str, set[str]] = {name: set() for name in config.repos}
    # Reverse of depends_on (provider -> consumer). Deps are guaranteed
    # configured by _validate_dependency_graph; guard regardless.
    for name, repo in config.repos.items():
        for dep in repo.depends_on:
            if dep in config.repos:
                graph[dep].add(name)
    return {name: sorted(targets) for name, targets in graph.items()}


def compute_affected(
    forward_graph: dict[str, list[str]], changed: list[str]
) -> list[str]:
    """BFS from changed repos through forward edges to find all affected repos."""
    visited: set[str] = set()
    queue = deque(changed)
    while queue:
        repo = queue.popleft()
        if repo in visited:
            continue
        visited.add(repo)
        for downstream in forward_graph.get(repo, []):
            if downstream not in visited:
                queue.append(downstream)
    return list(visited)


def compute_provider_closure(
    config: OrchestrationConfig, seeds: list[str]
) -> set[str]:
    """Expand *seeds* to their full transitive ``depends_on`` closure.

    Package semantics: if A declares ``A -> B`` and B depends on XYZ, then
    certifying A must stage B *and* B's entire provider closure — A never has
    to know or declare XYZ. This walks ``depends_on`` edges to a fixpoint so
    the plan/staging set includes every transitive provider, not just the
    immediate ones.

    This walks ``depends_on`` in the upstream direction (consumer -> provider).
    The downstream-invalidation graph (``build_forward_graph``) walks the
    *reverse* of the same edges: that one answers "who must be re-validated
    when X changes"; this closure answers "what must be staged so an involved
    repo can build". Both are derived from the single ``depends_on`` graph.

    Every ``depends_on`` edge is guaranteed to reference a configured repo by
    ``_validate_dependency_graph`` at config load, so an unconfigured provider
    fails loudly there rather than being silently dropped here.
    """
    closure: set[str] = set()
    queue = deque(seeds)
    while queue:
        repo = queue.popleft()
        if repo in closure:
            continue
        closure.add(repo)
        for dep in config.repos[repo].depends_on:
            if dep not in closure:
                queue.append(dep)
    return closure


def topo_sort(
    repos: list[str], config: OrchestrationConfig
) -> list[str]:
    """Topological sort of a subset of repos using depends_on edges."""
    subset = set(repos)
    in_degree: dict[str, int] = {r: 0 for r in repos}
    adj: dict[str, list[str]] = {r: [] for r in repos}

    for r in repos:
        for dep in config.repos[r].depends_on:
            if dep in subset:
                adj[dep].append(r)
                in_degree[r] += 1

    queue = deque(r for r in repos if in_degree[r] == 0)
    result: list[str] = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for child in adj[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(result) != len(repos):
        missing = subset - set(result)
        print(f"error: dependency cycle detected involving: {missing}", file=sys.stderr)
        sys.exit(1)

    return result


# ---------------------------------------------------------------------------
# Commit input loading and validation
# ---------------------------------------------------------------------------

def load_commit_input(path: Path) -> dict[str, str]:
    """Load candidate commits from a JSON file.

    Expected format:
    {
      "drift-lang": "abc1234def5678...",
      "drift-net-tls": "9999aaabbb..."
    }
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        print("error: commit input must be a JSON object mapping repo names to SHAs",
              file=sys.stderr)
        sys.exit(1)
    return raw


def validate_shas(commits: dict[str, str]) -> None:
    """Reject anything that is not a lowercase hex SHA (7-40 chars)."""
    bad: list[str] = []
    for name, sha in commits.items():
        if not _SHA_RE.match(sha):
            bad.append(f"  {name}: {sha!r}")
    if bad:
        print("error: commit values must be exact hex SHAs (7-40 lowercase hex chars):",
              file=sys.stderr)
        for line in bad:
            print(line, file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Commit resolution
# ---------------------------------------------------------------------------

def resolve_commits(
    config: OrchestrationConfig,
    submitted: dict[str, str],
    lock: Optional[WorkspaceLock],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build the full commit map for a run.

    Returns (commits, sources) where sources maps each repo to
    "submitted" or "certified snapshot".
    """
    commits: dict[str, str] = {}
    sources: dict[str, str] = {}
    missing: list[str] = []

    for name in config.repos:
        if name in submitted:
            commits[name] = submitted[name]
            sources[name] = "submitted"
        elif lock and name in lock.repos:
            commits[name] = lock.repos[name]
            sources[name] = "certified snapshot"
        else:
            missing.append(name)

    if missing:
        print(
            f"error: no commit specified and no certified snapshot for: "
            f"{', '.join(missing)}\n"
            f"hint: add these repos to your commit input file "
            f"or create an initial {config.lock_filename}",
            file=sys.stderr,
        )
        sys.exit(1)

    return commits, sources


def detect_changed(
    commits: dict[str, str],
    lock: Optional[WorkspaceLock],
) -> list[str]:
    """Derive which repos changed by diffing against the certified snapshot."""
    if lock is None:
        return list(commits.keys())
    changed = []
    for name, sha in commits.items():
        certified = lock.repos.get(name)
        if certified is None or certified != sha:
            changed.append(name)
    return changed


# ---------------------------------------------------------------------------
# Plan computation
# ---------------------------------------------------------------------------

def compute_plan(
    config: OrchestrationConfig,
    commits: dict[str, str],
    sources: dict[str, str],
    changed: list[str],
    fasttrack: bool = False,
) -> ExecutionPlan:
    forward_graph = build_forward_graph(config)
    affected = compute_affected(forward_graph, changed)

    # Pull in dependency providers needed by affected repos (e.g. drift-lang
    # as toolchain input even when it didn't change). This must be the *full
    # transitive* provider closure, not just immediate providers: if affected
    # repo A depends on B and B depends on XYZ, A's staging set needs XYZ too,
    # even though A never declares it. Downstream invalidation is the reverse
    # walk of the same depends_on graph, computed above as `affected`.
    involved_set = compute_provider_closure(config, list(affected))

    # Involved repos: all affected + their providers, topologically sorted.
    involved = topo_sort(list(involved_set), config)

    # Validated repos: affected repos that are package_repos (not toolchain).
    validated = [r for r in involved
                 if config.repos[r].kind != "toolchain" and r in affected]

    # Build step list.
    steps: list[dict] = []

    for repo_name in involved:
        repo = config.repos[repo_name]
        if repo.kind == "toolchain":
            if "setup_venv" in repo.commands:
                steps.append({
                    "repo": repo_name,
                    "action": "setup_venv",
                    "command": repo.commands["setup_venv"],
                    "reason": "create build venv + install deps and pex",
                })
            if "stage_toolchain" in repo.commands:
                steps.append({
                    "repo": repo_name,
                    "action": "stage_toolchain",
                    "command": repo.commands["stage_toolchain"],
                    "reason": "toolchain staging" if repo_name in changed
                        else "toolchain input from certified snapshot",
                })
        # Stage packages for any package_repo that is involved (whether
        # validated or just a dependency provider for a validated repo).
        if repo.kind == "package_repo" and "stage_packages" in repo.commands:
            if repo_name in validated:
                reason = ("directly changed" if repo_name in changed
                          else f"depends on {_dep_reason(repo, changed)}")
            else:
                reason = "dependency provider for validated repos"
            steps.append({
                "repo": repo_name,
                "action": "stage_packages",
                "command": repo.commands["stage_packages"],
                "reason": reason,
            })
        if repo_name in validated:
            validation_reason = ("directly changed" if repo_name in changed
                                 else f"depends on {_dep_reason(repo, changed)}")
            # Fork every gate into one step per certification lane. Both
            # lanes run against the same staged toolchain and the same
            # staged package set; only the gate-execution env differs
            # (DRIFT_DEBUG selects the runtime at link time). In
            # fasttrack mode only the normal lane runs.
            active_lanes = ("normal",) if fasttrack else _LANES
            for lane in active_lanes:
                for gate in ("test", "stress", "perf"):
                    if gate in repo.commands:
                        steps.append({
                            "repo": repo_name,
                            "action": gate,
                            "lane": lane,
                            "command": repo.commands[gate],
                            "reason": validation_reason,
                        })

    # Inject orch-owned cert-suite policy into every step whose action
    # is declared in cert_suite_policy. The helper is a no-op for actions
    # not in the policy map.
    for step in steps:
        step["command"] = apply_cert_suite_policy(
            step["command"], step["action"], config.cert_suite_policy,
        )

    return ExecutionPlan(
        candidate_commits=commits,
        commit_sources=sources,
        changed_repos=changed,
        involved_repos=involved,
        validated_repos=validated,
        steps=steps,
        fasttrack=fasttrack,
    )


def _dep_reason(repo: RepoConfig, changed: list[str]) -> str:
    overlap = [d for d in repo.depends_on if d in changed]
    if overlap:
        return ", ".join(overlap)
    return ", ".join(repo.depends_on)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_plan(plan: ExecutionPlan, config: OrchestrationConfig) -> None:
    if plan.fasttrack:
        print("Mode: FASTTRACK (debug-lane gates skipped)")
        print()
    print("Candidate commits:")
    for r in plan.involved_repos:
        sha = plan.candidate_commits.get(r, "???")
        short = sha[:7] if len(sha) >= 7 else sha
        source = plan.commit_sources.get(r, "???")
        print(f"  {r:30s} {short}  ({source})")
    print()

    print("Changed:")
    for r in plan.changed_repos:
        print(f"  - {r}")
    print()

    print("Involved:")
    for i, r in enumerate(plan.involved_repos, 1):
        sha = plan.candidate_commits.get(r, "???")
        short = sha[:7] if len(sha) >= 7 else sha
        if r in plan.changed_repos:
            tag = "(directly changed)"
        elif config.repos[r].kind == "toolchain":
            tag = "(toolchain input)"
        else:
            tag = "(dependency provider)"
        print(f"  {i}. {r:30s} {short}  {tag}")
    print()

    print("Validated:")
    for i, r in enumerate(plan.validated_repos, 1):
        print(f"  {i}. {r}")
    print()

    print(f"Steps ({len(plan.steps)}):")
    for i, step in enumerate(plan.steps, 1):
        cmd_str = " ".join(shlex.quote(a) for a in step["command"])
        lane = step.get("lane")
        lane_suffix = f" ({lane})" if lane else ""
        print(f"  {i}. [{step['repo']}] {step['action']}{lane_suffix}")
        print(f"     command: {cmd_str}")
        print(f"     reason:  {step['reason']}")
    print()


def print_plan_json(plan: ExecutionPlan) -> None:
    obj = {
        "candidate_commits": plan.candidate_commits,
        "commit_sources": plan.commit_sources,
        "changed_repos": plan.changed_repos,
        "involved_repos": plan.involved_repos,
        "validated_repos": plan.validated_repos,
        "steps": plan.steps,
        "fasttrack": plan.fasttrack,
    }
    print(json.dumps(obj, indent=2))


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _short_sha(sha: str) -> str:
    return sha[:7] if len(sha) >= 7 else sha


def _repo_results(summary: dict) -> dict[tuple[str, str], str]:
    """Derive per-(repo, lane) validation status from step results.

    Returns a map of ``(repo, lane) -> "PASS" | "FAIL" | "BLOCKED" |
    "NOT RUN"`` for every (validated_repo × lane) pair. Lanes are the
    fixed pair ``("normal", "debug")`` — both must be present in the
    result so reporting and verdict aggregation can see when a lane
    never ran.

    Tolerates legacy step records that lack a ``lane`` key (treats
    them as ``"normal"``) so historical summary replays still work.
    """
    validated = set(summary.get("validated_repos", []))
    repo_status: dict[tuple[str, str], str] = {}
    for step in summary.get("steps", []):
        repo = step["repo"]
        if repo not in validated:
            continue
        # Only certification gates contribute to lane verdicts; infra
        # steps (setup_venv, stage_*) are not validation.
        if step.get("name") not in _CERTIFICATION_GATES:
            continue
        lane = step.get("lane") or "normal"
        key = (repo, lane)
        status = step["status"]
        if status == "passed":
            repo_status.setdefault(key, "PASS")
        elif status == "failed":
            repo_status[key] = "FAIL"
        elif status == "blocked":
            repo_status[key] = "BLOCKED"

    # Seed NOT RUN for every (validated_repo, lane) pair that never
    # produced a gate step (e.g. fail-fast short-circuited the run).
    for repo in summary.get("validated_repos", []):
        for lane in _LANES:
            repo_status.setdefault((repo, lane), "NOT RUN")

    return repo_status


def _first_failure(summary: dict) -> Optional[dict]:
    """Find the first failed or blocked step."""
    for step in summary.get("steps", []):
        if step["status"] in ("failed", "blocked"):
            return step
    return None


def _failure_reason(summary: dict) -> str:
    """Extract a one-line failure reason from summary."""
    if summary.get("block_reason"):
        return summary["block_reason"]
    step = _first_failure(summary)
    if not step:
        return "unknown"
    log_path = Path(step.get("log_path", ""))
    if log_path.exists():
        text = log_path.read_text()
        stderr_section = text.split("--- stderr ---\n", 1)
        if len(stderr_section) == 2:
            lines = [l.strip() for l in stderr_section[1].strip().splitlines()
                     if l.strip()]
            if lines:
                # Prefer the first "error:" line over generic wrapper noise
                # like "error: Recipe `foo` failed on line N with exit code M".
                error_lines = [l for l in lines
                               if "error:" in l.lower()
                               and "Recipe `" not in l]
                reason = error_lines[0] if error_lines else lines[-1]
                # Strip file-location prefixes like "<package>:?:?: ".
                reason = re.sub(r"^<[^>]+>:\S+\s+", "", reason)
                # Replace absolute paths with basenames for readability.
                reason = re.sub(r"'/[^']+/([^'/]+)'", r"'\1'", reason)
                if len(reason) > 120:
                    reason = reason[:117] + "..."
                return reason
    if step["status"] == "blocked":
        return "command not found"
    return f"exit code {step.get('status', '?')}"


def generate_report(summary: dict) -> str:
    """Generate report.txt content from a summary dict."""
    verdict = summary["verdict"].upper()
    run_id = summary["run_id"]
    commits = summary.get("candidate_commits", {})
    sources = summary.get("commit_sources", {})
    validated = summary.get("validated_repos", [])
    lock_file = summary.get("lock_file", "workspace-lock.json")
    lock_updated = verdict == "CERTIFIED"

    lines: list[str] = []

    lines.append(f"Certification Result: {verdict}")
    if summary.get("fasttrack"):
        lines.append("Mode: FASTTRACK (debug-lane gates skipped)")
    lines.append("")
    lines.append(f"Run: {run_id}")

    submitted = [r for r, s in sources.items() if s == "submitted"]
    lines.append("Submitted commits:")
    for r in submitted:
        lines.append(f"  - {r} @ {_short_sha(commits.get(r, '???'))}")

    lines.append("")
    lines.append("Workspace snapshot:")
    for r, sha in commits.items():
        source = sources.get(r, "")
        label = f" ({source})" if source else ""
        lines.append(f"  - {r} @ {_short_sha(sha)}{label}")

    repo_results = _repo_results(summary)
    lines.append("")
    lines.append("Result by repo:")
    for r in validated:
        for lane in _LANES:
            status = repo_results.get((r, lane), "NOT RUN")
            lines.append(f"  - {r} ({lane}): {status}")

    if verdict in ("REJECTED", "BLOCKED"):
        fail_step = _first_failure(summary)
        reason = _failure_reason(summary)
        lines.append("")
        lines.append("Failure summary:")
        if fail_step:
            fail_lane = fail_step.get("lane")
            lane_label = f" [{fail_lane}]" if fail_lane else ""
            lines.append(f"  - {fail_step['repo']}{lane_label}")
            lines.append(f"    step: {fail_step['name']}")
            lines.append(f"    reason: {reason}")
        elif summary.get("block_reason"):
            lines.append(f"  - {reason}")

        fail_logs = [
            step["log_path"] for step in summary.get("steps", [])
            if step["status"] in ("failed", "blocked")
        ]
        if fail_logs:
            lines.append("")
            lines.append("Logs:")
            for log in fail_logs:
                lines.append(f"  - {Path(log).resolve()}")

    commit_mismatch = summary.get("toolchain_commit_mismatch")
    if commit_mismatch:
        lines.append("")
        lines.append("Toolchain identity warning:")
        lines.append(f"  {commit_mismatch}")

    contract = summary.get("toolchain_contract")
    if contract:
        lines.append("")
        lines.append("Toolchain contract:")
        lines.append(f"  DRIFT_TOOLCHAIN_ROOT: {contract['DRIFT_TOOLCHAIN_ROOT']}")
        lines.append(f"  ambient toolchain scrubbed: {contract['ambient_scrubbed']}")
        # Flag any gate-level contract violations.
        violations = [
            s for s in summary.get("steps", [])
            if s.get("contract_violation")
        ]
        if violations:
            lines.append("  contract violations:")
            for v in violations:
                lines.append(f"    - {v['repo']} {v['name']}: "
                             "not resolving from DRIFT_TOOLCHAIN_ROOT")

    lines.append("")
    lines.append("Lock update:")
    if lock_updated:
        lines.append(f"  - {lock_file} updated")
    else:
        lines.append(f"  - {lock_file} not updated")
    lines.append("")

    return "\n".join(lines)


def generate_report_short(summary: dict) -> str:
    """Generate report-short.txt content from a summary dict."""
    verdict = summary["verdict"].upper()
    verdict_label = (f"{verdict} (FASTTRACK)"
                     if summary.get("fasttrack") else verdict)
    commits = summary.get("candidate_commits", {})
    sources = summary.get("commit_sources", {})
    validated = summary.get("validated_repos", [])
    lock_file = summary.get("lock_file", "workspace-lock.json")
    lock_updated = verdict == "CERTIFIED"

    submitted = [r for r, s in sources.items() if s == "submitted"]
    submitted_label = _join_english(
        [f"{r}@{_short_sha(commits.get(r, '???'))}" for r in submitted]
    )

    lock_msg = (f"{lock_file} updated" if lock_updated
                else f"{lock_file} not updated")

    if verdict == "CERTIFIED":
        validated_label = ", ".join(validated)
        return (f"{verdict_label}: submitted {submitted_label}. "
                f"Validated: {validated_label}. {lock_msg}.")
    else:
        fail_step = _first_failure(summary)
        if fail_step:
            reason = _failure_reason(summary)
            fail_lane = fail_step.get("lane")
            lane_label = f" [{fail_lane}]" if fail_lane else ""
            return (f"{verdict_label}: submitted {submitted_label}. "
                    f"First failure: {fail_step['repo']} "
                    f"{fail_step['name']}{lane_label} "
                    f"({reason}). {lock_msg}.")
        elif summary.get("block_reason"):
            return (f"{verdict_label}: submitted {submitted_label}. "
                    f"{summary['block_reason']}. {lock_msg}.")
        else:
            return f"{verdict_label}: submitted {submitted_label}. {lock_msg}."


def _join_english(items: list[str]) -> str:
    if len(items) == 0:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


# ---------------------------------------------------------------------------
# Provenance collection
# ---------------------------------------------------------------------------

def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Parse "0.27.94" into (0, 27, 94) for comparison."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


def get_toolchain_version(ctx: "RunContext") -> Optional[str]:
    """Get the driftc version string from the staged toolchain."""
    driftc = ctx.toolchain_root / "bin" / "driftc"
    if not driftc.exists():
        return None
    try:
        result = subprocess.run(
            [str(driftc), "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def toolchain_supports_provenance(version_output: Optional[str]) -> bool:
    """Check if the staged toolchain is >= 0.27.94."""
    if not version_output:
        return False
    m = re.search(r"driftc\s+([\d.]+)", version_output)
    if not m:
        return False
    return _parse_version_tuple(m.group(1)) >= (0, 27, 94)


def scan_staged_artifacts(pkgs_root: Path) -> list[dict]:
    """Scan the staged packages root for deployed artifacts.

    Returns a list of artifact records carrying paths to the trust-v1
    sidecars that sit next to each ``.zdmp``:

    - ``artifact_path``       — the package itself (``<base>.zdmp``)
    - ``author_claim_path``   — ``<base>.author-claim`` (single)
    - ``cert_claim_paths``    — sorted list of ``<base>.cert-claim.*.json``
                                (one entry per cert suite; usually one,
                                possibly more)
    - ``author_profile_path`` — ``<base>.author-profile``
    - ``provenance_path``     — ``<base>.provenance.zst`` (or ``None`` if
                                missing, so completeness checks can
                                detect it explicitly)

    Descriptive only — records what's on disk. Strict trust-v1
    validation lives in ``build_run_snapshot``.
    """
    if not pkgs_root.exists():
        return []

    results: list[dict] = []
    for artifact_dir in sorted(pkgs_root.iterdir()):
        if not artifact_dir.is_dir():
            continue
        artifact_name = artifact_dir.name
        for version_dir in sorted(artifact_dir.iterdir()):
            if not version_dir.is_dir():
                continue
            version = version_dir.name
            record: dict = {
                "name": artifact_name,
                "version": version,
            }

            # Look for known artifact files (trust-v1 layout).
            zdmp = version_dir / f"{artifact_name}.zdmp"
            author_claim = version_dir / f"{artifact_name}.author-claim"
            cert_claims = sorted(
                version_dir.glob(f"{artifact_name}.cert-claim.*.json")
            )
            author_profile = version_dir / f"{artifact_name}.author-profile"
            provenance = version_dir / f"{artifact_name}.provenance.zst"

            if zdmp.exists():
                record["artifact_path"] = str(zdmp)
            if author_claim.exists():
                record["author_claim_path"] = str(author_claim)
            if cert_claims:
                record["cert_claim_paths"] = [str(p) for p in cert_claims]
            if author_profile.exists():
                record["author_profile_path"] = str(author_profile)
            if provenance.exists():
                record["provenance_path"] = str(provenance)
                prov_data = _read_provenance_bundle(provenance)
                if prov_data:
                    record["provenance"] = prov_data
            else:
                record["provenance_path"] = None

            results.append(record)

    return results


class RunSnapshotError(RuntimeError):
    """Raised when the run snapshot cannot be built: missing or malformed
    sidecar, out-of-shape field, or disagreeing duplicate entry. Aborts
    the certification run before any gate step runs — the toolchain's
    snapshot loader is strict, so partial or malformed snapshots would
    fail opaquely at gate time."""


def _validate_sha256_hex_id(value, *, where: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX_ID_RE.match(value):
        raise RunSnapshotError(
            f"{where}: 'source_content_id' {value!r} is not strict "
            f"'sha256:<64-lowercase-hex>' form"
        )
    return value


def _validate_ed25519_kid(value, *, where: str) -> str:
    if not isinstance(value, str) or not _ED25519_KID_RE.match(value):
        raise RunSnapshotError(
            f"{where}: signer kid {value!r} is not 'ed25519:<kid>' form"
        )
    return value


def _read_sidecar_json(path: Path, *, where: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RunSnapshotError(f"{where}: required sidecar missing: {path}")
    except OSError as e:
        raise RunSnapshotError(f"{where}: cannot read sidecar {path}: {e}")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise RunSnapshotError(
            f"{where}: sidecar is not valid JSON ({path}): {e}"
        )
    if not isinstance(obj, dict):
        raise RunSnapshotError(
            f"{where}: top-level value in {path} must be a JSON object"
        )
    return obj


def _extract_claim_fields(
    claim_path: Path, zdmp: Path, *,
    expected_format: str,
    expected_version: int,
    expected_body_schema_version: int,
    kind: str,
) -> tuple[str, str, str, str]:
    """Parse a trust-v1 claim sidecar (author-claim or cert-claim).

    Returns ``(package_id, version, source_content_id, signer_kid)``,
    where ``signer_kid`` is the first signature's kid. Both claim
    shapes share the same outer envelope and body fields, so one
    extractor parses both — the caller distinguishes by passing the
    expected format constants.

    Strict on every field; raises RunSnapshotError on any violation."""
    where = f"{kind} for {zdmp.name}"
    obj = _read_sidecar_json(claim_path, where=where)
    fmt = obj.get("format")
    if fmt != expected_format:
        raise RunSnapshotError(
            f"{where} ({claim_path}): expected format "
            f"{expected_format!r}, got {fmt!r}"
        )
    ver = obj.get("version")
    if ver != expected_version:
        raise RunSnapshotError(
            f"{where} ({claim_path}): expected version "
            f"{expected_version}, got {ver!r}"
        )
    body = obj.get("body")
    if not isinstance(body, dict):
        raise RunSnapshotError(
            f"{where} ({claim_path}): 'body' must be a JSON object"
        )
    body_sv = body.get("schema_version")
    if body_sv != expected_body_schema_version:
        raise RunSnapshotError(
            f"{where} ({claim_path}): body.schema_version {body_sv!r} != "
            f"{expected_body_schema_version}"
        )
    pkg_id = body.get("package_id")
    version = body.get("version")
    if not isinstance(pkg_id, str) or not pkg_id:
        raise RunSnapshotError(
            f"{where} ({claim_path}): missing or empty body.package_id"
        )
    if not isinstance(version, str) or not version:
        raise RunSnapshotError(
            f"{where} ({claim_path}): missing or empty body.version"
        )
    scid = _validate_sha256_hex_id(
        body.get("source_content_id"),
        where=f"{where} ({claim_path}) body",
    )
    sigs = obj.get("signatures")
    if not isinstance(sigs, list) or not sigs:
        raise RunSnapshotError(
            f"{where} ({claim_path}): 'signatures' must be a non-empty list"
        )
    first = sigs[0]
    if not isinstance(first, dict):
        raise RunSnapshotError(
            f"{where} ({claim_path}): signatures[0] must be a JSON object"
        )
    kid = _validate_ed25519_kid(
        first.get("kid"),
        where=f"{where} ({claim_path}) signatures[0].kid",
    )
    return pkg_id, version, scid, kid


def _write_snapshot_atomic(snapshot: dict, out_path: Path) -> None:
    """Serialize a run snapshot dict to disk via tmp + rename. Used by
    both the empty-seed write and the post-stage refresh so any
    concurrent reader (a gate starting as orch writes) only ever
    observes a complete JSON file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, out_path)


def write_empty_run_snapshot(run_id: str, out_path: Path) -> dict:
    """Seed a format-valid but empty run snapshot at out_path. The
    0.31.6+ toolchain requires DRIFT_RUN_SNAPSHOT to point at a valid
    snapshot file for both stage and certify modes, so the first
    stage_packages step needs a file to reference. Subsequent
    stage_packages refreshes rebuild from the full packages tree."""
    snapshot = {
        "format": _RUN_SNAPSHOT_FORMAT,
        "version": _RUN_SNAPSHOT_VERSION,
        "run_id": run_id,
        "packages": {},
    }
    _write_snapshot_atomic(snapshot, out_path)
    return snapshot


# ---------------------------------------------------------------------------
# External capability provisioning
#
# Repos declare gate prerequisites that are neither package artifacts nor part
# of any checkout (a schema-migration tool, a DB service, …) via per-repo
# ``requires: ["tool:mariachi", "service:mariadb"]``. The platform contract is
# a single resolved JSON document per run plus ONE env var pointing at it:
#
#   DRIFT_CERT_CAPABILITIES=<run-root>/capabilities.json
#
# Repos read that document and adapt it internally. The orchestrator injects no
# per-tool env vars (no MARIACHI_BIN, no DB_HOST) — that brittle "wrapper around
# today's env names" is exactly what this replaces. The committed
# ``capabilities`` section carries behavior policy only; host facts come from
# the host-local ``cert-env.json`` (see CertEnv); the per-run document is the
# resolved merge.
# ---------------------------------------------------------------------------

CAPABILITIES_ENV_VAR = "DRIFT_CERT_CAPABILITIES"
_CAPABILITIES_DOC_NAME = "capabilities.json"
_CAPABILITIES_DOC_SCHEMA_VERSION = 1
_SEMVER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)*")


def required_capability_ids(config: OrchestrationConfig, plan: "ExecutionPlan") -> list[str]:
    """The set of capability ids required by the run's validated repos.

    Union over ``requires`` of every repo that will actually run gates
    (``plan.validated_repos``); dependency providers that are only staged do
    not run gates, so their requirements (if any) do not apply. Sorted for a
    deterministic document/preflight order.
    """
    ids: set[str] = set()
    for repo_name in plan.validated_repos:
        ids.update(config.repos[repo_name].requires)
    return sorted(ids)


def build_capabilities_document(
    config: OrchestrationConfig,
    plan: "ExecutionPlan",
    cert_env: Optional[CertEnv],
    run_id: str,
) -> dict:
    """Resolve required capabilities into the per-run document.

    Merges the committed capability (kind) with the host-local resolution facts
    (``bin`` for tools; required ``host`` / ``port`` / ``credential_env`` plus an
    optional ``instance`` label for services — omitted when absent, never null).
    Emits only the *consumption* view — validation policy
    (``min_version`` / ``version_argv``) is preflight-only and never written
    here. A service is just a shared instance to connect to; the document
    carries no locks/concurrency/schemas — schema lifecycle is the project's own
    concern (managed via its schema tool, e.g. Mariachi).

    Always returns a versioned document, even when nothing is required
    (``"capabilities": {}``), so a consuming repo never special-cases a missing
    file. Assumes the preflight has already passed, i.e. every required
    capability resolves; missing facts surface as ``null`` rather than raising.
    """
    caps: dict[str, dict] = {}
    for cap_id in required_capability_ids(config, plan):
        cap = config.capabilities.get(cap_id)
        res = (cert_env.get(cap_id) if cert_env else None) or {}
        kind = cap.kind if cap else (cap_id.split(":", 1)[0] if ":" in cap_id else "")
        if kind == "tool":
            caps[cap_id] = {"kind": "tool", "bin": res.get("bin")}
        elif kind == "service":
            entry = {
                "kind": "service",
                "host": res.get("host"),
                "port": res.get("port"),
                "credential_env": res.get("credential_env"),
            }
            # `instance` is an optional human-facing label, not a connection
            # requirement — omit the key entirely when the host didn't provide
            # one rather than emit a null.
            if res.get("instance") is not None:
                entry["instance"] = res["instance"]
            caps[cap_id] = entry
        else:
            caps[cap_id] = {"kind": kind, **res}
    return {
        "schema_version": _CAPABILITIES_DOC_SCHEMA_VERSION,
        "run_id": run_id,
        "capabilities": caps,
    }


def write_capabilities_document(document: dict, out_path: Path) -> None:
    """Write the resolved capabilities document atomically."""
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, indent=2) + "\n")
    tmp.replace(out_path)


def _parse_semver(text: str) -> Optional[tuple[int, ...]]:
    """Extract the first semver-like token (\\d+(.\\d+)*) and return its
    integer tuple, or None if no token is present."""
    m = _SEMVER_TOKEN_RE.search(text or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(0).split("."))


def _version_at_least(found: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    """Numeric tuple compare with the shorter side zero-padded."""
    width = max(len(found), len(minimum))
    f = found + (0,) * (width - len(found))
    m = minimum + (0,) * (width - len(minimum))
    return f >= m


def run_external_deps_preflight(
    config: OrchestrationConfig,
    plan: "ExecutionPlan",
    cert_env: Optional[CertEnv],
) -> Optional[str]:
    """Validate every external capability the run's gates require.

    Runs after checkouts and BEFORE staging, so a missing tool/service blocks
    the run in seconds rather than failing deep inside a gate after minutes of
    staging. For each required id: the host must resolve it (``cert-env.json``);
    a tool's ``bin`` must exist and be executable (+ ``min_version`` when
    declared); a service's ``credential_env`` must be set & non-empty and its
    ``host:port`` must accept a TCP connection.

    Validates exactly the endpoint/binary that will be written into the
    per-run document and consumed by the gates. Returns a human-readable block
    reason, or ``None`` if all required capabilities are satisfied (including
    the trivial "nothing required" case).
    """
    required = required_capability_ids(config, plan)
    if not required:
        return None

    print("  preflight: external capabilities ...", flush=True)
    failures: list[str] = []
    for cap_id in required:
        cap = config.capabilities[cap_id]
        res = cert_env.get(cap_id) if cert_env else None
        if res is None:
            print(f"    {cap_id:28s} NOT PROVIDED")
            failures.append(
                f"capability {cap_id!r} is required but not provided on this "
                f"host; add it to your cert-env file (--cert-env / "
                f"DRIFT_CERT_ENV / ./cert-env.json)"
            )
            continue
        if cap.kind == "tool":
            err = _preflight_tool(cap, res)
        elif cap.kind == "service":
            err = _preflight_service(cap, res)
        else:
            err = f"unknown capability kind {cap.kind!r}"
        if err:
            print(f"    {cap_id:28s} FAILED")
            failures.append(f"{cap_id}: {err}")
        else:
            print(f"    {cap_id:28s} ok")

    if failures:
        return "external capability check failed:\n    - " + "\n    - ".join(failures)
    return None


def _preflight_tool(cap: Capability, res: dict) -> Optional[str]:
    """Validate a tool capability: bin present/executable + optional version."""
    bin_path = res.get("bin")
    if not bin_path:
        return "host resolution has no 'bin'"
    p = Path(bin_path)
    if not p.is_file() or not os.access(bin_path, os.X_OK):
        return f"binary not found or not executable: {bin_path}"
    if cap.min_version:
        argv = [bin_path if tok == "{bin}" else tok
                for tok in (cap.version_argv or ["{bin}", "--version"])]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"version probe failed: {exc}"
        if proc.returncode != 0:
            # A probe that errors out but happens to print a version string
            # must not pass — the command failing is itself a block.
            return (f"version probe `{' '.join(argv)}` exited "
                    f"{proc.returncode}")
        found = _parse_semver((proc.stdout or "") + "\n" + (proc.stderr or ""))
        if found is None:
            return (f"could not determine version (no semver token in "
                    f"`{' '.join(argv)}` output)")
        # min_version is load-validated to be parseable, so this is non-None.
        minimum = _parse_semver(cap.min_version) or ()
        if not _version_at_least(found, minimum):
            got = ".".join(str(n) for n in found)
            return f"version {got} is below required min_version {cap.min_version}"
    return None


def _preflight_service(cap: Capability, res: dict) -> Optional[str]:
    """Validate a service capability: secret present + TCP reachability."""
    cred_env = res.get("credential_env")
    if not cred_env:
        return "host resolution has no 'credential_env'"
    if not os.environ.get(cred_env):
        return f"credential env {cred_env!r} is not set (or empty) in the environment"
    host = res.get("host")
    port = res.get("port")
    if not host or not port:
        return "host resolution missing 'host'/'port'"
    try:
        with socket.create_connection((host, int(port)), timeout=5):
            pass
    except OSError as exc:
        return f"{host}:{port} unreachable ({exc})"
    return None


def build_run_snapshot(
    pkgs_root: Path, run_id: str, out_path: Path,
) -> dict:
    """Walk the staged packages root, read each package's trust-v1
    ``.author-claim`` + ``.cert-claim.<kid>.json`` sidecars, and emit a
    run snapshot at ``out_path``. Strict on every field; raises
    RunSnapshotError on any violation. Builds the full entry dict in
    memory before writing so partial snapshots never hit disk.

    Write is atomic: final path is rename-swapped from a sibling tmp
    file. A gate step starting concurrently could otherwise observe
    truncated JSON and fail with an opaque parse error.

    Asserts the claim bodies' ``package_id`` / ``version`` match the
    on-disk directory layout; mismatches are a hard failure so the
    resolver and snapshot cannot disagree on what a key refers to.
    Also asserts the author and cert claims agree on
    ``source_content_id`` — disagreement means the cert was minted
    against a different source than the author signed.

    Snapshot schema field names are preserved (``author_key`` and
    ``source_attestation_key``) but populated with **v1 semantics**:

    - ``author_key`` ← certifier kid (first signature of the cert-claim)
    - ``source_attestation_key`` ← author kid (first signature of the
      author-claim)

    These names are stale; the downstream toolchain still expects them.
    See ``resolver._read_author_key`` / ``_read_source_attestation_meta``
    in drift-lang for the matching consumer.

    Multiple cert-claims (one per cert suite) may sit next to a single
    author-claim. We pick the deterministic ``sorted-first`` cert-claim,
    matching the resolver's tie-break rule.
    """
    if not pkgs_root.exists():
        raise RunSnapshotError(f"staged pkgs_root missing: {pkgs_root}")

    entries: dict[tuple[str, str], dict] = {}

    for artifact_dir in sorted(pkgs_root.iterdir()):
        if not artifact_dir.is_dir():
            continue
        for version_dir in sorted(artifact_dir.iterdir()):
            if not version_dir.is_dir():
                continue
            zdmps = sorted(version_dir.glob("*.zdmp"))
            if not zdmps:
                # Not a package directory (e.g. an empty shell). The
                # toolchain's scan contract means absence of .zdmp = no
                # package to pin.
                continue
            for zdmp in zdmps:
                base = zdmp.stem
                author_path = version_dir / f"{base}.author-claim"
                cert_paths = sorted(
                    version_dir.glob(f"{base}.cert-claim.*.json")
                )
                if not author_path.exists():
                    raise RunSnapshotError(
                        f"staged package {zdmp} is missing required "
                        f"sidecar: {author_path}"
                    )
                if not cert_paths:
                    raise RunSnapshotError(
                        f"staged package {zdmp} has no cert-claim "
                        f"sidecar (expected {base}.cert-claim.<kid>.json "
                        f"under {version_dir})"
                    )
                # Deterministic tie-break: first sorted cert-claim wins,
                # matching the resolver. This is stable across runs as
                # long as no cert-claim is added or removed.
                cert_path = cert_paths[0]

                author_pkg, author_ver, author_scid, author_kid = (
                    _extract_claim_fields(
                        author_path, zdmp,
                        expected_format=_AUTHOR_CLAIM_FORMAT,
                        expected_version=_AUTHOR_CLAIM_VERSION,
                        expected_body_schema_version=
                            _AUTHOR_CLAIM_BODY_SCHEMA_VERSION,
                        kind="author-claim",
                    )
                )
                cert_pkg, cert_ver, cert_scid, cert_kid = (
                    _extract_claim_fields(
                        cert_path, zdmp,
                        expected_format=_CERT_CLAIM_FORMAT,
                        expected_version=_CERT_CLAIM_VERSION,
                        expected_body_schema_version=
                            _CERT_CLAIM_BODY_SCHEMA_VERSION,
                        kind="cert-claim",
                    )
                )

                # Asserted invariant: both claims' declared identity
                # matches the directory layout the resolver walks.
                # Divergence means the snapshot key would disagree with
                # how the resolver addresses the package — fail here,
                # not at consume time.
                for label, pkg_val, ver_val, claim_path in (
                    ("author-claim", author_pkg, author_ver, author_path),
                    ("cert-claim",   cert_pkg,   cert_ver,   cert_path),
                ):
                    if pkg_val != artifact_dir.name:
                        raise RunSnapshotError(
                            f"{label} body.package_id {pkg_val!r} for "
                            f"{zdmp} does not match on-disk directory "
                            f"name {artifact_dir.name!r} "
                            f"({claim_path})"
                        )
                    if ver_val != version_dir.name:
                        raise RunSnapshotError(
                            f"{label} body.version {ver_val!r} for "
                            f"{zdmp} does not match on-disk directory "
                            f"name {version_dir.name!r} ({claim_path})"
                        )

                # Asserted invariant: the cert binds to the same source
                # the author signed. If these diverge the cert is
                # vouching for a different source than the package
                # actually contains.
                if author_scid != cert_scid:
                    raise RunSnapshotError(
                        f"source_content_id mismatch between author and "
                        f"cert claims for {zdmp}: "
                        f"author={author_scid} ({author_path}) vs "
                        f"cert={cert_scid} ({cert_path})"
                    )

                zdmp_sha = hashlib.sha256(zdmp.read_bytes()).hexdigest()
                key = (author_pkg, author_ver)
                # Field names preserved for the downstream toolchain;
                # see the function docstring for v1 semantics.
                new_entry = {
                    "source_content_id": author_scid,
                    "author_key": cert_kid,
                    "source_attestation_key": author_kid,
                    "sha256": zdmp_sha,
                }
                if key in entries:
                    if entries[key] != new_entry:
                        raise RunSnapshotError(
                            f"conflicting run-snapshot entries for "
                            f"{author_pkg}@{author_ver}: same "
                            f"(pkg_id, version) seen twice with "
                            f"different metadata:\n"
                            f"  first: {entries[key]}\n"
                            f"  again: {new_entry}"
                        )
                    # Duplicate but byte-identical — accept silently.
                    continue
                entries[key] = new_entry

    snapshot = {
        "format": _RUN_SNAPSHOT_FORMAT,
        "version": _RUN_SNAPSHOT_VERSION,
        "run_id": run_id,
        "packages": {
            f"{pkg_id}|{version}": entry
            for (pkg_id, version), entry in sorted(entries.items())
        },
    }
    _write_snapshot_atomic(snapshot, out_path)
    return snapshot


def _read_provenance_bundle(path: Path) -> Optional[dict]:
    """Read and parse a .provenance.zst file, extracting key fields."""
    try:
        import zstandard
        compressed = path.read_bytes()
        dctx = zstandard.ZstdDecompressor()
        raw = dctx.decompress(compressed)
        bundle = json.loads(raw)
        prov = bundle.get("provenance", {})
        return {
            "artifact_name": prov.get("artifact_name"),
            "artifact_version": prov.get("artifact_version"),
            "artifact_sha256": prov.get("artifact_sha256"),
            "compiler_version": prov.get("compiler_version"),
            "compiler_commit": prov.get("compiler_commit"),
            "abi": prov.get("abi"),
            "build_utc": prov.get("build_utc"),
        }
    except Exception:
        return None


def check_provenance_completeness(
    artifacts: list[dict], require_provenance: bool
) -> list[str]:
    """Check that all artifacts have provenance bundles.

    Returns a list of error messages for missing provenance.
    """
    if not require_provenance:
        return []
    errors: list[str] = []
    for art in artifacts:
        if art.get("provenance_path") is None:
            errors.append(
                f"{art['name']}@{art['version']}: missing .provenance.zst"
            )
    return errors


# ---------------------------------------------------------------------------
# Run execution
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    run_id: str
    run_started_utc: str   # ISO 8601 (UTC); stamps cert-claim provenance
    run_root: Path
    checkouts_root: Path
    toolchain_root: Path
    pkgs_root: Path
    apps_root: Path
    logs_root: Path


def create_run_context(config: OrchestrationConfig, plan: ExecutionPlan) -> RunContext:
    """Create the run directory structure."""
    now = datetime.now(timezone.utc)
    # Use first changed repo + short SHA for human-readable run ID.
    first_changed = plan.changed_repos[0]
    first_sha = plan.candidate_commits[first_changed][:7]
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{first_changed}-{first_sha}"

    run_root = Path(config.run_root) / run_id
    ctx = RunContext(
        run_id=run_id,
        run_started_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        run_root=run_root,
        checkouts_root=run_root / "checkouts",
        toolchain_root=run_root / "toolchain",
        pkgs_root=run_root / "pkgs",
        apps_root=run_root / "apps",
        logs_root=run_root / "logs",
    )

    for d in [
        ctx.checkouts_root, ctx.toolchain_root, ctx.pkgs_root,
        ctx.apps_root, ctx.logs_root,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    return ctx


def materialize_checkout(
    repo: RepoConfig, sha: str, checkouts_root: Path
) -> Path:
    """Clone a repo and check out the exact commit SHA."""
    checkout_dir = checkouts_root / repo.name

    # Clone from the sibling repo on disk (local clone, no network).
    source = str(Path(repo.path).resolve())
    result = subprocess.run(
        ["git", "clone", "--no-checkout", source, str(checkout_dir)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git clone failed for {repo.name}: {result.stderr.strip()}"
        )

    result = subprocess.run(
        ["git", "checkout", sha],
        cwd=checkout_dir, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git checkout {sha} failed for {repo.name}: {result.stderr.strip()}"
        )

    return checkout_dir


def resolve_placeholders(
    argv: list[str], ctx: RunContext
) -> list[str]:
    """Substitute staging path placeholders in a command argv."""
    subs = {
        "{toolchain_root}": str(ctx.toolchain_root.resolve()),
        "{pkgs_root}": str(ctx.pkgs_root.resolve()),
        "{apps_root}": str(ctx.apps_root.resolve()),
        "{staged_drift}": str((ctx.toolchain_root / "bin" / "drift").resolve()),
        "{staged_driftc}": str((ctx.toolchain_root / "bin" / "driftc").resolve()),
    }
    resolved = []
    for arg in argv:
        for placeholder, value in subs.items():
            arg = arg.replace(placeholder, value)
        resolved.append(arg)
    return resolved


_CERTIFICATION_GATES = frozenset(("test", "stress", "perf"))

# Certification lanes. Both lanes run against the same staged toolchain
# and the same staged package set. The debug lane is selected at link time
# inside the consumer build by setting DRIFT_DEBUG=1; the normal lane runs
# with DRIFT_DEBUG explicitly unset. Selection is purely a runtime-link
# concern — there is no per-lane staging.
_LANES: tuple[str, ...] = ("normal", "debug")
_DUAL_RUNTIME_BLOCK_REASON = (
    "staged toolchain does not declare dual-runtime support"
)


def _verify_dual_runtime_support(ctx: "RunContext") -> Optional[str]:
    """Verify the staged toolchain manifest declares both runtime variants.

    Reads ``lib/manifest.json`` from the staged toolchain root and checks
    that ``runtimes.normal.lib`` and ``runtimes.debug.lib`` are both
    present and that the referenced files exist on disk under the staged
    toolchain root. Returns ``None`` on success, or a human-readable
    error message describing the first failed check.
    """
    manifest_path = ctx.toolchain_root / "lib" / "manifest.json"
    if not manifest_path.exists():
        return f"missing toolchain manifest: {manifest_path}"
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return f"toolchain manifest is not valid JSON ({manifest_path}): {e}"

    runtimes = manifest.get("runtimes")
    if not isinstance(runtimes, dict):
        return (
            f"toolchain manifest missing 'runtimes' map ({manifest_path}); "
            f"a dual-runtime-aware drift-lang build is required"
        )

    for lane in _LANES:
        entry = runtimes.get(lane)
        if not isinstance(entry, dict):
            return (
                f"toolchain manifest missing 'runtimes.{lane}' entry "
                f"({manifest_path})"
            )
        rel = entry.get("lib")
        if not isinstance(rel, str) or not rel:
            return (
                f"toolchain manifest 'runtimes.{lane}.lib' is missing or "
                f"not a string ({manifest_path})"
            )
        # Resolve relative to the staged toolchain root.
        lib_path = (ctx.toolchain_root / rel).resolve()
        if not lib_path.is_file():
            return (
                f"toolchain manifest declares 'runtimes.{lane}.lib' = {rel} "
                f"but file does not exist on disk under staged toolchain "
                f"({lib_path})"
            )
    return None


def _scrub_ambient_toolchain(path_value: str, staged_bin: str) -> str:
    """Remove PATH entries that contain ambient drift/driftc binaries.

    Keeps only the staged toolchain bin and entries that do NOT contain
    a ``drift`` or ``driftc`` binary, so certification gates cannot
    accidentally resolve tools from a production or user-local install.
    """
    kept: list[str] = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        if entry == staged_bin:
            kept.append(entry)
            continue
        entry_path = Path(entry)
        if (entry_path / "drift").exists() or (entry_path / "driftc").exists():
            continue  # ambient toolchain — drop
        kept.append(entry)
    return os.pathsep.join(kept)


def build_step_env(
    config: OrchestrationConfig,
    ctx: RunContext,
    *,
    gate: bool = False,
    lane: Optional[str] = None,
    action: str = "",
) -> dict[str, str]:
    """Build environment variables for step execution.

    When *gate* is True the environment is hardened for certification:
    ambient PATH entries that contain ``drift`` or ``driftc`` are removed
    so that downstream recipes can only succeed if they resolve tooling
    from ``DRIFT_TOOLCHAIN_ROOT``.

    The *lane* selects the runtime certification lane for gate steps.
    Only ``"debug"`` injects ``DRIFT_DEBUG=1``; the ``"normal"`` lane
    runs with ``DRIFT_DEBUG`` explicitly unset. ``DRIFT_DEBUG`` is
    always scrubbed from the inherited environment first so the
    operator's shell state cannot leak into a run.

    Note: ``DRIFT_COMPILER_DEBUG`` is the compiler-internal structured
    debug flag and is intentionally left untouched — orchestration only
    owns the runtime-lane selector.
    """
    env = dict(os.environ)
    # Force unbuffered stdout/stderr on any embedded-CPython drift-CLI
    # invocation (drift/driftc are scie/embedded-CPython binaries that
    # block-buffer under a pipe otherwise, so the activity watchdog below
    # sees nothing until the process exits and flushes — confirmed via
    # drift-workflows' 20260710T210823Z repro of the stage_packages
    # watchdog kill). Harmless for non-Python subprocesses.
    env["PYTHONUNBUFFERED"] = "1"
    # Hermetic lane selection: scrub any inherited DRIFT_DEBUG (and the
    # retired DRIFT_OPTIMIZED) before re-adding only what this lane wants.
    env.pop("DRIFT_DEBUG", None)
    env.pop("DRIFT_OPTIMIZED", None)
    # Provenance stamping: bind any `drift deploy` cert-claim emitted in this
    # run to THIS orchestrator run. Without these, drift_deploy falls back to a
    # random UUID and the epoch sentinel 1970-01-01T00:00:00Z for run_started_utc.
    # Set for every step (only `drift deploy` reads them) so all emissions agree.
    env["DRIFT_DEPLOY_RUN_ID"] = ctx.run_id
    env["DRIFT_DEPLOY_RUN_STARTED_UTC"] = ctx.run_started_utc
    if gate and lane == "debug":
        env["DRIFT_DEBUG"] = "1"
    # Honor DRIFT_TEST_JOBS if the operator set it; otherwise default to
    # nproc/2 (matching the compiler build's policy). Floor at 1.
    if "DRIFT_TEST_JOBS" not in env:
        env["DRIFT_TEST_JOBS"] = str(max(1, (os.cpu_count() or 2) // 2))
    toolchain_root = str(ctx.toolchain_root.resolve())
    pkgs_root = str(ctx.pkgs_root.resolve())

    subs = {
        "{toolchain_root}": toolchain_root,
        "{pkgs_root}": pkgs_root,
    }
    for var, template in config.environment.get("vars", {}).items():
        value = template
        for placeholder, resolved in subs.items():
            value = value.replace(placeholder, resolved)
        env[var] = value

    # Certification-phase selector. stage_packages = producer role
    # (DRIFT_CERT_MODE=stage). Certification gates = consumer role
    # (DRIFT_CERT_MODE=certify + DRIFT_RUN_SNAPSHOT=<path>). All
    # other steps (setup_venv, stage_toolchain) leave the env unset —
    # drift-lang's own build machinery is not a certification surface.
    # The retired DRIFT_SOURCE_REBUILD is hard-rejected by 0.31.5+; we
    # explicitly scrub any inherited value so an operator's shell can't
    # bleed it into the run.
    env.pop("DRIFT_SOURCE_REBUILD", None)
    env.pop("DRIFT_CERT_MODE", None)
    env.pop("DRIFT_RUN_SNAPSHOT", None)
    # Both stage and certify modes consume upstream packages under
    # certification semantics (source-identity pinned by the run
    # snapshot), so both require DRIFT_RUN_SNAPSHOT. The modes differ
    # only on outputs: stage exempts the package currently being
    # produced from snapshot entry requirements; certify does not.
    # Orch seeds an empty-but-valid snapshot at run start so the first
    # stage_packages has a file to reference.
    if gate:
        env["DRIFT_CERT_MODE"] = "certify"
        env["DRIFT_RUN_SNAPSHOT"] = str(
            (ctx.run_root / "run-snapshot.json").resolve()
        )
        # External capability contract: point gates at the single resolved
        # document. The orchestrator adds ONLY this var — no per-tool env
        # vars. The document always exists (written at run start, possibly
        # empty), so consumers never special-case a missing file. Secrets
        # named by a capability's `credential_env` are inherited from
        # os.environ above, never added or renamed here.
        env[CAPABILITIES_ENV_VAR] = str(
            (ctx.run_root / _CAPABILITIES_DOC_NAME).resolve()
        )
    elif action == "stage_packages":
        env["DRIFT_CERT_MODE"] = "stage"
        env["DRIFT_RUN_SNAPSHOT"] = str(
            (ctx.run_root / "run-snapshot.json").resolve()
        )

    staged_bin = str((ctx.toolchain_root / "bin").resolve())

    if gate:
        # Certification gates: scrub ambient drift/driftc from PATH so
        # repos that depend on PATH instead of DRIFT_TOOLCHAIN_ROOT fail.
        scrubbed = _scrub_ambient_toolchain(
            env.get("PATH", ""), staged_bin,
        )
        env["PATH"] = staged_bin + os.pathsep + scrubbed
    else:
        # Infra steps (setup_venv, stage_*): keep full PATH for flexibility.
        env["PATH"] = staged_bin + os.pathsep + env.get("PATH", "")

    return env


def _authorable_artifacts(manifest_path: Path) -> list[str]:
    """Return the names of authorable artifacts declared in a manifest.

    Author claims are minted per ``package`` and ``app`` artifact, so these
    are exactly the artifacts `drift author verify` (and `drift deploy`)
    check. (The old ``library`` kind was replaced by ``package``/``app`` in
    driftc 0.33.57 and is a hard parse error as of 0.33.61.)
    """
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    names: list[str] = []
    for art in manifest.get("artifacts", []):
        if art.get("kind", "package") in ("package", "app") and art.get("name"):
            names.append(art["name"])
    return names


def _fmt_duration(seconds: float) -> str:
    """Human-friendly step duration: '4.2s', '1m03s', '2h05m'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def run_author_claim_preflight(
    config: OrchestrationConfig,
    plan: ExecutionPlan,
    ctx: "RunContext",
    checkout_dirs: dict[str, Path],
) -> Optional[str]:
    """Fail-fast author-claim freshness gate.

    Runs once the toolchain is staged (our base platform) and before any
    package staging or certification gate. For every involved package
    repo it verifies the committed author-claim still binds the
    checked-out source, using the *staged* ``drift author verify`` — the
    same self-contained toolchain binary that certifies packages, with
    its own bundled interpreter. No orchestrator-side Python, no internal
    module entrypoint: the platform validates its own consumers. The
    check is keyless, build-free and side-effect-free, so a stale/missing
    claim costs ~50ms per artifact instead of a full ``drift deploy``
    plus the per-repo gate matrix the planner would otherwise run first.

    Returns ``None`` if all claims are fresh — or if the staged toolchain
    is too old to expose ``verify`` (skip with a warning; ``drift deploy``
    still enforces it later). Returns a human-readable block reason if any
    claim is stale or missing.
    """
    # Use the staged toolchain binary — the same `drift` that does the
    # certification, with its own bundled runtime. stage_toolchain ran
    # before us, so it exists.
    staged_drift = (ctx.toolchain_root / "bin" / "drift").resolve()
    if not staged_drift.is_file():
        print("  preflight: author-claim verify ... SKIPPED "
              "(staged drift binary not found)")
        return None

    # Every involved package repo that will be staged is a claim source —
    # whether it is directly validated or pulled in as a dependency
    # provider. This matches exactly the set `drift deploy` would check.
    pkg_repos = [
        r for r in plan.involved_repos
        if config.repos[r].kind == "package_repo"
        and "stage_packages" in config.repos[r].commands
    ]

    def _short_sci(value: Optional[str]) -> str:
        # sha256:<64 hex> -> sha256:<first 10 hex>… for readable console output.
        if not value:
            return "?"
        return value[:17] + "…" if len(value) > 18 else value

    print("  preflight: author-claim verify ...", flush=True)
    _t0 = time.monotonic()
    failures: list[tuple[str, str]] = []
    ok_count = 0
    repos_checked = 0
    for repo_name in pkg_repos:
        manifest_path = (
            checkout_dirs[repo_name] / "drift" / "manifest.json"
        ).resolve()
        artifacts = _authorable_artifacts(manifest_path)
        print(f"    {repo_name}:")
        if not artifacts:
            print(f"      (no authorable artifacts in {manifest_path})")
            failures.append((repo_name, "no authorable artifacts in manifest"))
            continue
        repos_checked += 1
        # Sub-indent one artifact per line (a repo may declare 10s of them)
        # and pad names so the status column lines up within the repo.
        width = max(len(a) for a in artifacts)
        for art in artifacts:
            label = f"{repo_name}/{art}"
            cmd = [
                str(staged_drift), "author", "verify",
                "--manifest", str(manifest_path),
                "--artifact", art, "--json",
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                )
            except subprocess.TimeoutExpired:
                print(f"      {art:<{width}}  TIMED OUT")
                failures.append((label, "verify timed out"))
                continue
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                # An older toolchain has no `verify` subcommand at all.
                if "invalid choice: 'verify'" in result.stderr:
                    print(f"      {art:<{width}}  -")
                    print("    SKIPPED: staged toolchain lacks `author "
                          "verify`; deploy will enforce claims later")
                    return None
                detail = (result.stderr or result.stdout).strip() or "no output"
                print(f"      {art:<{width}}  ERROR (exit {result.returncode})")
                failures.append((label, f"unexpected verify output: {detail}"))
                continue
            status = data.get("status")
            if status == "ok":
                ok_count += 1
                print(f"      {art:<{width}}  ok")
            elif status == "stale":
                sci = data.get("source_content_id", {})
                detail = (f"STALE — claim {_short_sci(sci.get('claim'))} "
                          f"≠ source {_short_sci(sci.get('computed'))}")
                print(f"      {art:<{width}}  {detail}")
                failures.append((label, detail))
            elif status == "missing_claim":
                print(f"      {art:<{width}}  MISSING author-claim")
                failures.append((label, "MISSING author-claim"))
            else:
                print(f"      {art:<{width}}  unexpected status {status!r}")
                failures.append((label, f"unexpected status {status!r}"))

    # Report what we did NOT check, so "didn't verify" is never silently
    # indistinguishable from "verified and fine".
    skipped: list[str] = []
    for repo_name in plan.involved_repos:
        if repo_name in pkg_repos:
            continue
        kind = config.repos[repo_name].kind
        if kind == "toolchain":
            reason = "toolchain, no author-claims"
        elif kind == "package_repo":
            reason = "no stage_packages recipe"
        else:
            reason = kind
        skipped.append(f"{repo_name} ({reason})")
    if skipped:
        print(f"    skipped: {', '.join(skipped)}")

    _elapsed = time.monotonic() - _t0
    if failures:
        total = ok_count + len(failures)
        print(f"  FAILED ({len(failures)} of {total} claim(s) bad, "
              f"{_fmt_duration(_elapsed)})")
        repos = sorted({label.split("/", 1)[0] for label, _ in failures})
        print(f"    fix: re-run `drift author --overwrite` in "
              f"{', '.join(repos)}, commit the refreshed .author-claim, "
              f"then re-pin and re-run.")
        return (
            f"author-claim preflight failed for {len(failures)} artifact(s): "
            + ", ".join(label for label, _ in failures)
        )
    print(f"  ok ({ok_count} claims fresh across {repos_checked} repos, "
          f"{_fmt_duration(_elapsed)})")
    return None


def execute_run(
    config: OrchestrationConfig,
    plan: ExecutionPlan,
    cert_env: Optional[CertEnv] = None,
) -> dict:
    """Execute the full certification run. Returns the summary dict."""
    started_at = datetime.now(timezone.utc)
    ctx = create_run_context(config, plan)

    print(f"Run: {ctx.run_id}")
    print(f"Dir: {ctx.run_root}")
    if plan.fasttrack:
        print("Mode: FASTTRACK (debug-lane gates skipped)")
    print()

    # Materialize fresh checkouts for all involved repos.
    checkout_dirs: dict[str, Path] = {}
    for repo_name in plan.involved_repos:
        repo = config.repos[repo_name]
        sha = plan.candidate_commits[repo_name]
        short = sha[:7]
        print(f"  checkout {repo_name} @ {short} ...", end=" ", flush=True)
        try:
            checkout_dirs[repo_name] = materialize_checkout(
                repo, sha, ctx.checkouts_root
            )
            print("ok")
        except RuntimeError as e:
            print("FAILED")
            print(f"  {e}", file=sys.stderr)
            summary = _build_summary(
                ctx, config, plan, started_at, "blocked",
                steps_results=[], block_reason=str(e),
                fasttrack=plan.fasttrack,
            )
            _write_run_outputs(ctx, summary)
            return summary
    print()

    # External capability preflight: validate every tool/service the run's
    # gates require BEFORE staging, so a missing dependency blocks in seconds
    # instead of failing deep inside a gate after minutes of staging. On pass,
    # write the resolved per-run capabilities document (always written, even
    # when empty) and inject it via DRIFT_CERT_CAPABILITIES at gate time.
    block_reason = run_external_deps_preflight(config, plan, cert_env)
    if block_reason:
        print(f"    {block_reason}", file=sys.stderr)
        summary = _build_summary(
            ctx, config, plan, started_at, "blocked",
            steps_results=[], block_reason=block_reason,
            fasttrack=plan.fasttrack,
        )
        _write_run_outputs(ctx, summary)
        return summary
    write_capabilities_document(
        build_capabilities_document(config, plan, cert_env, ctx.run_id),
        ctx.run_root / _CAPABILITIES_DOC_NAME,
    )
    print()

    # Seed an empty run snapshot before any step runs. Both stage and
    # certify modes require DRIFT_RUN_SNAPSHOT to reference a valid
    # file; the first stage_packages has no prior staged inputs, so an
    # empty-but-format-valid snapshot is the correct starting state.
    # Each successful stage_packages refreshes the file from the full
    # packages tree so later stage steps can verify their upstream inputs.
    snapshot_path = ctx.run_root / "run-snapshot.json"
    write_empty_run_snapshot(ctx.run_id, snapshot_path)

    # Execute steps.
    step_results: list[dict] = []
    verdict = "certified"
    dual_runtime_verified = False
    claims_verified = False
    seen_artifacts: set[tuple[str, str]] = set()

    for step in plan.steps:
        repo_name = step["repo"]
        action = step["action"]
        lane = step.get("lane")
        raw_cmd = step["command"]
        resolved_cmd = resolve_placeholders(raw_cmd, ctx)
        cwd = checkout_dirs[repo_name]
        is_gate = action in _CERTIFICATION_GATES
        # Lane-aware log filename so the two lanes' gate logs don't
        # overwrite each other. Infra steps keep the historical name.
        if lane:
            log_file = ctx.logs_root / f"{repo_name}.{action}.{lane}.log"
        else:
            log_file = ctx.logs_root / f"{repo_name}.{action}.log"

        # Author-claim preflight: once, immediately before the first
        # package staging. The toolchain (our base platform) is staged by
        # now, so validate every package repo's claim with the staged
        # `drift author verify` before we compile/sign or run any gate.
        if action == "stage_packages" and not claims_verified:
            claims_verified = True
            block_reason = run_author_claim_preflight(
                config, plan, ctx, checkout_dirs
            )
            if block_reason:
                print(f"    {block_reason}", file=sys.stderr)
                summary = _build_summary(
                    ctx, config, plan, started_at, "blocked",
                    steps_results=step_results, block_reason=block_reason,
                    fasttrack=plan.fasttrack,
                )
                _write_run_outputs(ctx, summary)
                return summary

        label = (f"[{repo_name}] {action} ({lane})" if lane
                 else f"[{repo_name}] {action}")
        print(f"  {label} ...", end=" ", flush=True)

        # Pre-gate dual-runtime capability check. Runs once, the first
        # time we are about to execute a certification gate. By that
        # point stage_toolchain has already succeeded (otherwise we'd
        # never reach a gate), so the manifest must be present.
        if is_gate and not dual_runtime_verified:
            err = _verify_dual_runtime_support(ctx)
            dual_runtime_verified = True
            if err:
                print("BLOCKED (dual-runtime check)")
                print(f"    {err}")
                verdict = "blocked"
                summary = _build_summary(
                    ctx, config, plan, started_at, "blocked",
                    steps_results=step_results,
                    block_reason=f"{_DUAL_RUNTIME_BLOCK_REASON}: {err}",
                    fasttrack=plan.fasttrack,
                )
                _write_run_outputs(ctx, summary)
                return summary

        step_env = build_step_env(
            config, ctx, gate=is_gate, lane=lane, action=action,
        )
        step_started = datetime.now(timezone.utc)
        contract_violation = False
        returncode: Optional[int] = None
        timed_out = False

        try:
            with open(log_file, "w") as lf:
                # Write header immediately so `tail -f` shows context.
                lf.write(
                    f"=== {label} ===\n"
                    f"command: {resolved_cmd}\n"
                    f"cwd: {cwd}\n"
                    f"certification_gate: {is_gate}\n"
                    f"lane: {lane or '-'}\n"
                    f"DRIFT_DEBUG: {step_env.get('DRIFT_DEBUG', '')}\n"
                    f"DRIFT_TOOLCHAIN_ROOT: "
                    f"{step_env.get('DRIFT_TOOLCHAIN_ROOT', 'NOT SET')}\n"
                    f"\n"
                    f"--- output ---\n"
                )
                lf.flush()

                proc = subprocess.Popen(
                    resolved_cmd, cwd=cwd, env=step_env,
                    stdout=lf, stderr=subprocess.STDOUT,
                )
                # Activity-based timeout: kill after N seconds of no new
                # output. Gate recipes are contractually required to
                # emit progress on stdout/stderr (at least every ~60s)
                # so this watchdog catches true hangs quickly. If a
                # recipe redirects test output to side logs without a
                # stream copy, it violates the contract and will trip
                # this watchdog — fix the recipe, not the limit.
                #
                # stage_packages is a deliberate exception: its command is
                # `drift deploy` (not a repo recipe we control), and its
                # dominant phase — the --source-rebuild fresh-graph compile
                # for the final app artifact — is legitimately silent for
                # ~108s on an idle host even with PYTHONUNBUFFERED=1 (no
                # per-module progress is emitted during that compile).
                # 120s left ~11% headroom, so any modest host load flaked
                # the watchdog (confirmed via drift-workflows' 20260710
                # repro of run 20260710-193117's rejection). Widened here
                # rather than raised globally so true hangs in gate steps
                # are still caught quickly.
                _inactivity_limit = 300 if action == "stage_packages" else 120
                lf.flush()
                _last_size = os.path.getsize(log_file)
                _last_activity = time.monotonic()
                while proc.poll() is None:
                    time.sleep(1)
                    _cur_size = os.path.getsize(log_file)
                    if _cur_size != _last_size:
                        _last_size = _cur_size
                        _last_activity = time.monotonic()
                    if time.monotonic() - _last_activity > _inactivity_limit:
                        timed_out = True
                        proc.kill()
                        proc.wait()
                        break

                returncode = proc.returncode

                # Append trailer.
                lf.write(f"\n--- end ---\n")
                if timed_out:
                    lf.write(f"TIMEOUT: no output for {_inactivity_limit}s\n")
                else:
                    lf.write(f"exit: {returncode}\n")

            step_finished = datetime.now(timezone.utc)
            elapsed = (step_finished - step_started).total_seconds()

            if timed_out:
                status = "failed"
                verdict = "rejected"
                print(f"TIMEOUT ({_fmt_duration(elapsed)})")
                print(f"    see: {log_file}")
            elif returncode == 0:
                status = "passed"
                print(f"ok ({_fmt_duration(elapsed)})")
                if action == "stage_toolchain":
                    tc_ver = get_toolchain_version(ctx)
                    if tc_ver:
                        print(f"    staged toolchain: {tc_ver}")
                elif action == "stage_packages":
                    current = scan_staged_artifacts(ctx.pkgs_root)
                    new_rows = [
                        (a["name"], a["version"]) for a in current
                        if (a["name"], a["version"]) not in seen_artifacts
                    ]
                    if new_rows:
                        listing = ", ".join(
                            f"{n}@{v}" for n, v in sorted(new_rows)
                        )
                        print(f"    staged: {listing}")
                    seen_artifacts.update(
                        (a["name"], a["version"]) for a in current
                    )
                    # Refresh the run snapshot from the full packages tree
                    # after every successful stage_packages. The run
                    # interleaves stage/gate per repo (stage net-tls,
                    # gate net-tls, stage mariadb, gate mariadb, ...),
                    # so a snapshot written once before the first gate
                    # would miss every repo staged afterward. Scanning
                    # the full tree each time keeps the snapshot
                    # cumulative; duplicate agreeing entries are
                    # accepted silently, conflicting entries are a
                    # hard failure. The next stage step also reads
                    # this file (stage mode consumes upstreams under
                    # certification semantics), so a failed refresh
                    # blocks the whole run.
                    try:
                        snapshot = build_run_snapshot(
                            ctx.pkgs_root, ctx.run_id, snapshot_path,
                        )
                    except RunSnapshotError as e:
                        print(f"    snapshot refresh FAILED: {e}",
                              file=sys.stderr)
                        status = "blocked"
                        verdict = "blocked"
                        step_record = {
                            "repo": repo_name,
                            "name": action,
                            "lane": lane,
                            "status": status,
                            "command": resolved_cmd,
                            "log_path": str(log_file),
                            "started_at": step_started.isoformat(),
                            "finished_at": datetime.now(
                                timezone.utc).isoformat(),
                            "duration_s": round(elapsed, 1),
                        }
                        step_results.append(step_record)
                        summary = _build_summary(
                            ctx, config, plan, started_at, "blocked",
                            steps_results=step_results,
                            block_reason=f"run-snapshot refresh failed: {e}",
                            fasttrack=plan.fasttrack,
                        )
                        _write_run_outputs(ctx, summary)
                        return summary
                    print(
                        f"    snapshot: "
                        f"{len(snapshot['packages'])} packages pinned"
                    )
            else:
                status = "failed"
                verdict = "rejected"

                # Detect contract violation from the log.
                if is_gate:
                    log_text = log_file.read_text()
                    for marker in ("drift: not found", "driftc: not found",
                                   "drift: command not found",
                                   "driftc: command not found",
                                   "No such file or directory"):
                        if marker in log_text:
                            contract_violation = True
                            break
                    if contract_violation:
                        with open(log_file, "a") as lf:
                            lf.write(
                                "\n--- contract violation ---\n"
                                "Gate failed because tooling was not "
                                "resolved from DRIFT_TOOLCHAIN_ROOT.\n"
                                "The repo recipe must use "
                                "$DRIFT_TOOLCHAIN_ROOT/bin/drift and "
                                "$DRIFT_TOOLCHAIN_ROOT/bin/driftc instead "
                                "of relying on ambient PATH.\n"
                            )

                if contract_violation:
                    print(f"FAILED ({_fmt_duration(elapsed)}, contract "
                          f"violation: not using DRIFT_TOOLCHAIN_ROOT)")
                else:
                    print(f"FAILED ({_fmt_duration(elapsed)})")
                print(f"    see: {log_file}")

        except FileNotFoundError as e:
            step_finished = datetime.now(timezone.utc)
            elapsed = (step_finished - step_started).total_seconds()
            status = "blocked"
            verdict = "blocked"
            log_file.write_text(f"=== {label} ===\ncommand not found: {e}\n")
            print(f"BLOCKED (command not found, {_fmt_duration(elapsed)})")

        step_record: dict = {
            "repo": repo_name,
            "name": action,
            "lane": lane,
            "status": status,
            "command": resolved_cmd,
            "log_path": str(log_file),
            "started_at": step_started.isoformat(),
            "finished_at": step_finished.isoformat(),
            "duration_s": round(elapsed, 1),
        }
        if is_gate:
            step_record["certification_gate"] = True
            step_record["DRIFT_TOOLCHAIN_ROOT"] = step_env.get(
                "DRIFT_TOOLCHAIN_ROOT", None)
            step_record["DRIFT_DEBUG"] = step_env.get("DRIFT_DEBUG", "")
            if contract_violation:
                step_record["contract_violation"] = True
        step_results.append(step_record)

        # Fail fast: stop on first failure.
        if status in ("failed", "blocked"):
            break

    print()

    # Collect artifact provenance from staged packages.
    toolchain_version = get_toolchain_version(ctx)
    require_provenance = toolchain_supports_provenance(toolchain_version)
    artifacts = scan_staged_artifacts(ctx.pkgs_root)

    if artifacts and verdict == "certified":
        prov_errors = check_provenance_completeness(artifacts, require_provenance)
        if prov_errors:
            verdict = "rejected"
            print("Provenance check failed:")
            for err in prov_errors:
                print(f"  - {err}")
            print()

    # Verify toolchain commit identity if drift-lang was submitted.
    toolchain_identity = _resolve_toolchain_identity(ctx)
    toolchain_commit_mismatch: Optional[str] = None
    if "drift-lang" in plan.candidate_commits:
        toolchain_commit_mismatch = _verify_toolchain_commit(
            toolchain_identity, plan.candidate_commits["drift-lang"],
        )
        if toolchain_commit_mismatch:
            print(f"Toolchain identity warning: {toolchain_commit_mismatch}")
            print()

    summary = _build_summary(
        ctx, config, plan, started_at, verdict, step_results,
        toolchain_version=toolchain_version,
        artifacts=artifacts,
        toolchain_commit_mismatch=toolchain_commit_mismatch,
        fasttrack=plan.fasttrack,
    )

    _write_run_outputs(ctx, summary)

    # Update the config-scoped workspace lock only on certified verdict.
    if verdict == "certified":
        _update_workspace_lock(config, plan, ctx, summary)

    total_elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    print(f"Verdict: {verdict}  ({_fmt_duration(total_elapsed)} total)")
    return summary


_ARTIFACT_PATH_KEYS: tuple[tuple[str, str], ...] = (
    # (record-key, display-label). Order = trust lineage: package,
    # author signature, cert signature(s), author identity, build provenance.
    ("artifact_path",       "artifact"),
    ("author_claim_path",   "author claim"),
    ("cert_claim_paths",    "cert claim"),
    ("author_profile_path", "author profile"),
    ("provenance_path",     "provenance"),
)


def _artifact_path_lines(
    art: dict, rewrite: Optional[callable] = None,
) -> list[str]:
    """Render the sidecar-paths block for one artifact record.

    *rewrite*, if given, is applied to each path string (used by
    ``promote_run`` to re-root paths under ``pkgs/``). The same renderer
    is used by the in-run ``artifacts.txt`` and the promoted snapshot's
    ``artifacts.txt`` so both stay in sync as new sidecars appear.
    """
    lines: list[str] = []
    for key, label in _ARTIFACT_PATH_KEYS:
        value = art.get(key)
        if not value:
            continue
        paths = value if isinstance(value, list) else [value]
        for p in paths:
            shown = rewrite(p) if rewrite else p
            lines.append(f"  {label}: {shown}")
    return lines


def _generate_artifacts_txt(summary: dict) -> str:
    """Generate artifacts.txt listing all produced artifact paths."""
    lines: list[str] = []
    artifacts = summary.get("artifacts", [])
    if not artifacts:
        lines.append("No artifacts produced.")
        return "\n".join(lines) + "\n"

    for art in artifacts:
        lines.append(f"{art['name']}@{art['version']}")
        lines.extend(_artifact_path_lines(art))
        lines.append("")

    return "\n".join(lines)


def _write_run_outputs(ctx: RunContext, summary: dict) -> None:
    """Write summary.json, report.txt, report-short.txt, and artifacts.txt."""
    summary_json_path = ctx.run_root / "summary.json"
    summary_json_path.write_text(json.dumps(summary, indent=2) + "\n")

    report_path = ctx.run_root / "report.txt"
    report_path.write_text(generate_report(summary))

    report_short_path = ctx.run_root / "report-short.txt"
    report_short_path.write_text(generate_report_short(summary) + "\n")

    artifacts_path = ctx.run_root / "artifacts.txt"
    artifacts_path.write_text(_generate_artifacts_txt(summary))

    print(f"Summary:   {summary_json_path}")
    print(f"Report:    {report_path}")
    print(f"Short:     {report_short_path}")
    print(f"Artifacts: {artifacts_path}")


def _resolve_toolchain_identity(ctx: RunContext) -> Optional[dict]:
    """Read the staged toolchain version and paths if available."""
    # Flat layout: toolchain_root is the toolchain root directly,
    # with bin/, lib/ etc. underneath.  No inner current symlink
    # or versioned subdirectory.
    driftc_bin = ctx.toolchain_root / "bin" / "driftc"
    drift_bin = ctx.toolchain_root / "bin" / "drift"
    if not driftc_bin.exists() and not drift_bin.exists():
        return None
    identity: dict = {"directory": ctx.toolchain_root.name}
    if driftc_bin.exists():
        try:
            result = subprocess.run(
                [str(driftc_bin), "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                identity["driftc_version"] = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        identity["driftc"] = str(driftc_bin)
    if drift_bin.exists():
        identity["drift"] = str(drift_bin)
    return identity


_GIT_FIELD_RE = re.compile(r"\bgit ([0-9a-f]{7,40})\b")


def _verify_toolchain_commit(
    identity: Optional[dict], submitted_sha: str,
) -> Optional[str]:
    """Check that the staged toolchain's embedded git hash matches the
    submitted drift-lang commit.

    Returns an error message if there is a mismatch, or None if the
    identity is unavailable or matches.
    """
    if not identity:
        return None
    version_str = identity.get("driftc_version", "")
    m = _GIT_FIELD_RE.search(version_str)
    if not m:
        return None
    embedded = m.group(1)
    # The embedded hash is typically short (7 chars).  Compare as a prefix.
    if submitted_sha.startswith(embedded) or embedded.startswith(submitted_sha):
        return None
    return (
        f"toolchain commit mismatch: staged driftc reports git {embedded}, "
        f"but submitted drift-lang commit is {submitted_sha[:7]}. "
        f"The built toolchain does not embed the submitted source identity."
    )


def _build_summary(
    ctx: RunContext,
    config: OrchestrationConfig,
    plan: ExecutionPlan,
    started_at: datetime,
    verdict: str,
    steps_results: list[dict],
    block_reason: Optional[str] = None,
    toolchain_version: Optional[str] = None,
    artifacts: Optional[list[dict]] = None,
    toolchain_commit_mismatch: Optional[str] = None,
    fasttrack: bool = False,
) -> dict:
    finished_at = datetime.now(timezone.utc)
    summary: dict = {
        "schema_version": 1,
        "run_id": ctx.run_id,
        "config_name": config.config_name,
        "lock_file": str(config.lock_path),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "verdict": verdict,
        "changed_repos": plan.changed_repos,
        "candidate_commits": plan.candidate_commits,
        "commit_sources": plan.commit_sources,
        "involved_repos": plan.involved_repos,
        "validated_repos": plan.validated_repos,
        "staging": {
            "run_root": str(ctx.run_root),
            "toolchain_root": str(ctx.toolchain_root),
            "pkgs_root": str(ctx.pkgs_root),
            "apps_root": str(ctx.apps_root),
            "logs_root": str(ctx.logs_root),
            "toolchain_identity": _resolve_toolchain_identity(ctx),
        },
        "toolchain_contract": {
            "DRIFT_TOOLCHAIN_ROOT": str(
                ctx.toolchain_root.resolve()
            ),
            "ambient_scrubbed": True,
            "enforcement": "certification gates ran with ambient "
                           "drift/driftc removed from PATH",
        },
        "steps": steps_results,
    }
    if toolchain_version:
        summary["toolchain_version"] = toolchain_version
    if artifacts:
        summary["artifacts"] = artifacts
    if block_reason:
        summary["block_reason"] = block_reason
    if toolchain_commit_mismatch:
        summary["toolchain_commit_mismatch"] = toolchain_commit_mismatch
    if fasttrack:
        summary["fasttrack"] = True
    return summary


def _update_workspace_lock(
    config: OrchestrationConfig,
    plan: ExecutionPlan,
    ctx: RunContext,
    summary: dict,
) -> None:
    lock_path = config.lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock_data = {
        "schema_version": 1,
        "updated_at": summary["finished_at"],
        "source_run_id": ctx.run_id,
        "verdict": "certified",
        "changed_repos": plan.changed_repos,
        "repos": {},
    }
    for name, sha in plan.candidate_commits.items():
        lock_data["repos"][name] = {
            "path": config.repos[name].path,
            "commit": sha,
        }

    lock_path.write_text(json.dumps(lock_data, indent=2) + "\n")
    print(f"Updated: {lock_path}")


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def _load_run_summary(run_root: Path) -> dict:
    summary_path = run_root / "summary.json"
    if not summary_path.exists():
        print(f"error: run summary not found: {summary_path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(summary_path.read_text())


def _safe_unlink(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _check_dest_cleanliness(dest: Path) -> list[str]:
    """Check the promotion destination for transient/leaked artifacts.

    Returns a list of problem descriptions, empty if clean.
    """
    problems: list[str] = []
    if not dest.exists():
        return problems
    for entry in dest.iterdir():
        if entry.name.startswith(".drift-deploy-staging"):
            problems.append(
                f"leaked deploy staging directory: {entry}"
            )
    return problems


def promote_run(run_id: str, dest_root: Path) -> int:
    """Promote a certified run into the snapshot-scoped certified tree.

    Layout:
        <dest_root>/certified/snapshots/<run-id>/toolchain/
        <dest_root>/certified/snapshots/<run-id>/pkgs/
        <dest_root>/certified/snapshots/<run-id>/apps/   (if any kind:app artifacts)
        <dest_root>/certified/snapshots/<run-id>/summary.json
        <dest_root>/certified/snapshots/<run-id>/report.txt
        <dest_root>/certified/current -> snapshots/<run-id>
    """
    run_root = Path("build/runs") / run_id
    summary = _load_run_summary(run_root)

    if summary.get("verdict") != "certified":
        print(
            f"error: run {run_id} is not certified "
            f"(verdict={summary.get('verdict', 'unknown')})",
            file=sys.stderr,
        )
        return 1

    if summary.get("fasttrack"):
        print(
            f"note: run {run_id} was a fasttrack run "
            f"(debug-lane gates skipped — artifacts are complete, "
            f"but debug-lane test coverage is absent)",
        )

    staging = summary.get("staging", {})
    toolchain_root_str = staging.get("toolchain_root", "")
    # `pkgs_root` is the current key; fall back to the legacy `libs_root`
    # so summaries written before the libs→pkgs rename still promote.
    pkgs_root_str = staging.get("pkgs_root") or staging.get("libs_root", "")
    if not toolchain_root_str or not pkgs_root_str:
        print(f"error: run {run_id} summary is missing staging roots",
              file=sys.stderr)
        return 1
    source_toolchain_root = Path(toolchain_root_str)
    source_pkgs_root = Path(pkgs_root_str)

    resolved_dest = dest_root.expanduser().resolve()

    # Reject destinations with leaked transient state.
    dest_problems = _check_dest_cleanliness(resolved_dest)
    if dest_problems:
        print("error: destination root contains transient artifacts "
              "that must be cleaned up before promotion:",
              file=sys.stderr)
        for p in dest_problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    # Snapshot destination.
    snapshots_dir = resolved_dest / "certified" / "snapshots"
    snapshot_dir = snapshots_dir / run_id

    if snapshot_dir.exists():
        print(f"error: snapshot already exists: {snapshot_dir}",
              file=sys.stderr)
        print("Promotion must not overwrite an existing certified snapshot.",
              file=sys.stderr)
        return 1

    snapshot_dir.mkdir(parents=True)
    snapshot_toolchain = snapshot_dir / "toolchain"
    snapshot_pkgs = snapshot_dir / "pkgs"

    print(f"Promoting certified run: {run_id}")
    print(f"  source toolchain: {source_toolchain_root}")
    print(f"  source pkgs:      {source_pkgs_root}")
    print(f"  snapshot:         {snapshot_dir}")
    print()

    # Copy toolchain: flat layout — toolchain_root is the toolchain root
    # directly, with bin/, lib/ etc. underneath.
    if not (source_toolchain_root / "bin").exists():
        print(f"error: staged toolchain missing bin/: {source_toolchain_root}",
              file=sys.stderr)
        shutil.rmtree(snapshot_dir)
        return 1

    shutil.copytree(source_toolchain_root, snapshot_toolchain, symlinks=True)

    # Copy packages.
    if not source_pkgs_root.exists():
        print(f"error: staged packages root not found: {source_pkgs_root}",
              file=sys.stderr)
        shutil.rmtree(snapshot_dir)
        return 1

    shutil.copytree(source_pkgs_root, snapshot_pkgs, symlinks=True)

    # Copy apps (kind:app artifacts staged via --app-dest). Optional: the
    # key is absent in pre-app-cert summaries and the dir is empty when no
    # app was staged this run. A certified app dir carries the binary plus
    # its author/cert/provenance legs, so it must travel with the snapshot
    # or `verify-app` and app consumers can't resolve it from certified/.
    apps_root_str = staging.get("apps_root", "")
    source_apps_root = Path(apps_root_str) if apps_root_str else None
    if (
        source_apps_root
        and source_apps_root.exists()
        and any(source_apps_root.iterdir())
    ):
        shutil.copytree(
            source_apps_root, snapshot_dir / "apps", symlinks=True,
        )
        print(f"  source apps:      {source_apps_root}")

    # Copy certification metadata into the snapshot.
    for name in ("summary.json", "report.txt", "report-short.txt"):
        src = run_root / name
        if src.exists():
            shutil.copy2(src, snapshot_dir / name)

    # Copy the canonical workspace lock into the snapshot.
    lock_file = summary.get("lock_file", "")
    if lock_file:
        lock_src = Path(lock_file)
        if lock_src.exists():
            shutil.copy2(lock_src, snapshot_dir / "workspace-lock.json")

    # Generate snapshot-local artifacts.txt by re-rooting actual artifact
    # paths from the summary relative to the snapshot's pkgs/ directory.
    # Reuses pkgs_root_str / apps_root_str resolved above (legacy-key aware).
    artifacts = summary.get("artifacts", [])

    def _rereoot(src_path: str) -> str:
        if pkgs_root_str and src_path.startswith(pkgs_root_str):
            rel = src_path[len(pkgs_root_str):].lstrip("/")
            return f"pkgs/{rel}"
        if apps_root_str and src_path.startswith(apps_root_str):
            rel = src_path[len(apps_root_str):].lstrip("/")
            return f"apps/{rel}"
        return src_path

    art_lines: list[str] = []
    if artifacts:
        for art in artifacts:
            art_lines.append(f"{art['name']}@{art['version']}")
            art_lines.extend(_artifact_path_lines(art, rewrite=_rereoot))
            art_lines.append("")
    else:
        art_lines.append("No artifacts produced.")
    (snapshot_dir / "artifacts.txt").write_text("\n".join(art_lines) + "\n")

    # Update the certified/current convenience symlink.
    current_symlink = resolved_dest / "certified" / "current"
    if current_symlink.exists() or current_symlink.is_symlink():
        _safe_unlink(current_symlink)
    current_symlink.symlink_to(Path("snapshots") / run_id)

    print("Promotion complete:")
    print(f"  snapshot:  {snapshot_dir}")
    print(f"  toolchain: {snapshot_toolchain}")
    print(f"  pkgs:      {snapshot_pkgs}")
    print(f"  current:   {current_symlink} -> {current_symlink.resolve()}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Workspace certification orchestrator"
    )
    parser.add_argument(
        "--config",
        default="orchestration.json",
        help="Path to orchestration.json",
    )
    parser.add_argument(
        "--cert-env",
        default=None,
        help="Path to the host-local cert-env.json (external capability "
             "resolution). Default lookup: --cert-env, then $DRIFT_CERT_ENV, "
             "then ./cert-env.json. Host-specific; never committed.",
    )

    sub = parser.add_subparsers(dest="command")

    plan_parser = sub.add_parser("plan", help="Compute and display execution plan")
    plan_parser.add_argument(
        "input_file",
        help="JSON file mapping repo names to candidate commit SHAs",
    )
    plan_parser.add_argument(
        "--json",
        action="store_true",
        help="Output plan as JSON",
    )
    plan_parser.add_argument(
        "--fasttrack",
        action="store_true",
        help="Skip debug-lane gates (test/stress/perf). Build artifacts are still complete; debug-lane test coverage is absent.",
    )

    certify_parser = sub.add_parser("certify", help="Execute a certification run")
    certify_parser.add_argument(
        "input_file",
        help="JSON file mapping repo names to candidate commit SHAs",
    )
    certify_parser.add_argument(
        "--fasttrack",
        action="store_true",
        help="Skip debug-lane gates (test/stress/perf). Build artifacts are still complete; debug-lane test coverage is absent.",
    )

    promote_parser = sub.add_parser(
        "promote", help="Promote a certified run into a snapshot-scoped certified tree"
    )
    promote_parser.add_argument(
        "run_id",
        help="Certified run id to promote",
    )
    promote_parser.add_argument(
        "--dest-root",
        default="~/opt/drift",
        help="Destination root; snapshot published under "
             "<dest-root>/certified/snapshots/<run-id>/ "
             "(default: ~/opt/drift)",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "promote":
        sys.exit(promote_run(args.run_id, Path(args.dest_root)))

    # Commands below here require a valid orchestration config.
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        config = OrchestrationConfig.load(config_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.command in ("plan", "certify"):
        plan = _load_and_plan(config, args.input_file, fasttrack=args.fasttrack)
        if plan is None:
            sys.exit(0)

        if args.command == "plan":
            if args.json:
                print_plan_json(plan)
            else:
                print_plan(plan, config)
        else:
            cert_env = _resolve_cert_env(args.cert_env)
            summary = execute_run(config, plan, cert_env=cert_env)
            sys.exit(0 if summary["verdict"] == "certified" else 1)


def _resolve_cert_env(cli_path: Optional[str]) -> Optional[CertEnv]:
    """Resolve and load the host-local cert-env file.

    Lookup order: ``--cert-env`` → ``$DRIFT_CERT_ENV`` → ``./cert-env.json``.
    An explicitly named path that does not exist is an error (the operator
    asked for it); the implicit default is optional — a run that needs no
    capability runs fine without one, and one that does is blocked clearly by
    the preflight.
    """
    explicit = cli_path or os.environ.get("DRIFT_CERT_ENV")
    if explicit:
        path = Path(explicit)
        if not path.exists():
            print(f"error: cert-env file not found: {path}", file=sys.stderr)
            sys.exit(1)
    else:
        path = Path("cert-env.json")
        if not path.exists():
            return None
    try:
        return CertEnv.load(path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _load_and_plan(
    config: OrchestrationConfig, input_file: str, *, fasttrack: bool = False,
) -> Optional[ExecutionPlan]:
    """Shared input validation and plan computation for plan/certify commands."""
    input_path = Path(input_file)
    if not input_path.exists():
        print(f"error: commit input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    submitted = load_commit_input(input_path)

    unknown = [n for n in submitted if n not in config.repos]
    if unknown:
        print(f"error: unknown repos in input: {', '.join(unknown)}",
              file=sys.stderr)
        sys.exit(1)

    validate_shas(submitted)

    lock = WorkspaceLock.load(config.lock_path)

    commits, sources = resolve_commits(config, submitted, lock)
    changed = detect_changed(commits, lock)

    if not changed:
        print("No repos changed relative to certified snapshot.")
        return None

    return compute_plan(config, commits, sources, changed, fasttrack=fasttrack)


if __name__ == "__main__":
    main()
