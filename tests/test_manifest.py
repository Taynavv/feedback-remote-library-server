import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    return json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))


def test_manifest_declares_library_capability_relationship():
    manifest = _manifest()

    assert "capability-pipelines.v1" in manifest["standards"]
    assert "plugin-runtime-idempotent.v1" in manifest["standards"]
    library = manifest["capabilities"]["library"]
    assert library["roles"] == ["requester", "observer"]
    assert library["requests"] == ["list-providers", "get-current", "inspect"]
    assert library["observes"] == ["providers-refreshed", "source-changed"]
    assert "commands" not in library
    assert "events" not in library
    assert library["compatibility"] == "none"
    assert library["ownership"] == "requester-only"
    assert library["safety"] == "safe"
    assert library["version"] == 1
    assert manifest.get("capability_api") in (None, {"standard": "capability-pipelines.v1", "version": 1})
    assert manifest["settings"]["server_files"] == ["remote_library_server/settings.json"]
    # settings.json holds the plaintext authToken, so it must NOT ride along in the
    # diagnostics bundle (which operators export and share on bug reports).
    assert manifest["diagnostics"]["server_files"] == ["remote_library_server/state.json"]
    assert "remote_library_server/settings.json" not in manifest["diagnostics"]["server_files"]


def test_manifest_does_not_declare_canonical_library_provider_role():
    manifest = _manifest()

    assert "provider" not in manifest["capabilities"]["library"]["roles"]
    assert "owner" not in manifest["capabilities"]["library"]["roles"]


def test_manifest_does_not_declare_server_management_domain():
    manifest = _manifest()

    assert "remote-library-server" not in manifest.get("capabilities", {})


def test_screen_does_not_register_runtime_capability_handlers():
    screen = (ROOT / "screen.js").read_text(encoding="utf-8")

    assert "registerParticipant(" not in screen
    assert "emitEvent(" not in screen


def test_manifest_declares_license():
    manifest = _manifest()

    assert manifest.get("license") == "AGPL-3.0-or-later"


def test_manifest_referenced_files_exist():
    manifest = _manifest()

    for key in ("screen", "script", "routes", "icon"):
        assert (ROOT / manifest[key]).is_file(), f"manifest {key!r} points at a missing file"
    assert (ROOT / manifest["settings"]["html"]).is_file()
