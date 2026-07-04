from __future__ import annotations

import json

from remote_library_server.store import RemoteLibraryServerStore


def test_save_settings_merges_updates_under_store_lock(tmp_path):
    store = RemoteLibraryServerStore(tmp_path)

    first = store.save_settings({"enabled": True, "sourceName": "Studio"})
    second = store.save_settings({"port": 9876})

    assert first["enabled"] is True
    assert second["enabled"] is True
    assert second["sourceName"] == "Studio"
    assert second["port"] == 9876


def test_add_activity_appends_and_trims_under_store_lock(tmp_path):
    store = RemoteLibraryServerStore(tmp_path)

    for index in range(205):
        store.add_activity("direct-server", "event", f"event-{index}")

    state = json.loads(store.state_path.read_text())
    assert len(state["activity"]) == 200
    assert state["activity"][0]["message"] == "event-5"
    assert state["activity"][-1]["message"] == "event-204"