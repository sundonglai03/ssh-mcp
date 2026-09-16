"""Tests for how the client layer wires arguments down to ``RemoteClient``.

These guard the seam between argument resolution and the transport object: a
value that resolves correctly but never reaches ``RemoteClient`` is still a bug,
and nothing else in the suite would notice.
"""

import pytest

from ssh_mcp import client as client_module
from ssh_mcp.config import DEFAULT_CONNECT_TIMEOUT


class _CapturingClient:
    """Stands in for ``RemoteClient``, recording its constructor arguments."""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def execute_commands(self, commands, timeout=None):
        return "Command: uptime\nExit code: 0\nStdout:\nok\nStderr:\n<empty>"

    def close(self):
        pass


@pytest.fixture
def _capture_client(monkeypatch):
    _CapturingClient.last_kwargs = None
    monkeypatch.setattr(client_module, "RemoteClient", _CapturingClient)
    return _CapturingClient


def test_connect_timeout_reaches_the_remote_client(_capture_client):
    client_module.execute_remote_commands(
        ["uptime"], host="10.0.0.5", user="root", password="pw", connect_timeout=3.5
    )

    assert _capture_client.last_kwargs["connect_timeout"] == 3.5


def test_connect_timeout_falls_back_to_the_default(_capture_client):
    client_module.execute_remote_commands(
        ["uptime"], host="10.0.0.5", user="root", password="pw"
    )

    assert _capture_client.last_kwargs["connect_timeout"] == DEFAULT_CONNECT_TIMEOUT


def test_command_timeout_is_not_sent_as_the_connect_timeout(_capture_client):
    """``timeout`` bounds one command; it must not leak into the dial budget."""
    client_module.execute_remote_commands(
        ["uptime"], host="10.0.0.5", user="root", password="pw", timeout=99.0
    )

    assert _capture_client.last_kwargs["connect_timeout"] == DEFAULT_CONNECT_TIMEOUT
