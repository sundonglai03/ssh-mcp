# ssh-mcp

基于 Paramiko 和 SCP 的无状态 SSH MCP Server：执行远程命令、上传文件、下载文件。支持密码、SSH 私钥、stdio 和 Streamable HTTP。

## 快速开始

```bash
cd /path/to/ssh-mcp
uv sync
```

### stdio（桌面 MCP 客户端）

```json
{
  "mcpServers": {
    "ssh-mcp": {
      "command": "/path/to/ssh-mcp/.venv/bin/python",
      "args": ["-m", "ssh_mcp.mcp_server"]
    }
  }
}
```

使用 `.venv/bin/python` 的绝对路径；不要在客户端配置中依赖 `cwd` 或 `uv run`。

### Streamable HTTP

```bash
.venv/bin/python -m ssh_mcp.mcp_server \
  --transport streamable-http --host 127.0.0.1 --port 8001 --path /mcp
```

MCP 地址：`http://127.0.0.1:8001/mcp`。这是 MCP 协议端点，不是普通网页。
可信内网可以使用 HTTP；跨不可信网段时建议设置 `SSH_MCP_AUTH_TOKEN` 或使用
`--auth-token`。没有证书也可以先用 HTTP + Token，HTTPS 不是运行前提。

### Docker

```bash
docker compose up -d --build
docker compose logs -f ssh-mcp
docker compose down
```

镜像名为 `sundonglai/ssh-mcp:latest`，容器名为 `ssh-mcp`。默认监听
`127.0.0.1:8001`。健康检查为 `http://127.0.0.1:8001/health`。服务不保存凭据、
主机别名或兼容缓存；本地文件通过 `./work:/work` 映射。

如果使用宿主机私钥，在 Compose 中增加：

```yaml
- ${HOME}/.ssh:/home/ssh-mcp/.ssh:ro
```

工具参数使用容器路径，例如 `/home/ssh-mcp/.ssh/id_ed25519` 和 `/work/app.tar.gz`。
如果设置了 `SSH_STRICT_HOST_KEYS=true`，该挂载目录还应包含已核对过的
`known_hosts`；默认内网模式不需要手动维护它。

## 工具

| 工具 | 作用 |
| --- | --- |
| `ssh_execute_command` | 执行一条远程命令 |
| `ssh_upload_directory` | 上传文件或目录 |
| `ssh_download_file` | 下载单个文件 |

每次调用都直接提供连接信息，服务不会记住它们：

```text
ssh_execute_command(command="uptime", host="10.0.0.5", user="root", password="...")
ssh_execute_command(command="uptime", host="10.0.0.5", user="root", password="...")
```

每个请求独立建立连接；不会保存密码、主机别名或连接配置。

## 连接参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `host` / `user` | 必填 | SSH 目标 |
| `password` | 无 | 密码认证 |
| `ssh_key_filepath` | 无 | 私钥路径 |
| `port` | `22` | SSH 端口 |
| `timeout` | `30` | 单条命令执行超时；返回退出码 `124` |
| `connect_timeout` | `10` | TCP + SSH 握手超时 |
| `remote_path` | `/tmp` | 上传目标目录 |
| `fail_on_error` | `false` | 非零退出码是否让工具失败 |

## 安全注意事项

- 默认自动接受内网中的新 SSH 主机密钥，首次连接不需要手动配置服务器。需要严格校验时设置
  `SSH_STRICT_HOST_KEYS=true`，并把目标加入 `~/.ssh/known_hosts`（例如使用 `ssh-keyscan` 后人工核对指纹）。
- 远程命令工具本身具有完整 shell 权限，不是命令白名单沙箱。
- HTTP 模式默认只绑定回环地址；远程部署必须增加认证和 HTTPS。
- 不要把私钥复制进 Docker 镜像，使用只读 volume 挂载。

## 项目结构

```text
ssh_mcp/
├── mcp_server.py  # MCP 工具和 transport 入口
├── client.py      # SSH/SCP 业务客户端
├── config.py      # 单次调用参数校验
└── server.py      # Paramiko 底层连接
```

## 开发

```bash
.venv/bin/python -m pytest
docker compose config
git diff --check
```

不要提交 `.venv/`、私钥、`work/` 下的业务文件或 Docker 数据。

## License

MIT
