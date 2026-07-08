# feedback-remote-library-server — development guide

Remote Library Server is a [FeedBack](https://github.com/got-feedback/feedBack)
plugin (id `remote_library_server`) that shares the running instance's local library
over a small direct HTTP API on its own port, so the
[Remote Library Client](https://github.com/Taynavv/feedback-remote-library-client) on
another machine can browse and pull songs from it — over the LAN, or **peer-to-peer by a
Library ID over iroh** with no port forwarding. It is a thin wrapper around FeedBack's
`local` library provider — it never builds a second catalog of its own.

## Architecture

| File | Role |
|---|---|
| [routes.py](routes.py) | `setup(app, context)`: management endpoints on FeedBack's main backend (settings / status / start / stop / activity / local-songs) **plus a second, standalone FastAPI app** (`_create_direct_app`) bound to its own host/port that serves the direct library API (`/source`, `/songs`, `/artists`, `/stats`, `/tuning-names`, `/songs/{id}/art`, `/songs/{id}/package`, NAM-tone endpoints) |
| [remote_library_server/models.py](remote_library_server/models.py) | `PackageForm` enum + the remote song-summary shapes returned by the direct API |
| [remote_library_server/store.py](remote_library_server/store.py) | Settings + runtime state persistence (`settings.json` / `state.json` in the plugin's server-files dir) |
| [remote_library_server/crypto.py](remote_library_server/crypto.py) | URL-safe encode/decode of remote song IDs (references to library-relative filenames) |
| [remote_library_server/iroh_tunnel.py](remote_library_server/iroh_tunnel.py) | `IrohTunnel` — the optional "Share over iroh (P2P)" transport: binds an iroh endpoint (a **persistent** secret key ⇒ a stable Library ID / EndpointId), accepts QUIC streams, and pipes each straight to the running direct server on `127.0.0.1:<port>`. The direct API is untouched. Needs `iroh` (`requirements.txt`, lazy-imported); the key lives in its own file, never in `settings.json` |
| [screen.html](screen.html) / [screen.js](screen.js) | Management screen: enable, bind host/port, source name, NAM-asset toggle, **"Share over iroh" toggle + copyable Library ID**, live activity |
| [settings.html](settings.html) | Settings surface |
| [tests/](tests) | pytest, content-free: fake local providers + synthetic packages |

## Load-bearing subtleties — do not "clean up" casually

- **The format token is `sloppak`, not `feedpak` — on purpose.** FeedBack core's
  library scanner still reports `format: "sloppak"` for every song package (new
  `.feedpak` and legacy `.sloppak` share one on-disk format) and still serves it via
  the `.sloppak` / `.zip` code paths. The server's package-form detection
  (`_package_form_for_song`, `PackageForm.SLOPPAK_*`) mirrors that core contract.
  Renaming these to `feedpak` desyncs from core and stops advertising real packages.
  User-facing docs say "feedpak"; the wire/detection tokens stay `sloppak`.
- **The NAM-tone-sync schema is an external contract.** `NAM_TONE_SYNC_SCHEMA =
  "slopsmith.nam-tone-sync.v1"` must match the manifest stamped by FeedBack's
  NAM-tone export (produced elsewhere, not here). Do not rebrand it — the client
  checks the identical string, and a mismatch silently drops all tone sync.
- **`protocol: "slopsmith-direct-library.v1"`** in `/source` is the client↔server
  handshake tag; the client defaults to the same literal. Change it in BOTH repos or
  neither.
- **Package downloads are path-checked before serving.** Remote song IDs decode to
  library-relative filenames and are re-resolved under the configured DLC/library
  root; a decoded path that escapes the root is refused. Keep that check ahead of any
  file read.
- **Directory-form packages are not downloadable.** A song whose package is an
  unpacked directory (`sloppak-directory`) is advertised but has no single-file
  download; `tests/test_routes.py` asserts this.
- **Autostart waits for the library scan.** With `enabled`, the server reports
  `waitingForScan` and only binds once FeedBack's startup scan reaches `complete`, so
  it never races the local index.
- **The playback settings key is a pure function of the library-relative filename.**
  `_playback_settings_key(relative_name)` derives from `relative_name` (+ its package
  form) alone. The song summary and the NAM tone-sync payload compute it independently,
  and the client correlates song↔tone by exact string match — seeding it from richer
  per-song fields (`id`, `songKey`, …) at only one call site silently desyncs the two.
  `tests/test_routes.py::test_settings_key_is_stable_regardless_of_provider_song_fields`
  guards this.
- **Direct-API auth is optional and gated by `authToken`.** Empty token ⇒ open server
  (the localhost/trusted-LAN default). Non-empty ⇒ every endpoint except `/health`
  requires `Authorization: Bearer <token>` or a `?token=` query param, checked live per
  request by `_require_auth` with `hmac.compare_digest`. **This gate also protects iroh
  clients** (they hit the same direct app), so recommend a token whenever iroh sharing is on.
- **iroh sharing tunnels the existing direct API — it is not a second protocol.** `IrohTunnel`
  binds an iroh endpoint and pipes each accepted QUIC stream to `127.0.0.1:<direct port>`, so the
  direct FastAPI app (and its `authToken` gate) serve iroh clients unchanged. Its lifecycle *follows
  the direct server*: `_start_direct_server` / `_stop_direct_server` start/stop it, `save_settings`
  calls `_reconcile_iroh_tunnel`, and it needs the direct server running (that's its pipe target).
- **The iroh identity is a persistent secret kept out of settings.** The secret key (⇒ the stable
  Library ID) lives in `iroh_identity.key` in the store dir via `_load_or_create_secret`, NOT in
  `settings.json` (which the UI reads) — the Library ID must survive restarts and the private key must
  never reach the settings API. Only `irohEnabled` (the toggle) is a setting. `iroh` is lazy-imported,
  so the plain direct server runs without the native dependency.

## Rules

- **License**: AGPL-3.0-or-later. Keep contributions compatible.
- **No song content, ever**: no packages, audio, or artwork committed to the repo,
  tests, or CI. Tests synthesize fakes.
- Match the release tag to `plugin.json`'s `version` — the release workflow fails the
  build if they disagree. `feedback_target` records the FeedBack version the plugin
  was last verified against.

## Development

```bash
python -m venv .venv

# Windows
.venv/Scripts/pip install pytest fastapi httpx iroh
.venv/Scripts/python -m pytest -q

# macOS / Linux
.venv/bin/pip install pytest fastapi httpx iroh
.venv/bin/python -m pytest -q
```

`iroh` (from `requirements.txt`) is only needed to run the iroh tunnel tests — they `importorskip`
it, so the suite still passes without it. CI installs it so those tests run for real.
