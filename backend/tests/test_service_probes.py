import asyncio
import threading
from unittest.mock import patch

import pytest

from app.api.services import _probe_http_client


@pytest.mark.asyncio
async def test_slow_http_client_initialization_does_not_block_other_dependency_probes():
    started = threading.Event()
    release = threading.Event()
    client = object()

    def factory(**kwargs):
        assert kwargs == {"timeout": 8}
        started.set()
        assert release.wait(2)
        return client

    with patch("app.api.services.httpx.AsyncClient", side_effect=factory):
        task = asyncio.create_task(_probe_http_client(8))
        assert await asyncio.to_thread(started.wait, 1)
        # 证书初始化阻塞时，独立探测仍可继续。
        progress = await asyncio.wait_for(asyncio.sleep(0, result=True), .5)
        assert progress
        release.set()
        assert await task is client
