# Contributing

Thanks for your interest in Remote Library Server, a
[FeedBack](https://github.com/got-feedback/feedBack) plugin. It shares the local FeedBack
library over a small direct HTTP API for the
[Remote Library Client](https://github.com/Taynavv/feedback-remote-library-client) plugin.

## Ground rules

- **License:** contributions are accepted under **AGPL-3.0-or-later** (see
  [LICENSE](LICENSE)). By submitting a change you agree it may be distributed under that
  license. Each source file carries an `SPDX-License-Identifier: AGPL-3.0-or-later` header —
  keep it.
- **No song content, ever.** Tests and fixtures must be content-free — synthetic songs and
  fake providers only. Never commit real songs, packages, or audio.
- **Keep the load-bearing contracts intact.** See [CLAUDE.md](CLAUDE.md) for specifics: the
  `slopsmith-direct-library.v1` protocol tag and `slopsmith.nam-tone-sync.v1` schema literal
  (must match the client verbatim), the `_playback_settings_key` derivation (must match the
  client and FeedBack core), and the path-confinement guards on song IDs and NAM assets.
- **Never leak the operator's `authToken`.** It is stored in `settings.json`; keep it out of
  responses, logs, and the diagnostics bundle.

## Development setup

```bash
python -m venv .venv
# Activate:  Windows: .venv\Scripts\activate  |  macOS/Linux: source .venv/bin/activate
pip install pytest fastapi httpx ruff
```

## Before you open a pull request

Run the same gates CI runs:

```bash
ruff check .
pytest -q
```

- Add or update tests for any behavior change.
- Match the surrounding style; keep lines within the 120-character limit (ruff enforces
  `E`, `F`, and `I` rules).
- If you cut a release, keep `plugin.json` `version` in sync with the release tag — the
  release workflow fails the build if they disagree. Update `feedback_target` when you
  verify against a new FeedBack version.

## Reporting issues

- **Functional bugs:** open a GitHub issue with reproduction steps and your environment.
- **Security vulnerabilities:** do **not** open a public issue — follow
  [SECURITY.md](SECURITY.md).
