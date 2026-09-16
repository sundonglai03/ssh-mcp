"""Tests for connection resolution in ssh_mcp.config.

Connection details come from call arguments only; the environment must never
influence the result, so several tests deliberately set ``SSH_*`` variables and
assert they are ignored.
"""

import pytest

from ssh_mcp.config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_REMOTE_PATH,
    DEFAULT_TIMEOUT,
    resolve_connection,
)


@pytest.fixture(autouse=True)
def _hostile_env(monkeypatch, tmp_path):
    """Put misleading SSH_* values in the environment for every test."""
    monkeypatch.setenv("SSH_HOST", "env-host")
    monkeypatch.setenv("SSH_PORT", "2222")
    monkeypatch.setenv("SSH_USER", "env-user")
    monkeypatch.setenv("SSH_PASSWORD", "env-pass")
    monkeypatch.setenv("SSH_KEY_FILEPATH", "/env/key")
    monkeypatch.setenv("SSH_TIMEOUT", "7.5")
    monkeypatch.setenv("SSH_REMOTE_PATH", "/env/path")


def test_arguments_supply_every_value():
    settings = resolve_connection(
        host="10.0.0.5",
        user="root",
        password="pw",
        ssh_key_filepath="/keys/id_ed25519",
        port=2200,
        timeout=12.5,
        remote_path="/srv",
    )

    assert settings.host == "10.0.0.5"
    assert settings.user == "root"
    assert settings.password == "pw"
    assert settings.ssh_key_filepath == "/keys/id_ed25519"
    assert settings.port == 2200
    assert settings.timeout == 12.5
    assert settings.remote_path == "/srv"


def test_environment_never_overrides_arguments():
    settings = resolve_connection(host="10.0.0.5", user="root", password="pw")

    assert settings.host == "10.0.0.5"
    assert settings.user == "root"
    assert settings.password == "pw"


def test_environment_never_supplies_missing_arguments():
    with pytest.raises(ValueError, match="host"):
        resolve_connection(user="root", password="pw")

    with pytest.raises(ValueError, match="user"):
        resolve_connection(host="10.0.0.5", password="pw")


def test_defaults_applied_for_optional_arguments():
    settings = resolve_connection(host="10.0.0.5", user="root")

    assert settings.port == DEFAULT_PORT
    assert settings.timeout == DEFAULT_TIMEOUT
    assert settings.remote_path == DEFAULT_REMOTE_PATH
    assert settings.password == ""
    assert settings.ssh_key_filepath == ""


def test_passwordless_connection_is_allowed_for_key_auth():
    settings = resolve_connection(
        host="10.0.0.5", user="root", ssh_key_filepath="/keys/id_ed25519"
    )

    assert settings.password == ""
    assert settings.ssh_key_filepath == "/keys/id_ed25519"


def test_missing_host_raises_actionable_error():
    with pytest.raises(ValueError, match="host 参数"):
        resolve_connection(user="root", password="pw")


def test_missing_user_raises_actionable_error():
    with pytest.raises(ValueError, match="user 参数"):
        resolve_connection(host="example.com", password="pw")


@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_blank_host_and_user_are_rejected(blank):
    with pytest.raises(ValueError, match="host"):
        resolve_connection(host=blank, user="root")

    with pytest.raises(ValueError, match="user"):
        resolve_connection(host="10.0.0.5", user=blank)


@pytest.mark.parametrize("bad_port", ["0", "70000", "abc", -1])
def test_invalid_port_is_rejected(bad_port):
    with pytest.raises(ValueError, match="port"):
        resolve_connection(host="h", user="u", password="p", port=bad_port)


@pytest.mark.parametrize("bad_timeout", ["0", "-3", "soon"])
def test_invalid_timeout_is_rejected(bad_timeout):
    with pytest.raises(ValueError, match="timeout"):
        resolve_connection(host="h", user="u", password="p", timeout=bad_timeout)


def test_connect_timeout_defaults_to_ten_seconds():
    settings = resolve_connection(host="h", user="u")

    assert settings.connect_timeout == DEFAULT_CONNECT_TIMEOUT


def test_connect_timeout_is_independent_of_the_command_timeout():
    """The two budgets must not be wired to the same value.

    Regression: the connection budget was a private constant, so a caller who
    could not reach a host was told to raise ``timeout`` -- which only bounds
    command execution and could never help.
    """
    settings = resolve_connection(host="h", user="u", timeout=99.0, connect_timeout=2.5)

    assert settings.timeout == 99.0
    assert settings.connect_timeout == 2.5


@pytest.mark.parametrize("bad", ["0", "-1", "soon"])
def test_invalid_connect_timeout_is_rejected(bad):
    with pytest.raises(ValueError, match="connect_timeout"):
        resolve_connection(host="h", user="u", password="p", connect_timeout=bad)


def test_values_are_stripped():
    settings = resolve_connection(host="  10.0.0.5  ", user=" root ", password="pw")

    assert settings.host == "10.0.0.5"
    assert settings.user == "root"


def test_connection_requires_explicit_host_and_user():
    with pytest.raises(ValueError, match="host"):
        resolve_connection(user="root")

    with pytest.raises(ValueError, match="user"):
        resolve_connection(host="10.0.0.5")
