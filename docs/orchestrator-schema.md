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
      "affects": [
        "drift-mariadb-client",
        "drift-net-tls",
        "drift-web"
      ],
      "commands": {
        "stage_toolchain": ["drift", "deploy", "--dest", "{toolchain_root}"]
      }
    },
    "drift-mariadb-client": {
      "path": "../drift-mariadb-client",
      "kind": "package_repo",
      "depends_on": ["drift-lang"],
      "affects": [],
      "commands": {
        "test": ["just", "test"],
        "stage_packages": ["drift", "deploy", "--dest", "{libs_root}"]
      }
    },
    "drift-net-tls": {
      "path": "../drift-net-tls",
      "kind": "package_repo",
      "depends_on": ["drift-lang"],
      "affects": ["drift-web"],
      "commands": {
        "test": ["just", "test"],
        "stage_packages": ["drift", "deploy", "--dest", "{libs_root}"]
      }
    },
    "drift-web": {
      "path": "../drift-web",
      "kind": "package_repo",
      "depends_on": ["drift-lang", "drift-net-tls"],
      "affects": [],
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
  }
}
```

Notes:

- commands are argv arrays, not shell strings
- placeholder substitution should be done by the Python driver
- repo-specific env can be added later if needed
- certification inputs should be exact commit SHAs, not branch names or moving refs
- `drift-lang` is a toolchain input and never a validation target
- `changed_repos` may include repos that triggered the run but were not themselves validation targets
- reports should still attribute downstream failures to the triggering candidate repos for team decision-making

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
