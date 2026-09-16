import importlib
import os


def test_ssh_mcp_package_is_importable():
    module = importlib.import_module("ssh_mcp")
    assert hasattr(module, "upload_directory")
    assert hasattr(module, "download_file")
    assert hasattr(module, "execute_remote_commands")
    assert hasattr(module, "RemoteClient")
    assert module.__version__


def test_package_imports_regardless_of_cwd():
    """Guards the regression that broke the MCP server: a package must resolve
    by name, never by the process working directory."""
    original = os.getcwd()
    try:
        os.chdir("/")
        for name in (
            "ssh_mcp",
            "ssh_mcp.client",
            "ssh_mcp.server",
            "ssh_mcp.mcp_server",
        ):
            importlib.import_module(name)
    finally:
        os.chdir(original)
