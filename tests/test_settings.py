"""settings.json: saved atomically, validated (against the site config too), audited, exported
and imported, by the store and by the CLI."""

import json
import os
from pathlib import Path

import pytest
from conftest import site_config

from aipanel.audit import Audit
from aipanel.cli import main
from aipanel.config.settings import Settings
from aipanel.config.store import SettingsError, SettingsStore

RULE = {"name": "nights", "days": ["mon", "tue"], "off": "23:00", "on": "07:00"}


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    site = site_config()  # protects 21:00-22:30 every day
    return SettingsStore(tmp_path / "settings.json", Audit(tmp_path / "audit.jsonl"), lambda: site)


def test_no_file_is_the_defaults(store: SettingsStore) -> None:
    assert store.get() == Settings() and not store.path.exists()
    assert store.get().schedule.enabled is False  # the schedule starts disabled


def test_update_saves_atomically_and_audits(store: SettingsStore, tmp_path: Path) -> None:
    store.update({"schedule.enabled": True, "poll": {"interval_seconds": 20}}, "alice")
    saved = json.loads(store.path.read_text())
    assert saved["schedule"]["enabled"] is True and saved["poll"]["interval_seconds"] == 20
    assert sorted(os.listdir(tmp_path)) == ["audit.jsonl", "settings.json", "settings.lock"]
    entry = store.audit.read(1)[0]
    assert entry["event"] == "settings.changed" and entry["actor"] == "alice"
    assert entry["changes"] == {
        "poll.interval_seconds": {"from": 15, "to": 20},
        "schedule.enabled": {"from": False, "to": True},
    }


def test_invalid_change_saves_nothing(store: SettingsStore) -> None:
    store.update({"poll.interval_seconds": 30}, "alice")
    before = store.path.read_text()
    for bad in (
        {"poll.interval_seconds": 1},
        {"poll.surprise": 1},
        {"schedule.rules": [{**RULE, "off": "25:00"}]},
        {"schedule.rules": [RULE, RULE]},
    ):
        with pytest.raises(SettingsError):
            store.update(bad, "alice")
    assert store.path.read_text() == before and len(store.audit.read(10)) == 1


def test_off_window_may_not_overlap_a_protected_window(store: SettingsStore) -> None:
    with pytest.raises(SettingsError, match="overlaps the protected window 'nightly backups'"):
        store.update({"schedule.rules": [{**RULE, "off": "22:00"}]}, "alice")
    with pytest.raises(SettingsError, match="overlaps"):  # Sunday night into Monday's window
        store.update(
            {"schedule.rules": [{**RULE, "days": ["sun"], "off": "23:00", "on": "21:30"}]}, "alice"
        )
    store.update({"schedule.rules": [RULE]}, "alice")


def test_export_and_import(store: SettingsStore) -> None:
    store.update({"schedule.rules": [RULE], "cost.standby_watts": 12.5}, "alice")
    exported = store.export_yaml()
    assert "'on': 07:00" in exported or "'on': '07:00'" in exported
    store.update({"cost.standby_watts": 99}, "alice")
    store.replace({"schedule": {"rules": [RULE]}}, "agent")  # what is left out: defaults
    assert store.get().cost.standby_watts == 15
    entry = store.audit.read(1)[0]
    assert entry["event"] == "settings.imported" and entry["actor"] == "agent"


def test_external_writes_are_picked_up(store: SettingsStore, tmp_path: Path) -> None:
    other = SettingsStore(store.path, store.audit, store.site)  # the CLI, beside the server
    other.update({"display.theme": "dark"}, "cli:alice")
    assert store.reload_if_changed() and store.get().display.theme == "dark"
    store.update({"poll.interval_seconds": 20}, "alice")  # read-modify-write keeps the CLI's change
    assert json.loads(store.path.read_text())["display"]["theme"] == "dark"


def test_a_broken_file_keeps_the_last_good_settings(store: SettingsStore) -> None:
    store.update({"poll.interval_seconds": 25}, "alice")
    store.path.write_text("{not json")
    store.reload_if_changed()
    assert store.error and store.get().poll.interval_seconds == 25


def test_read_only_state_directory(tmp_path: Path) -> None:
    directory = tmp_path / "ro"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        store = SettingsStore(directory / "settings.json", Audit(None), lambda: None)
        assert not store.writable
        with pytest.raises(SettingsError, match="not writable"):
            store.update({"poll.interval_seconds": 20}, "alice")
    finally:
        directory.chmod(0o700)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AIPANEL_DEMO", "1")
    monkeypatch.setenv("AIPANEL_STATE_DIR", str(state))
    monkeypatch.delenv("AIPANEL_CONFIG", raising=False)
    return state


def test_cli_settings(cli_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rules = '[{name: nights, days: [mon], off: "23:00", on: "07:00"}]'
    assert (
        main(
            [
                "settings",
                "--actor",
                "agent",
                "set",
                "schedule.enabled=true",
                f"schedule.rules={rules}",
            ]
        )
        == 0
    )
    assert main(["settings", "get", "schedule.enabled"]) == 0
    assert "schedule.enabled = true" in capsys.readouterr().out
    assert main(["settings", "set", "poll.interval_seconds=1"]) == 1
    assert "not saved" in capsys.readouterr().err

    exported = tmp_path / "settings.yaml"
    assert main(["settings", "export", str(exported)]) == 0
    assert main(["settings", "set", "schedule.enabled=false"]) == 0
    assert main(["settings", "import", str(exported)]) == 0
    assert json.loads((cli_env / "settings.json").read_text())["schedule"]["enabled"] is True
    actors = {e["actor"] for e in Audit(cli_env / "audit.jsonl").read(10)}
    assert "cli:agent" in actors


def test_cli_refuses_an_overlap(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rules = '[{name: early, days: [mon], off: "21:00", on: "22:00"}]'
    assert main(["settings", "set", f"schedule.rules={rules}"]) == 1
    assert "protected window" in capsys.readouterr().err
