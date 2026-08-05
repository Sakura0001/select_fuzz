# GitHub Actions Trigger Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure a feature-branch commit runs CI once through its pull request and does not also run duplicate push and bundle workflows.

**Architecture:** Restrict `ci` push events to `main` and pull-request events to `main`. Restrict the CentOS 7 bundle to `main` pushes plus manual dispatch, leaving all job bodies unchanged.

**Tech Stack:** GitHub Actions YAML, Python/PyYAML validation, Git.

## Global Constraints

- Pull requests targeting `main` run `ci`.
- Pushes to `main` run `ci` and `build-centos7-bundle`.
- Feature-branch pushes do not directly run either workflow.
- `build-centos7-bundle` remains manually dispatchable.
- Job definitions, permissions, pinned action versions, and artifacts remain unchanged.

---

### Task 1: Deduplicate Workflow Triggers

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/build-centos7-bundle.yml`

**Interfaces:**
- Consumes: GitHub event names `push`, `pull_request`, and `workflow_dispatch`.
- Produces: the trigger matrix documented in `docs/superpowers/specs/2026-08-05-github-actions-trigger-dedup-design.md`.

- [x] **Step 1: Run the desired trigger-matrix assertion against current files**

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import yaml

ci = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())
bundle = yaml.safe_load(Path('.github/workflows/build-centos7-bundle.yml').read_text())
ci_on = ci.get('on', ci.get(True))
bundle_on = bundle.get('on', bundle.get(True))
assert ci_on == {
    'push': {'branches': ['main']},
    'pull_request': {'branches': ['main']},
}
assert bundle_on == {
    'push': {'branches': ['main']},
    'workflow_dispatch': None,
}
PY
```

Expected: FAIL because `ci.push` is unrestricted and the bundle still runs for feature-branch pushes and pull requests.

- [x] **Step 2: Apply the minimal trigger-only changes**

Set `.github/workflows/ci.yml` to:

```yaml
on:
  push:
    branches:
      - main
  pull_request:
    branches:
      - main
```

Set `.github/workflows/build-centos7-bundle.yml` to:

```yaml
on:
  push:
    branches:
      - main
  workflow_dispatch:
```

- [x] **Step 3: Re-run the trigger-matrix assertion**

Run the Step 1 command again.

Expected: PASS with exit code 0.

- [x] **Step 4: Verify repository formatting and scope**

```bash
git diff --check
git diff -- .github/workflows/ci.yml .github/workflows/build-centos7-bundle.yml
```

Expected: no formatting errors, and only the `on` blocks change.

- [ ] **Step 5: Commit, push, and inspect Actions**

```bash
git add .github/workflows/ci.yml .github/workflows/build-centos7-bundle.yml docs/superpowers/plans/2026-08-05-github-actions-trigger-dedup.md
git commit -m "ci: deduplicate workflow triggers"
git push
```

Expected: the open pull request creates one `ci` run for the new commit and no `build-centos7-bundle` run.
