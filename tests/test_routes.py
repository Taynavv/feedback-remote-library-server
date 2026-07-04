from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeLocalProvider:
    def __init__(self, package_name: str, art_result=None):
        self.package_name = package_name
        self.art_result = art_result

    def _song(self) -> dict:
        return {
            "filename": self.package_name,
            "title": "Clean Tone",
            "artist": "The Fixtures",
            "album": "Bench",
            "year": 2026,
            "duration": 123.4,
            "format": "sloppak" if Path(self.package_name).suffix == ".sloppak" else "psarc",
            "stem_count": 4 if Path(self.package_name).suffix == ".sloppak" else 0,
            "stem_ids": ["drums", "bass", "guitar", "vocals"] if Path(self.package_name).suffix == ".sloppak" else [],
            "arrangements": [{"name": "Lead"}],
            "has_lyrics": True,
            "tuning": "E Standard",
        }

    def query_page(self, **kwargs):
        q = str(kwargs.get("q") or "").lower()
        song = self._song()
        songs = [song] if q in song["title"].lower() else []
        return songs, len(songs)

    def query_artists(self, **kwargs):
        song = self._song()
        return [{
            "name": song["artist"],
            "album_count": 1,
            "song_count": 1,
            "albums": [{"name": song["album"], "songs": [song]}],
        }], 1

    def query_stats(self, **kwargs):
        return {"total_songs": 1, "total_artists": 1, "letters": {"T": 1}}

    def tuning_names(self):
        return {"tunings": [{"name": "E Standard", "sort_key": 0, "count": 1}]}

    async def get_art(self, song_id: str):
        assert song_id == self.package_name
        if self.art_result is not None:
            return self.art_result
        return Response(content=b"cover-bytes", media_type="image/png")


class FakeLocalProviderWithoutArt:
    def __init__(self, package_name: str):
        self._provider = FakeLocalProvider(package_name)

    def query_page(self, **kwargs):
        return self._provider.query_page(**kwargs)

    def query_artists(self, **kwargs):
        return self._provider.query_artists(**kwargs)

    def query_stats(self, **kwargs):
        return self._provider.query_stats(**kwargs)

    def tuning_names(self):
        return self._provider.tuning_names()


class FakeLibraryProviders:
    def __init__(self, provider):
        self.provider = provider

    def get(self, provider_id: str):
        return self.provider if provider_id == "local" else None

    def provider_method(self, provider, method_name: str):
        return getattr(provider, method_name, None)


def _client(
    tmp_path,
    package_name="song.sloppak",
    package_content: bytes | None = None,
    art_result=None,
    local_provider=None,
):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)

    dlc_dir = tmp_path / "dlc"
    dlc_dir.mkdir()
    package_path = dlc_dir / package_name
    if package_name.endswith("/"):
        package_path.mkdir(parents=True)
        (package_path / "manifest.yaml").write_text("title: Clean Tone\n")
    elif package_name.endswith(".sloppak") and package_content is None:
        with zipfile.ZipFile(package_path, "w") as archive:
            archive.writestr("manifest.json", "{}")
            archive.writestr("cover.png", b"cover-bytes")
            archive.writestr("song.bin", b"audio")
    else:
        package_path.write_bytes(package_content or b"package-bytes")

    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "get_dlc_dir": lambda: dlc_dir,
        "library_providers": FakeLibraryProviders(
            local_provider or FakeLocalProvider(package_name.rstrip("/"), art_result)
        ),
    })
    management_client = TestClient(app)
    management_client.post("/api/plugins/remote_library_server/settings", json={
        "enabled": False,
        "host": "127.0.0.1",
        "port": 9876,
        "sourceName": "Studio Source",
    })
    direct_client = TestClient(routes._create_direct_app())
    return management_client, direct_client, package_path


def _enable_nam_tone_sharing(management_client):
    response = management_client.post("/api/plugins/remote_library_server/settings", json={
        "enabled": False,
        "host": "127.0.0.1",
        "port": 9876,
        "sourceName": "Studio Source",
        "shareNamToneAssets": True,
    })
    assert response.status_code == 200


def _write_nam_tone_fixture(config_dir: Path, filename: str = "song.psarc") -> dict:
    config_dir.mkdir(parents=True, exist_ok=True)
    models_dir = config_dir / "nam_models"
    irs_dir = config_dir / "nam_irs"
    models_dir.mkdir()
    irs_dir.mkdir()
    model_bytes = b'{"version": "test model"}'
    ir_bytes = b"RIFF-test-ir"
    (models_dir / "clean.nam").write_bytes(model_bytes)
    (irs_dir / "room.wav").write_bytes(ir_bytes)
    conn = sqlite3.connect(config_dir / "nam_tone.db")
    conn.executescript("""
        CREATE TABLE presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            model_file TEXT,
            ir_file TEXT,
            input_gain REAL NOT NULL DEFAULT 1.0,
            output_gain REAL NOT NULL DEFAULT 0.5,
            gate_threshold REAL NOT NULL DEFAULT -60.0,
            settings_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE tone_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            tone_key TEXT NOT NULL,
            preset_id INTEGER NOT NULL,
            UNIQUE(filename, tone_key),
            FOREIGN KEY (preset_id) REFERENCES presets(id)
        );
    """)
    conn.execute(
        "INSERT INTO presets (name, model_file, ir_file, input_gain, output_gain, gate_threshold, settings_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Clean NAM", "clean.nam", "room.wav", 1.25, 0.75, -55.0, json.dumps({"cab": "open"})),
    )
    preset_id = conn.execute("SELECT id FROM presets WHERE name = ?", ("Clean NAM",)).fetchone()[0]
    conn.execute(
        "INSERT INTO tone_mappings (filename, tone_key, preset_id) VALUES (?, ?, ?)",
        (filename, "Clean", preset_id),
    )
    conn.commit()
    conn.close()
    return {
        "modelSha": "sha256:" + hashlib.sha256(model_bytes).hexdigest(),
        "irSha": "sha256:" + hashlib.sha256(ir_bytes).hexdigest(),
    }


def test_management_status_uses_direct_server_shape(tmp_path):
    management_client, _direct_client, _package_path = _client(tmp_path)

    response = management_client.get("/api/plugins/remote_library_server/status")

    assert response.status_code == 200
    data = response.json()
    assert data["source"]["sourceName"] == "Studio Source"
    assert data["source"]["songCount"] == 1
    assert data["server"]["port"] == 9876
    assert data["server"]["protocol"] == "slopsmith-direct-library.v1"
    assert "relay" not in data
    assert management_client.get("/api/plugins/remote_library_server/pairing/requests").status_code == 404


def test_shutdown_stops_direct_server(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    stopped = []

    monkeypatch.setattr(routes, "_stop_direct_server", lambda: stopped.append(True) or {})

    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "get_dlc_dir": lambda: tmp_path / "dlc",
        "library_providers": FakeLibraryProviders(FakeLocalProvider("song.sloppak")),
        "get_scan_status": lambda: {"running": False, "stage": "complete"},
    })

    assert routes._shutdown in app.router.on_shutdown
    routes._shutdown()
    assert stopped == [True]


def test_direct_source_and_song_search_do_not_expose_paths(tmp_path):
    _management_client, direct_client, package_path = _client(tmp_path)

    source = direct_client.get("/source")
    songs = direct_client.get("/songs?q=clean&pageSize=10")

    assert source.status_code == 200
    assert source.json()["sourceName"] == "Studio Source"
    assert source.json()["capabilities"] == ["library.read", "art.read", "song.sync"]
    assert source.json()["server"]["url"] == "http://127.0.0.1:9876"
    assert source.json()["server"]["protocol"] == "slopsmith-direct-library.v1"
    assert songs.status_code == 200
    song = songs.json()["songs"][0]
    assert song["title"] == "Clean Tone"
    assert song["artist"] == "The Fixtures"
    assert song["hasLyrics"] is True
    assert song["stemCount"] == 4
    assert song["stemIds"] == ["drums", "bass", "guitar", "vocals"]
    assert "has_lyrics" not in song
    assert "stem_count" not in song
    assert "stem_ids" not in song
    assert song["artworkUrl"].startswith("/songs/")
    assert song["packageUrl"].startswith("/songs/")
    assert str(package_path) not in str(song)


def test_direct_songs_use_local_provider_paged_query_without_package_hashing(tmp_path):
    _management_client, direct_client, _package_path = _client(tmp_path)

    response = direct_client.get("/songs?q=clean&page=0&pageSize=1&sort=title&direction=desc")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["nextCursor"] is None
    assert data["query"]["filtersApplied"] is True
    song = data["songs"][0]
    assert song["title"] == "Clean Tone"
    assert song["remoteSongId"].startswith("song_")
    assert song["settingsKey"].startswith("settings-v1-")
    assert "packageHash" not in song


def test_invalid_cursor_returns_400(tmp_path):
    management_client, direct_client, _package_path = _client(tmp_path)

    direct_response = direct_client.get("/songs?cursor=abc")
    management_response = management_client.get("/api/plugins/remote_library_server/local-songs?cursor=abc")

    assert direct_response.status_code == 400
    assert management_response.status_code == 400
    assert "cursor" in direct_response.json()["detail"]


def test_negative_cursor_returns_400(tmp_path):
    management_client, direct_client, _package_path = _client(tmp_path)

    direct_response = direct_client.get("/songs?cursor=-1")
    management_response = management_client.get("/api/plugins/remote_library_server/local-songs?cursor=-1")

    assert direct_response.status_code == 400
    assert management_response.status_code == 400
    assert "non-negative" in direct_response.json()["detail"]


def test_unaligned_cursor_returns_400(tmp_path):
    management_client, direct_client, _package_path = _client(tmp_path)

    direct_response = direct_client.get("/songs?cursor=1&pageSize=50")
    management_response = management_client.get("/api/plugins/remote_library_server/local-songs?cursor=1&pageSize=50")

    assert direct_response.status_code == 400
    assert management_response.status_code == 400
    assert "aligned to pageSize" in direct_response.json()["detail"]


def test_direct_server_does_not_allow_all_cors_origins(tmp_path):
    _management_client, direct_client, _package_path = _client(tmp_path)

    response = direct_client.get("/source", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_stop_direct_server_preserves_references_when_thread_stays_alive(monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)

    class FakeServer:
        should_exit = False

    class StillAliveThread:
        def __init__(self):
            self.joined = False

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.joined = True

    server = FakeServer()
    thread = StillAliveThread()
    monkeypatch.setattr(routes, "_direct_server", server)
    monkeypatch.setattr(routes, "_direct_thread", thread)

    status = routes._stop_direct_server()

    assert server.should_exit is True
    assert thread.joined is True
    assert status["running"] is True
    assert routes._direct_server is server
    assert routes._direct_thread is thread


def test_wildcard_bind_advertises_discovered_host(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    monkeypatch.setattr(routes, "_discover_lan_host", lambda: "192.0.2.10")

    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "get_dlc_dir": lambda: tmp_path / "dlc",
        "library_providers": FakeLibraryProviders(FakeLocalProvider("song.psarc")),
    })
    client = TestClient(app)

    saved = client.post("/api/plugins/remote_library_server/settings", json={
        "enabled": False,
        "host": "0.0.0.0",
        "port": 9876,
        "sourceName": "Studio Source",
    })

    assert saved.status_code == 200
    assert saved.json()["server"]["url"] == "http://192.0.2.10:9876"


def test_ipv6_direct_url_wraps_literal_host(tmp_path):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)

    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "get_dlc_dir": lambda: tmp_path / "dlc",
        "library_providers": FakeLibraryProviders(FakeLocalProvider("song.psarc")),
    })
    client = TestClient(app)

    saved = client.post("/api/plugins/remote_library_server/settings", json={
        "enabled": False,
        "host": "::1",
        "port": 9876,
        "sourceName": "Studio Source",
    })

    assert saved.status_code == 200
    assert saved.json()["server"]["url"] == "http://[::1]:9876"


def test_ensure_bindable_uses_resolved_ipv6_socket(monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    bound = []

    class FakeSocket:
        def __init__(self, family, socktype, proto):
            bound.append({"family": family, "socktype": socktype, "proto": proto, "sockaddr": None})

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def setsockopt(self, level, optname, value):
            pass

        def bind(self, sockaddr):
            bound[-1]["sockaddr"] = sockaddr

    monkeypatch.setattr(
        routes.socket,
        "getaddrinfo",
        lambda host, port, type, flags: [
            (routes.socket.AF_INET6, routes.socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))
        ],
    )
    monkeypatch.setattr(routes.socket, "socket", FakeSocket)

    routes._ensure_bindable("::1", 9876)

    assert bound == [
        {
            "family": routes.socket.AF_INET6,
            "socktype": routes.socket.SOCK_STREAM,
            "proto": 6,
            "sockaddr": ("::1", 9876, 0, 0),
        }
    ]


def test_oversized_search_query_returns_422(tmp_path):
    management_client, direct_client, _package_path = _client(tmp_path)
    huge_query = "x" * 1001

    direct_response = direct_client.get("/songs", params={"q": huge_query})
    artists_response = direct_client.get("/artists", params={"q": huge_query})
    stats_response = direct_client.get("/stats", params={"q": huge_query})
    management_response = management_client.get(
        "/api/plugins/remote_library_server/local-songs", params={"q": huge_query}
    )

    assert direct_response.status_code == 422
    assert artists_response.status_code == 422
    assert stats_response.status_code == 422
    assert management_response.status_code == 422


def test_nam_tone_sync_is_disabled_by_default(tmp_path):
    _management_client, direct_client, _package_path = _client(
        tmp_path, package_name="song.psarc", package_content=b"small-package"
    )
    song = direct_client.get("/songs").json()["songs"][0]

    response = direct_client.get(f"/songs/{song['remoteSongId']}/nam-tone-sync")

    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_nam_tone_sync_exports_song_mappings_and_referenced_assets(tmp_path):
    management_client, direct_client, _package_path = _client(
        tmp_path, package_name="song.psarc", package_content=b"small-package"
    )
    song = direct_client.get("/songs").json()["songs"][0]
    expected = _write_nam_tone_fixture(tmp_path / "config", song["settingsKey"])
    _enable_nam_tone_sharing(management_client)

    response = direct_client.get(f"/songs/{song['remoteSongId']}/nam-tone-sync")

    assert response.status_code == 200
    data = response.json()
    assert data["schema"] == "slopsmith.nam-tone-sync.v1"
    assert data["sourceFilename"] == "song.psarc"
    assert data["sourceSettingsKey"] == song["settingsKey"]
    assert data["targetSettingsKey"] == song["settingsKey"]
    assert data["mappings"] == [{"toneKey": "Clean", "presetRef": "preset:1"}]
    preset = data["presets"][0]
    assert preset["name"] == "Clean NAM"
    assert preset["inputGain"] == 1.25
    assert preset["outputGain"] == 0.75
    assert preset["gateThreshold"] == -55.0
    assert preset["settings"] == {"cab": "open"}
    assert preset["modelFile"]["name"] == "clean.nam"
    assert preset["modelFile"]["sha256"] == expected["modelSha"]
    assert preset["irFile"]["name"] == "room.wav"
    assert preset["irFile"]["sha256"] == expected["irSha"]

    model = direct_client.get(preset["modelFile"]["url"])
    ir = direct_client.get(preset["irFile"]["url"])

    assert model.status_code == 200
    assert model.content == b'{"version": "test model"}'
    assert model.headers["content-type"].startswith("application/json")
    assert ir.status_code == 200
    assert ir.content == b"RIFF-test-ir"
    assert ir.headers["content-type"].startswith("audio/wav")


def test_nam_tone_asset_endpoint_only_serves_referenced_song_assets(tmp_path):
    management_client, direct_client, _package_path = _client(
        tmp_path, package_name="song.psarc", package_content=b"small-package"
    )
    _write_nam_tone_fixture(tmp_path / "config", "song.psarc")
    (tmp_path / "config" / "nam_models" / "other.nam").write_bytes(b"other")
    _enable_nam_tone_sharing(management_client)
    song = direct_client.get("/songs").json()["songs"][0]

    response = direct_client.get(f"/songs/{song['remoteSongId']}/nam-tone-assets/model/other.nam")

    assert response.status_code == 404


def test_direct_package_download_returns_original_file(tmp_path):
    _management_client, direct_client, package_path = _client(
        tmp_path, package_name="song.psarc", package_content=b"small-package"
    )
    song = direct_client.get("/songs").json()["songs"][0]

    response = direct_client.get(f"/songs/{song['remoteSongId']}/package")

    assert response.status_code == 200
    assert response.content == package_path.read_bytes()


def test_directory_sloppak_is_not_advertised_as_downloadable(tmp_path):
    _management_client, direct_client, _package_path = _client(tmp_path, package_name="song.sloppak/")

    response = direct_client.get("/songs?q=clean&pageSize=10")

    assert response.status_code == 200
    song = response.json()["songs"][0]
    assert song["packageForm"] == "sloppak-directory"
    assert song["syncSupport"] == "not-syncable"
    assert song["status"] == "not-syncable"
    assert "package-download" not in song["capabilities"]
    assert "packageUrl" not in song


def test_provider_without_get_art_does_not_advertise_artwork(tmp_path):
    _management_client, direct_client, _package_path = _client(
        tmp_path,
        package_name="song.psarc",
        package_content=b"small-package",
        local_provider=FakeLocalProviderWithoutArt("song.psarc"),
    )

    source = direct_client.get("/source")
    songs = direct_client.get("/songs")

    assert source.status_code == 200
    assert source.json()["capabilities"] == ["library.read", "song.sync"]
    assert songs.status_code == 200
    song = songs.json()["songs"][0]
    assert "artwork" not in song["capabilities"]
    assert song["artworkThumbHash"] is None
    assert "artworkUrl" not in song


def test_direct_artwork_rejects_non_http_redirect_urls(tmp_path):
    _management_client, direct_client, _package_path = _client(
        tmp_path,
        package_name="song.psarc",
        package_content=b"small-package",
        art_result={"url": "file:///etc/passwd"},
    )
    song = direct_client.get("/songs").json()["songs"][0]

    response = direct_client.get(f"/songs/{song['remoteSongId']}/art")

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported artwork URL"


def test_direct_artwork_returns_zip_cover(tmp_path):
    _management_client, direct_client, _package_path = _client(tmp_path)
    song = direct_client.get("/songs").json()["songs"][0]

    response = direct_client.get(f"/songs/{song['remoteSongId']}/art")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"cover-bytes"


def test_direct_unknown_package_404s(tmp_path):
    _management_client, direct_client, _package_path = _client(
        tmp_path, package_name="song.psarc", package_content=b"small-package"
    )

    response = direct_client.get("/songs/song_not-real/package")

    assert response.status_code == 404


def test_direct_api_is_open_when_no_token_configured(tmp_path):
    _management_client, direct_client, _package_path = _client(tmp_path)

    assert direct_client.get("/source").status_code == 200
    assert direct_client.get("/source").json()["auth"] == {"required": False}


def test_direct_api_requires_token_when_configured(tmp_path):
    management_client, direct_client, _package_path = _client(
        tmp_path, package_name="song.psarc", package_content=b"small-package"
    )
    saved = management_client.post("/api/plugins/remote_library_server/settings", json={
        "enabled": False,
        "host": "127.0.0.1",
        "port": 9876,
        "sourceName": "Studio Source",
        "authToken": "s3cret-token",
    })
    assert saved.status_code == 200

    # /health stays open for liveness checks.
    assert direct_client.get("/health").status_code == 200
    # Protected endpoints reject missing / wrong tokens.
    assert direct_client.get("/source").status_code == 401
    assert direct_client.get("/songs").status_code == 401
    assert direct_client.get("/source", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Accepted via bearer header or ?token= query param.
    assert direct_client.get("/source", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
    assert direct_client.get("/source?token=s3cret-token").status_code == 200
    authed = direct_client.get("/source", headers={"Authorization": "Bearer s3cret-token"})
    assert authed.json()["auth"] == {"required": True}


def test_direct_api_handles_non_ascii_token_without_500(tmp_path):
    management_client, direct_client, _package_path = _client(tmp_path)
    token = "café-🎸-tøken"
    management_client.post("/api/plugins/remote_library_server/settings", json={
        "enabled": False,
        "host": "127.0.0.1",
        "port": 9876,
        "sourceName": "Studio Source",
        "authToken": token,
    })

    # A wrong token is a clean 401 (not a 500 from comparing against a non-ASCII secret).
    assert direct_client.get("/source", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # The correct non-ASCII token authenticates via the query param (HTTP headers cannot
    # carry non-Latin-1 bytes, so the ?token= path is the transport for such tokens).
    assert direct_client.get("/source", params={"token": token}).status_code == 200


def test_settings_key_is_stable_regardless_of_provider_song_fields(tmp_path):
    class RichProvider(FakeLocalProvider):
        def _song(self) -> dict:
            song = super()._song()
            song["id"] = "library-id-42"
            song["songKey"] = "songkey-xyz"
            song["sourceKind"] = "something-else"
            return song

    management_client, direct_client, _package_path = _client(
        tmp_path,
        package_name="song.psarc",
        package_content=b"small-package",
        local_provider=RichProvider("song.psarc"),
    )
    song = direct_client.get("/songs").json()["songs"][0]
    _write_nam_tone_fixture(tmp_path / "config", song["settingsKey"])
    _enable_nam_tone_sharing(management_client)

    payload = direct_client.get(f"/songs/{song['remoteSongId']}/nam-tone-sync").json()

    # The song summary and the tone-sync payload must derive an identical settings key
    # even when the provider song carries id/songKey/sourceKind fields.
    assert payload["sourceSettingsKey"] == song["settingsKey"]
    assert payload["targetSettingsKey"] == song["settingsKey"]