# External Capability Provisioning for Certification Gates

## Context

`drift-workflows` joined the certification pool. Its first real cert run
(`20260623-134311-drift-workflows-63ac864`) staged cleanly through every package but **failed
inside the `test` gate**:

```
error: mariachi venv missing — run 'cd ../../mariachi && just setup'
```

The gate needs external resources the orchestrator does not provision: the **Mariachi**
schema-migration tool (resolved by the recipe via a fragile relative `../../mariachi`, absent in a
hermetic checkout) and a reachable **MariaDB** test instance. These are *external cert
tools/services* — not package artifacts, not part of any repo checkout. Today they only work via
ad-hoc operator shell state, so runs are non-reproducible and failures surface deep inside
`just test` instead of up front. Mariachi is the first case; the pattern (Redis, a browser, a GPU,
…) will recur. We need a first-class, declarative platform contract for it.

### Why not inject per-tool env vars (the rejected wrapper approach)

An earlier draft had the orchestrator inject a per-tool env contract (`MARIACHI_BIN`, `DB_HOST`,
`DB_PORT`, …). Review killed it, and verification showed why: the workflows gates only honor
`MARIACHI_BIN` and `MDB_ROOT_PWD`; `DB_HOST`/`DB_PORT`/`DB_LOCK` are **hardcoded justfile literals**
and the Python harnesses read **`MDB_HOST`/`MDB_PORT`** (different names). Injecting those would be
silently ignored or mis-named, and the orchestrator's contract would just be a brittle wrapper
around whatever env vars each repo happens to use today. That is not a platform API.

## The contract: one document, one env var

The orchestrator resolves all capabilities a run needs, writes **one canonical JSON document for the
run**, and injects **exactly one env var** pointing at it:

```
DRIFT_CERT_CAPABILITIES=<run-root>/capabilities.json
```

Repos read that document and adapt it internally, in whatever language/tooling they use. The
platform API is `DRIFT_CERT_CAPABILITIES` + the document schema — **nothing else**. No `env.sh`, no
generated legacy env vars, no `MARIACHI_BIN` injection, no per-tool env contract. A repo may still
set `MARIACHI_BIN` *internally* from the document if its recipes want it, but that is the repo's
private detail, not the cert contract.

This makes capabilities a real contract instead of a wrapper, and it eliminates the entire
env-name-mismatch class the review found: the orchestrator names no tool env vars at all.

### Document schema (the platform API — versioned)

`<run-root>/capabilities.json`, written once per run, containing the resolved union of capabilities
required by the run's validated repos:

```jsonc
{
  "schema_version": 1,
  "run_id": "20260623-134311-drift-workflows-63ac864",
  "capabilities": {
    "tool:mariachi": {
      "kind": "tool",
      "bin": "/host/path/to/mariachi/.venv/bin/mariachi"
    },
    "service:mariadb": {
      "kind": "service",
      "allocation": "shared-exclusive",      // from committed policy
      "host": "127.0.0.1",                   // host-local facts below
      "port": 34114,
      "credential_env": "MDB_ROOT_PWD",      // NAME of the env holding the secret — not the secret
      "instance": "mdb114-a",
      "lock_key": "mariadb-mdb114-a"
    }
  }
}
```

`credential_env` names the env var the repo should read for the password; the secret **value** stays
in the process env / CI vault and never lands in the file. The repo reads `credential_env` from the
doc, then reads that env var. The orchestrator neither writes nor echoes the secret — it must already
be present in the run's environment (inherited by name; preflight checks presence), and
`build_step_env` already starts from `dict(os.environ)` so it flows through. The orchestrator's only
*added* env var is `DRIFT_CERT_CAPABILITIES`.

**Fields emitted, by kind (precise — repos code against this):**
- `tool:*` → `kind`, `bin`.
- `service:*` → `kind`, `allocation`, `host`, `port`, `credential_env`, `instance`, `lock_key`.

Validation policy (`min_version`, `version_argv`) is **preflight-only and NOT emitted** into the run
document — the document is the resolved *consumption* view, not the validation config. (If a consumer
ever needs the version, we'd emit a resolved actual `version`, not the policy; not in scope now.)

## Where each field comes from (three owners, unchanged split)

| Layer | File | Committed? | Owns |
|---|---|---|---|
| **Repo author** | `orchestration.json` repo `requires` | yes | *what my gates need*, by capability id |
| **Platform policy** | `orchestration.json` `capabilities` | yes (portable) | which capabilities exist; `kind`; required *behavior* (`allocation` = sharing/access, service class, schema mode) — **no host facts** |
| **Host resolution** | `cert-env.json` (host-local) | **no** (gitignored / CI profile) | machine *facts*: tool `bin`, service `host`/`port`, `credential_env`, `instance`/pool id, `lock_key` |
| **Per-run document** | `<run-root>/capabilities.json` | n/a (generated) | the resolved merge the repo reads at gate time |

```jsonc
// orchestration.json — committed, portable (BEHAVIOR only; no host facts, no env names)
"capabilities": {
  "tool:mariachi":   { "kind": "tool", "min_version": "1.0.0",
                       "version_argv": ["{bin}", "--version"] },   // optional; omit ⇒ presence-only check
  "service:mariadb": { "kind": "service", "allocation": "shared-exclusive" }   // behavior class only
},
"repos": {
  "drift-workflows": {
    "depends_on": ["drift-lang", "drift-mariadb-client", "drift-web"],
    "requires":   ["tool:mariachi", "service:mariadb"]
  }
}

// cert-env.json — host-local, UNCOMMITTED (machine FACTS only)
{
  "tool:mariachi":   { "bin": "/this/host/.venv/bin/mariachi" },
  "service:mariadb": { "host": "127.0.0.1", "port": 34114, "credential_env": "MDB_ROOT_PWD",
                       "instance": "mdb114-a", "lock_key": "mariadb-mdb114-a" }
}
```

Split rule: committed policy declares required **behavior** (`allocation`: shared vs isolated,
exclusive vs concurrent; service class; schema mode). Host-local declares machine **facts** (`bin`,
`host`, `port`, `credential_env`, `instance`/pool id, and `lock_key`). `lock_key` is host-local on
purpose — `mariadb-mdb114-a` names a *specific instance*, so it is not a committed constant; if a
lock name were ever a genuinely pool-wide contract it would move to policy. The resolved per-run
document carries both, merged.

Tool version is committed policy: a tool may declare `min_version` (a pool-wide minimum, e.g.
Mariachi `>= 1.0.0` as workflows called out) plus `version_argv` (how to query it, with `{bin}`
resolved host-locally). Both optional — omitting them makes the preflight a presence-only check.

## Rollout consequence (read this)

This is **stricter** than the wrapper approach and has a real ordering implication:

- The orchestrator change is **backward-compatible** and lands independently — it only writes a file
  and sets one new env var; repos that ignore it are unaffected.
- But because the orchestrator **no longer injects `MARIACHI_BIN`**, the failing drift-workflows gate
  is **not fixed by the orchestrator alone**. drift-workflows must adopt a small reader: parse
  `DRIFT_CERT_CAPABILITIES`, set its internal `MARIACHI_BIN`/DB coords from the document. The cert
  goes green only when **both** land. This is a required, coordinated workflows-side change (message
  to the team), not an optional follow-up.

## Implementation (all in `orchestrate.py` unless noted)

Reuse existing patterns. Note this model is *simpler* than per-tool injection — there is no per-repo
env threading; injection is one fixed env var.

**Step 0 — scaffolding.** PLAN.md + PROGRESS.md (done). Add `cert-env.json` and `state/cert-env.json`
to `.gitignore` (currently ignores `build/` only; `state/` is committed).

**Step 1 — config model + load validation** (`OrchestrationConfig.load` 107–148; mirror
`cert_suite_policy` at 135 and `_validate_repo_recipes` / `_validate_dependency_graph`).
- `Capability` dataclass (`id`, `kind`, committed *behavior* policy: services e.g. `allocation`;
  tools optional `min_version` + `version_argv`). Host facts like `lock_key`/`bin` come from
  `CertEnv`, not here.
- `capabilities: dict[str, Capability]` on `OrchestrationConfig`; parse `raw.get("capabilities", {})`.
- `requires: list[str]` on `RepoConfig` (53–59; construct 120–126 with `r.get("requires", [])`).
- `_validate_capabilities(config)` after `_validate_dependency_graph`: structural (kind ∈
  {tool,service}) + referential (every repo `requires` id names a declared capability), collected
  clear `ValueError` (unknown-`depends_on` style).

**Step 2 — host-local cert-env model.**
- `CertEnv` dataclass + `load(path) -> Optional[CertEnv]`: parses `cert-env.json`, keyed by capability
  id → resolution dict (`bin` for tools; `host`/`port`/`credential_env` for services). Validate shape
  per kind.
- Resolution order in `main()`: `--cert-env` → `DRIFT_CERT_ENV` → `./cert-env.json` if present → `None`.

**Step 3 — resolve + write the per-run document.**
- `build_capabilities_document(config, plan, cert_env) -> dict`: for each capability id required by
  `plan.validated_repos`, merge committed policy + host resolution into the resolved entry; wrap with
  `schema_version` + `run_id`. (Reuse `write_*`-style helpers near `write_empty_run_snapshot`.)
- **Always write** `ctx.run_root / "capabilities.json"`, once per run — even when the required set is
  empty (`{schema_version, run_id, "capabilities": {}}`). Consistent contract: the document and the
  env var always exist, so a consuming repo never has to special-case "no file."

**Step 4 — inject the single env var** (`build_step_env`, 1514–1521; subs at 1553–1561; call site 1894).
- For **gate** steps, always set `env["DRIFT_CERT_CAPABILITIES"] =
  str((ctx.run_root/"capabilities.json").resolve())` (the document always exists, possibly empty).
  No repo-identity threading, no per-tool exports. (Path is derivable from `ctx`; no signature change
  needed beyond what's already passed.) The orchestrator adds *only* this var — secrets named by
  `credential_env` are inherited from `os.environ`, never added or renamed here.

**Step 5 — preflight, blocking before staging** (model: `run_author_claim_preflight` 1855–1868 /
`_verify_dual_runtime_support` 1878–1892).
- `run_external_deps_preflight(config, plan, cert_env) -> Optional[str]`. Required ids = union of
  `requires` over `plan.validated_repos`. Per id: host resolution present? tool `bin` exists &
  `os.access(X_OK)` and — if the tool declares `min_version` — run `version_argv` (with `{bin}`
  resolved) and version-check (defined below); service `credential_env` set & non-empty + TCP connect
  `host:port` (stdlib `socket`, short timeout). Stream `... ok` to stdout (≤60s cadence). Return block
  reason or `None`.
- **Version parsing (pinned, no ad-hoc behavior):** run `version_argv`, capture **stdout+stderr**,
  extract the **first semver-like token** matching `\d+(?:\.\d+)*`, split into integers, and compare
  **numerically** (tuple compare, shorter side zero-padded) against `min_version` parsed the same way.
  Block if below. If no token is found or the command errors, block with a clear "could not determine
  `<id>` version" reason (don't silently pass).
- Insert in `execute_run` **after the checkout loop (1818), before `write_empty_run_snapshot` (1827)**:
  load `cert_env`, run preflight; on block → `_build_summary(..., "blocked", block_reason=...)` +
  `_write_run_outputs` + `return`. On pass → write the document (Step 3), then continue.

**Step 6 — CLI** (`main()` 2531–2537): global `--cert-env` before `add_subparsers()`; `certify` loads
`CertEnv` → `execute_run`. `plan` stays host-independent (could print required capability ids).

**Step 7 — config + docs.**
- `orchestration.json`: add `capabilities` (policy only) + `requires` on `drift-workflows`.
- Committed `cert-env.example.json` documenting host-local shape.
- `docs/orchestrator-schema.md`: document `DRIFT_CERT_CAPABILITIES`, the **`capabilities.json` schema
  as the platform contract** (this is the public API repos code against), `requires`, the three
  config layers, and the rollout note.

**Step 8 — certification onboarding manual (`docs/certification-onboarding.md`).** No such manual
exists today (docs/ has only `orchestrator-plan.md` and `orchestrator-schema.md`; the join-the-pool
contract lives in scattered work docs). Now that joining means declaring `depends_on`, `requires`,
command contracts, package manifests, author claims, and consuming `DRIFT_CERT_CAPABILITIES`, that is
a formal certification contract — it needs one authoritative "how to become certifiable" doc, not
scattered notes. Cover:
- **Required repo commands** — `test`, `stress`, `perf`, `stage_packages`; plus the stage/certify env
  split (`DRIFT_CERT_MODE` / `DRIFT_RUN_SNAPSHOT`) and the ≤60s stdout-cadence watchdog.
- **`depends_on`** — declare **direct providers only**; downstream invalidation is derived (no
  `affects`, no consumer tracking).
- **Package staging** — from **committed manifest/source/claims only**, never `build/dist`; Lock v2
  required.
- **Author-claim / trust** — trust-v1 cert-suite expectations; cert-suite policy is orchestrator-owned
  (recipes must not embed `--cert-suite-*`).
- **Capability declaration** — `requires` with concrete `tool:`/`service:` ids.
- **`DRIFT_CERT_CAPABILITIES` JSON contract** — the schema + worked **bash and Python** consumption
  examples (read the file, pull `tool:mariachi.bin`, read the `credential_env`-named secret).
- **External service/tool requirements + blocked-run behavior** — validated before staging; a missing
  capability blocks with a clear reason, no gate runs.
- **Evidence + verdicts** — what the orchestrator records per run, and the precise meaning of
  **certified / blocked / rejected**.
Pull authoritative details from the existing contracts rather than re-deriving them (toolchain root,
two-lane model, cert-mode env split, gate stdout contract, Lock v2, trust-v1, fasttrack).

## Confirmed code anchors (orchestrate.py)

- `OrchestrationConfig` + `load()`: 96–148; optional-section parse at 135; validators 159–178, 181–206.
- `RepoConfig`: 53–59 (name, path, kind, depends_on, commands); constructed 120–126.
- `build_step_env`: 1514–1521; `environment.vars`/subs 1553–1561; sole call site 1894.
- `execute_run`: checkout loop ends 1818; `write_empty_run_snapshot` 1827; block-and-return 1855–1868,
  1878–1892. Insert preflight + document write **between 1818 and 1827**.
- `RunContext`: 1351–1358 (run_id, run_root, checkouts_root, toolchain_root, libs_root, logs_root).
- argparse: `--config` 2531–2535; subparsers 2539/2555/2566.
- `.gitignore`: ignores `.claude __pycache__ build .codex* .claude-session`; **`state/` NOT ignored.**
- No existing host-local config mechanism — blank slate.

## Verification

- **Load validation:** `requires` naming an undeclared capability → clear load-time error; bad `kind`
  → error. `./orchestrate.py plan run-all-latest.json` still works (host-independent).
- **Preflight blocks early:** no `cert-env.json` → `certify` → `blocked: capability tool:mariachi not
  available on this host …` **before** staging. With a `min_version` declared and an older stub
  Mariachi, the version check blocks too.
- **Document + single-var injection:** with a valid `cert-env.json`, confirm
  `<run-root>/capabilities.json` is written with the resolved `tool:mariachi`/`service:mariadb`
  entries matching the schema. The orchestrator adds **only** `DRIFT_CERT_CAPABILITIES` (it injects no
  `MARIACHI_BIN`/`DB_*`); the secret named by `credential_env` (e.g. `MDB_ROOT_PWD`) **may** be
  inherited by name from the process env, but its **value** appears nowhere in `capabilities.json`,
  logs, or the run summary.
- **Empty-document consistency:** a run whose validated repos declare no `requires` still writes a
  versioned `{ "capabilities": {} }` and still injects `DRIFT_CERT_CAPABILITIES`.
- **Round-trip:** a throwaway gate command that reads `DRIFT_CERT_CAPABILITIES`, loads the JSON, and
  echoes `tool:mariachi.bin` proves the contract end-to-end without needing real Mariachi.
- **End-to-end (needs workflows adoption):** once drift-workflows reads `DRIFT_CERT_CAPABILITIES` and
  sets its internal Mariachi/DB coords, re-run the cert; `test-singular` proceeds past schema load.
  (Perf gate then needs a committed baseline for the cert host's machine-id — known next blocker.)

## Decisions (locked)

1. **Capability naming — concrete.** `tool:mariachi` / `service:mariadb`. Abstraction (capability →
   provider indirection) deferred until real provider substitution is actually needed; concrete names
   are easier for repo teams to understand and implement.
2. **Policy vs host split.** Committed `capabilities` = required **behavior only** (`allocation`,
   service class, schema mode). Host-local `cert-env.json` = machine **facts** (`bin`/`host`/`port`/
   `credential_env`/`instance`/`lock_key`). `lock_key` is host-local — it names a specific instance,
   not a committed constant.
3. **Capabilities API — single resolved JSON.** `DRIFT_CERT_CAPABILITIES` → one run-local
   `capabilities.json`. No orchestrator `env.sh`, no per-tool env-var injection. Repos read the JSON
   and adapt internally.
