# To: drift-workflows — adopt `DRIFT_CERT_CAPABILITIES` (action needed)

**TL;DR.** The orchestrator now provisions external cert tools/services through a
single resolved JSON document and one env var, `DRIFT_CERT_CAPABILITIES`. It
**no longer injects `MARIACHI_BIN` or any `DB_*` env vars**. Until your gates
read the document, the cert `test` gate stays red (it's the same
`mariachi venv missing` failure — the orchestrator just stopped papering over it
with a guessed env var). This is the strict-but-clean contract we agreed on; the
orchestrator side is landed. Your move is a small reader + a two-mode rule.

You don't need to restructure anything, and **the orchestrator does not care
what env vars (if any) you use internally** — it names none. The only job is: in
**certification mode**, read the document and use its values authoritatively
instead of local literals/defaults. How you thread those values into your gates
is entirely yours.

---

## 1. The contract

The orchestrator sets exactly one env var for gate steps:

```
DRIFT_CERT_CAPABILITIES=<run-root>/capabilities.json
```

Your declared capabilities (already wired in `orchestration.json`) are
`tool:mariachi` and `service:mariadb`. The document looks like:

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

`credential_env` is the **name** of the env var holding the DB password (the
value is never in the file). The orchestrator guarantees that env var
(`MDB_ROOT_PWD`) is present in the gate environment — you read it by name, as you
do today. No secret handling changes.

`service:mariadb` is just a **shared MariaDB instance** you connect to. The
orchestrator models no schemas, locks, or concurrency: you own your sandbox
schema(s) via Mariachi (drop/recreate/control, as many as you need), and the
platform does not serialize the instance across projects — you're isolated by
your own schema. See the note in §3 on your current instance-wide flock.

---

## 2. What you must do

### (a) Two modes — keep gates runnable both ways

- **Local developer mode** — `DRIFT_CERT_CAPABILITIES` **unset**: use your
  current repo-local defaults/overrides. `just test|stress|perf` must keep
  working standalone exactly as now.
- **Certification mode** — `DRIFT_CERT_CAPABILITIES` **set**: treat the document
  as **authoritative** for declared capabilities. Do **not** fall back to local
  paths/ports/defaults if a required field is missing — fail early with a clear
  error. (A silent local fallback would let a gate pass against the wrong
  endpoint while the orchestrator believes it validated the certified one.)

### (b) A single bootstrap that maps the doc → your existing env names

Add one shim, sourced at the top of the gate recipes (or in a `_certenv` helper),
that — only when `DRIFT_CERT_CAPABILITIES` is set — exports the vars your recipes
and harnesses already consume. Everything downstream keeps reading the same
names; you've just changed where they come from in cert mode.

```bash
# _certenv.sh — source me; no-op in local mode.
if [[ -n "${DRIFT_CERT_CAPABILITIES:-}" ]]; then
  caps="$DRIFT_CERT_CAPABILITIES"
  req() { jq -er "$1" "$caps" || { echo "cert-cap: missing $1 in $caps" >&2; exit 1; }; }

  export MARIACHI_BIN="$(req '.capabilities["tool:mariachi"].bin')"
  export MDB_HOST="$(req     '.capabilities["service:mariadb"].host')"
  export MDB_PORT="$(req     '.capabilities["service:mariadb"].port')"
  # credential_env names the secret var; it is already in the environment.
  cred="$(req '.capabilities["service:mariadb"].credential_env')"
  [[ -n "${!cred:-}" ]] || { echo "cert-cap: secret \$$cred not set" >&2; exit 1; }
fi
```

`jq -e` makes a missing field a hard failure — that's the "no fallback in cert
mode" rule, enforced. (The DB lock is *not* in the document — see §3; you keep
managing your own schema lock.)

### (c) Make the currently-hardcoded justfile vars env-driven

Your gates today hardcode literals that ignore the environment:

```just
DB_HOST := "127.0.0.1"
DB_PORT := "34114"
MARIACHI := env("MARIACHI_BIN", "../../mariachi/.venv/bin/mariachi")
```

`MARIACHI` is already env-driven (good — it just needs `MARIACHI_BIN` set, which
the shim does). The DB host/port need to read the environment so the shim's
values flow through, while keeping your local defaults:

```just
DB_HOST := env("MDB_HOST", "127.0.0.1")
DB_PORT := env("MDB_PORT", "34114")
```

Your Python harnesses already do this (`os.environ.get("MDB_HOST", "127.0.0.1")`,
etc.), so they need **no change** once the shim exports `MDB_HOST`/`MDB_PORT` —
they'll pick up the certified endpoint automatically. Leave your `DB_LOCK` /
flocker wiring exactly as it is — the orchestrator does not provide or want it
(see §3).

---

## 3. DB locking stays yours — and you can loosen it

The orchestrator does **not** carry a lock key or any concurrency policy; a
`service:mariadb` is just the shared instance. You own your sandbox schema(s) via
Mariachi and you own how you serialize access to them. Nothing to wire from the
document here.

Worth a look while you're in there, though: your gates currently flock on the
**instance** key — `flocker --key serial-mariadb-mdb114-a -j 1`. Since each
project has its **own schema**, that over-serializes: it blocks drift-workflows
and drift-mariadb-client from running at the same time even though their schemas
never collide. If you want cross-project parallelism on the shared box, scope the
flocker key to **your schema** (e.g. one key per project/schema) instead of the
instance name. Entirely your call — it's a recipe change on your side, and the
orchestrator stays out of it.

---

## 4. Verify locally before the next cert run

You can prove the cert path without the orchestrator: hand-write a doc and point
the env var at it.

```bash
cat > /tmp/caps.json <<'JSON'
{ "schema_version": 1, "run_id": "local-test",
  "capabilities": {
    "tool:mariachi":   { "kind": "tool", "bin": "'"$(command -v mariachi)"'" },
    "service:mariadb": { "kind": "service",
                         "host": "127.0.0.1", "port": 34114,
                         "credential_env": "MDB_ROOT_PWD", "instance": "mdb114-a" } } }
JSON
export DRIFT_CERT_CAPABILITIES=/tmp/caps.json
export MDB_ROOT_PWD=...        # your local root pw
just test                     # should resolve mariachi + DB from the doc
unset DRIFT_CERT_CAPABILITIES
just test                     # local mode still works off defaults
```

Both invocations must pass: cert mode reads the doc, local mode uses defaults.

---

## 5. Notes / FYI

- **Preflight is your friend.** In a real run the orchestrator validates every
  capability *before* staging: missing tool, unreachable `host:port`, unset
  secret, or Mariachi below `min_version: 1.0.0` all **block in seconds** with a
  clear reason — no more failing deep inside `just test`.
- You do **not** put host paths or secrets anywhere committed. The cert host's
  `cert-env.json` (host-local, gitignored) maps `tool:mariachi.bin` and the
  MariaDB facts; that's our side.
- `requires: ["tool:mariachi", "service:mariadb"]` is already set for
  `drift-workflows` in `orchestration.json`. You do not declare `net-tls`/etc. —
  providers come from `depends_on`.
- Full contract + bash/Python examples + the two-mode rule:
  `docs/certification-onboarding.md` §6, and `docs/orchestrator-schema.md`
  (External capabilities).

**Definition of done:** with `DRIFT_CERT_CAPABILITIES` set, `just test|stress|perf`
source Mariachi + DB coords from the document (no `MARIACHI_BIN`/`DB_*` assumed
from ambient env), and with it unset they still run locally. Ping us when the
reader is in and we'll re-run the cert — nothing else needed from your side.
