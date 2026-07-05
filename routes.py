# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import os
import re
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib import parse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from remote_library_server.crypto import sha256_hex
from remote_library_server.models import PackageForm, RemoteSongStatus, RemoteSongSummary, SyncSupport
from remote_library_server.store import RemoteLibraryServerStore

_store: RemoteLibraryServerStore | None = None
_get_dlc_dir = None
_library_providers = None
_direct_server = None
_direct_thread: threading.Thread | None = None
_autostart_thread: threading.Thread | None = None
_get_scan_status = None
_config_dir: Path | None = None
_shutdown_requested = threading.Event()
# Reentrant so _restart_direct_server can hold it across stop+start (which each
# re-acquire it) and make the restart atomic against a concurrent start/settings-save.
_server_lock = threading.RLock()
NAM_TONE_SYNC_SCHEMA = "slopsmith.nam-tone-sync.v1"


def _plugin_version() -> str:
    try:
        manifest = json.loads(Path(__file__).with_name("plugin.json").read_text(encoding="utf-8"))
        return str(manifest.get("version") or "0")
    except (OSError, ValueError):
        return "0"


def _fnv1a_base36(value: str) -> str:
    h = 2166136261
    for char in str(value or ""):
        h ^= ord(char)
        h = (h * 16777619) & 0xFFFFFFFF
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if h == 0:
        return "0"
    out = ""
    while h:
        h, rem = divmod(h, 36)
        out = digits[rem] + out
    return out


def _playback_settings_key(relative_name: str) -> str:
    # Derived purely from the library-relative filename (+ its package form) so that
    # every call site — the song summary and the NAM tone-sync payload — computes an
    # identical key. See CLAUDE.md: this must stay a pure function of relative_name.
    source_kind = _package_form_for_relative(relative_name) or "unknown"
    seed = relative_name or "unknown"
    suffix = _fnv1a_base36(f"{source_kind}:{seed}").rjust(7, "0")[-7:]
    return f"settings-v1-{suffix}"


def _settings() -> dict:
    return _store.load_settings() if _store else {}


def _auth_token() -> str:
    return str(_settings().get("authToken") or "").strip()


def _require_auth(request: Request) -> None:
    """Optional bearer-token gate for the direct API.

    When no token is configured the server is open (localhost/trusted-LAN default).
    When a token is set, callers must supply it via ``Authorization: Bearer <token>``
    or a ``?token=`` query parameter (the latter for media URLs used in ``<img>``/
    download contexts that cannot set headers).
    """
    expected = _auth_token()
    if not expected:
        return
    header = request.headers.get("authorization") or ""
    provided = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not provided:
        provided = request.query_params.get("token") or ""
    # Compare as bytes: hmac.compare_digest rejects str with non-ASCII chars, which would
    # otherwise turn a non-ASCII token into an unhandled 500 on every request.
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="invalid or missing auth token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _source_id() -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", socket.gethostname()).strip("-").lower()
    return f"direct_{slug or 'source'}"


def _source_name() -> str:
    return str(_settings().get("sourceName") or _default_source_name())


def _default_source_name() -> str:
    return f"Remote Library on {socket.gethostname()}"


def _bind_host() -> str:
    return str(_settings().get("host") or "127.0.0.1")


def _bind_port() -> int:
    try:
        return max(1, min(65535, int(_settings().get("port") or 8765)))
    except (TypeError, ValueError):
        return 8765


def _discover_lan_host() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            host = probe.getsockname()[0]
            if host and not host.startswith("127."):
                return host
    except OSError:
        pass
    return socket.gethostname()


def _display_host() -> str:
    host = _bind_host()
    return _discover_lan_host() if host in {"0.0.0.0", "::"} else host


def _direct_url() -> str:
    host = _display_host()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{_bind_port()}"


def _local_provider_has_art() -> bool:
    return callable(_local_provider_method("get_art"))


def _normalize_settings(data: dict) -> dict:
    current = _settings()
    incoming = dict(data or {})
    normalized = {
        "enabled": bool(incoming.get("enabled", current.get("enabled"))),
        "host": str(incoming.get("host", current.get("host")) or "127.0.0.1").strip() or "127.0.0.1",
        "sourceName": (
            str(incoming.get("sourceName", current.get("sourceName")) or _default_source_name()).strip()
            or _default_source_name()
        ),
    }
    try:
        normalized["port"] = max(1, min(65535, int(incoming.get("port", current.get("port")) or 8765)))
    except (TypeError, ValueError):
        normalized["port"] = 8765
    normalized["shareNamToneAssets"] = bool(
        incoming.get("shareNamToneAssets", current.get("shareNamToneAssets", False))
    )
    normalized["authToken"] = str(incoming.get("authToken", current.get("authToken")) or "").strip()
    return normalized


def _scan_status() -> dict:
    if callable(_get_scan_status):
        try:
            status = _get_scan_status()
            return status if isinstance(status, dict) else {}
        except Exception:
            return {}
    return {"running": False, "stage": "complete"}


def _scan_ready() -> bool:
    status = _scan_status()
    return not bool(status.get("running")) and status.get("stage") == "complete"


def _local_library_root() -> Path | None:
    if callable(_get_dlc_dir):
        resolved = _get_dlc_dir()
        if isinstance(resolved, (str, os.PathLike)):
            path = Path(resolved)
            return path if path.exists() else None
    dlc_dir = os.environ.get("DLC_DIR")
    if not dlc_dir:
        return None
    path = Path(dlc_dir)
    return path if path.exists() else None


def _local_provider():
    if _library_providers is None:
        return None
    try:
        return _library_providers.get("local") if hasattr(_library_providers, "get") else None
    except Exception:
        return None


def _local_provider_method(method_name: str):
    provider = _local_provider()
    if provider is None:
        return None
    if hasattr(_library_providers, "provider_method"):
        try:
            method = _library_providers.provider_method(provider, method_name)
            if callable(method):
                return method
        except Exception:
            pass
    method = getattr(provider, method_name, None)
    return method if callable(method) else None


def _call_local_provider(method_name: str, **kwargs):
    method = _local_provider_method(method_name)
    if not callable(method):
        raise HTTPException(status_code=503, detail="Local library provider is unavailable")
    return method(**kwargs)


async def _call_local_provider_async(method_name: str, **kwargs):
    result = _call_local_provider(method_name, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _remote_song_id_for_relative(relative_name: str) -> str:
    encoded = base64.urlsafe_b64encode(relative_name.encode("utf-8")).decode("ascii").rstrip("=")
    return f"song_{encoded}"


def _relative_for_remote_song_id(song_id: str) -> str | None:
    raw = str(song_id or "")
    if not raw.startswith("song_"):
        return None
    encoded = raw[5:]
    try:
        padding = "=" * (-len(encoded) % 4)
        relative_name = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except Exception:
        return None
    path = Path(relative_name)
    if not relative_name or path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _package_path_for_relative(relative_name: str) -> Path | None:
    root = _local_library_root()
    if not root:
        return None
    try:
        package_path = (root / relative_name).resolve()
        package_path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return package_path if package_path.exists() and (package_path.is_file() or package_path.is_dir()) else None


def _csv_values(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _parse_has_lyrics(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "with", "lyrics"}:
        return 1
    if normalized in {"0", "false", "no", "without", "none"}:
        return 0
    return None


def _query_filters(
    *,
    q: str = "",
    format_filter: str = "",
    arrangements_has: str = "",
    arrangements_lacks: str = "",
    stems_has: str = "",
    stems_lacks: str = "",
    has_lyrics: str = "",
    tunings: str = "",
) -> dict:
    return {
        "q": q,
        "format_filter": format_filter,
        "arrangements_has": _csv_values(arrangements_has),
        "arrangements_lacks": _csv_values(arrangements_lacks),
        "stems_has": _csv_values(stems_has),
        "stems_lacks": _csv_values(stems_lacks),
        "has_lyrics": _parse_has_lyrics(has_lyrics),
        "tunings": _csv_values(tunings),
    }


def _package_form_for_song(song: dict) -> PackageForm:
    relative_name = str(song.get("filename") or "")
    fmt = str(song.get("format") or "").lower()
    suffix = Path(relative_name).suffix.lower()
    package_path = _package_path_for_relative(relative_name) if relative_name else None
    if package_path and package_path.is_dir() and suffix == ".sloppak":
        return PackageForm.SLOPPAK_DIRECTORY
    if fmt == "psarc" or suffix == ".psarc":
        return PackageForm.PSARC_FILE
    if fmt == "sloppak" or suffix in {".sloppak", ".zip"}:
        return PackageForm.SLOPPAK_ZIP
    return PackageForm.UNSUPPORTED


def _package_form_for_relative(relative_name: str) -> str:
    suffix = Path(relative_name or "").suffix.lower()
    if suffix == ".psarc":
        return "psarc"
    if suffix in {".sloppak", ".zip"}:
        return "sloppak"
    return "unsupported"


def _remote_summary_from_local_song(song: dict) -> dict:
    has_artwork = _local_provider_has_art()
    relative_name = str(song.get("filename") or "")
    if not relative_name:
        raise ValueError("local library song is missing filename")
    package_form = _package_form_for_song(song)
    downloadable_package = _package_path_for_relative(relative_name)
    syncable = bool(downloadable_package and downloadable_package.is_file()) and package_form in {
        PackageForm.PSARC_FILE,
        PackageForm.SLOPPAK_ZIP,
    }
    remote_song_id = _remote_song_id_for_relative(relative_name)
    identity = f"{_source_id()}:{relative_name}".encode("utf-8")
    capabilities = ["package-download"] if syncable else []
    if has_artwork:
        capabilities.insert(0, "artwork")
    if _nam_tone_sharing_enabled():
        capabilities.append("nam-tone-sync")
    summary = RemoteSongSummary(
        source_id=_source_id(),
        remote_song_id=remote_song_id,
        title=song.get("title") or Path(relative_name).stem,
        artist=song.get("artist") or "",
        album=song.get("album") or "",
        year=_coerce_int(song.get("year")),
        duration=_coerce_float(song.get("duration")),
        format=song.get("format") or ("psarc" if package_form == PackageForm.PSARC_FILE else "sloppak"),
        package_form=package_form,
        manifest_hash=sha256_hex(identity),
        package_hash="",
        size_bytes=_coerce_int(song.get("sizeBytes") or song.get("size") or song.get("size_bytes")) or 0,
        artwork_thumb_hash=sha256_hex(f"art:{_source_id()}:{relative_name}".encode("utf-8")) if has_artwork else None,
        arrangements=list(song.get("arrangements") or []),
        has_lyrics=bool(song.get("has_lyrics", song.get("hasLyrics", False))),
        stem_count=_coerce_int(song.get("stem_count", song.get("stemCount"))) or 0,
        stem_ids=list(song.get("stem_ids") or song.get("stemIds") or []),
        tuning=song.get("tuning") or song.get("tuning_name") or song.get("tuningName") or "",
        capabilities=capabilities,
        sync_support=SyncSupport.SYNCABLE if syncable else SyncSupport.NOT_SYNCABLE,
        status=RemoteSongStatus.REMOTE_ONLY if syncable else RemoteSongStatus.NOT_SYNCABLE,
    ).to_dict()
    summary["settingsKey"] = _playback_settings_key(relative_name)
    summary.pop("packageHash", None)
    if has_artwork:
        summary["artworkUrl"] = f"/songs/{remote_song_id}/art"
    if syncable:
        summary["packageUrl"] = f"/songs/{remote_song_id}/package"
    if _nam_tone_sharing_enabled():
        summary["namToneSyncUrl"] = f"/songs/{remote_song_id}/nam-tone-sync"
    return summary


def _summaries_from_songs(songs: list[dict]) -> list[dict]:
    # A single malformed song (e.g. one missing its filename) must not 500 an entire
    # /songs or /artists page — skip the ones that cannot be summarized.
    summaries = []
    for song in songs:
        try:
            summaries.append(_remote_summary_from_local_song(song))
        except ValueError:
            continue
    return summaries


def _config_root() -> Path:
    if _config_dir is not None:
        return _config_dir
    if _store is not None:
        return _store.root.parent
    return Path(os.environ.get("CONFIG_DIR") or ".")


def _nam_tone_sharing_enabled() -> bool:
    return bool(_settings().get("shareNamToneAssets"))


def _nam_db_path() -> Path:
    return _config_root() / "nam_tone.db"


def _nam_models_dir() -> Path:
    return _config_root() / "nam_models"


def _nam_irs_dir() -> Path:
    return _config_root() / "nam_irs"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _safe_child(root: Path, name: str | None) -> Path | None:
    if not name:
        return None
    root_resolved = root.resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError:
        return None
    return path


def _nam_mapping_filenames(filename: str) -> list[str]:
    names = [filename]
    normalized = filename.replace("\\", "/")
    if normalized.startswith("sloppak/") and normalized.lower().endswith(".sloppak"):
        psarc_name = f"{Path(normalized).stem}_p.psarc"
        if psarc_name not in names:
            names.append(psarc_name)
    return names


def _empty_nam_tone_sync_payload(
    relative_name: str,
    song_id: str,
    warnings: list[str] | None = None,
    settings_key: str = "",
) -> dict:
    return {
        "schema": NAM_TONE_SYNC_SCHEMA,
        "sourceId": _source_id(),
        "remoteSongId": song_id,
        "sourceFilename": relative_name,
        "sourceSettingsKey": settings_key,
        "mappings": [],
        "presets": [],
        "warnings": warnings or [],
    }


def _nam_asset_metadata(asset_type: str, name: str | None, song_id: str, warnings: list[str]) -> dict | None:
    if not name:
        return None
    root = _nam_models_dir() if asset_type == "model" else _nam_irs_dir()
    path = _safe_child(root, name)
    if path is None:
        warnings.append(f"Preset references an invalid {asset_type} file path: {name}")
        return None
    if not path.exists() or not path.is_file():
        warnings.append(f"Preset references a missing {asset_type} file: {name}")
        return None
    stat = path.stat()
    return {
        "type": asset_type,
        "name": name,
        "sizeBytes": stat.st_size,
        "sha256": _sha256_file(path),
        "url": f"/songs/{parse.quote(song_id, safe='')}/nam-tone-assets/{asset_type}/{parse.quote(name, safe='')}",
    }


def _nam_tone_sync_payload(relative_name: str, song_id: str) -> dict:
    if not _nam_tone_sharing_enabled():
        raise HTTPException(status_code=403, detail="NAM tone asset sharing is disabled")
    settings_key = _playback_settings_key(relative_name)
    db_path = _nam_db_path()
    if not db_path.exists():
        return _empty_nam_tone_sync_payload(relative_name, song_id, settings_key=settings_key)
    filenames = [settings_key, *_nam_mapping_filenames(relative_name)]
    placeholders = ",".join("?" for _ in filenames)
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT tm.tone_key, p.id, p.name, p.model_file, p.ir_file, "
                "p.input_gain, p.output_gain, p.gate_threshold, p.settings_json "
                "FROM tone_mappings tm JOIN presets p ON tm.preset_id = p.id "
                f"WHERE tm.filename IN ({placeholders}) "
                "ORDER BY CASE tm.filename WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END, tm.tone_key",
                (*filenames, settings_key, relative_name),
            ).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="NAM tone database is unavailable") from exc

    warnings: list[str] = []
    mappings: list[dict] = []
    presets_by_ref: dict[str, dict] = {}
    seen_tones: set[str] = set()
    for row in rows:
        tone_key = str(row[0] or "")
        if not tone_key or tone_key in seen_tones:
            continue
        seen_tones.add(tone_key)
        preset_ref = f"preset:{row[1]}"
        mappings.append({"toneKey": tone_key, "presetRef": preset_ref})
        if preset_ref in presets_by_ref:
            continue
        settings = {}
        try:
            settings = json.loads(row[8] or "{}")
            if not isinstance(settings, dict):
                settings = {}
        except json.JSONDecodeError:
            warnings.append(f"Preset {row[2] or row[1]} has invalid settings JSON")
        presets_by_ref[preset_ref] = {
            "ref": preset_ref,
            "name": str(row[2] or ""),
            "modelFile": _nam_asset_metadata("model", row[3], song_id, warnings),
            "irFile": _nam_asset_metadata("ir", row[4], song_id, warnings),
            "inputGain": float(row[5] if row[5] is not None else 1.0),
            "outputGain": float(row[6] if row[6] is not None else 0.5),
            "gateThreshold": float(row[7] if row[7] is not None else -60.0),
            "settings": settings,
        }

    return {
        **_empty_nam_tone_sync_payload(relative_name, song_id, warnings, settings_key),
        "targetSettingsKey": settings_key,
        "mappings": mappings,
        "presets": list(presets_by_ref.values()),
    }


def _nam_tone_asset_path(asset_type: str, name: str) -> Path | None:
    if asset_type == "model":
        return _safe_child(_nam_models_dir(), name)
    if asset_type == "ir":
        return _safe_child(_nam_irs_dir(), name)
    return None


def _nam_tone_asset_referenced(payload: dict, asset_type: str, name: str) -> bool:
    field = "modelFile" if asset_type == "model" else "irFile"
    return any((preset.get(field) or {}).get("name") == name for preset in payload.get("presets") or [])


def _remote_artists_from_local_artists(artists: list[dict]) -> list[dict]:
    remote_artists = []
    for artist in artists:
        albums = []
        for album in artist.get("albums") or []:
            songs = _summaries_from_songs(album.get("songs") or [])
            albums.append({**album, "songs": songs})
        remote_artists.append({**artist, "albums": albums})
    return remote_artists


def _coerce_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _coerce_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _local_song_count() -> int:
    stats = _call_local_provider("query_stats", **_query_filters())
    try:
        return int((stats or {}).get("total_songs") or 0)
    except (TypeError, ValueError):
        return 0


def _library_art_response(result: Any) -> Response:
    if result is None:
        raise HTTPException(status_code=404, detail="artwork not found")
    if isinstance(result, Response):
        return result
    if isinstance(result, (bytes, bytearray, memoryview)):
        return Response(content=bytes(result), media_type="image/png")
    if isinstance(result, (str, Path)):
        return FileResponse(str(result))
    if isinstance(result, dict):
        url = result.get("url") or result.get("art_url") or result.get("artUrl")
        if isinstance(url, str) and url:
            if parse.urlparse(url).scheme not in {"http", "https"}:
                raise HTTPException(status_code=400, detail="unsupported artwork URL")
            return RedirectResponse(url)
        path = result.get("path") or result.get("file")
        if isinstance(path, (str, Path)):
            return FileResponse(str(path), media_type=result.get("media_type") or result.get("content_type"))
        content = result.get("content") or result.get("bytes")
        if isinstance(content, (bytes, bytearray, memoryview)):
            media_type = result.get("media_type") or result.get("content_type") or "image/png"
            return Response(content=bytes(content), media_type=media_type)
    raise HTTPException(status_code=500, detail="unsupported artwork response")


def _source_payload() -> dict:
    capabilities = ["library.read", "song.sync"]
    if _local_provider_has_art():
        capabilities.insert(1, "art.read")
    if _nam_tone_sharing_enabled():
        capabilities.append("nam-tone-sync.read")
    return {
        "ok": True,
        "sourceId": _source_id(),
        "sourceName": _source_name(),
        "songCount": _local_song_count(),
        "capabilities": capabilities,
        "namToneSync": {"enabled": _nam_tone_sharing_enabled()},
        "auth": {"required": bool(_auth_token())},
        "server": {
            "url": _direct_url(),
            "protocol": "slopsmith-direct-library.v1",
        },
    }


def _query_page_payload(
    *,
    q: str = "",
    page_size: int = 50,
    cursor: str | None = None,
    page: int = 0,
    sort: str = "artist",
    direction: str = "asc",
    format_filter: str = "",
    arrangements_has: str = "",
    arrangements_lacks: str = "",
    stems_has: str = "",
    stems_lacks: str = "",
    has_lyrics: str = "",
    tunings: str = "",
) -> dict:
    try:
        offset = int(cursor or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="cursor must be a non-negative integer offset") from exc
    if offset < 0:
        raise HTTPException(status_code=400, detail="cursor must be a non-negative integer offset")
    if offset % page_size != 0:
        raise HTTPException(status_code=400, detail="cursor must be a non-negative integer offset aligned to pageSize")
    page_number = max(0, int(page or 0)) if not cursor else offset // page_size
    songs, total = _call_local_provider(
        "query_page",
        page=page_number,
        size=page_size,
        sort=sort,
        direction=direction,
        **_query_filters(
            q=q,
            format_filter=format_filter,
            arrangements_has=arrangements_has,
            arrangements_lacks=arrangements_lacks,
            stems_has=stems_has,
            stems_lacks=stems_lacks,
            has_lyrics=has_lyrics,
            tunings=tunings,
        ),
    )
    # Base the next cursor on the page actually served (page_number), not on the raw
    # cursor offset — otherwise paginating via ?page= always advertised nextCursor=pageSize.
    next_offset = (page_number + 1) * page_size
    return {
        "source": _source_payload(),
        "songs": _summaries_from_songs(songs),
        "total": int(total or 0),
        "nextCursor": str(next_offset) if next_offset < int(total or 0) else None,
        "query": {
            "page": page_number,
            "pageSize": page_size,
            "sort": sort,
            "direction": direction,
            "filtersApplied": True,
        },
    }


def _create_direct_app() -> FastAPI:
    direct_app = FastAPI(title="FeedBack Remote Library Direct Server", version=_plugin_version())
    protected = [Depends(_require_auth)]

    @direct_app.get("/health")
    def health() -> dict:
        return {"ok": True, "sourceId": _source_id()}

    @direct_app.get("/source", dependencies=protected)
    def source() -> dict:
        return _source_payload()

    @direct_app.get("/songs", dependencies=protected)
    def songs(
        q: str = Query("", max_length=1000),
        pageSize: int = Query(50, ge=1, le=500),
        cursor: str | None = None,
        page: int = 0,
        sort: str = "artist",
        direction: str = "asc",
        format: str = "",
        arrangements_has: str = "",
        arrangements_lacks: str = "",
        stems_has: str = "",
        stems_lacks: str = "",
        has_lyrics: str = "",
        tunings: str = "",
    ) -> dict:
        return _query_page_payload(
            q=q,
            page_size=pageSize,
            cursor=cursor,
            page=page,
            sort=sort,
            direction=direction,
            format_filter=format,
            arrangements_has=arrangements_has,
            arrangements_lacks=arrangements_lacks,
            stems_has=stems_has,
            stems_lacks=stems_lacks,
            has_lyrics=has_lyrics,
            tunings=tunings,
        )

    @direct_app.get("/artists", dependencies=protected)
    def artists(
        letter: str = "",
        q: str = Query("", max_length=1000),
        pageSize: int = Query(50, ge=1, le=100),
        page: int = 0,
        format: str = "",
        arrangements_has: str = "",
        arrangements_lacks: str = "",
        stems_has: str = "",
        stems_lacks: str = "",
        has_lyrics: str = "",
        tunings: str = "",
    ) -> dict:
        local_artists, total = _call_local_provider(
            "query_artists",
            letter=letter,
            page=max(0, int(page or 0)),
            size=pageSize,
            **_query_filters(
                q=q,
                format_filter=format,
                arrangements_has=arrangements_has,
                arrangements_lacks=arrangements_lacks,
                stems_has=stems_has,
                stems_lacks=stems_lacks,
                has_lyrics=has_lyrics,
                tunings=tunings,
            ),
        )
        return {
            "artists": _remote_artists_from_local_artists(local_artists),
            "total_artists": int(total or 0),
            "query": {"page": page, "pageSize": pageSize, "filtersApplied": True},
        }

    @direct_app.get("/stats", dependencies=protected)
    def stats(
        q: str = Query("", max_length=1000),
        format: str = "",
        arrangements_has: str = "",
        arrangements_lacks: str = "",
        stems_has: str = "",
        stems_lacks: str = "",
        has_lyrics: str = "",
        tunings: str = "",
    ) -> dict:
        result = _call_local_provider(
            "query_stats",
            **_query_filters(
                q=q,
                format_filter=format,
                arrangements_has=arrangements_has,
                arrangements_lacks=arrangements_lacks,
                stems_has=stems_has,
                stems_lacks=stems_lacks,
                has_lyrics=has_lyrics,
                tunings=tunings,
            ),
        )
        return {**result, "query": {"filtersApplied": True}}

    @direct_app.get("/tuning-names", dependencies=protected)
    def tuning_names() -> dict:
        return _call_local_provider("tuning_names")

    @direct_app.get("/songs/{song_id}/art", dependencies=protected)
    async def song_art(song_id: str) -> Response:
        relative_name = _relative_for_remote_song_id(song_id)
        if not relative_name:
            raise HTTPException(status_code=404, detail="song not found")
        result = await _call_local_provider_async("get_art", song_id=relative_name)
        response = _library_art_response(result)
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
        return response

    @direct_app.get("/songs/{song_id}/package", dependencies=protected)
    def song_package(song_id: str) -> FileResponse:
        relative_name = _relative_for_remote_song_id(song_id)
        package_path = _package_path_for_relative(relative_name or "") if relative_name else None
        if not package_path or not package_path.is_file():
            raise HTTPException(status_code=404, detail="package not found")
        try:
            return FileResponse(
                package_path,
                media_type="application/octet-stream",
                filename=package_path.name,
                headers={"X-Slopsmith-Remote-Song-Id": song_id},
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="package not found") from exc

    @direct_app.get("/songs/{song_id}/nam-tone-sync", dependencies=protected)
    def song_nam_tone_sync(song_id: str) -> dict:
        relative_name = _relative_for_remote_song_id(song_id)
        if not relative_name or not _package_path_for_relative(relative_name):
            raise HTTPException(status_code=404, detail="song not found")
        return _nam_tone_sync_payload(relative_name, song_id)

    @direct_app.get("/songs/{song_id}/nam-tone-assets/{asset_type}/{name:path}", dependencies=protected)
    def song_nam_tone_asset(song_id: str, asset_type: str, name: str) -> FileResponse:
        if asset_type not in {"model", "ir"}:
            raise HTTPException(status_code=404, detail="asset not found")
        relative_name = _relative_for_remote_song_id(song_id)
        if not relative_name or not _package_path_for_relative(relative_name):
            raise HTTPException(status_code=404, detail="song not found")
        payload = _nam_tone_sync_payload(relative_name, song_id)
        path = _nam_tone_asset_path(asset_type, name)
        if (
            path is None
            or not path.exists()
            or not path.is_file()
            or not _nam_tone_asset_referenced(payload, asset_type, name)
        ):
            raise HTTPException(status_code=404, detail="asset not found")
        media_type = "application/json" if asset_type == "model" else "audio/wav"
        try:
            return FileResponse(str(path), media_type=media_type, filename=path.name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="asset not found") from exc

    return direct_app


def _is_direct_server_running() -> bool:
    return bool(_direct_thread and _direct_thread.is_alive())


def _ensure_bindable(host: str, port: int) -> None:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError as exc:
        raise ValueError(f"cannot bind direct server on {host}:{port}: {exc}") from exc
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        with socket.socket(family, socktype, proto) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(sockaddr)
                return
            except OSError as exc:
                last_error = exc
    if last_error is not None:
        raise ValueError(f"cannot bind direct server on {host}:{port}: {last_error}") from last_error
    raise ValueError(f"cannot bind direct server on {host}:{port}: no bind address found")


def _start_direct_server() -> dict:
    global _direct_server, _direct_thread
    with _server_lock:
        if _is_direct_server_running():
            return _server_status()
        host = _bind_host()
        port = _bind_port()
        _ensure_bindable(host, port)
        import uvicorn

        config = uvicorn.Config(_create_direct_app(), host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="remote-library-direct-server", daemon=True)
        thread.start()
        # Confirm uvicorn actually bound before reporting success: the pre-bind probe in
        # _ensure_bindable only proves the port was free a moment earlier, and any real bind
        # failure would otherwise be swallowed inside this daemon thread.
        deadline = time.monotonic() + 5.0
        while not getattr(server, "started", False) and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not getattr(server, "started", False):
            server.should_exit = True
            thread.join(timeout=3)
            raise ValueError(f"direct server failed to start on {host}:{port}")
        _direct_server = server
        _direct_thread = thread
    if _store:
        _store.add_activity("direct-server", "started", f"Direct server started on {host}:{port}")
    return _server_status()


def _stop_direct_server() -> dict:
    global _direct_server, _direct_thread
    with _server_lock:
        if _direct_server is not None:
            _direct_server.should_exit = True
        if _direct_thread is not None and _direct_thread.is_alive():
            _direct_thread.join(timeout=3)
            if _direct_thread.is_alive():
                return _server_status()
        _direct_server = None
        _direct_thread = None
    if _store:
        _store.add_activity("direct-server", "stopped", "Direct server stopped")
    return _server_status()


def _restart_direct_server() -> dict:
    # Hold the reentrant lock across the whole stop+start so a concurrent start or
    # settings-save can't slip into the window where the server is momentarily stopped.
    with _server_lock:
        _stop_direct_server()
        return _start_direct_server()


def _autostart_after_scan() -> None:
    while _settings().get("enabled") and not _scan_ready():
        if _shutdown_requested.wait(timeout=1):
            return
    if _shutdown_requested.is_set() or not _settings().get("enabled") or _is_direct_server_running():
        return
    try:
        _start_direct_server()
    except ValueError as exc:
        if _store:
            _store.add_activity("direct-server", "failed", str(exc))


def _schedule_autostart_after_scan() -> None:
    global _autostart_thread
    if _shutdown_requested.is_set():
        return
    if not _settings().get("enabled") or _is_direct_server_running():
        return
    if _scan_ready():
        try:
            _start_direct_server()
        except ValueError as exc:
            if _store:
                _store.add_activity("direct-server", "failed", str(exc))
        return
    if _autostart_thread and _autostart_thread.is_alive():
        return
    if _store:
        _store.add_activity("direct-server", "waiting", "Autostart waiting for library scan to finish")
    _autostart_thread = threading.Thread(target=_autostart_after_scan, name="remote-library-autostart", daemon=True)
    _autostart_thread.start()


def _shutdown() -> None:
    _shutdown_requested.set()
    _stop_direct_server()


def _server_status() -> dict:
    return {
        "running": _is_direct_server_running(),
        "waitingForScan": bool(_settings().get("enabled") and not _is_direct_server_running() and not _scan_ready()),
        "host": _bind_host(),
        "port": _bind_port(),
        "url": _direct_url(),
        "protocol": "slopsmith-direct-library.v1",
        "authRequired": bool(_auth_token()),
    }


def setup(app, context):
    global _store, _get_dlc_dir, _library_providers, _get_scan_status, _config_dir
    _shutdown_requested.clear()
    _config_dir = Path(context["config_dir"])
    _store = RemoteLibraryServerStore(_config_dir)
    _get_dlc_dir = context.get("get_dlc_dir")
    _library_providers = context.get("library_providers")
    _get_scan_status = context.get("get_scan_status")
    app.router.on_shutdown.append(_shutdown)
    if _settings().get("enabled"):
        _schedule_autostart_after_scan()

    @app.get("/api/plugins/remote_library_server/settings")
    def get_settings():
        return _settings()

    @app.post("/api/plugins/remote_library_server/settings")
    def save_settings(data: dict):
        settings = _store.save_settings(_normalize_settings(data))
        try:
            if not settings.get("enabled"):
                server = _stop_direct_server()
            elif _is_direct_server_running():
                server = _restart_direct_server()
            else:
                _schedule_autostart_after_scan()
                server = _server_status()
        except ValueError as exc:
            _store.add_activity("direct-server", "failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**settings, "server": server}

    @app.get("/api/plugins/remote_library_server/status")
    def status():
        root = _local_library_root()
        return {
            "source": {
                "sourceId": _source_id(),
                "sourceName": _source_name(),
                "songCount": _local_song_count(),
                "libraryRootConfigured": bool(root),
            },
            "server": _server_status(),
            "settings": _settings(),
            "scan": _scan_status(),
            "defaults": {
                "host": "127.0.0.1",
                "port": 8765,
                "sourceName": _default_source_name(),
            },
        }

    @app.post("/api/plugins/remote_library_server/start")
    def start_server():
        try:
            return {"server": _start_direct_server(), "settings": _settings()}
        except ValueError as exc:
            _store.add_activity("direct-server", "failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/plugins/remote_library_server/stop")
    def stop_server():
        return {"server": _stop_direct_server(), "settings": _settings()}

    @app.get("/api/plugins/remote_library_server/local-songs")
    def local_songs(
        q: str = Query("", max_length=1000),
        pageSize: int = Query(50, ge=1, le=500),
        cursor: str | None = None,
    ):
        return _query_page_payload(q=q, page_size=pageSize, cursor=cursor)

    @app.get("/api/plugins/remote_library_server/activity")
    def activity():
        return {"events": _store.list_activity()}

    return app