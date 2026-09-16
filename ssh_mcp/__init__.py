"""Public API for ssh-mcp.

This package supports three entry styles:

* plain Python usage    - ``from ssh_mcp import execute_remote_commands``
* CLI                   - ``ssh-mcp execute ...``
* MCP stdio server      - ``python -m ssh_mcp.mcp_server``
"""

from .client import download_file, execute_remote_commands, run, upload_directory
from .server import RemoteClient

__version__ = "0.4.2"

__all__ = [
    "__version__",
    "RemoteClient",
    "upload_directory",
    "download_file",
    "execute_remote_commands",
    "run",
]
