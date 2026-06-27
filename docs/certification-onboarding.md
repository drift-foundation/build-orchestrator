# Certification Onboarding — how to become certifiable

This is the single authoritative guide for a repository joining the
certification pool. When the orchestrator certifies a candidate it expects each
repo to honor the contract below: a command surface, a dependency declaration,
committed-source staging, signed author claims, and (if its gates need external
tools/services) capability declarations. Get these right and your repo is
certifiable; the orchestrator handles ordering, staging, gating, and evidence.

The orchestrator config is the source of truth — see
[`orchestrator-schema.md`](orchestrator-schema.md) for exact field shapes. This
doc explains what a *repo team* must provide.

---

## 1. Required commands

Declare these under your repo entry's `commands` (argv arrays, not shell
strings). The orchestrator invokes them in a hermetic checkout with the staged
toolchain on `PATH` and `DRIFT_TOOLCHAIN_ROOT` set.

| Command | Role | When it runs |
|---|---|---|
| `stage_packages` | Produce/sign your packages into the shared packages root | producer phase |
| `test` | Functional correctness gate | certification gate |
| `stress` | Concurrency/contention gate | certification gate |
| `perf` | Throughput-vs-baseline gate | certification gate |

**Stage vs. certify split.** The orchestrator sets `DRIFT_CERT_MODE` and
`DRIFT_RUN_SNAPSHOT`:
- `stage_packages` runs with `DRIFT_CERT_MODE=stage` (producer role).
- gates run with `DRIFT_CERT_MODE=certify` (consumer role).
Both reference a `DRIFT_RUN_SNAPSHOT` the orchestrator maintains. Your recipes
should not set these themselves.

**Stdout cadence (watchdog).** Gate recipes must stream progress to stdout at
least every ~60s; the orchestrator's watchdog kills a step after ~120s of
silence. This matters most for DB-heavy `stress`/`perf` — emit periodic
progress, don't run long-silent.

**Cert-suite flags are orchestrator-owned.** Do **not** put `--cert-suite-id`,
`--cert-suite-evidence-sha256`, or `--cert-suite-no-evidence` in any recipe;
config load rejects them. The orchestrator injects cert-suite policy itself.

**Gates restore entry state.** A gate must leave the host as it found it —
idempotent and self-cleaning. Anything it starts (a container, daemon, process)
or allocates (a port, a temp dir) it must tear down on exit, success or failure;
anything that was already present, it leaves untouched. Leftover state that a
later run inherits is a defect (see §5 for the DB-fixture case).

---

## 2. Dependencies — `depends_on` (direct providers only)

Declare only your **direct** providers (the repos whose packages/toolchain you
consume). Do **not** track who depends on *you* — there is no `affects` field;
the orchestrator derives downstream invalidation by reversing `depends_on`.

- Adding a new consumer never requires editing your config.
- A `depends_on` entry that names no configured repo is a **load-time error**.
- Transitive providers are pulled in automatically (full provider closure); you
  declare B, and B's own providers are staged for you.

---

## 3. Package staging — committed source only

`stage_packages` must produce your packages from **committed manifest, source,
and claims** — never from a local `build/`, `dist/`, or other uncommitted
artifact. The orchestrator certifies what is in the commit it checked out.

- Ship a `drift/manifest.json` at the checkout root (a single repo may declare
  multiple artifacts in one manifest — the multi-artifact convention).
- **Lock v2 is required**; v1 locks are rejected.
- Staging runs against the shared packages root via the staged `drift`; the bare
  `drift deploy --dest <pkgs_root>` form is what the orchestrator expects.

---

## 4. Author claims & trust (trust-v1)

Every artifact carries a committed **author claim** that binds the signed
identity to the exact source content. Before any gate runs, the orchestrator
runs a keyless **author-claim preflight** (`drift author verify`) over each
involved package: a stale or missing claim **blocks** the run in milliseconds,
before staging or gating.

- Re-mint the author claim whenever the artifact source changes (a stale claim
  = source changed without re-signing).
- Sign with your pool identity (e.g. the Foundation key for Foundation repos).
- Cert-suite evidence policy is owned by the orchestrator (see §1).

---

## 5. Declaring external capabilities — `requires`

If your gates need a tool or service that is **not** a package and **not** in
your checkout (a schema-migration tool, a database, …), declare it as a named
capability in your repo entry:

```json
"requires": ["tool:mariachi", "service:mariadb"]
```

- Ids are `tool:<name>` or `service:<name>`. An unknown id is a load-time error.
- You declare *what you need*, by name. **You do not put host paths, ports, or
  secrets in committed config** — the platform resolves those per host.
- The capability must be declared in the orchestrator's `capabilities` policy
  and resolved on the cert host (`cert-env.json`); if it isn't, your run is
  **blocked** with a clear reason before staging (see §7).

**Declare a `service:*` only for a platform-provided endpoint you *consume*.**
A `service:*` capability means *the platform owns and provisions the endpoint,
and your gate connects to it* — typically because your gate coordinates with
other products or shared schemas on the same instance. It does **not** mean "my
tests use a database."

- **Consume (declare it):** your gate connects to a shared instance the platform
  stands up. Declare `service:mariadb`; the orchestrator provides the connection
  facts and blocks the run if the endpoint isn't reachable.
- **Own (do not declare `service:*`):** your gate starts and manages its *own*
  MariaDB — a private instance whose lifecycle you control inside the gate. That
  is a **repo-private test fixture**, not a shared service. Do **not** declare
  `service:mariadb`. (Example: `drift-mariadb-client` exercises MariaDB but owns
  the instance lifecycle itself, so it does not advertise `service:mariadb`.)
  - **Recommended pattern:** spin the instance up from a **Docker image** on a
    private port. It's the most portable way to get a MariaDB at a custom port
    with no interference from other repos' gates. When you do this you depend on
    a container runtime, so **declare `requires: [..., "tool:docker"]`** — that
    makes the preflight verify Docker on the cert host and block early if it's
    missing, instead of failing deep inside your first schema setup. (Pin the
    image by digest, or have the platform pre-provision it, so the gate doesn't
    pull over the network at run time.) The point still stands: declare what the
    *platform* must provide (the container runtime), not the database itself.
- **Restore entry state (required).** A gate must leave the host exactly as it
  found it. If your gate **starts** the private container, it must **stop and
  remove it** on exit (success or failure); if the instance was already running
  when the gate began (e.g. a dev box), leave it as-is. A container, process,
  port, or schema left behind after the gate is a defect — concurrent and
  subsequent runs must not inherit your leftover state.

**A consumed `service` is a shared instance — you own your schema(s).** When you
do consume one, the orchestrator hands you connection facts (host/port/
credential) for a reused instance; it models no schemas, locks, or concurrency.
You create/drop/control your own **sandbox schema(s)** against it (via your
schema tool, e.g. Mariachi), as many as you need. Different projects are isolated
by separate schemas, so the platform does not serialize the instance across
projects. If *your own* gates need to serialize concurrent runs against *your*
schema, that's your gate's concern — key your lock to your schema, not to the
shared instance (an instance-wide lock needlessly blocks unrelated projects).

---

## 6. The `DRIFT_CERT_CAPABILITIES` contract

The orchestrator resolves your required capabilities into **one JSON document
per run** and points your gates at it with a **single env var**:

```
DRIFT_CERT_CAPABILITIES=<run-root>/capabilities.json
```

**This is the only capability-related env var the orchestrator sets.** There is
no `MARIACHI_BIN`, no `DB_HOST` injected by the platform. Read the document and
adapt it to whatever your recipes use internally (you may set your own
`MARIACHI_BIN` from it — that's your private detail, not the contract).

Document shape (see [`orchestrator-schema.md`](orchestrator-schema.md) for the
authoritative schema):

```json
{
  "schema_version": 1,
  "run_id": "<run-id>",
  "capabilities": {
    "tool:mariachi":   { "kind": "tool", "bin": "/host/path/.venv/bin/mariachi" },
    "service:mariadb": { "kind": "service",
                         "host": "127.0.0.1", "port": 34114,
                         "credential_env": "MDB_ROOT_PWD", "instance": "mdb114-a" }
  }
}
```

`credential_env` is the **name** of the env var holding the secret — the value
is never in the file. The orchestrator guarantees that env var is present in
the gate environment; you read it by name. For a service, `host`/`port`/
`credential_env` are always present; `instance` is an optional label and may be
absent (the key is omitted, never `null`) — don't depend on it.

### Bash (jq)

```bash
caps="$DRIFT_CERT_CAPABILITIES"
mariachi_bin=$(jq -r '.capabilities["tool:mariachi"].bin' "$caps")

db_host=$(jq -r '.capabilities["service:mariadb"].host'           "$caps")
db_port=$(jq -r '.capabilities["service:mariadb"].port'           "$caps")
cred_env=$(jq -r '.capabilities["service:mariadb"].credential_env' "$caps")
db_password="${!cred_env}"          # indirect expansion: read the env var it names

export MARIACHI_BIN="$mariachi_bin" # optional: adapt to your recipes internally
```

### Python

```python
import json, os

doc = json.load(open(os.environ["DRIFT_CERT_CAPABILITIES"]))
caps = doc["capabilities"]

mariachi_bin = caps["tool:mariachi"]["bin"]

svc = caps["service:mariadb"]
db = {
    "host": svc["host"],
    "port": int(svc["port"]),
    "password": os.environ[svc["credential_env"]],  # value read by name
}
```

The document is **always present** (even an empty `"capabilities": {}` when a
run requires nothing) and the env var is always set for gate steps, so you never
need to special-case a missing file. Treat `schema_version` as a versioned
contract: it bumps only on incompatible changes.

### Two modes: local developer vs. certification

Repos must keep gates runnable in two modes, keyed off whether
`DRIFT_CERT_CAPABILITIES` is set:

- **Local developer mode** (env unset): gates should use repo-local defaults and
  normal developer overrides. This keeps `just test`, `just stress`, and
  `just perf` usable outside the orchestrator.
- **Certification mode** (env set): gates must treat the capabilities document as
  **authoritative** for declared capabilities. Do **not** fall back to local
  paths, ports, or defaults if a required capability or field is missing — fail
  early with a clear error instead.

The reason: a silent local fallback in cert mode would let a gate pass against
the *wrong* (developer) endpoint while the orchestrator believes it validated
the certified one — exactly the kind of mismatch the single-document contract
exists to prevent.

---

## 7. External requirements & blocked-run behavior

The orchestrator validates every required capability **before staging**:

- the cert host must resolve it (`cert-env.json`);
- a `tool` binary must exist, be executable, and meet any `min_version`;
- a `service` `credential_env` must be set, and its `host:port` must accept a
  TCP connection.

Any gap **blocks the run in seconds**, with a clear reason
(`blocked: capability service:mariadb not provided on this host …`) — instead of
failing deep inside `just test` after minutes of staging. A blocked run is an
environment problem, not a verdict on your code: fix the host/declaration and
re-run.

If your gates need a tool/service that isn't yet in the pool, raise it with the
orchestrator team to add a `capabilities` entry and provision it on the cert
host; declaring `requires` alone does not provision anything.

---

## 8. Evidence & verdicts

Each run writes durable evidence under `build/runs/<run-id>/` — `summary.json`
(machine-readable), `report.txt`, per-step logs, the resolved
`capabilities.json`, and the workspace lock. A run ends in exactly one verdict:

| Verdict | Meaning | Typical cause |
|---|---|---|
| **certified** | Every required gate passed; the candidate is safe for real use and promotable. | clean run |
| **rejected** | A gate **ran and failed** — a real quality/correctness signal about the candidate. | failing `test`/`stress`/`perf` |
| **blocked** | The run could **not be evaluated** — a precondition failed before/around gating. Not a judgment on your code. | missing capability, stale author claim, dual-runtime/toolchain issue, checkout failure |

Practical distinction: **rejected** means *fix your code*; **blocked** means
*fix the environment/declaration and re-run*. Reports attribute downstream
failures back to the candidate repos that triggered the run.

---

## Quick checklist

- [ ] `commands`: `test`, `stress`, `perf`, `stage_packages` declared; gates stream stdout ≥ every 60s.
- [ ] No `--cert-suite-*` flags in any recipe.
- [ ] `depends_on`: direct providers only; every entry is a configured repo.
- [ ] Root `drift/manifest.json`; staging from committed source; Lock v2.
- [ ] Author claims fresh and signed with the pool identity.
- [ ] `requires`: external tools/services declared by capability id (no host paths/secrets).
- [ ] Gates read `DRIFT_CERT_CAPABILITIES` and adapt internally (no reliance on platform-injected per-tool env vars).
- [ ] Gates support both local mode and certification mode; cert mode treats `DRIFT_CERT_CAPABILITIES` as authoritative and does not use local fallbacks.
