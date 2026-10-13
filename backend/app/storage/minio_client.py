"""
MinIO对象存储客户端
"""

import io
import logging
from datetime import timedelta
from threading import Lock

from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = logging.getLogger(__name__)


class MinIOClient:
    """MinIO客户端封装"""

    def __init__(self):
        self._client = None
        self.bucket = settings.minio.bucket
        self._bucket_ready = False
        self._bucket_lock = Lock()

    @property
    def client(self):
        if self._client is None:
            self._client = Minio(
                settings.minio.endpoint,
                access_key=settings.minio.access_key,
                secret_key=settings.minio.secret_key,
                secure=settings.minio.secure,
            )
        return self._client

    def _ensure_bucket(self):
        """确保bucket存在"""
        try:
            with self._bucket_lock:
                if self._bucket_ready:
                    return
                if not self.client.bucket_exists(self.bucket):
                    try:
                        self.client.make_bucket(self.bucket)
                    except S3Error as exc:
                        if exc.code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                            raise
                self._bucket_ready = True
        except S3Error as e:
            logger.error(f"Failed to create bucket: {e}")
            raise

    def upload(self, object_name: str, data: bytes, content_type: str = "application/pdf") -> str:
        """
        上传文件

        Args:
            object_name: 对象名称（路径）
            data: 文件字节数据
            content_type: 内容类型

        Returns:
            对象URL
        """
        try:
            self._ensure_bucket()
            data_stream = io.BytesIO(data)
            self.client.put_object(
                self.bucket,
                object_name,
                data_stream,
                length=len(data),
                content_type=content_type,
            )
            logger.info(f"Uploaded: {object_name}")
            return f"{self.bucket}/{object_name}"
        except S3Error as e:
            logger.error(f"Failed to upload {object_name}: {e}")
            raise

    def download(self, object_name: str) -> bytes:
        """
        下载文件

        Args:
            object_name: 对象名称

        Returns:
            文件字节数据
        """
        try:
            response = self.client.get_object(self.bucket, object_name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as e:
            logger.error(f"Failed to download {object_name}: {e}")
            raise

    def get_url(self, object_name: str, expires: int = 3600) -> str:
        """
        获取对象的预签名URL

        Args:
            object_name: 对象名称
            expires: 过期时间（秒）

        Returns:
            预签名URL
        """
        try:
            url = self.client.presigned_get_object(
                self.bucket, object_name, expires=timedelta(seconds=expires)
            )
            return url
        except S3Error as e:
            logger.error(f"Failed to get URL for {object_name}: {e}")
            raise

    def delete(self, object_name: str):
        """
        删除文件

        Args:
            object_name: 对象名称
        """
        try:
            self.client.remove_object(self.bucket, object_name)
            logger.info(f"Deleted: {object_name}")
        except S3Error as e:
            logger.error(f"Failed to delete {object_name}: {e}")
            raise

    def exists(self, object_name: str) -> bool:
        """
        检查对象是否存在

        Args:
            object_name: 对象名称

        Returns:
            是否存在
        """
        try:
            self.client.stat_object(self.bucket, object_name)
            return True
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject", "NoSuchBucket"):
                return False
            raise


# 全局MinIO客户端实例
minio_client = MinIOClient()
