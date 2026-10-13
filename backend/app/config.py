"""
配置管理模块
从 YAML 文件和环境变量加载配置
"""

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    """数据库配置"""

    host: str = "localhost"
    port: int = 5432
    name: str = "document_parser"
    user: str = "postgres"
    password: str = "postgres"
    pool_size: int = 10
    max_overflow: int = 20

    @property
    def url(self) -> str:
        """获取数据库连接URL"""
        return f"postgresql+asyncpg://{quote(self.user, safe='')}:{quote(self.password, safe='')}@{self.host}:{self.port}/{quote(self.name, safe='')}"


class RedisSettings(BaseSettings):
    """Redis配置"""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    max_connections: int = 50

    @property
    def url(self) -> str:
        """获取Redis连接URL"""
        if self.password:
            return f"redis://:{quote(self.password, safe='')}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class MinIOSettings(BaseSettings):
    """MinIO配置"""

    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "documents"
    secure: bool = False


class ModelSettings(BaseSettings):
    """模型服务配置"""

    base_url: str
    api_key: str | None = None
    model_name: str
    # Remote model upgrades must change this deployment revision; the OCR server
    # does not expose a model digest that the parser can verify automatically.
    model_version: str = "unknown"
    timeout: int = 60
    max_retries: int = 3
    route: str | None = None
    dimensions: int | None = None

    @field_validator("dimensions")
    @classmethod
    def positive_dimensions(cls, value):
        if value is not None and value <= 0:
            raise ValueError("dimensions must be positive")
        return value

    @field_validator("route")
    @classmethod
    def relative_route(cls, value):
        if value is not None and (not value.strip() or any(token in value for token in ("://", "..", "?", "#"))):
            raise ValueError("route must be a relative endpoint path")
        return value


class CelerySettings(BaseSettings):
    """Celery配置"""

    broker_url: str = "redis://localhost:6379/0"
    result_backend: str = "redis://localhost:6379/0"
    task_serializer: str = "json"
    result_serializer: str = "json"
    accept_content: list = ["json"]
    timezone: str = "Asia/Shanghai"
    enable_utc: bool = True
    task_default_queue: str = "celery"


class AppSettings(BaseSettings):
    """应用配置"""

    name: str = "document-parser"
    version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    # Local validation opts in here; enabled uploads default to reliable v2.
    reliable_enabled: bool = False
    assistant_enabled: bool = False


class LoggingSettings(BaseSettings):
    """日志配置"""

    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    file: str = "logs/app.log"
    max_bytes: int = 10485760  # 10MB
    backup_count: int = 5


class UploadSettings(BaseSettings):
    """文件上传配置"""

    max_file_size: int = 104857600  # 100MB
    allowed_extensions: list = [".pdf"]
    temp_dir: str = "/tmp/document-parser"


class AgentSettings(BaseSettings):
    """Bounded checklist agent budgets; scores never certify correctness."""

    max_rounds: int = 40
    max_rounds_per_batch: int = 6
    max_actions_per_round: int = 4
    max_view_pages: int = 20
    max_tokens: int = 6000
    max_context_chars: int = 24000
    max_total_tokens: int = 200000
    field_batch_size: int = 1
    max_region_views: int = 24
    max_search_results: int = 8
    # Plan B: whole-page views at 120 DPI (about 1.4k tokens); region crops keep 180 DPI.
    render_dpi: int = 120
    region_render_dpi: int = 180
    # Plan B: retained page images per call (was a hard-coded 3).
    history_images: int = 2
    # Plan B: reasoning output switch; default unchanged until the A/B result decides.
    # Reliable field extraction uses structured JSON decisions; local/default
    # runs disable reasoning text so it does not consume the output budget.
    thinking_enabled: bool = False
    # Plan B: pre-call estimate start values (measured); corrected per call from real usage.
    token_per_char: float = 0.55
    image_tokens_per_kpx: float = 1.0
    # Plan B defect fix: answer cap floor; calls without this much room are refused.
    min_output_tokens: int = 2500
    # Plan C: "retrieval" (deterministic candidates + verification) or "legacy" (tool loop).
    # Default stays "legacy" until the A/B gate (docs section 10.4) is passed and confirmed.
    agent_mode: str = "legacy"
    field_token_budget: int = 16000
    max_calls_per_field: int = 3
    candidates_per_call: int = 12
    max_candidates_per_field: int = 24
    max_candidates_multi: int = 36
    max_images_per_call: int = 12
    max_image_tokens_per_call: int = 4000
    candidate_window_chars: int = 300
    retrieval_image_policy: str = "auto"


class Settings:
    """全局配置类"""

    def __init__(self):
        self.app: AppSettings
        self.database: DatabaseSettings
        self.redis: RedisSettings
        self.minio: MinIOSettings
        self.celery: CelerySettings
        self.logging: LoggingSettings
        self.upload: UploadSettings
        self.agent: AgentSettings
        self.models: dict[str, ModelSettings] = {}
        self.fields: dict[str, Any] = {}

        # 加载配置
        self._load_config()

    def _load_config(self):
        """加载配置文件"""
        # 获取配置目录
        config_dir = Path(__file__).parent.parent.parent / "config"

        # 加载默认配置
        config_data = self._load_yaml(config_dir / "default.yaml")

        # 根据环境加载配置
        env = os.getenv("ENV", "development")
        env_config_path = config_dir / f"{env}.yaml"
        if env_config_path.exists():
            env_config = self._load_yaml(env_config_path)
            config_data = self._deep_merge(config_data, env_config)

        # 加载本地配置（优先级最高）
        local_config_path = config_dir / "local.yaml"
        if local_config_path.exists():
            local_config = self._load_yaml(local_config_path)
            config_data = self._deep_merge(config_data, local_config)

        # Process environment wins; YAML defaults never mutate it.
        environment = {
            k: str(v) for k, v in (config_data.get("environment") or {}).items() if v is not None
        }
        environment.update(os.environ)

        def env_value(key):
            value = environment.get(key)
            return value if value not in (None, "") else None

        def section(name, model):
            values = dict(config_data.get(name, {}))
            for field in model.model_fields:
                key = f"{name}_{field}".upper()
                if env_value(key) is not None:
                    values[field] = env_value(key)
            return model(**values)

        # 解析配置
        self.app = section("app", AppSettings)
        if env_value("RELIABLE_ENABLED") is not None:
            self.app.reliable_enabled = str(env_value("RELIABLE_ENABLED")).lower() in {"1", "true", "yes", "on"}
        if env_value("ASSISTANT_ENABLED") is not None:
            self.app.assistant_enabled = str(env_value("ASSISTANT_ENABLED")).lower() in {"1", "true", "yes", "on"}
        self.database = section("database", DatabaseSettings)
        self.redis = section("redis", RedisSettings)
        self.minio = section("minio", MinIOSettings)
        self.celery = section("celery", CelerySettings)
        self.logging = section("logging", LoggingSettings)
        self.upload = section("upload", UploadSettings)
        self.agent = section("agent", AgentSettings)
        if any(env_value(f"REDIS_{key}") is not None for key in ("HOST", "PORT", "DB", "PASSWORD")):
            if env_value("CELERY_BROKER_URL") is None:
                self.celery.broker_url = self.redis.url
            if env_value("CELERY_RESULT_BACKEND") is None:
                self.celery.result_backend = self.redis.url

        # 加载模型配置：补全缺失的密钥与地址
        models_config = config_data.get("models", {})
        for key, value in models_config.items():
            settings_value = dict(value)
            for field in ModelSettings.model_fields:
                env_key = f"{key}_{field}".upper()
                if env_value(env_key) is not None:
                    settings_value[field] = env_value(env_key)
            if key in ("text_llm", "vision_llm"):
                settings_value["api_key"] = settings_value.get("api_key") or env_value(
                    "OPENAI_API_KEY"
                )
                settings_value["base_url"] = (
                    env_value(f"{key}_BASE_URL".upper())
                    or env_value("OPENAI_BASE_URL")
                    or env_value("OPENAI_API_BASE")
                    or settings_value.get("base_url", "")
                )
            if not settings_value.get("model_name"):
                settings_value["model_name"] = environment.get("OPENAI_MODEL", "gpt-6.1-sol")
            self.models[key] = ModelSettings(**settings_value)

        # 加载字段配置
        self.fields = config_data.get("fields", {})

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        """加载YAML文件"""
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """深度合并字典"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result


# 全局配置实例
settings = Settings()
