# build-orchestrator

Commit-pinned certification orchestrator for Drift workspaces.

This repo stages candidate toolchains and packages from exact git commits,
runs downstream certification from fresh checkouts, records reports and
artifact inventories, and updates a certified workspace snapshot when the
full stack passes.

## What It Does

- resolves a workspace snapshot from submitted repo commits plus the last
  certified snapshot
- materializes fresh checkouts for all involved repos
- stages the candidate Drift toolchain
- stages downstream package artifacts into a candidate package root
- runs repo-owned certification gates (`just test`, `just stress`, `just perf`)
  in a hardened environment with `DRIFT_TOOLCHAIN_ROOT` and ambient toolchain
  scrubbing
- records run evidence, reports, and artifact locations
- updates the config-scoped workspace lock only on certified runs
- promotes certified runs into immutable snapshot-scoped destinations

## Current Workspace

The default orchestration config covers:

- `../drift-lang`
- `../drift-mariadb-client`
- `../drift-net-tls`
- `../drift-web`

`drift-lang` is treated as the toolchain source for certification runs. It
is staged for downstream use but is not itself a validation target.

## Inputs And Outputs

Primary inputs:

- `orchestration.json` — workspace graph and command contract
- commit input JSON — submitted candidate commits for the run
- `state/<config>.workspace-lock.json` — last certified workspace snapshot
  (scoped by config filename)

Primary outputs per run:

- `report.txt`
- `report-short.txt`
- `summary.json`
- `artifacts.txt`

## Usage

```bash
# Plan a run (dry run, no execution):
./orchestrate.py plan path/to/commits.json

# Execute a certification run:
./orchestrate.py certify path/to/commits.json

# Promote a certified run into the certified snapshot tree:
./orchestrate.py promote <run-id>
./orchestrate.py promote <run-id> --dest-root ~/opt/drift
```

`plan` and `certify` require `--config` (defaults to `orchestration.json`).
`promote` does not — it operates solely from the run's `summary.json`.

### Promotion

Promotion publishes a certified run's exact outputs into an immutable
snapshot-scoped directory:

```text
~/opt/drift/certified/
  snapshots/
    <run-id>/
      toolchain/          # staged toolchain (with current symlink)
      libs/               # staged package artifacts
      summary.json        # certification evidence
      report.txt
      report-short.txt
      artifacts.txt
  current -> snapshots/<run-id>
```

- The run ID is the certification identity — not the version string
- Snapshots are immutable; promoting the same run-id twice is rejected
- `certified/current` is a convenience symlink to the latest promoted snapshot

Certification runs operate on:

- exact commit SHAs
- fresh checkouts
- staged toolchain and package roots

Ambient local worktrees are not part of the certification source of truth.

## Repository Layout

```text
docs/                 # design docs and schema notes
build/runs/           # per-run outputs and logs
state/                # certified workspace snapshot
orchestrate.py        # Python driver
orchestration.json    # workspace config
```

## Documentation

- [Orchestrator plan](docs/orchestrator-plan.md)
- [Orchestrator schema](docs/orchestrator-schema.md)
