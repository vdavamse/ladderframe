from pathlib import Path

import pytest

from ladderframe.config import ConfigError, expand_env_vars, load_config
from ladderframe.config.loader import deep_merge

ETC = Path(__file__).parent / "fixtures" / "rootfs" / "etc"


def test_env_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_SET", "yes")
    monkeypatch.delenv("LF_UNSET", raising=False)
    missing: set[str] = set()
    value = expand_env_vars({"a": "${LF_SET}", "b": ["${LF_UNSET:-dflt}", "x${LF_UNSET}y"]}, missing)
    assert value == {"a": "yes", "b": ["dflt", "xy"]}
    assert missing == {"LF_UNSET"}


def test_deep_merge_replaces_lists() -> None:
    merged = deep_merge({"a": {"x": 1, "y": [1, 2]}, "b": 1}, {"a": {"y": [3]}, "c": 2})
    assert merged == {"a": {"x": 1, "y": [3]}, "b": 1, "c": 2}


def test_defaults_and_root_config() -> None:
    config = load_config(ETC)
    assert config.name == "fixture"
    assert config.tool_settings["Bash"]["timeout_ms"] == 120000  # from defaults.yaml
    assert config.resolve_model("sonnet") == "anthropic:claude-sonnet-5-5"
    assert config.resolve_model("claude-opus-5-5") == "anthropic:claude-opus-5-5"


def test_profile_overlay() -> None:
    config = load_config(ETC, profile="strict")
    assert config.permissions.default == "deny"
    assert config.permissions.allow == ["Read", "Echo"]
    assert config.permissions.deny == ["Bash(rm *)"]  # untouched keys survive the overlay


def test_missing_profile_is_an_error() -> None:
    with pytest.raises(ConfigError):
        load_config(ETC, profile="nope")


def test_validation_errors_do_not_print_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_SECRET", "sk-live-123")
    (tmp_path / "ladderframe.yaml").write_text("name: x\n")
    (tmp_path / "ladderframe.bad.yaml").write_text("server:\n  api_keys: ${LF_SECRET}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path, profile="bad")
    message = str(excinfo.value)
    assert "sk-live-123" not in message and excinfo.value.__cause__ is None
    assert "ladderframe.bad.yaml" in message and "server.api_keys" in message


def test_check_refuses_web_protocol_with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from ladderframe import Runtime
    from ladderframe.core.check import check_runtime

    monkeypatch.chdir(ETC.parent)
    runtime = Runtime.load(ETC.parent)
    runtime.config.server.auth = "api-key"
    runtime.config.server.api_keys = ["k"]
    runtime.config.server.protocols = ["web"]
    assert any("without authentication" in error for error in check_runtime(runtime).errors)
