"""SSH and SCP client implementation."""

import os
import shlex
import socket
import tempfile
import time
from typing import List

from paramiko import AutoAddPolicy, RejectPolicy, SSHClient
from paramiko.ssh_exception import AuthenticationException
from scp import SCPClient, SCPException

from .config import DEFAULT_CONNECT_TIMEOUT, DEFAULT_TIMEOUT
from .log import LOGGER, shorten

DEFAULT_COMMAND_TIMEOUT = DEFAULT_TIMEOUT
EXIT_CODE_TIMEOUT = 124

TIMEOUT_DRAIN_SECONDS = 0.5


def _read_stream(stream) -> str:
    """Read a channel stream to EOF and never let a read failure break the caller."""
    try:
        return stream.read().decode(errors="replace")
    except Exception:  # pragma: no cover - channel may already be closed
        return ""


def _drain_channel(channel, budget: float = TIMEOUT_DRAIN_SECONDS) -> tuple[str, str]:
    """Collect whatever the remote already produced without waiting for EOF.

    Used on timeout: blocking until EOF would wait for the very command we are
    trying to abandon, and would throw away the output it already produced.
    """
    out, err = b"", b""
    deadline = time.monotonic() + budget
    try:
        channel.settimeout(0.1)
    except Exception:  # pragma: no cover - fake channels in tests
        pass

    while time.monotonic() < deadline:
        try:
            if channel.recv_ready():
                out += channel.recv(65536)
            elif channel.recv_stderr_ready():
                err += channel.recv_stderr(65536)
            else:
                break
        except Exception:
            break

    return out.decode(errors="replace"), err.decode(errors="replace")


def _close_streams(*streams) -> None:
    """Best-effort close of every object handed out by exec_command."""
    for stream in streams:
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:  # pragma: no cover - closing a dead channel is fine
            pass


class RemoteClient:
    """Connect to a remote host over SSH and upload or execute commands."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        ssh_key_filepath: str,
        remote_path: str,
        port: int = 22,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        auto_add_host_keys: bool | None = None,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.ssh_key_filepath = ssh_key_filepath
        self.remote_path = remote_path
        self.port = port
        self.connect_timeout = connect_timeout
        if auto_add_host_keys is None:
            strict = os.getenv("SSH_STRICT_HOST_KEYS", "").strip().lower()
            auto_add_host_keys = strict not in {"1", "true", "yes", "on"}
        self.auto_add_host_keys = auto_add_host_keys
        self.client: SSHClient | None = None
        self.scp_client: SCPClient | None = None

    @property
    def connection(self) -> SSHClient:
        """Open and cache the SSH connection."""
        if self.client is not None:
            return self.client
        try:
            self.client = SSHClient()
            self.client.load_system_host_keys()
            if self.auto_add_host_keys:
                self.client.set_missing_host_key_policy(AutoAddPolicy())
            else:
                # Strict mode: an unknown host key is an error, not a prompt.
                self.client.set_missing_host_key_policy(RejectPolicy())

            connect_kwargs = {
                "username": self.user,
                "port": self.port,
                "timeout": self.connect_timeout,
            }
            if self.password:
                connect_kwargs["password"] = self.password
            if self.ssh_key_filepath:
                connect_kwargs["key_filename"] = self.ssh_key_filepath

            self.client.connect(self.host, **connect_kwargs)
            return self.client
        except AuthenticationException as e:
            LOGGER.error(
                f"认证失败（{self.user}@{self.host}:{self.port}）：{e}。"
                "请检查本次调用传入的 password / ssh_key_filepath 参数。"
            )
            self.client = None
            raise
        except Exception as e:
            LOGGER.error(f"连接 {self.user}@{self.host}:{self.port} 失败：{e}")
            self.client = None
            raise

    @property
    def scp(self) -> SCPClient:
        if self.scp_client is not None:
            return self.scp_client
        conn = self.connection
        transport = conn.get_transport()
        if transport is None:
            raise RuntimeError("无法建立 SSH 传输通道，SCP 连接失败。")
        self.scp_client = SCPClient(transport)
        return self.scp_client

    def close(self) -> None:
        """Close the SSH and SCP connections."""
        if self.scp_client is not None:
            self.scp_client.close()
            self.scp_client = None

        if self.client is not None:
            self.client.close()
            self.client = None

    def _ensure_remote_dir(self, remote_path: str) -> None:
        """Create the remote destination directory before any SCP transfer.

        SCP silently writes the payload to a *file* named after the destination
        when the remote path does not exist, which destroys data when several
        files target the same directory. Creating it up front keeps the
        "remote_path 是目录" contract true.
        """
        quoted = shlex.quote(remote_path)
        script = (
            f"if [ -e {quoted} ] && [ ! -d {quoted} ]; then "
            "echo __SSH_MCP_NOT_A_DIR__; "
            f"else mkdir -p {quoted} && echo __SSH_MCP_DIR_OK__; fi"
        )

        stdin, stdout, stderr = self.connection.exec_command(script, timeout=15)
        try:
            stdout.channel.settimeout(15)
        except Exception:  # pragma: no cover - fake channels in tests
            pass
        output = _read_stream(stdout)
        _close_streams(stdin, stdout, stderr)

        if "__SSH_MCP_NOT_A_DIR__" in output:
            raise NotADirectoryError(
                f"远程路径已存在但不是目录，无法上传：{remote_path}"
            )
        if "__SSH_MCP_DIR_OK__" not in output:
            raise RuntimeError(
                f"无法创建远程目录：{remote_path}（远端输出：{output.strip() or '<empty>'}）"
            )

    def _upload_single_path(
        self, local_path: str, recursive: bool | None = None
    ) -> None:
        """Upload a single local file or directory using ``scp -r`` semantics.

        A directory lands as ``<remote_path>/<directory name>/`` - the source
        directory name is kept, exactly like ``scp -r /local/x host:/dest``
        creating ``/dest/x``. A single file lands as ``<remote_path>/<name>``.
        """
        source = str(local_path)
        if not os.path.exists(source):
            raise FileNotFoundError(f"Upload source does not exist: {source}")

        if os.path.isdir(source):
            # scp.py mirrors scp -r: it sends the directory entry itself, so the
            # basename is recreated under remote_path. Directories are always
            # recursive - a non-recursive directory copy has no meaning.
            self.scp.put(source, remote_path=self.remote_path, recursive=True)
            target = f"{self.remote_path.rstrip('/')}/{os.path.basename(source)}"
            LOGGER.info(f"已完成上传目录 {source} 到 {target}（主机：{self.host}）")
            return

        self.scp.put(source, remote_path=self.remote_path, recursive=False)
        LOGGER.info(
            f"已完成上传文件 {source} 到 {self.remote_path}（主机：{self.host}）"
        )

    def bulk_upload(
        self, filepaths: str | List[str], recursive: bool | None = None
    ) -> None:
        """Upload one file, one directory, or a list of paths to the remote host."""
        try:
            sources = (
                [str(filepaths)]
                if isinstance(filepaths, (str, os.PathLike))
                else [str(path) for path in filepaths]
            )

            # Validate everything locally first: no point opening a connection
            # or creating a remote directory for a transfer that cannot start.
            for source in sources:
                if not os.path.exists(source):
                    raise FileNotFoundError(f"Upload source does not exist: {source}")

            self._ensure_remote_dir(self.remote_path)

            for source in sources:
                self._upload_single_path(source, recursive=recursive)

            if len(sources) > 1:
                LOGGER.info(
                    f"已完成上传 {len(sources)} 个对象到 {self.remote_path}（主机：{self.host}）"
                )
        except SCPException as e:
            LOGGER.error(f"批量上传过程中发生 SCPException：{e}")
            raise
        except Exception as e:
            LOGGER.error(f"批量上传过程中发生未知异常：{e}")
            raise

    def download_file(self, filepath: str, local_path: str | None = None) -> str:
        """Download a file from the remote host to a safe local destination."""
        try:
            if local_path is None:
                download_dir = os.path.join(tempfile.gettempdir(), "ssh-mcp-downloads")
                os.makedirs(download_dir, exist_ok=True)
                filename = os.path.basename(filepath) or "downloaded_file"
                local_path = os.path.join(download_dir, filename)
            else:
                local_dir = os.path.dirname(local_path)
                if local_dir:
                    os.makedirs(local_dir, exist_ok=True)

            self.scp.get(filepath, local_path=local_path)
            os.chmod(local_path, 0o600)
            LOGGER.info(f"已从 {self.host} 下载 {filepath} 到 {local_path}")
            return local_path
        except SCPException as e:
            LOGGER.error(f"下载文件过程中发生 SCPException：{e}")
            raise
        except Exception as e:
            LOGGER.error(f"下载文件过程中发生未知异常：{e}")
            raise

    def _wait_for_exit(self, channel, timeout: float) -> bool:
        """Poll the channel until the remote command exits.

        Returns True when the command finished on its own, False when the
        deadline elapsed. Polling is required because ``recv_exit_status``
        blocks past the configured timeout on busy channels.
        """
        try:
            ready_check = channel.exit_status_ready
        except AttributeError:  # pragma: no cover - minimal fake channels
            return True

        deadline = time.monotonic() + timeout
        while True:
            if ready_check():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.2, remaining))

    def _format_command_result(
        self, cmd: str, exit_status: int, output: str, error: str
    ) -> str:
        """Build a structured result string for MCP clients and CLI consumers."""
        lines = [
            f"Command: {cmd}",
            f"Exit code: {exit_status}",
            "Stdout:",
            output.strip() if output.strip() else "<empty>",
            "Stderr:",
            error.strip() if error.strip() else "<empty>",
        ]
        return "\n".join(lines)

    def execute_commands(
        self,
        commands: List[str],
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Execute a sequence of commands and return structured output.

        ``timeout`` is enforced per command: a command that runs longer is
        aborted (the channel is closed) and reported with exit code 124.
        """
        conn = self.connection
        outputs: list[str] = []

        for cmd in commands:
            stdin, stdout, stderr = conn.exec_command(cmd, timeout=timeout)
            channel = stdout.channel
            channel.settimeout(timeout)

            finished = self._wait_for_exit(channel, timeout)
            timed_out = not finished
            exit_status = EXIT_CODE_TIMEOUT if timed_out else 0

            if finished:
                try:
                    exit_status = channel.recv_exit_status()
                except (socket.timeout, TimeoutError, OSError):
                    # The channel itself gave up even though we believed the
                    # command had finished - still a timeout, not a crash.
                    timed_out = True
                    exit_status = EXIT_CODE_TIMEOUT

            if timed_out:
                # Grab the output produced so far, then hang up instead of
                # blocking until the abandoned command finally exits.
                output, error = _drain_channel(channel)
                _close_streams(stdin, stdout, stderr, channel)
                error = (error + "\n" if error else "") + (
                    f"Command timed out after {timeout} seconds and the connection channel was closed."
                )
            else:
                output = _read_stream(stdout)
                error = _read_stream(stderr)

            # Command output is logged truncated: a single verbose command must
            # not bury real errors in the log stream.
            LOGGER.debug(f"$ {cmd}\n{shorten(output)}")
            if error:
                LOGGER.error(f"$ {cmd}\n{shorten(error)}")

            outputs.append(self._format_command_result(cmd, exit_status, output, error))

        return "\n\n".join(outputs)
