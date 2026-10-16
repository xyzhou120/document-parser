"""按来源链接获取 PDF；开发期可直接使用，来源策略为可选配置。"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
import threading
from urllib.parse import quote, urljoin, urlsplit


class DownloadError(ValueError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def validate_url(url: str, sources: dict):
    try:
        p = urlsplit(url)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        if any(ord(c) < 32 or c == '\\' for c in url):
            raise ValueError()
        port = p.port or (443 if p.scheme == 'https' else 80)
        host = p.hostname.encode('idna').decode().lower()
        origin = f'{p.scheme}://{("[" + host + "]") if ":" in host else host}:{port}'
        policy = sources.get(origin, {})
        if sources and (not policy or not policy.get('networks')):
            raise DownloadError('SOURCE_NOT_CONFIGURED')
        return p, origin, host, port, policy
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError('INVALID_SOURCE_URL') from exc


def allowed_addresses(host, port, policy, *, resolver=socket.getaddrinfo):
    try:
        networks = [ipaddress.ip_network(item, strict=False) for item in policy.get('networks', [])]
        addresses = list(dict.fromkeys(item[4][0] for item in resolver(host, port, type=socket.SOCK_STREAM)))
        if not addresses:
            raise DownloadError('SOURCE_DNS_EMPTY', retryable=True)
        if not networks:
            return addresses
        for address in addresses:
            ip = ipaddress.ip_address(address)
            # 即使白名单误写 /0，也拒绝元数据、回环、组播和未指定地址。
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or not any(ip in n for n in networks):
                raise DownloadError('SOURCE_ADDRESS_DENIED')
        return addresses
    except DownloadError:
        raise
    except (OSError, ValueError) as exc:
        raise DownloadError('SOURCE_DNS_OR_POLICY_ERROR', retryable=isinstance(exc, OSError)) from exc


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, *, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


def request_target(path: str | None, query: str) -> str:
    """把路径和查询编码成 HTTP 请求行。保留已有的百分号编码，补上未编码的中文。"""
    encoded = quote(path or "/", safe="/%:@")
    if not encoded.startswith("/"):
        encoded = "/" + encoded
    if not query:
        return encoded
    return encoded + "?" + quote(query, safe="=&%+")


def tls_verify_enabled(address: str, policy: dict, *, sources_configured: bool) -> bool:
    """来源策略显式关闭校验时照做。未配置来源时，内网地址不校验证书。

    开发环境的附件服务常用 IP 和自签证书。公网地址在未配置策略时仍校验证书。
    """
    if "tls_verify" in policy:
        return bool(policy["tls_verify"])
    if sources_configured:
        return True
    ip = ipaddress.ip_address(address)
    return not (ip.is_private or ip.is_loopback)


class _PinnedHTTPS(_PinnedHTTP):
    def __init__(self, host, port, *, address, timeout, verify=True):
        super().__init__(host, port, address=address, timeout=timeout)
        self.verify = verify

    def connect(self):
        super().connect()
        context = ssl.create_default_context() if self.verify else ssl._create_unverified_context()
        self.sock = context.wrap_socket(self.sock, server_hostname=self.host)


def fetch_pdf(url: str, *, sources: dict, max_bytes: int, timeout: int, heartbeat=None):
    """不使用环境代理；重定向只允许同 origin，连接不再进行第二次 DNS 查询。"""
    started = time.monotonic()
    origin0 = None
    for _ in range(4):
        p, origin, host, port, policy = validate_url(url, sources)
        origin0 = origin0 or origin
        if origin != origin0:
            raise DownloadError('CROSS_ORIGIN_REDIRECT_DENIED')
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise DownloadError('DOWNLOAD_TIMEOUT', retryable=True)
        addresses = allowed_addresses(host, port, policy)
        cls = _PinnedHTTPS if p.scheme == 'https' else _PinnedHTTP
        kwargs = {"host": host, "port": port, "address": addresses[0], "timeout": min(remaining, 20)}
        if p.scheme == "https":
            kwargs["verify"] = tls_verify_enabled(addresses[0], policy, sources_configured=bool(sources))
        connection = cls(**kwargs)
        deadline = None
        expired = threading.Event()
        try:
            headers = dict(policy.get('headers') or {})
            if p.scheme != 'https' and headers:
                raise DownloadError('AUTH_HEADERS_REQUIRE_HTTPS')
            if any(k.lower() in {'host', 'connection', 'transfer-encoding', 'content-length', 'proxy-authorization'}
                   or '\r' in str(v) or '\n' in str(v) for k, v in headers.items()):
                raise DownloadError('SOURCE_HEADER_POLICY_INVALID')
            headers.update({'Accept': 'application/pdf', 'Accept-Encoding': 'identity'})
            connection.connect()
            active_socket = connection.sock
            def expire():
                expired.set()
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                active_socket.close()
            deadline = threading.Timer(max(0.001, timeout - (time.monotonic() - started)), expire)
            deadline.daemon = True
            deadline.start()
            connection.request('GET', request_target(p.path, p.query), headers=headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise DownloadError('REDIRECT_WITHOUT_LOCATION')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise DownloadError(f'HTTP_{response.status}', retryable=response.status in {408, 429, 500, 502, 503, 504})
            length = response.getheader('Content-Length')
            if length and (not length.isdigit() or int(length) > max_bytes):
                raise DownloadError('FILE_TOO_LARGE')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise DownloadError('ENCODED_RESPONSE_DENIED')
            chunks, size = [], 0
            last_heartbeat = time.monotonic()
            while True:
                if time.monotonic() - started > timeout:
                    raise DownloadError('DOWNLOAD_TIMEOUT', retryable=True)
                if expired.is_set():
                    raise DownloadError('DOWNLOAD_TIMEOUT', retryable=True)
                active_socket.settimeout(max(0.001, min(20, timeout - (time.monotonic() - started))))
                part = response.read1(64 * 1024)
                if not part:
                    break
                size += len(part)
                if size > max_bytes:
                    raise DownloadError('FILE_TOO_LARGE')
                chunks.append(part)
                if heartbeat and time.monotonic() - last_heartbeat >= 10:
                    heartbeat()
                    last_heartbeat = time.monotonic()
                # 短连接在最后一块读取后可能已关闭 socket；不要再设置它的超时。
                if response.isclosed():
                    break
            data = b''.join(chunks)
            if expired.is_set():
                raise DownloadError('DOWNLOAD_TIMEOUT', retryable=True)
            mime = response.getheader('Content-Type', '').split(';')[0].lower().strip()
            if not data.startswith(b'%PDF-') or mime not in {'application/pdf', 'application/octet-stream', ''}:
                raise DownloadError('PDF_TYPE_MISMATCH')
            # 签名不足以排除损坏/加密；本地 PDF 结构检查不调用 OCR 或模型。
            import fitz
            try:
                with fitz.open(stream=data, filetype='pdf') as pdf:
                    if pdf.needs_pass or pdf.page_count < 1:
                        raise DownloadError('PDF_ENCRYPTED_OR_EMPTY')
                    pages = pdf.page_count
            except DownloadError:
                raise
            except Exception as exc:
                raise DownloadError('PDF_INVALID') from exc
            return data, {'sha256': hashlib.sha256(data).hexdigest(), 'size': size, 'mime_type': 'application/pdf',
                'http_status': response.status, 'content_type': mime, 'page_count': pages,
                'duration_seconds': round(time.monotonic() - started, 3),
                # 不存来源响应中的任意文本/认证材料。
                'etag_digest': hashlib.sha256((response.getheader('ETag') or '').encode()).hexdigest(),
                'source_digest': hashlib.sha256(url.encode()).hexdigest()}
        except DownloadError:
            raise
        except ssl.SSLError as exc:
            raise DownloadError('SOURCE_TLS_ERROR') from exc
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError('DOWNLOAD_TIMEOUT' if expired.is_set() else 'SOURCE_NETWORK_ERROR', retryable=True) from exc
        finally:
            if deadline is not None:
                deadline.cancel()
            connection.close()
    raise DownloadError('TOO_MANY_REDIRECTS')
