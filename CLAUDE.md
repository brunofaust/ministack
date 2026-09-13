# CLAUDE.md

## BusyDone Fork Maintenance

- Reconcile upstream version bumps semantically against the BusyDone fork's git
  history, preserving the fork contracts that BusyDone relies on.
- Verify vendor version bumps in BusyDone after updating
  `docker/ministack-revision.txt`. Do not run this repository's test or lint
  suites solely for the bump; exercise the relevant BusyDone production paths
  from the consumer repository.
