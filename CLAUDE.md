# MiniStack Agent Guidance

This fork exists only as a test dependency for BusyDone's local E2E environment.

- Change MiniStack only when a concrete MiniStack behavior blocks or fails a
  BusyDone local E2E path.
- Do not proactively audit or fix MiniStack-only security, correctness,
  performance, packaging, or CI findings.
- Do not create BusyDone Linear tickets for findings outside that local-E2E
  scope.
- Keep `.github/workflows/` empty in this fork. Do not add or run fork CI.
- Keep Git hooks disabled with `core.hooksPath=/dev/null`; do not install hooks.
- Validate only focused behavior required by the affected BusyDone local E2E
  path. Do not run full suites, E2E, or Playwright locally unless explicitly
  requested.
- `CLAUDE.md` is canonical. `AGENTS.md` must be a relative symlink to it.
