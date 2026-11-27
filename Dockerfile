FROM ghcr.io/astral-sh/uv:python3.13-trixie

COPY ./frex/pyproject.toml /frex/pyproject.toml
COPY ./frex/uv.lock /frex/uv.lock
WORKDIR /frex
RUN uv sync --frozen --no-install-project -i https://mirrors.aliyun.com/pypi/simple/

COPY ./frex /frex
RUN uv sync --frozen -i https://mirrors.aliyun.com/pypi/simple/

ENV TZ=Asia/Shanghai