"""The site config: validation, reloading with a last good version, and the schemas."""

from pathlib import Path

import pytest
from conftest import MakePanel, site_config
from pydantic import ValidationError

from aipanel import yamlio
from aipanel.cli import main
from aipanel.config import schema
from aipanel.config.loader import ConfigError, SiteConfigLoader, parse
from aipanel.config.site import SecretRef, SiteConfig

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "config.yaml"


def example() -> dict[str, object]:
    data = yamlio.load(EXAMPLE.read_text())
    assert isinstance(data, dict)
    return data


def test_the_example_config_is_valid() -> None:
    config = parse(EXAMPLE.read_text(), demo=False)
    assert config.site.name == "gpu-1" and config.protected_windows[0].start == "02:00"
    assert config.auth.groups_separator == "|"


def test_example_uses_documentation_addresses_only() -> None:
    text = EXAMPLE.read_text()
    for host in ("192.0.2.10", "192.0.2.20", "example.com"):
        assert host in text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"surprise": 1}, "Extra inputs are not permitted"),
        ({"auth": {"mode": "trusted_header", "allowed_groups": []}}, "at least one entry"),
        (
            {
                "bmc": {
                    "host": "192.0.2.10",
                    "username": {"env": "U", "file": "/f"},
                    "password": {"env": "P"},
                }
            },
            "exactly one",
        ),
        ({"site": {"name": "gpu-1", "timezone": "Mars/Olympus"}}, "unknown time zone"),
        ({"site": {"name": "gpu 1; reboot"}}, "String should match pattern"),
        ({"actions": {"enabled": ["format_disk"]}}, "Input should be"),
        ({"prometheus": None}, "no prometheus section"),
    ],
)
def test_invalid_configs_are_refused(change: dict[str, object], message: str) -> None:
    data = example() | change
    if change.get("prometheus", 0) is None:  # cost reads Prometheus: dropping it is an error
        data["cost"] = {
            "power": {"source": "prometheus", "query": "x"},
            "price": {"source": "prometheus", "query": "y"},
        }
    with pytest.raises(ValidationError, match=message):
        SiteConfig.model_validate(data)


def test_llm_guest_must_be_listed() -> None:
    data = example()
    data["host"] = {**data["host"], "llm_guest": 999}  # type: ignore[dict-item]
    with pytest.raises(ValidationError, match="llm_guest must be one of guests"):
        SiteConfig.model_validate(data)


def test_auth_none_only_in_demo_mode() -> None:
    text = EXAMPLE.read_text().replace("mode: trusted_header", "mode: none")
    with pytest.raises(ConfigError, match="only in DEMO mode"):
        parse(text, demo=False)
    assert parse(text, demo=True).auth.mode == "none"


def test_yaml_on_off_and_times_stay_strings() -> None:
    data = yamlio.load("off: 23:00\non: 07:00\nflag: true\nn: 30\n")
    assert data == {"off": "23:00", "on": "07:00", "flag": True, "n": 30}


def test_secret_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    assert SecretRef(file=secret).read() == "s3cret"
    monkeypatch.setenv("AIPANEL_TEST_SECRET", "from-env")
    assert SecretRef(env="AIPANEL_TEST_SECRET").read() == "from-env"
    with pytest.raises(LookupError):
        SecretRef(env="AIPANEL_TEST_UNSET").read()


def test_reload_keeps_the_last_good_config(tmp_path: Path, make_panel: MakePanel) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE.read_text())
    loader = SiteConfigLoader(path, demo=False)
    assert loader.valid and not loader.reload_if_changed()

    path.write_text(EXAMPLE.read_text().replace("gpu-1", "gpu-2", 1))
    assert loader.reload_if_changed() and loader.current.site.name == "gpu-2"

    path.write_text(EXAMPLE.read_text() + "\nsurprise: true\n")
    assert not loader.reload_if_changed()
    assert not loader.valid and "surprise" in (loader.error or "")
    assert loader.current.site.name == "gpu-2"  # the last good one

    path.write_text(EXAMPLE.read_text())
    assert loader.reload_if_changed() and loader.valid


def test_invalid_config_drops_the_metric(make_panel: MakePanel, tmp_path: Path) -> None:
    panel = make_panel()
    path = tmp_path / "config.yaml"
    path.write_text("site: {name: gpu-demo}\n")
    panel.loader.path, panel.loader.error = path, None
    panel.watch()
    assert panel.metrics.get("aipanel_config_valid") == 0
    assert panel.site == site_config()  # unchanged


def test_no_config_no_start() -> None:
    with pytest.raises(ConfigError):
        SiteConfigLoader(None, demo=False)


def test_the_schemas_are_current() -> None:
    for name, content in schema.schemas().items():
        path = ROOT / "schema" / name
        assert path.read_text() == schema.render(content), (
            f"run: aipanel schema --out schema ({name})"
        )


def test_cli_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLE)]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("site: {name: x}\n")
    assert main(["validate", str(bad)]) == 1
    assert "not valid" in capsys.readouterr().err


def test_cli_schema_check(tmp_path: Path) -> None:
    assert main(["schema", "--out", str(tmp_path), "--check"]) == 1
    assert main(["schema", "--out", str(tmp_path)]) == 0
    assert main(["schema", "--out", str(tmp_path), "--check"]) == 0
