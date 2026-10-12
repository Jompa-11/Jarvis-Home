import json

from conftest import make_config

from jarvis.core.settings import SettingsStore


def test_safe_defaults_from_config(tmp_path):
    from jarvis.config import JarvisConfig

    st = SettingsStore(JarvisConfig(data_dir=tmp_path), None).current
    assert st.dry_run_computer is True and st.dry_run_smart_home is True
    assert st.microphone_enabled is False and st.camera_enabled is False


def test_only_user_changes_are_persisted(tmp_path):
    path = tmp_path / "settings.json"
    cfg = make_config(tmp_path)
    store = SettingsStore(cfg, path)
    store.update({"dry_run_smart_home": True, "speak_replies": True})
    assert json.loads(path.read_text()) == {"dry_run_smart_home": True, "speak_replies": True}
    # Setting a value back to the configured default drops the override.
    store.update({"speak_replies": False})
    assert json.loads(path.read_text()) == {"dry_run_smart_home": True}


def test_config_edits_apply_when_not_overridden(tmp_path):
    path = tmp_path / "settings.json"
    SettingsStore(make_config(tmp_path), path).update({"local_only": True})
    # The user later turns computer dry-run off in the config file.
    reloaded = SettingsStore(make_config(tmp_path, computer={"dry_run": False, "enabled": True}), path)
    assert reloaded.current.dry_run_computer is False  # config change honoured
    assert reloaded.current.local_only is True  # UI override kept


def test_invalid_or_stale_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"active_profile": "deleted-person", "bogus": 1}))
    store = SettingsStore(make_config(tmp_path), path)
    assert store.current.active_profile == "owner" and store.overrides == {}
    path.write_text("{not json")
    assert SettingsStore(make_config(tmp_path), path).current.active_profile == "owner"
