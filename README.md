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
- runs repo-owned `just test` flows in a controlled staged environment
- records run evidence, reports, and artifact locations
- updates `state/workspace-lock.json` only on certified runs

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
- `state/workspace-lock.json` — last certified workspace snapshot

Primary outputs per run:

- `report.txt`
- `report-short.txt`
- `summary.json`
- `artifacts.txt`

## Development

The driver is implemented in Python:

```bash
./orchestrate.py --config orchestration.json plan path/to/commits.json
```

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
