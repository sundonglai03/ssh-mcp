FROM python:3.12-slim

ARG UV_VERSION=0.12.5
RUN pip install --no-cache-dir "uv==$UV_VERSION"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY ssh_mcp ./ssh_mcp
RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 1000 ssh-mcp \
    && mkdir -p /work /home/ssh-mcp/.ssh \
    && chown -R ssh-mcp:ssh-mcp /app /work /home/ssh-mcp

USER ssh-mcp
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4)"

CMD ["python", "-m", "ssh_mcp.mcp_server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8001", "--path", "/mcp"]
