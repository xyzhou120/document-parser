"""任务重试边界：临时依赖故障可以重试，输入和规则错误不能重试。"""

import httpx
from minio.error import S3Error
from sqlalchemy.exc import OperationalError


def is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError | ConnectionError | httpx.TransportError | OperationalError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 429) or exc.response.status_code >= 500
    if isinstance(exc, S3Error):
        return exc.code in {"SlowDown", "RequestTimeout", "ServiceUnavailable", "InternalError"}
    # urllib3 是 MinIO 的现有传输依赖。
    from urllib3.exceptions import HTTPError

    return isinstance(exc, HTTPError)
