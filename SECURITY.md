# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately rather than opening a public issue.
Use GitHub's **[Report a vulnerability](https://github.com/Taynavv/feedback-remote-library-server/security/advisories/new)**
flow (repository **Security** tab → *Report a vulnerability*). We aim to acknowledge
reports within a few days and will coordinate a fix and disclosure with you.

## Threat model

Remote Library Server exposes the running FeedBack instance's local library over a small
HTTP API on its own port. Understand what that means before you enable it:

- **No authentication by default.** While running, the server serves the entire local
  library — including original package downloads — to anyone who can reach it. This is
  intended for same-machine or trusted-LAN use. The read-only API protects the library's
  *integrity*, not its *confidentiality*: anyone allowed to connect can browse and
  download everything.
- **Bind address controls direct-HTTP exposure.** The default `127.0.0.1` is same-machine
  only. Binding `0.0.0.0` exposes the library to the entire local network.
- **iroh sharing is the exception — it reaches the public internet regardless of bind
  address.** When "Share over iroh" is on, the server is reachable from anywhere by its
  Library ID (a self-authenticating public key), tunnelled to the same direct API. The
  bind host no longer limits who can reach it, so the `127.0.0.1` default provides **no**
  protection on the iroh path. The **only** access control there is the `authToken` —
  set one before enabling iroh unless you intend the library to be open to anyone holding
  the ID. The Library ID is a capability: treat it like a secret URL, and share it only
  over channels you trust.
- **`authToken` is the access control.** When set, every endpoint except `GET /health`
  requires the token (`Authorization: Bearer <token>` or a `?token=` query parameter),
  compared in constant time. It protects the direct-HTTP and iroh paths identically. The
  token is stored in plaintext in the plugin's `settings.json`, so protect that file.
- **NAM tone asset sharing is opt-in** (`shareNamToneAssets`, off by default) and only
  serves model/IR files referenced by a song's own exported tone manifest. Requested
  paths are resolved and confined under the FeedBack config directory.
- **Package/asset paths are validated.** Remote song IDs decode to library-relative
  filenames that are re-resolved under the configured library root; any path that
  escapes the root is refused.

## Revoking access

- **The `authToken` is your primary revocation lever.** Rotating it immediately cuts off
  every client — direct-HTTP and iroh alike — that knew the old token.
- **For iroh, regenerating the Library ID revokes a leaked ID.** The Library ID is stable
  by design (backed by a persistent key), so a leaked ID otherwise stays valid until you
  change it. Use **Regenerate ID** on the Remote Server screen to issue a new one; the old
  ID stops resolving, but every current follower must re-add the new ID. There is no
  per-follower revocation — the token and the Library ID are the two levers.

## Recommendations

- Keep the bind host at `127.0.0.1` unless another device needs access.
- If you bind to a non-loopback address, set a strong `authToken` and restrict the port
  to a trusted network. Do not expose it directly to the internet — use a VPN or
  firewall.
- **Set an `authToken` before enabling iroh** unless you truly want anyone with the
  Library ID to browse. iroh is public by nature.
- Leave NAM tone asset sharing disabled unless you need it.
- The plugin's management endpoints (settings/start/stop) add no authentication of their
  own; they inherit whatever protects FeedBack's main backend. Keep that backend on a
  trusted interface.
- **Keep the plugin updated.** Security fixes only protect you once installed. A git-clone
  install can update in place via FeedBack's *Check for Updates*; a zip install must be
  updated by downloading the latest [release](https://github.com/Taynavv/feedback-remote-library-server/releases)
  and replacing the folder. See the README's *Install* section for the trade-off.
