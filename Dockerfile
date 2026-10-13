# Imagem do servidor MCP do Bazar.
# Etapa 1: instala as dependências com o uv, travadas pelo uv.lock.
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Primeiro só as dependências (camada em cache enquanto o uv.lock não muda)...
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
# ...depois o código.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# Etapa 2: só o ambiente pronto. Sem uv, sem compilador, sem o código-fonte solto.
FROM python:3.14-slim
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# Usuário sem privilégios, numérico: o Kubernetes consegue conferir que não é root.
USER 10001:10001
EXPOSE 8090
ENTRYPOINT ["python", "-m", "bazar_agente.mcp_bazar"]
