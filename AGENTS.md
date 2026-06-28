# AGENTS.md — build-orchestrator

Operating notes for agents working in this repo. The orchestrator certifies and
promotes the Drift package/app pool; `drift-lang` is the toolchain input.

## "Announce it" — certified-release announcement

When the user says **"announce it"**, **"announce the certified release"**, or any
close variant, produce **one** release note that summarizes everything new in the
**current certified snapshot since the previous certified snapshot** — a single
message others can read to catch up fast. Do **not** write one note per package;
fold them all into one.

### Hard preconditions — do NOT announce if any fail

The point of an announcement is "this is certified and ready to consume." Never
announce a release whose artifacts are incomplete or broken. Before writing
anything, verify against `~/opt/drift/certified/current`:

- `summary.json` `verdict == "certified"` (and note if it was `--fasttrack`).
- `toolchain/bin/{drift,driftc}` present; record `staging.toolchain_identity.driftc_version`.
- **Every** package under `pkgs/<name>/<ver>/` (named `libs/` in snapshots
  predating the libs→pkgs rename) has its full leg set:
  `<name>.zdmp` + `<name>.author-claim` + `<name>.cert-claim.<kid>.json` + `<name>.provenance.zst`.
- **Every** app under `apps/<name>/<ver>/` (if any) has: the binary `<name>` +
  `<name>.author-claim` + `<name>.cert-claim.<kid>.json` + `<name>.provenance.zst`.

If anything is missing/inconsistent (e.g. an app that certified in the run but
never reached `certified/current`), **stop and report the gap to the user instead
of announcing.** Fix the orchestrator, re-promote, re-verify, then announce.

### What counts as "since last certified"

The certified tree lives at `~/opt/drift/certified/` (promote's default
`--dest-root` is `~/opt/drift`):

- `current` → `snapshots/<newest-run-id>` is the release being announced.
- The **previous** certified snapshot is the next-newest entry in
  `snapshots/` (names sort chronologically: `YYYYMMDD-HHMMSS-<repo>-<sha>`).
- **Version diff:** compare `pkgs/<pkg>/<ver>` (or legacy `libs/`) and
  `apps/<app>/<ver>` dir names between the two snapshots. Report every bump
  (old → new) and any newly-added package/app.
- **Substance (what changed, not just numbers):** each snapshot's `summary.json`
  carries `candidate_commits` (repo → SHA). For each repo, run
  `git -C ../<repo> log --oneline <prev_sha>..<cur_sha>` to pull the real
  changelog, and distill it into 1–3 highlights per repo.
- **Toolchain:** report the driftc version + ABI from both snapshots' toolchain
  identity (e.g. `0.33.56 → 0.33.61 | abi 18`).

### Note shape

Write to the announce channel (see below), filename
`<UTC>Z-build-orchestrator-certified-release.md` where `<UTC>` is
`date -u +%Y%m%dT%H%M%SZ`. Include:

- Title: pool release + toolchain version/ABI + the snapshot run-id.
- A compact version table: each package/app, `old → new` (mark **NEW** entries).
- Per-repo highlights (the distilled `git log`), grouped by repo.
- Anything consumers must act on (ABI change, breaking API, new dep edge).
- How to consume: point `DRIFT_TOOLCHAIN_ROOT` / `DRIFT_PACKAGE_ROOT` at
  `~/opt/drift/certified/current/{toolchain,pkgs}` (apps under `.../apps`).
- Sign-off: build-orchestrator (sl@pushcoin.com).

## Announce channel — what it is and what goes in it

`/tmp/drift-announce/` is a shared file drop, convention `<UTC>Z-<repo>-<type>.md`.
It is for **compiler/toolchain communication only**:

- **Inbound** — `drift-lang` release notes (we read these and act on them).
- **Outbound from us** — only two kinds:
  1. **certified-release announcements** (above);
  2. **bug reports for the compiler/toolchain** — i.e. defects in `drift-lang`
     itself.

**Internal orchestrator bugs/gaps do NOT go to the channel.** If the problem is
ours (e.g. promote not copying `apps/`, a stale version/kind pin in
`orchestrate.py`), fix it in this repo and report to the user directly — never
post it to the announce channel.
