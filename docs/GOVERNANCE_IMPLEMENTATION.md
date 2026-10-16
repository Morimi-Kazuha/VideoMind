# Governance implementation

Baseline: `480b4be`; isolated branch/worktree. Existing untracked source-workspace artifacts are excluded. No deployment or push.

## Compatibility boundaries

Preserve TaskKey, queue names, checkpoint and execution schemas, source identities, prompts, ranking, feature defaults, tool ledger recovery, request generations, and public compatibility exports. Deletion is retryable cleanup, not an atomic multi-store transaction; concurrent deletion/write barriers remain deferred.

## Validation

Python 3.12 is the primary environment; Python 3.13 is a CI compatibility job. Install with `pip install -c constraints-py312.txt -e ".[test]"`. Backend baseline (Python 3.12.14): 1835 passed, 38 explicit live/fixture skips, 1 warning (67.57s). Frontend baseline: 112 passing Node tests and successful production build.

## Delivery

Each corrective behavior and each mechanical refactor is a separate commit. Revert individual commits in reverse dependency order; no schema migration. Playback credentials require draining old instances and up to 15 minutes for old process-local grants to expire. Keep feature flags unchanged.
