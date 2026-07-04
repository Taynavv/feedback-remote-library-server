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
  library — including original package downloads — to anyone who can reach its host and
  port. This is intended for same-machine or trusted-LAN use.
- **Bind address controls exposure.** The default `127.0.0.1` is same-machine only.
  Binding `0.0.0.0` exposes the library to the entire local network.
- **`authToken` is the access control.** When set, every endpoint except `GET /health`
  requires the token (`Authorization: Bearer <token>` or a `?token=` query parameter),
  compared in constant time. The token is stored in plaintext in the plugin's
  `settings.json`.
- **NAM tone asset sharing is opt-in** (`shareNamToneAssets`, off by default) and only
  serves model/IR files referenced by a song's own exported tone manifest. Requested
  paths are resolved and confined under the FeedBack config directory.
- **Package/asset paths are validated.** Remote song IDs decode to library-relative
  filenames that are re-resolved under the configured library root; any path that
  escapes the root is refused.

## Recommendations

- Keep the bind host at `127.0.0.1` unless another device needs access.
- If you bind to a non-loopback address, set a strong `authToken` and restrict the port
  to a trusted network. Do not expose it directly to the internet — use a VPN or
  firewall.
- Leave NAM tone asset sharing disabled unless you need it.
