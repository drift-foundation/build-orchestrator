# Build Orchestrator Plan

## Goal

Create a workspace-local orchestrator that coordinates candidate validation across these sibling repositories:

- `../drift-lang`
- `../drift-mariadb-client`
- `../drift-net-tls`
- `../drift-web`

The orchestrator should remove manual repo switching and ensure upstream candidate changes automatically trigger the correct downstream rebuild and validation work.

## Core Requirement

The orchestrator must not rely on a previously deployed Drift toolchain leaking in from `PATH`.

Instead:

- certification runs should evaluate exact committed candidate states from fresh checkouts for all repos
- `drift-lang` changes must produce a staged compiler/toolchain artifact for the current run.
- Downstream repos must use that staged toolchain explicitly.
- Package resolution and deploy validation must target a staged package root for the current run.
- The orchestrator's job ends at certification. Owner teams decide whether to deploy to production destinations such as `~/opt/drift/`.

This keeps orchestration deterministic and ensures downstream validation uses exactly the toolchain built in the current run.

## Problem Statement

Today the workflow is distributed across multiple repos:

- `drift-lang` owns the compiler/toolchain and the `drift prepare` / `drift deploy` machinery.
- Package repos own their own manifests, tests, and deploy flows.
- A change in one upstream repo can require retesting and republishing several downstream repos.

The current pain points are:

- manual context switching between repos
- manually choosing which downstream repos are affected
- manually wiring `DRIFTC` and package roots
- uncertainty about whether an upstream candidate is safe for downstream consumers
- risk of testing against stale deployed tools or packages

## Current Dependency Graph

### Repo-Level Graph

- `drift-lang` -> `drift-mariadb-client`
- `drift-lang` -> `drift-net-tls`
- `drift-lang` -> `drift-web`
- `drift-net-tls` -> `drift-web`

Current evidence:

- all package repos depend on the Drift compiler/tooling
- `drift-web` depends on `net-tls`
- no repo-level downstream consumer was identified for `drift-mariadb-client`

### Intra-Repo Artifact Graph

From the manifests:

- `mariadb-wire-proto` -> `mariadb-rpc`
- `web-jwt` -> `web-rest`
- `net-tls` -> `web-client`

These matter because a repo-level orchestrator may still need to support artifact-level version bumps and deploy ordering inside a single repo.

## Existing Lifecycle Commands

The orchestrator should treat `just test` as the only stable repo-local interface. Build and deploy work should use the staged Drift toolchain directly.

### `drift-lang`

- `just test`

Relevant behavior:

- owns the compiler and deploy tooling
- can build a self-contained deployment
- already tests the deploy/build/prepare toolchain path

Orchestrator rule:

- `drift-lang` is staged for downstream use
- `drift-lang` is not test-gated by the orchestrator

### `drift-mariadb-client`

- `just test`

### `drift-net-tls`

- `just test`

### `drift-web`

- `just test`

Operational rule:

- `just test` may include arbitrary repo-specific logic and remains repo-owned.
- build/deploy orchestration should invoke the staged `drift` and `driftc` tools directly rather than relying on repo-local convenience wrappers.
- the orchestrator must not run `prepare`, because that modifies `drift-lock.json`, which remains owned by the repo team.

## Design Principles

### 1. Stage First, Promote Later

Every orchestration run should use isolated staging locations, for example:

- toolchain staging root: `build/runs/<run-id>/toolchain`
- package staging root: `build/runs/<run-id>/libs`
- logs/state root: `build/runs/<run-id>/state`

The orchestrator should validate everything against these staging roots first. Promotion to a persistent target is a separate step.

### 2. Explicit Environment, No Ambient Resolution

The orchestrator should explicitly set the environment for each repo step:

- `DRIFTC`
- package root env used by Drift/package tooling
- any repo-specific env needed for tests or deploy

No step should rely on `PATH` accidentally containing a previously deployed compiler.

### 3. Fresh Checkout Reproducibility

Certification runs should evaluate exact committed candidate states from fresh checkouts.

That means:

- all repos under evaluation are sourced from exact git commit SHAs
- the orchestrator should materialize those commits into a clean run-local workspace
- certification should not use ambient sibling worktrees
- the run should be reproducible on any box with the same commit set

### 4. Dependency-Driven Execution

The orchestrator should compute the affected repo set from a declared graph, then execute work in topological order.

Examples:

- change in `drift-lang` -> stage `drift-lang`, then validate `drift-mariadb-client`, `drift-net-tls`, `drift-web`
- change in `drift-net-tls` -> rebuild/retest `drift-net-tls`, `drift-web`
- change in `drift-web` -> rebuild/retest `drift-web` only
- change in `drift-mariadb-client` -> rebuild/retest `drift-mariadb-client` only

Important rule:

- `drift-lang` is never a validation target in the orchestrator
- `drift-lang` is an input provider whose staged toolchain is the basis for downstream validation

Practical workflow note:

- a repo can be a changed trigger for a run without being a validation target
- this is especially true for `drift-lang`
- downstream failures are still actionable feedback to the team that submitted the triggering candidate

### 5. Keep Validation and Promotion Decisions Separate

There are two distinct workflows:

- validation cascade: prove the affected graph is green using staged artifacts
- production promotion: performed later by the owning team after the orchestrator reports a verdict

The orchestrator is responsible only for validation and certification, not production promotion.

### 6. Reuse Existing Repo Commands Narrowly

The orchestrator should use:

- `just test` for repo-owned test logic
- staged `drift` / `driftc` directly for build and deploy logic

Repo-local test behavior stays owned by the repo. Cross-repo ordering, staged environment wiring, and candidate certification are owned by the orchestrator.

## Proposed Architecture

## A. Workspace Model

The orchestrator repo should define the workspace explicitly in config.

Suggested concepts:

- workspace root: parent directory containing the sibling repos
- candidate commit inputs
- repo definitions
- repo dependencies
- repo lifecycle commands
- environment propagation rules
- staged output locations

Example conceptual config:

```yaml
workspace_root: ..

repos:
  drift-lang:
    path: ../drift-lang
    kind: toolchain
    affects:
      - drift-mariadb-client
      - drift-net-tls
      - drift-web
    commands:
      stage_toolchain: drift deploy --dest {toolchain_root}

  drift-mariadb-client:
    path: ../drift-mariadb-client
    kind: package_repo
    depends_on:
      - drift-lang
    commands:
      test: just test
      deploy: drift deploy --dest {libs_root}

  drift-net-tls:
    path: ../drift-net-tls
    kind: package_repo
    depends_on:
      - drift-lang
    commands:
      test: just test
      deploy: drift deploy --dest {libs_root}

  drift-web:
    path: ../drift-web
    kind: package_repo
    depends_on:
      - drift-lang
      - drift-net-tls
    commands:
      test: just test
      deploy: drift deploy --dest {libs_root}
```

This is only a shape, not final syntax.

## B. Execution Model

Each orchestrator run should:

1. determine selected candidate commit SHAs
2. materialize fresh checkouts for the run
3. determine changed repos
4. compute the transitive affected set
5. create a new run directory
6. stage the compiler/toolchain if `drift-lang` is in scope
7. execute repo lifecycle steps in dependency order
8. stop on first failure unless explicitly configured otherwise
9. leave behind logs, staged artifacts, and a certification record for inspection

### Example: `drift-lang` Changed

1. materialize the selected `drift-lang` candidate commit in a fresh checkout
2. stage a fresh toolchain deployment using the candidate's own staged Drift tooling
3. export `DRIFTC` from the staged toolchain
4. run downstream repos against that staged toolchain
5. publish downstream packages into the staged libs root as needed
6. run downstream consumer repos against the staged libs root
7. emit a certification verdict

### Example: `drift-net-tls` Changed

1. materialize the selected `drift-net-tls` candidate commit in a fresh checkout
2. build/deploy staged `net-tls` package into the run-local libs root
3. run `drift-web` test against that staged package root
4. emit a certification verdict

## C. Staged Environment Contract

The orchestrator should construct a per-run environment contract that every repo step receives.

Proposed variables:

- `DRIFTC=<run-toolchain>/bin/driftc`
- `DRIFT_PACKAGE_ROOT=<run-libs-root>`
- `DRIFT_PKG_ROOT=<run-libs-root>` where expected by existing tooling
- `PATH=<run-toolchain>/bin:...` only if needed, but explicit vars remain preferred

Important rule:

- `PATH` may assist execution, but correctness must not depend on it.
- commands should still receive explicit paths or env vars for the staged compiler/package roots.

## D. Affected-Repo Calculation

Affected scope should be graph-based, not command-line folklore.

Inputs:

- explicit repo names, or
- detected file changes mapped to repos

Outputs:

- ordered execution plan
- reason for inclusion of each repo

Recommended plan categories:

- `changed_repos`: repos whose candidate commits differ from the current certified workspace snapshot
- `involved_repos`: repos materialized or used during the run
- `validated_repos`: repos whose deploy/test validation is actually executed

Example plan output:

```text
Changed:
  - drift-net-tls

Involved:
  1. drift-lang      (toolchain input from certified workspace or selected candidate)
  2. drift-net-tls   (directly changed)
  3. drift-web       (depends on drift-net-tls)

Validated:
  1. drift-net-tls   (directly changed)
  2. drift-web       (depends on drift-net-tls)
```

This should be inspectable before the orchestrator executes anything.

## E. Validation Flow and Certification Output

### Validation Flow

Purpose:

- verify the workspace is consistent using staged outputs only

Characteristics:

- no persistent publish destination
- no required permanent version bumps
- safe to run repeatedly during development

Possible command shape:

```text
orch validate --changed drift-lang
orch validate --changed drift-net-tls
orch validate --from-git
```

### Promotion Flow

Purpose:

- produce a durable answer about whether a submitted candidate is downstream-compatible

Characteristics:

- no manifest edits
- no lockfile edits
- no `prepare`
- no production publish
- emit machine-readable and human-readable run summaries

Suggested verdict states:

- `certified`: all affected downstream validations passed
- `rejected`: one or more affected validations failed
- `blocked`: candidate could not be evaluated cleanly, for example because staging deploy failed or committed dependency state was not usable

Suggested output files:

- `build/runs/<run-id>/summary.json`
- `build/runs/<run-id>/summary.md`
- `state/workspace-lock.json`

Suggested summary fields:

- candidate repo
- candidate version
- candidate commit SHA
- changed repos
- involved repos
- validated repos
- staged toolchain path
- staged package root
- per-repo status
- overall verdict
- log locations

Reporting rule:

- certification reports should attribute downstream failures back to the candidate repos that triggered the run
- this attribution is part of the team decision process, not the execution model

Pinned decision:

- use a workspace-snapshot certification model, not per-repo certification
- a successful run certifies the exact repo/version/commit combination used across the workspace
- the orchestrator updates `state/workspace-lock.json` only on successful certification runs
- failed or blocked runs produce reports but do not modify the current certified workspace snapshot
- certification runs use exact commit SHAs and fresh checkouts for every repo in the evaluated workspace

## F. Versioning Strategy

Current versioning model:

- each repo has a single version shared by all artifacts in that repo

Current manifests use exact versions for inter-package dependencies, for example:

- `web-client` pins `net-tls` to `0.3.9`
- `mariadb-rpc` pins `mariadb-wire-proto` to `0.1.2`
- `web-rest` pins `web-jwt` to `0.2.5`

Implication for the orchestrator:

- the orchestrator treats the repo as the candidate versioned unit
- it does not edit downstream manifests or lockfiles
- it validates whatever committed dependency metadata the owning team supplied with the candidate
- certification is tied to exact commits first and versions second

Pinned decision:

- repo `commit` is the true identity of a candidate during certification
- repo `version` is recorded alongside the commit for reporting and promotion readiness
- certification updates the current workspace snapshot only when the evaluated repos come from exact committed fresh checkouts
- local dirty-worktree evaluation is out of scope for certification

## G. Toolchain-Specific Handling

`drift-lang` is not just another package repo. It is the source of the compiler and deploy tooling used by downstream repos.

The orchestrator should treat `drift-lang` as a special node:

- its staged output is a runnable toolchain
- downstream repos consume that toolchain through explicit env wiring
- a `drift-lang` change invalidates all downstream validation results
- the orchestrator stages the toolchain but does not run the compiler team's internal tests
- `drift-lang` is never a validation target

This is the central reason the staged-toolchain model is required.

## H. Repo Interface Expectations

Before the orchestrator is feature-complete, some repo behaviors may need clarification.

Current expected contract:

- each repo provides `just test`
- build/deploy can be performed with direct staged `drift` commands
- committed lockfile state is already correct when a team submits a candidate for validation

Open normalization question:

- does every package repo build and deploy correctly when driven only through the staged `drift` toolchain and explicit staging roots?

`drift-net-tls` is the repo most likely to reveal gaps here, but the right time to identify them is during implementation of staged build/test/deploy flows.

## Proposed Implementation Phases

## Phase 1: Plan and Dry Run

Deliverables:

- orchestrator config for repo graph and commands
- command to compute affected repos
- command to print execution plan without running steps

Success criteria:

- given a changed repo set, produce the correct topological affected plan

## Phase 2: Validation Cascade

Deliverables:

- create run-local staging roots
- materialize fresh candidate checkouts
- stage `drift-lang` toolchain when needed
- execute `test` and related validation commands in dependency order
- execute staged `drift` / `driftc` build and deploy commands directly where needed
- pass staged `DRIFTC` and package-root env explicitly
- capture logs and run metadata

Success criteria:

- `drift-lang` change validates all downstream repos against the newly staged toolchain
- `drift-net-tls` change validates `drift-net-tls` and `drift-web`

## Phase 3: Staged Package Publish

Deliverables:

- package repos can deploy to run-local staged libs root
- downstream repos resolve against staged package outputs
- no lockfile regeneration is required during validation

Success criteria:

- downstream package consumers use freshly staged upstream package artifacts, not globally installed ones

## Phase 4: Certification Records and Signaling

Deliverables:

- durable per-run summaries
- machine-readable verdict record
- human-readable certification report
- stable verdict states: `certified`, `rejected`, `blocked`
- current certified workspace snapshot in `state/workspace-lock.json`

Success criteria:

- an upstream team can hand over a candidate commit/version and receive a clear downstream compatibility verdict with evidence
- the orchestrator can update the certified workspace snapshot only when the entire evaluated workspace passes

## Implementation Approach

The orchestrator should be implemented in Python.

Reasoning:

- the core tasks are graph traversal, process execution, environment construction, structured JSON I/O, and report generation
- Python is a better fit than bash for dependency planning, state management, error handling, and readable implementation
- bash can still be used only where repo-owned test commands or tools already require it

Pinned decision:

- use Python as the orchestrator driver
- do not build the control plane in bash or `just`
- `just` remains a downstream test entrypoint only where the sibling repo already defines it

## Phase 5: Change Detection Integration

Deliverables:

- derive changed repos from git status/diff
- optionally support commit-range based planning
- emit machine-readable run summaries

Success criteria:

- minimal manual input required for common workflows

## Failure and Safety Policy

- fail fast by default
- never publish to persistent destinations during validation mode
- never rely on ambient `PATH` state for compiler selection
- never rely on ambient sibling repo worktrees for certification identity
- preserve per-run logs and staged outputs for diagnosis
- make the execution plan visible before running
- never run `prepare` or any other command that mutates sibling repo dependency metadata

## Open Questions

1. How should candidate submission be expressed: explicit repo + commit arguments, a manifest file, or a run config checked into `build-orchestrator`?
2. What exact command contract should the orchestrator use for direct staged `drift deploy` in each package repo?
3. Should the orchestrator support partial downstream validation targets for faster local iteration?
4. What is the long-term signaling mechanism beyond local summary files: commit in `build-orchestrator`, PR comment, webhook, or chat notification?

## Recommended First Cut

Start with the smallest useful slice:

- static config for the four repos
- affected-repo computation
- staged run directory creation
- fresh checkout materialization
- `drift-lang` staged toolchain support
- validation mode only
- no sibling repo edits
- no `prepare`
- certification summary output

That is enough to eliminate the highest-friction manual workflow:

- build the new toolchain once
- point downstream repos at it
- run the affected cascade automatically
- emit a clear pass/fail/blocked candidate verdict

## Summary

The orchestrator should be a workspace-level control plane, not a replacement for repo-local build logic.

Its primary job is to:

- understand the dependency graph
- create isolated staged environments
- wire downstream repos to the staged compiler and staged package roots
- execute affected validation flows in the correct order
- emit a candidate certification verdict with supporting evidence

The staged toolchain requirement is the anchor design constraint. Once that is enforced, the rest of the orchestration model becomes much simpler and more reliable.
