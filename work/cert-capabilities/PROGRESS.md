# PROGRESS — External Capability Provisioning

Tracks where we stand against `PLAN.md`. Update statuses as work lands.
Status legend: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

_Last updated: 2026-06-23 — single-document contract; decisions locked; onboarding-doc added; second review folded in (version modeling, credential_env wording, always-write empty doc). Awaiting approval to implement._

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

- **Phase:** planning; implementation NOT started.
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
  - [ ] `capabilities.json` is a **versioned public API**: keep `schema_version`, document the schema
        normatively, additive-only changes without a bump.
  - [ ] **Rollout ordering is the real cost:** orchestrator lands first (backward-compatible), but the
        red workflows gate stays red until drift-workflows ships a reader. Required coordinated ask to
        the workflows team — not optional.
  - [ ] `allocation`/`lock_key` are v1 **descriptive metadata** (repo self-serializes); orchestrator
        does not enforce the lock yet. Don't over-promise.
  - [ ] Confirm the secret named by `credential_env` is inherited by the gate process and not scrubbed.
  - [ ] (Optional, future) toolchain-side accessor (`drift cert cap get …`) so repos don't each
        hand-roll JSON parsing. Not required for v1.

## Steps

- [~] **Step 0 — scaffolding.**
  - [x] `work/cert-capabilities/PLAN.md` (single-document contract)
  - [x] `work/cert-capabilities/PROGRESS.md`
  - [ ] `.gitignore`: add `cert-env.json`, `state/cert-env.json`
- [ ] **Step 1 — config model + load validation.** `Capability` (kind + policy fields);
      `capabilities` parse; `RepoConfig.requires`; `_validate_capabilities`.
- [ ] **Step 2 — host-local cert-env model.** `CertEnv` + `load()`; resolution order
      `--cert-env` → `DRIFT_CERT_ENV` → `./cert-env.json` → None.
- [ ] **Step 3 — resolve + write per-run document.** `build_capabilities_document()` →
      `<run-root>/capabilities.json` (`schema_version`, `run_id`, resolved required capabilities).
- [ ] **Step 4 — inject single env var.** `build_step_env` sets `DRIFT_CERT_CAPABILITIES` for gate
      steps only (no per-repo threading, no per-tool exports).
- [ ] **Step 5 — preflight before staging.** `run_external_deps_preflight()` between lines 1818 and
      1827; validate each required capability (tool exec, service tcp + `credential_env` present);
      block-and-return; on pass write the document.
- [ ] **Step 6 — CLI.** Global `--cert-env`; `certify` loads `CertEnv` → `execute_run`.
- [ ] **Step 7 — config + docs.** `orchestration.json` `capabilities` (behavior only) + drift-workflows
      `requires`; `cert-env.example.json`; document the `capabilities.json` schema in
      `docs/orchestrator-schema.md`.
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
