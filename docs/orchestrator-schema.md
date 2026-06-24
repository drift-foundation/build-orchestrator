# Orchestrator Schema Draft

## Purpose

This document proposes the initial file formats for:

- static orchestrator configuration
- per-run certification reports
- the current certified workspace snapshot

These schemas are intentionally simple and JSON-oriented so they are easy to read and write from Python.

## Files

- `orchestration.json`
- `build/runs/<run-id>/summary.json`
- `state/workspace-lock.json`

## 1. `orchestration.json`

Purpose:

- static workspace description
- repo graph
- command contract
- environment contract
- candidate commit inputs

Suggested shape:

```json
{
  "schema_version": 1,
  "workspace": {
    "root": "..",
    "run_root": "build/runs",
    "state_root": "state"
  },
  "repos": {
    "drift-lang": {
      "path": "../drift-lang",
      "kind": "toolchain",
      "depends_on": [],
      "commands": {
        "stage_toolchain": ["drift", "deploy", "--dest", "{toolchain_root}"]
      }
    },
    "drift-mariadb-client": {
      "path": "../drift-mariadb-client",
      "kind": "package_repo",
      "depends_on": ["drift-lang"],
      "commands": {
        "test": ["just", "test"],
        "stage_packages": ["drift", "deploy", "--dest", "{libs_root}"]
      }
    },
    "drift-net-tls": {
      "path": "../drift-net-tls",
      "kind": "package_repo",
      "depends_on": ["drift-lang"],
      "commands": {
        "test": ["just", "test"],
        "stage_packages": ["drift", "deploy", "--dest", "{libs_root}"]
      }
    },
    "drift-web": {
      "path": "../drift-web",
      "kind": "package_repo",
      "depends_on": ["drift-lang", "drift-net-tls"],
      "commands": {
        "test": ["just", "test"],
        "stage_packages": ["drift", "deploy", "--dest", "{libs_root}"]
      }
    }
  },
  "environment": {
    "toolchain_bin_rel": "bin",
    "driftc_rel": "bin/driftc",
    "drift_rel": "bin/drift",
    "vars": {
      "DRIFTC": "{toolchain_root}/bin/driftc",
      "DRIFT_PACKAGE_ROOT": "{libs_root}",
      "DRIFT_PKG_ROOT": "{libs_root}"
    }
  },
  "cert_suite_policy": {
    "stage_packages": {
      "phase": "stage",
      "suite_id": "orch/stage-packages"
    }
  }
}
```

Notes:

- `depends_on` is the single source of truth for the dependency graph: a repo declares only its direct providers
- there is no `affects` field — the orchestrator walks `depends_on` upstream for provider staging (transitive closure) and reverses it for downstream invalidation, so adding a consumer never requires editing an upstream provider's config (a stale `affects` key is rejected at load)
- every `depends_on` entry must name a configured repo; an unknown provider is a load-time error
- commands are argv arrays, not shell strings
- placeholder substitution should be done by the Python driver
- repo-specific env can be added later if needed
- certification inputs should be exact commit SHAs, not branch names or moving refs
- `drift-lang` is a toolchain input and never a validation target
- `changed_repos` may include repos that triggered the run but were not themselves validation targets
- reports should still attribute downstream failures to the triggering candidate repos for team decision-making

### Cert-suite policy (trust-v1)

`drift deploy` emits a v1 cert claim on every invocation and requires
explicit cert-suite evidence policy. The orchestrator owns this policy
— **project repos must not specify `--cert-suite-id`,
`--cert-suite-evidence-sha256`, or `--cert-suite-no-evidence` in their
recipes**. Config load rejects any repo command that contains one of
these flags.

The `cert_suite_policy` field is a map from action name (e.g.
`stage_packages`) to an entry of the form:

```json
{
  "phase": "stage" | "release",
  "suite_id": "<suite-id-string>"
}
```

Two phases:

- **`stage`** — producer staging. The action produces an artifact but
  no standalone evidence artifact exists yet. Orch appends
  `--cert-suite-id <suite-id> --cert-suite-no-evidence`. Use this for
  `stage_packages` (suite id by convention: `orch/stage-packages`).
- **`release`** — release/promotion. The cert claim binds to a real
  evidence artifact (digest of report / log / archive). Orch will
  append `--cert-suite-id <release-suite-id>
  --cert-suite-evidence-sha256 sha256:<digest>` with the digest
  computed at runtime. *Not yet wired*: the promote path will assemble
  this when implemented.

If `cert_suite_policy` is omitted from `orchestration.json`, the
orchestrator falls back to the built-in default (a `stage`-phase entry
for `stage_packages` with suite id `orch/stage-packages`) so existing
configs continue to work.

Policy is applied at step-construction time in `compute_plan`; actions
not present in the policy map pass through unchanged.

### External capabilities (`DRIFT_CERT_CAPABILITIES`)

Gates often need external resources that are neither package artifacts nor
part of any repo checkout — a schema-migration tool, a database service, etc.
A repo declares these as **named capabilities** it requires; the orchestrator
resolves them and hands the gate **one resolved JSON document** via a single
env var. The platform contract is `DRIFT_CERT_CAPABILITIES` + the document
schema — **the orchestrator injects no per-tool env vars** (no `MARIACHI_BIN`,
no `DB_HOST`). Repos read the document and adapt it internally.

Three layers, three owners:

| Layer | Where | Committed? | Owns |
|---|---|---|---|
| Repo author | repo `requires` | yes | which capabilities the gates need, by id |
| Platform policy | top-level `capabilities` | yes (portable) | which capabilities exist; `kind`; **behavior only** |
| Host resolution | `cert-env.json` (host-local) | **no** (gitignored) | machine **facts**: paths, host/port, credential env name |

**Capability ids** are `tool:<name>` or `service:<name>`. A repo's `requires`
is a list of ids; an id that names no declared capability is a load-time error.

**Committed `capabilities`** carries *policy only* — never host facts:

```json
"capabilities": {
  "tool:mariachi":   { "kind": "tool", "min_version": "1.0.0",
                       "version_argv": ["{bin}", "--version"] },
  "service:mariadb": { "kind": "service" }
}
```

- `min_version` / `version_argv` (tools, optional): the preflight runs the
  probe (`{bin}` resolved host-locally), extracts the first semver-like token
  from stdout+stderr, and compares numerically; omit them for a presence-only
  check. **Preflight-only — never emitted into the run document.**
- A `service:*` means **the platform provisions an endpoint the repo
  consumes** — declare it only when a gate connects to a platform-provided
  instance (typically to coordinate with other products or shared schemas on it).
  It does **not** mean "the tests use a database." If a gate starts and owns its
  *own* DB (a private container/process whose lifecycle it manages), that is a
  **repo-private fixture**, not a service capability — the repo declares nothing
  for it (e.g. `drift-mariadb-client` owns its instance lifecycle and does not
  advertise `service:mariadb`). The recommended way to own a private DB is a
  **Docker image on a private port** (portable, custom port, no cross-repo
  interference); that depends on a container runtime, so the repo declares
  **`tool:docker`** — model the actual external prerequisite, not the database.
  Rule of thumb: name what the *platform* must provide. (Gates that start such a
  fixture must also tear it down — see "Gates restore entry state" in the
  onboarding doc.)
- For a consumed `service`, the contract models **no schemas, locks, or
  concurrency**: each project owns its own sandbox schema(s) against the instance
  (created/dropped via its schema tool, e.g. Mariachi) and may create as many as
  it needs. Isolation between projects is by separate schema, so the orchestrator
  does not serialize a shared service across projects; any self-serialization is
  the project's own gate concern.

**Host-local `cert-env.json`** supplies the machine facts, keyed by capability
id. It is host/CI-specific and **never committed** (gitignored). Lookup order:
`--cert-env <path>` → `$DRIFT_CERT_ENV` → `./cert-env.json`. See
`cert-env.example.json`.

```json
{
  "tool:mariachi":   { "bin": "/host/path/.venv/bin/mariachi" },
  "service:mariadb": { "host": "127.0.0.1", "port": 34114,
                       "credential_env": "MDB_ROOT_PWD", "instance": "mdb114-a" }
}
```

For a service, `host`, `port`, and `credential_env` are required (the preflight
enforces them); `instance` is an **optional** human-facing label — when the host
omits it, the run document omits the key rather than emitting `null`.
`credential_env` names the env var holding the secret — **the secret value
never appears in any file**. The orchestrator validates it is set and inherits
it by name into the gate environment; it adds only `DRIFT_CERT_CAPABILITIES`.

**Preflight.** Before staging, the orchestrator validates every capability the
run's validated repos require: the host must resolve it, a tool's `bin` must be
executable (and meet `min_version`), a service's `credential_env` must be set
and its `host:port` must accept a TCP connection. A gap **blocks the run** with
a clear reason in seconds — not deep inside a gate after minutes of staging.

**The run document** `build/runs/<run-id>/capabilities.json` is the resolved
merge (committed behavior + host facts), the public API repos code against. It
is **always written** (even empty) and the env var **always set** for gate
steps, so consumers never special-case a missing file. Emitted fields per kind:

```json
{
  "schema_version": 1,
  "run_id": "<run-id>",
  "capabilities": {
    "tool:mariachi":   { "kind": "tool", "bin": "<resolved path>" },
    "service:mariadb": { "kind": "service",
                         "host": "127.0.0.1", "port": 34114,
                         "credential_env": "MDB_ROOT_PWD", "instance": "mdb114-a" }
  }
}
```

`schema_version` is bumped only on incompatible changes; additive fields do not
bump it. See `docs/certification-onboarding.md` for bash/Python consumption
examples.

## 2. `build/runs/<run-id>/summary.json`

Purpose:

- durable evidence for one orchestration run
- human-readable report can be rendered from this file

Suggested shape:

```json
{
  "schema_version": 1,
  "run_id": "20260320-153000-drift-lang-abc1234",
  "started_at": "2026-03-20T21:30:00Z",
  "finished_at": "2026-03-20T22:04:12Z",
  "verdict": "certified",
  "changed_repos": ["drift-lang"],
  "candidate_commits": {
    "drift-lang": "abc1234",
    "drift-mariadb-client": "def5678",
    "drift-net-tls": "9999aaa",
    "drift-web": "bbbb111"
  },
  "involved_repos": [
    "drift-lang",
    "drift-mariadb-client",
    "drift-net-tls",
    "drift-web"
  ],
  "validated_repos": [
    "drift-mariadb-client",
    "drift-net-tls",
    "drift-web"
  ],
  "workspace_snapshot": {
    "drift-lang": {
      "path": "../drift-lang",
      "version": "0.27.24-dev",
      "commit": "abc1234",
      "git_state": "clean"
    },
    "drift-mariadb-client": {
      "path": "../drift-mariadb-client",
      "version": "0.1.2",
      "commit": "def5678",
      "git_state": "clean"
    },
    "drift-net-tls": {
      "path": "../drift-net-tls",
      "version": "0.3.9",
      "commit": "9999aaa",
      "git_state": "clean"
    },
    "drift-web": {
      "path": "../drift-web",
      "version": "0.2.5",
      "commit": "bbbb111",
      "git_state": "clean"
    }
  },
  "staging": {
    "run_root": "build/runs/20260320-153000-drift-lang-abc1234",
    "toolchain_root": "build/runs/20260320-153000-drift-lang-abc1234/toolchain",
    "libs_root": "build/runs/20260320-153000-drift-lang-abc1234/libs",
    "logs_root": "build/runs/20260320-153000-drift-lang-abc1234/logs"
  },
  "steps": [
    {
      "repo": "drift-lang",
      "name": "stage_toolchain",
      "status": "passed",
      "command": ["drift", "deploy", "--dest", "build/runs/.../toolchain"],
      "log_path": "build/runs/20260320-153000-drift-lang-abc1234/logs/drift-lang.stage_toolchain.log",
      "started_at": "2026-03-20T21:30:01Z",
      "finished_at": "2026-03-20T21:33:11Z"
    },
    {
      "repo": "drift-net-tls",
      "name": "test",
      "status": "passed",
      "command": ["just", "test"],
      "log_path": "build/runs/20260320-153000-drift-lang-abc1234/logs/drift-net-tls.test.log",
      "started_at": "2026-03-20T21:33:12Z",
      "finished_at": "2026-03-20T21:45:02Z"
    }
  ],
  "notes": [
    "Candidate validated using staged toolchain only",
    "No sibling repo metadata was modified"
  ]
}
```

Recommended enums:

- `verdict`: `certified`, `rejected`, `blocked`
- `git_state`: `clean`, `dirty`
- step `status`: `passed`, `failed`, `skipped`, `blocked`

Policy:

- certification runs use fresh checkouts of exact commit SHAs for all repos in scope
- local dirty-worktree runs are out of scope for certification

## 3. `state/workspace-lock.json`

Purpose:

- record the current certified workspace snapshot
- represent the exact repo/version/commit set approved together

Suggested shape:

```json
{
  "schema_version": 1,
  "updated_at": "2026-03-20T22:04:12Z",
  "source_run_id": "20260320-153000-drift-lang-abc1234",
  "verdict": "certified",
  "changed_repos": ["drift-lang"],
  "repos": {
    "drift-lang": {
      "path": "../drift-lang",
      "version": "0.27.24-dev",
      "commit": "abc1234"
    },
    "drift-mariadb-client": {
      "path": "../drift-mariadb-client",
      "version": "0.1.2",
      "commit": "def5678"
    },
    "drift-net-tls": {
      "path": "../drift-net-tls",
      "version": "0.3.9",
      "commit": "9999aaa"
    },
    "drift-web": {
      "path": "../drift-web",
      "version": "0.2.5",
      "commit": "bbbb111"
    }
  }
}
```

Update rule:

- write this file only after a `certified` run
- do not modify it for `rejected` or `blocked` runs
- do not modify it for runs that were not based on exact committed fresh checkouts

## Suggested Python Data Model

The first implementation can map directly to a few Python concepts:

- `OrchestrationConfig`
- `RepoConfig`
- `RunSummary`
- `WorkspaceSnapshot`
- `StepResult`
- `WorkspaceLock`

Using Python `dataclasses` would be sufficient initially. A validation layer can be added later if needed.

## Recommended Parsing Rules

- detect repo `commit` with `git rev-parse HEAD`
- derive repo `version` from repo-owned metadata
- fail clearly if version cannot be determined
- do not infer certification identity from version alone

## Open Schema Questions

1. Should `workspace-lock.json` also record the exact staged output hashes or paths, or is repo commit identity enough for now?
2. Should the summary include the full environment passed to each step, or only the key orchestrator variables?
3. Should command arrays always be stored verbatim in reports, even when secrets or signing inputs are eventually involved?
