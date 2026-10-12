import pytest

from jarvis.config import ConfigError, load_config


def test_defaults_without_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml", env={})
    assert cfg.server.host == "127.0.0.1"
    assert cfg.computer.dry_run is True
    assert cfg.privacy.microphone_enabled is False
    assert cfg.ai.routes["conversation"][0].provider == "anthropic"
    assert cfg.default_profile == "owner"


def test_example_config_is_valid():
    from jarvis.config import PROJECT_ROOT

    cfg = load_config(PROJECT_ROOT / "config" / "jarvis.example.toml", env={})
    assert cfg.homeassistant.url is None or cfg.homeassistant.url.startswith("http")


def test_invalid_toml(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("server = [", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(p, env={})


@pytest.mark.parametrize(
    "body, fragment",
    [
        ('[server]\nhost = "0.0.0.0"', "loopback"),
        ('default_profile = "nobody"', "default_profile"),
        ('[[profiles]]\nid = "a"\nname = "A"\nrole = "adult"\n', "owner"),
        ('[homeassistant]\nurl = "homeassistant.local:8123"', "http"),
        ('[ai.routes]\nsmart_home = []', "conversation"),
        ('[server]\nport = 70000', "port"),
    ],
)
def test_invalid_values_are_reported(tmp_path, body, fragment):
    p = tmp_path / "c.toml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p, env={})
    assert fragment in str(exc.value)


def test_env_overrides(tmp_path):
    cfg = load_config(tmp_path / "none.toml", env={"JARVIS_PORT": "9001", "HA_URL": "http://ha.local:8123/"})
    assert cfg.server.port == 9001
    assert cfg.homeassistant.url == "http://ha.local:8123"
