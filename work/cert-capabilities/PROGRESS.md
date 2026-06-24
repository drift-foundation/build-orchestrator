# PROGRESS — External Capability Provisioning

Tracks where we stand against `PLAN.md`. Update statuses as work lands.
Status legend: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

_Last updated: 2026-06-23 — IMPLEMENTED + committed (b7ef786). Post-commit revision: `service` simplified to connection-facts-only (removed `allocation`/`concurrency`/`lock_key`). Pending: real-host end-to-end + workflows reader adoption._

## Service-contract revision (post-review)

`service:*` carries **connection facts only** — `host`/`port`/`credential_env`/
`instance`. Removed `allocation`/`concurrency`/`lock_key` from the model, config,
docs, and the run document. Rationale: each project owns its own sandbox
schema(s) on the shared instance (via Mariachi), unlimited count; projects are
isolated by schema, so the orchestrator does not serialize the instance or model
locks/concurrency. DB locking is the project's own gate concern, schema-scoped.
Touched: `orchestrate.py` (`Capability`, `_parse_capabilities`,
`build_capabilities_document`), `orchestration.json`, `cert-env.example.json`,
both docs, and `WORKFLOWS_ADOPTION.md`. Verified offline. **Uncommitted** (this
revision is on top of the committed feature).

## Second-review resolutions (folded into PLAN)

- [x] **Tool version modeling:** optional committed `min_version` + `version_argv` per tool; preflight
      runs the version check when declared, else presence-only. (Mariachi `>= 1.0.0`.) Parsing pinned:
      first semver-like token (`\d+(?:\.\d+)*`) from stdout+stderr, compared numerically; no token/err → block.
- [x] **Document schema precision:** `tool:*` emits `kind`+`bin`; `service:*` emits
      `kind`/`allocation`/`host`/`port`/`credential_env`/`instance`/`lock_key`. Validation policy
      (`min_version`/`version_argv`) is preflight-only, NOT emitted into the run document.
- [x] **credential_env wording:** orchestrator adds **only** `DRIFT_CERT_CAPABILITIES`; the secret
      named by `credential_env` is inherited by name from `os.environ` (never added/renamed); secret
      **values** never written to `capabilities.json`, logs, or summary.
- [x] **No-required-capabilities:** always write the versioned empty document
      (`"capabilities": {}`) and always inject the env var, for a special-case-free consumer contract.

## Decisions (locked)

1. **Capability naming:** concrete `tool:mariachi` / `service:mariadb`. Abstraction deferred.
2. **Policy vs host split:** committed `capabilities` = behavior only (`allocation`, class, schema
   mode); host-local `cert-env.json` = facts (`bin`/`host`/`port`/`credential_env`/`instance`/
   `lock_key`). `lock_key` is host-local (names a specific instance), not a committed constant.
3. **API:** `DRIFT_CERT_CAPABILITIES` → one run-local resolved JSON; no `env.sh`, no per-tool env
   injection; repos read + adapt internally.

## Current state

- **Phase:** orchestrator implementation COMPLETE + verified offline; not committed.
- **Motivating failure:** run `20260623-134311-drift-workflows-63ac864`, `test` gate
  (`test-singular` → `_db-load-schema`): `error: mariachi venv missing`.
- **Design (current):** `DRIFT_CERT_CAPABILITIES` → one resolved `capabilities.json` per run. The
  orchestrator injects **only** that single env var; repos read the JSON and adapt internally. No
  `env.sh`, no per-tool env injection, no `MARIACHI_BIN` from the orchestrator. Secrets stay as
  `credential_env` references. Host paths/allocations live in host-local `cert-env.json`, surfaced in
  the generated run-local JSON.
- **Design history:** (1) per-tool env injection contract — rejected by review (DB env-name
  mismatch: gates hardcode `DB_HOST`/`DB_PORT`, Python uses `MDB_HOST`/`MDB_PORT`, only `MARIACHI_BIN`
  + `MDB_ROOT_PWD` are env-driven). (2) Pivot to single-document contract — current.

## K's review verdict (approved, with conditions)

- [x] **Approve** the `DRIFT_CERT_CAPABILITIES` + resolved-JSON standard. Names zero tool env vars →
      eliminates the mismatch class entirely.
- Conditions to honor during implementation:
  - [x] `capabilities.json` is a **versioned public API**: `schema_version` emitted; schema documented
        normatively in `orchestrator-schema.md` + onboarding doc (additive-only without a bump).
  - [ ] **Rollout ordering is the real cost:** orchestrator lands first (backward-compatible), but the
        red workflows gate stays red until drift-workflows ships a reader. Required coordinated ask to
        the workflows team — not optional. *(message still to send)*
  - [x] `allocation`/`lock_key` are v1 **descriptive metadata** (repo self-serializes); orchestrator
        does not enforce the lock. Documented as such.
  - [x] Secret named by `credential_env` is inherited by the gate process (`build_step_env` starts from
        `dict(os.environ)`); orchestrator adds only `DRIFT_CERT_CAPABILITIES`. Preflight checks presence.
  - [ ] (Optional, future) toolchain-side accessor (`drift cert cap get …`). Not required for v1.

## Capability-surface decision: private DB → `tool:docker` (post-review)

drift-workflows moved MariaDB from a consumed `service:mariadb` to a repo-private
Docker container (it owns the lifecycle). Per the consume-vs-own rule, that means
declaring the actual external prerequisite — the container runtime — not hiding
it. Orch-side changes (this revision, uncommitted):
- `orchestration.json`: drift-workflows `requires` → `["tool:mariachi","tool:docker"]`;
  capabilities: **dropped `service:mariadb`**, **added `tool:docker`** (presence-only).
- `cert-env.example.json`: dropped the `service:mariadb` resolution, added
  `tool:docker` → `/usr/bin/docker`. (Cert host's real `cert-env.json` must provide it.)
- Docs: onboarding §5 now recommends the Docker-image pattern + declaring
  `tool:docker`; new **"Gates restore entry state"** rule (§1 + §5) — a gate that
  starts a container must tear it down (success or failure); leftover state is a defect.
Verified: preflight blocks early when `tool:docker` is unresolved; document emits
both tools; `service:mariadb` gone. Known limit: tool preflight checks the docker
*client* binary, not daemon liveness (provisioner covers the daemon).

Workflows side (all verified done): `tool:docker` declared; image digest-pinned
(`mariadb@sha256:…`); `db_instance.sh` uses `$DOCKER_BIN` + checks daemon
liveness; root `test`/`stress`/`perf` trap-`down` only if they `STARTED` the
container (sub-gates `up`-only, no trap → nesting-safe); `clean-data` rewired via
a new `db_instance.sh sql` (`docker exec`); lock key renamed off `mdb114-a`.

**Decision (accepted):** the cert host **may pull the pinned image digest at gate
time** — no image-presence preflight is required. Implication: gate-time registry
egress must be allowed on the cert host; first run (or post-GC) pays a one-time
pull. The `tool:docker` preflight covers the client only; daemon + image are the
gate's concern.

Remaining to a green cert (provisioning, not code): cert-host `cert-env.json`
provides `tool:mariachi.bin` + `tool:docker.bin`; the pinned image is reachable;
committed **perf baseline for the cert host's machine-id**.

## Doc clarification (post-review): consume vs. own a service

Added to both `orchestrator-schema.md` and `certification-onboarding.md`:
`service:*` means **the platform provisions an endpoint the repo consumes** —
declare it only for a platform-provided instance a gate connects to. A gate that
**owns** its DB lifecycle (private container/process) is a repo-private fixture,
not a service capability — declare nothing (e.g. `drift-mariadb-client` owns its
instance and does not advertise `service:mariadb`). A private fixture's own
external prerequisite is modeled separately (e.g. `tool:docker`), not
`service:mariadb`. Uncommitted, with the service-simplification revision.

## Steps

- [x] **Step 0 — scaffolding.** PLAN.md + PROGRESS.md; `.gitignore` adds `cert-env.json`, `state/cert-env.json`.
- [x] **Step 1 — config model + load validation.** `Capability`, `capabilities` field + `_parse_capabilities`,
      `RepoConfig.requires`, `_validate_capabilities` (structural + unknown-`requires` rejection).
- [x] **Step 2 — host-local cert-env model.** `CertEnv` + `load()`; `_resolve_cert_env` order
      `--cert-env` → `DRIFT_CERT_ENV` → `./cert-env.json` → None.
- [x] **Step 3 — resolve + write per-run document.** `build_capabilities_document()` +
      `write_capabilities_document()` → `<run-root>/capabilities.json` (always written, even empty).
- [x] **Step 4 — inject single env var.** `build_step_env` sets `DRIFT_CERT_CAPABILITIES` for gate
      steps only; no per-tool exports.
- [x] **Step 5 — preflight before staging.** `run_external_deps_preflight()` (+ `_preflight_tool`/
      `_preflight_service`, `_parse_semver`/`_version_at_least`) wired into `execute_run` after
      checkouts, before snapshot; block-and-return; on pass writes the document.
- [x] **Step 6 — CLI.** Global `--cert-env`; `certify` loads `CertEnv` → `execute_run`.
- [x] **Step 7 — config + docs.** `orchestration.json` `capabilities` + drift-workflows `requires`;
      `cert-env.example.json`; `capabilities.json` schema documented in `docs/orchestrator-schema.md`.
- [x] **Step 8 — onboarding manual.** `docs/certification-onboarding.md` written (commands, depends_on,
      committed staging + Lock v2, author-claim/trust-v1, `requires`, `DRIFT_CERT_CAPABILITIES` +
      bash/Python examples, blocked-run behavior, evidence + certified/blocked/rejected).
- [ ] **Step 8 — onboarding manual `docs/certification-onboarding.md`.** The formal "how to become
      certifiable" doc: required commands; direct-`depends_on`-only; committed-source staging + Lock v2;
      author-claim/trust-v1; `requires`; `DRIFT_CERT_CAPABILITIES` JSON contract + bash/Python examples;
      external-dep blocked-run behavior; evidence + certified/blocked/rejected meanings.

## Verification

- [ ] Unknown `requires` / bad `kind` → clear load-time error.
- [ ] `./orchestrate.py plan run-all-latest.json` still works (host-independent).
- [ ] No `cert-env.json` → `certify` blocks early with clear capability reason, before staging.
- [ ] Valid `cert-env.json` → `<run-root>/capabilities.json` written per schema; orchestrator **adds
      only** `DRIFT_CERT_CAPABILITIES` (no `MARIACHI_BIN`/`DB_*`). Inherited env (incl. `MDB_ROOT_PWD`)
      may still be present by name; secret **values** never in the doc/logs/summary.
- [ ] Round-trip: a throwaway gate command reads `DRIFT_CERT_CAPABILITIES`, loads JSON, echoes
      `tool:mariachi.bin`.
- [ ] End-to-end (needs workflows adoption): drift-workflows reads the doc → `test-singular` proceeds.

## Follow-ups / out of scope

- [ ] **Workflows adoption (required for green):** drift-workflows reader for `DRIFT_CERT_CAPABILITIES`
      that sets its internal Mariachi/DB coords. Message + coordinate.
- [ ] Perf-gate baseline for the cert host's machine-id (next blocker after `test`/`stress`).
- [ ] Optional: `drift-mariadb-client` declares `requires: ["service:mariadb"]`.
- [ ] Optional future: toolchain-side `capabilities.json` accessor; orchestrator-held lock; abstract
      capability names + provider indirection.
