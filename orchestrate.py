#!/usr/bin/env python3
"""Workspace certification orchestrator for the drift ecosystem."""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RepoConfig:
    name: str
    path: str
    kind: str
    depends_on: list[str]
    affects: list[str]
    commands: dict[str, list[str]]


@dataclass
class OrchestrationConfig:
    schema_version: int
    workspace_root: str
    run_root: str
    state_root: str
    repos: dict[str, RepoConfig]
    environment: dict
    config_name: str = "orchestration"

    @staticmethod
    def load(path: Path) -> "OrchestrationConfig":
        raw = json.loads(path.read_text())
        repos = {}
        for name, r in raw["repos"].items():
            repos[name] = RepoConfig(
                name=name,
                path=r["path"],
                kind=r["kind"],
                depends_on=r.get("depends_on", []),
                affects=r.get("affects", []),
                commands=r.get("commands", {}),
            )
        # Derive config_name from the filename, stripping .json.
        config_name = path.stem
        return OrchestrationConfig(
            schema_version=raw["schema_version"],
            workspace_root=raw["workspace"]["root"],
            run_root=raw["workspace"]["run_root"],
            state_root=raw["workspace"]["state_root"],
            repos=repos,
            environment=raw.get("environment", {}),
            config_name=config_name,
        )

    @property
    def lock_filename(self) -> str:
        return f"{self.config_name}.workspace-lock.json"

    @property
    def lock_path(self) -> Path:
        return Path(self.state_root) / self.lock_filename


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


# ---------------------------------------------------------------------------
# Graph operations
# ---------------------------------------------------------------------------

def build_forward_graph(config: OrchestrationConfig) -> dict[str, list[str]]:
    """Build adjacency list: repo -> list of repos it affects."""
    graph: dict[str, list[str]] = {name: [] for name in config.repos}
    for name, repo in config.repos.items():
        for target in repo.affects:
            if target in config.repos:
                graph[name].append(target)
    return graph


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
) -> ExecutionPlan:
    forward_graph = build_forward_graph(config)
    affected = compute_affected(forward_graph, changed)

    # Pull in dependency providers needed by affected repos (e.g. drift-lang
    # as toolchain input even when it didn't change).
    involved_set = set(affected)
    for repo_name in list(affected):
        for dep in config.repos[repo_name].depends_on:
            involved_set.add(dep)

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
            if "bootstrap" in repo.commands:
                steps.append({
                    "repo": repo_name,
                    "action": "bootstrap",
                    "command": repo.commands["bootstrap"],
                    "reason": "toolchain venv setup",
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
            # (DRIFT_DEBUG selects the runtime at link time).
            for lane in _LANES:
                for gate in ("test", "stress", "perf"):
                    if gate in repo.commands:
                        steps.append({
                            "repo": repo_name,
                            "action": gate,
                            "lane": lane,
                            "command": repo.commands[gate],
                            "reason": validation_reason,
                        })

    return ExecutionPlan(
        candidate_commits=commits,
        commit_sources=sources,
        changed_repos=changed,
        involved_repos=involved,
        validated_repos=validated,
        steps=steps,
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
        # steps (bootstrap, stage_*) are not validation.
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
        return (f"{verdict}: submitted {submitted_label}. "
                f"Validated: {validated_label}. {lock_msg}.")
    else:
        fail_step = _first_failure(summary)
        if fail_step:
            reason = _failure_reason(summary)
            fail_lane = fail_step.get("lane")
            lane_label = f" [{fail_lane}]" if fail_lane else ""
            return (f"{verdict}: submitted {submitted_label}. "
                    f"First failure: {fail_step['repo']} "
                    f"{fail_step['name']}{lane_label} "
                    f"({reason}). {lock_msg}.")
        elif summary.get("block_reason"):
            return (f"{verdict}: submitted {submitted_label}. "
                    f"{summary['block_reason']}. {lock_msg}.")
        else:
            return f"{verdict}: submitted {submitted_label}. {lock_msg}."


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


def scan_staged_artifacts(libs_root: Path) -> list[dict]:
    """Scan the staged libs root for deployed artifacts.

    Returns a list of artifact records with paths to the artifact,
    sig, author-profile, and provenance bundle if present.
    """
    artifacts: list[str] = []
    if not libs_root.exists():
        return []

    results: list[dict] = []
    for artifact_dir in sorted(libs_root.iterdir()):
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

            # Look for known artifact files.
            zdmp = version_dir / f"{artifact_name}.zdmp"
            sig = version_dir / f"{artifact_name}.sig"
            author_profile = version_dir / f"{artifact_name}.author-profile"
            provenance = version_dir / f"{artifact_name}.provenance.zst"

            if zdmp.exists():
                record["artifact_path"] = str(zdmp)
            if sig.exists():
                record["sig_path"] = str(sig)
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
    run_root: Path
    checkouts_root: Path
    toolchain_root: Path
    libs_root: Path
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
        run_root=run_root,
        checkouts_root=run_root / "checkouts",
        toolchain_root=run_root / "toolchain",
        libs_root=run_root / "libs",
        logs_root=run_root / "logs",
    )

    for d in [ctx.checkouts_root, ctx.toolchain_root, ctx.libs_root, ctx.logs_root]:
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
        "{libs_root}": str(ctx.libs_root.resolve()),
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
    # Hermetic lane selection: scrub any inherited DRIFT_DEBUG (and the
    # retired DRIFT_OPTIMIZED) before re-adding only what this lane wants.
    env.pop("DRIFT_DEBUG", None)
    env.pop("DRIFT_OPTIMIZED", None)
    if gate and lane == "debug":
        env["DRIFT_DEBUG"] = "1"
    # Honor DRIFT_TEST_JOBS if the operator set it; otherwise default to
    # nproc/2 (matching the compiler build's policy). Floor at 1.
    if "DRIFT_TEST_JOBS" not in env:
        env["DRIFT_TEST_JOBS"] = str(max(1, (os.cpu_count() or 2) // 2))
    toolchain_root = str(ctx.toolchain_root.resolve())
    libs_root = str(ctx.libs_root.resolve())

    subs = {
        "{toolchain_root}": toolchain_root,
        "{libs_root}": libs_root,
    }
    for var, template in config.environment.get("vars", {}).items():
        value = template
        for placeholder, resolved in subs.items():
            value = value.replace(placeholder, resolved)
        env[var] = value

    staged_bin = str((ctx.toolchain_root / "bin").resolve())

    if gate:
        # Certification gates: scrub ambient drift/driftc from PATH so
        # repos that depend on PATH instead of DRIFT_TOOLCHAIN_ROOT fail.
        scrubbed = _scrub_ambient_toolchain(
            env.get("PATH", ""), staged_bin,
        )
        env["PATH"] = staged_bin + os.pathsep + scrubbed
    else:
        # Infra steps (bootstrap, stage_*): keep full PATH for flexibility.
        env["PATH"] = staged_bin + os.pathsep + env.get("PATH", "")

    return env


def execute_run(
    config: OrchestrationConfig, plan: ExecutionPlan
) -> dict:
    """Execute the full certification run. Returns the summary dict."""
    started_at = datetime.now(timezone.utc)
    ctx = create_run_context(config, plan)

    print(f"Run: {ctx.run_id}")
    print(f"Dir: {ctx.run_root}")
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
            )
            _write_run_outputs(ctx, summary)
            return summary
    print()

    # Execute steps.
    step_results: list[dict] = []
    verdict = "certified"
    dual_runtime_verified = False

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
                )
                _write_run_outputs(ctx, summary)
                return summary

        step_env = build_step_env(
            config, ctx, gate=is_gate, lane=lane,
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
                # Activity-based timeout: kill only after 120s of no
                # new output.  This lets long-running but progressing
                # test suites (e.g. plain + ASAN passes) finish while
                # still catching true hangs quickly.
                _inactivity_limit = 120
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

            if timed_out:
                status = "failed"
                verdict = "rejected"
                print("TIMEOUT")
                print(f"    see: {log_file}")
            elif returncode == 0:
                status = "passed"
                print("ok")
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
                    print("FAILED (contract violation: not using DRIFT_TOOLCHAIN_ROOT)")
                else:
                    print("FAILED")
                print(f"    see: {log_file}")

        except FileNotFoundError as e:
            step_finished = datetime.now(timezone.utc)
            status = "blocked"
            verdict = "blocked"
            log_file.write_text(f"=== {label} ===\ncommand not found: {e}\n")
            print("BLOCKED (command not found)")

        step_record: dict = {
            "repo": repo_name,
            "name": action,
            "lane": lane,
            "status": status,
            "command": resolved_cmd,
            "log_path": str(log_file),
            "started_at": step_started.isoformat(),
            "finished_at": step_finished.isoformat(),
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

    # Collect artifact provenance from staged libs.
    toolchain_version = get_toolchain_version(ctx)
    require_provenance = toolchain_supports_provenance(toolchain_version)
    artifacts = scan_staged_artifacts(ctx.libs_root)

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
    )

    _write_run_outputs(ctx, summary)

    # Update the config-scoped workspace lock only on certified verdict.
    if verdict == "certified":
        _update_workspace_lock(config, plan, ctx, summary)

    print(f"Verdict: {verdict}")
    return summary


def _generate_artifacts_txt(summary: dict) -> str:
    """Generate artifacts.txt listing all produced artifact paths."""
    lines: list[str] = []
    artifacts = summary.get("artifacts", [])
    if not artifacts:
        lines.append("No artifacts produced.")
        return "\n".join(lines) + "\n"

    for art in artifacts:
        lines.append(f"{art['name']}@{art['version']}")
        for key in ("artifact_path", "sig_path", "author_profile_path",
                     "provenance_path"):
            path = art.get(key)
            if path:
                label = key.replace("_path", "").replace("_", " ")
                lines.append(f"  {label}: {path}")
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
            "libs_root": str(ctx.libs_root),
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
        <dest_root>/certified/snapshots/<run-id>/libs/
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

    staging = summary.get("staging", {})
    toolchain_root_str = staging.get("toolchain_root", "")
    libs_root_str = staging.get("libs_root", "")
    if not toolchain_root_str or not libs_root_str:
        print(f"error: run {run_id} summary is missing staging roots",
              file=sys.stderr)
        return 1
    source_toolchain_root = Path(toolchain_root_str)
    source_libs_root = Path(libs_root_str)

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
    snapshot_libs = snapshot_dir / "libs"

    print(f"Promoting certified run: {run_id}")
    print(f"  source toolchain: {source_toolchain_root}")
    print(f"  source libs:      {source_libs_root}")
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

    # Copy libs.
    if not source_libs_root.exists():
        print(f"error: staged libs root not found: {source_libs_root}",
              file=sys.stderr)
        shutil.rmtree(snapshot_dir)
        return 1

    shutil.copytree(source_libs_root, snapshot_libs, symlinks=True)

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
    # paths from the summary relative to the snapshot's libs/ directory.
    artifacts = summary.get("artifacts", [])
    libs_root_str = staging.get("libs_root", "")
    art_lines: list[str] = []
    if artifacts:
        for art in artifacts:
            art_lines.append(f"{art['name']}@{art['version']}")
            for key in ("artifact_path", "sig_path",
                        "author_profile_path", "provenance_path"):
                src_path = art.get(key, "")
                if not src_path:
                    continue
                label = key.replace("_path", "").replace("_", " ")
                # Re-root: strip the run-local libs root, prefix with libs/
                if libs_root_str and src_path.startswith(libs_root_str):
                    rel = src_path[len(libs_root_str):].lstrip("/")
                    art_lines.append(f"  {label}: libs/{rel}")
                else:
                    art_lines.append(f"  {label}: {src_path}")
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
    print(f"  libs:      {snapshot_libs}")
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

    certify_parser = sub.add_parser("certify", help="Execute a certification run")
    certify_parser.add_argument(
        "input_file",
        help="JSON file mapping repo names to candidate commit SHAs",
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

    config = OrchestrationConfig.load(config_path)

    if args.command in ("plan", "certify"):
        plan = _load_and_plan(config, args.input_file)
        if plan is None:
            sys.exit(0)

        if args.command == "plan":
            if args.json:
                print_plan_json(plan)
            else:
                print_plan(plan, config)
        else:
            summary = execute_run(config, plan)
            sys.exit(0 if summary["verdict"] == "certified" else 1)


def _load_and_plan(
    config: OrchestrationConfig, input_file: str
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

    return compute_plan(config, commits, sources, changed)


if __name__ == "__main__":
    main()
