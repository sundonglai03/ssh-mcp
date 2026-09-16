"""MCP server wrapper for SSH operations.

Registers real MCP tools for command execution, directory upload and file
download while keeping the plain Python API available from the same package.

Design rules:

* **Errors are raised, not returned.** The MCP SDK turns a raised exception
  into ``CallToolResult(isError=True)``, so the calling model can tell that a
  call failed instead of reading a success wrapper around an error string.
  Failures are raised as :class:`~mcp.server.mcpserver.exceptions.ToolError`
  on purpose: the SDK replaces *any other* exception with
  ``UnexpectedToolError``, whose message is only ``Error executing tool <name>``
  and which withholds the original text from the client. See
  :func:`_describe_error`.
* **Every call is explicit and stateless.** Pass ``host`` + ``user`` and the
  authentication material needed for that call. No password, host alias or
  connection profile is read or written by the MCP server.
* **Two independent timeouts.** ``timeout`` bounds a single command and reports
  exit code 124 with the output produced so far, so an overrunning command never
  raises. ``connect_timeout`` bounds the socket plus SSH handshake and does fail
  the call. A raised ``SocketTimeout`` therefore always points at the connection
  phase, which is why :func:`_describe_error` names ``connect_timeout`` there.
* **Every parameter is documented in the published schema.** The annotations
  below are what a calling model sees in ``tools/list``; keeping them on the
  signature is what stops callers from guessing at semantics such as
  ``fail_on_error``.
"""

from __future__ import annotations

import argparse
import os
import socket
from typing import Annotated, NoReturn, Sequence

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from paramiko.ssh_exception import (
    AuthenticationException,
    NoValidConnectionsError,
    SSHException,
)
from pydantic import Field
from scp import SCPException

from . import __version__
from .client import download_file, execute_remote_commands, upload_directory
from .config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_REMOTE_PATH,
    DEFAULT_TIMEOUT,
)
from .http_auth import BearerTokenMiddleware, HealthEndpointMiddleware

server = MCPServer("ssh-mcp", version=__version__)


def _seconds(value: float) -> str:
    """Render a duration for a human, without a trailing ``.0``."""
    return f"{value:g}"


def _oserror_text(exc: OSError) -> str:
    """Render an ``OSError`` without the useless ``[Errno None]`` wrapper.

    ``OSError(None, "boom")`` -- the shape several libraries use to raise a
    plain message through an ``OSError`` subclass -- stringifies to
    ``[Errno None] boom``. The ``None`` is noise the caller cannot act on, so
    it is dropped and the message kept.
    """
    if exc.errno is None and exc.strerror:
        return exc.strerror
    return str(exc)


CONNECTION_ARGS = (
    "连接信息由本次调用提供：传 host + user，以及 password 或 ssh_key_filepath。"
)

HostArg = Annotated[
    str | None,
    Field(description="远程主机地址。每次调用都必须提供。"),
]
UserArg = Annotated[
    str | None,
    Field(description="SSH 用户名。每次调用都必须提供。"),
]
PasswordArg = Annotated[
    str | None,
    Field(
        description=(
            "SSH 密码。与 ssh_key_filepath 至少给一个；"
            "都不给则回退到 ssh-agent / ~/.ssh 默认密钥。"
        )
    ),
]
KeyArg = Annotated[
    str | None,
    Field(description="SSH 私钥文件路径。与 password 至少给一个。"),
]
PortArg = Annotated[int | None, Field(description=f"SSH 端口，默认 {DEFAULT_PORT}。")]
TimeoutArg = Annotated[
    float | None,
    Field(
        description=(
            f"单条命令的执行超时秒数，默认 {_seconds(DEFAULT_TIMEOUT)}。超时不算异常："
            "返回退出码 124 并保留命令已产生的输出。"
        )
    ),
]
ConnectTimeoutArg = Annotated[
    float | None,
    Field(
        description=(
            f"建立连接（TCP + SSH 握手）的超时秒数，默认 {_seconds(DEFAULT_CONNECT_TIMEOUT)}。"
            "主机连不上 / 网络慢时调这个；调 timeout 对连接阶段无效。"
        )
    ),
]


def _describe_error(exc: BaseException) -> str:
    """Turn a low-level SSH/SCP failure into one readable diagnostic line.

    Subclasses are checked before their bases: ``socket.gaierror``,
    ``ConnectionRefusedError`` and ``FileNotFoundError`` are all ``OSError``,
    and ``socket.timeout`` is an alias of ``TimeoutError``.
    """
    if isinstance(exc, AuthenticationException):
        return (
            f"SSH 认证失败（{exc}）。"
            "请检查本次调用传入的 user / password / ssh_key_filepath 参数。"
        )
    if isinstance(exc, SSHException):
        return f"SSH 连接或协议错误（{type(exc).__name__}：{exc}）。"
    if isinstance(exc, TimeoutError):  # socket.timeout is an alias of TimeoutError
        # A command that overruns its own budget is reported as exit code 124
        # rather than raised, so arriving here means the *connection* phase ran
        # out of time. The knob that helps is connect_timeout, not timeout --
        # saying otherwise sends the caller after a parameter that cannot help.
        return (
            f"连接超时（{exc}）。请确认主机可达、端口开放、路由通畅；"
            f"链路确实慢时可加大 connect_timeout 参数（默认 {_seconds(DEFAULT_CONNECT_TIMEOUT)} 秒）。"
        )
    if isinstance(exc, socket.gaierror):
        return f"主机名无法解析（{exc}）。请检查 host 参数是否正确。"
    if isinstance(exc, ConnectionRefusedError):
        return f"连接被拒绝（{exc}）。请确认目标主机 SSH 端口已开放、port 参数正确。"
    if isinstance(exc, NoValidConnectionsError):
        # Subclasses socket.error, not SSHException, so it needs its own branch.
        return f"无法建立 SSH 连接（{exc}）。请确认主机在线、端口开放、防火墙已放行。"
    if isinstance(exc, FileNotFoundError):
        return f"本地路径不存在（{_oserror_text(exc)}）。"
    if isinstance(exc, NotADirectoryError):
        return f"路径存在但不是目录（{_oserror_text(exc)}）。"
    if isinstance(exc, IsADirectoryError):
        return f"目标是目录而非文件（{_oserror_text(exc)}）。下载只支持单个文件。"
    if isinstance(exc, PermissionError):
        return (
            f"权限不足（{_oserror_text(exc)}）。请检查本地目录写权限或远端路径读权限。"
        )
    if isinstance(exc, OSError):
        return f"网络或文件系统错误（{type(exc).__name__}：{_oserror_text(exc)}）。"
    if isinstance(exc, SCPException):
        return (
            f"SCP 传输失败（{exc}）。"
            "常见原因：远端文件不存在、路径无权限，或目标是目录。"
        )
    if isinstance(exc, ValueError):
        return f"参数或配置错误（{exc}）。"
    return f"{type(exc).__name__}: {exc}"


def _raise_tool_error(exc: BaseException, action: str) -> NoReturn:
    """Re-raise a failure as ``ToolError`` so its message reaches the client.

    The action prefix keeps failures readable to MCP clients.
    """
    detail = _describe_error(exc)
    raise ToolError(f"{action}失败：{detail}") from exc


@server.tool(
    name="ssh_execute_command",
    description=(
        "Run a command on a remote SSH host and return its stdout/stderr output. "
        + CONNECTION_ARGS
    ),
)
def ssh_execute_command(
    command: Annotated[
        str,
        Field(description="要执行的远程命令（单条）。"),
    ],
    host: HostArg = None,
    user: UserArg = None,
    password: PasswordArg = None,
    ssh_key_filepath: KeyArg = None,
    port: PortArg = None,
    timeout: TimeoutArg = None,
    connect_timeout: ConnectTimeoutArg = None,
    fail_on_error: Annotated[
        bool,
        Field(
            description=(
                "true 时命令以非 0 状态退出即当作失败抛出；默认 false，"
                "非 0 退出只体现在返回文本的 Exit code 行里。"
            )
        ),
    ] = False,
) -> str:
    """Execute one remote SSH command and return the real command output.

    Raises:
        ToolError: if no target was given, if the target is ambiguous, if
            connecting or executing fails, or if the command exits non-zero
            while ``fail_on_error`` is enabled.
    """
    try:
        result = execute_remote_commands(
            commands=[command],
            host=host,
            user=user,
            password=password,
            ssh_key_filepath=ssh_key_filepath,
            port=port,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )
    except Exception as exc:
        _raise_tool_error(exc, "执行远程命令")

    if not result:
        result = f"Command: {command}\nExit code: 0\nStdout:\n<empty>\nStderr:\n<empty>"

    if fail_on_error and "\nExit code: 0\n" not in result:
        raise ToolError(f"远程命令以非 0 状态退出：\n{result}")

    return result


@server.tool(
    name="ssh_upload_directory",
    description=(
        "Upload a local directory or file into a remote directory over SCP. "
        "上传本地目录或文件到远端目录，按 scp -r 语义保留目录名。" + CONNECTION_ARGS
    ),
)
def ssh_upload_directory(
    local_dir: Annotated[
        str,
        Field(description="要上传的本地目录或文件路径。"),
    ],
    host: HostArg = None,
    user: UserArg = None,
    password: PasswordArg = None,
    ssh_key_filepath: KeyArg = None,
    remote_path: Annotated[
        str,
        Field(
            description=(
                "远端目标目录，始终按目录处理，不存在会自动创建。"
                "目录按 scp -r 语义保留自身名字：local_dir=/tmp/x + remote_path=/srv → /srv/x。"
            )
        ),
    ] = DEFAULT_REMOTE_PATH,
    port: PortArg = None,
    connect_timeout: ConnectTimeoutArg = None,
) -> str:
    """Upload a local directory via SCP over SSH.

    ``remote_path`` is always treated as a *directory*; it is created on the
    remote host when missing so SCP never collapses the payload into a file.

    ``scp -r`` semantics: a directory keeps its own name, so
    ``local_dir=/tmp/x`` with ``remote_path=/srv`` lands in ``/srv/x`` and not
    in ``/srv``. A single file lands as ``/srv/<name>``.

    Raises:
        ToolError: if no target was given, the local source is missing, the
            connection fails, or the transfer is rejected.
    """
    try:
        return upload_directory(
            local_dir=local_dir,
            host=host,
            user=user,
            password=password,
            ssh_key_filepath=ssh_key_filepath,
            remote_path=remote_path,
            port=port,
            connect_timeout=connect_timeout,
        )
    except Exception as exc:
        _raise_tool_error(exc, "上传")


@server.tool(
    name="ssh_download_file",
    description=(
        "Download a file from a remote SSH host to a local path. "
        "把远端单个文件下载到本地。" + CONNECTION_ARGS
    ),
)
def ssh_download_file(
    remote_file: Annotated[
        str,
        Field(description="远端文件路径，只支持单个文件，不接受目录。"),
    ],
    host: HostArg = None,
    user: UserArg = None,
    password: PasswordArg = None,
    ssh_key_filepath: KeyArg = None,
    local_path: Annotated[
        str | None,
        Field(
            description=(
                "本地落盘路径。不传则落到系统临时目录的 ssh-mcp-downloads/ 下，"
                "返回值里给出实际位置；该路径已存在会被直接覆盖。"
            )
        ),
    ] = None,
    port: PortArg = None,
    connect_timeout: ConnectTimeoutArg = None,
) -> str:
    """Download a single file from the remote host via SCP.

    When ``local_path`` is omitted the file lands in the system temp directory
    under ``ssh-mcp-downloads/``; an existing file at ``local_path`` is
    overwritten without warning.

    Raises:
        ToolError: if no target was given, the remote file is missing or is a
            directory, the local destination cannot be written, or the
            connection fails.
    """
    try:
        return download_file(
            remote_file=remote_file,
            host=host,
            user=user,
            password=password,
            ssh_key_filepath=ssh_key_filepath,
            local_path=local_path,
            port=port,
            connect_timeout=connect_timeout,
        )
    except Exception as exc:
        _raise_tool_error(exc, "下载")


def create_mcp_server() -> MCPServer:
    """Return the MCP server instance for this SSH project."""
    return server


def main(argv: Sequence[str] | None = None) -> None:
    """Run the SSH tool server over stdio or Streamable HTTP transport.

    Supported transports:
    - stdio: default, for local MCP clients over stdin/stdout
    - streamable-http: for HTTP-based MCP clients such as Streamable HTTP
    """
    parser = argparse.ArgumentParser(
        prog="ssh-mcp-server",
        description="Run the SSH MCP server via stdio or Streamable HTTP.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport to use; default: stdio",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for HTTP mode")
    parser.add_argument(
        "--port", type=int, default=8000, help="Bind port for HTTP mode"
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="HTTP path for Streamable HTTP mode (default: /mcp)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.getenv("SSH_MCP_AUTH_TOKEN"),
        help="optional Bearer token; can also be set with SSH_MCP_AUTH_TOKEN",
    )
    parsed = parser.parse_args(list(argv) if argv is not None else None)

    if parsed.transport == "stdio":
        anyio.run(server.run_stdio_async)
        return

    _run_http(parsed)


def create_http_app(*, host: str, path: str, token: str | None):
    app = server.streamable_http_app(streamable_http_path=path, host=host)
    app = HealthEndpointMiddleware(app)
    return BearerTokenMiddleware(app, token) if token else app


def _run_http(parsed: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        create_http_app(host=parsed.host, path=parsed.path, token=parsed.auth_token),
        host=parsed.host,
        port=parsed.port,
        log_level=server.settings.log_level.lower(),
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "create_mcp_server",
    "main",
    "server",
    "ssh_download_file",
    "ssh_execute_command",
    "ssh_upload_directory",
]


if __name__ == "__main__":
    main()
