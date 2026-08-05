# GitHub Actions Trigger Deduplication Design

## Goal

Prevent the same feature-branch commit from running identical workflows once for
`push` and again for `pull_request`, while retaining validation before merge and
validation of the final `main` commit.

## Trigger Matrix

| Workflow | Pull request targeting `main` | Push to `main` | Feature-branch push | Manual |
| --- | --- | --- | --- | --- |
| `ci` | Run | Run | Do not run | No |
| `build-centos7-bundle` | Do not run | Run | Do not run | Run |

## Rationale

- Pull requests remain the required validation path for Python, frontend,
  coverage, and end-to-end checks.
- A push to `main` validates the exact merged commit once.
- The CentOS 7 artifact is only needed for an integrated `main` commit or an
  explicit manual build, so it should not consume resources on every PR update.
- Job definitions, permissions, pinned action versions, and artifact contents
  remain unchanged.

## Verification

- Parse both workflow files as YAML.
- Assert the trigger matrix above from the parsed documents.
- Run whitespace and repository-diff checks before committing.
- Push the feature branch and confirm that only the PR `ci` workflow is created
  for the new commit.
