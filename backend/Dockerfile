FROM python:3.12-slim

WORKDIR /app/backend

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装Poetry
RUN pip install poetry==2.1.3

# 复制依赖文件
COPY backend/pyproject.toml backend/poetry.lock ./

# 安装Python依赖
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi --no-root

# 复制应用代码
COPY backend/app ./app
COPY backend/migrations ./migrations
# 运行时 local.yaml 由 Compose 挂载，不能把本地凭据打包进镜像。
COPY config/default.yaml config/development.yaml /app/config/

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
