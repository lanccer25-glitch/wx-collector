from fastapi import APIRouter, Request
from fastapi.responses import Response
import httpx
import os
import hashlib
import time
import json
import asyncio
from core.config import cfg

CACHE_DIR = cfg.get("cache.dir", "data/cache")
CACHE_TTL = 3600

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)

router = APIRouter(prefix="/res", tags=["资源反向代理"])


def _read_cache(cache_filename: str):
    if not os.path.exists(cache_filename):
        return None, None
    if time.time() - os.path.getmtime(cache_filename) >= CACHE_TTL:
        return None, None
    with open(cache_filename, "rb") as f:
        content = f.read()
    headers_filename = cache_filename + ".headers"
    headers = {}
    if os.path.exists(headers_filename):
        with open(headers_filename, "r", encoding="utf-8") as f:
            headers = json.load(f)
    return content, headers


def _write_cache(cache_filename: str, content: bytes, headers: dict):
    try:
        with open(cache_filename, "wb") as f:
            f.write(content)
        with open(cache_filename + ".headers", "w", encoding="utf-8") as f:
            json.dump(headers, f)
    except Exception as e:
        print(f"缓存响应失败: {e}")


@router.api_route(
    "/logo/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    operation_id="reverse_proxy_logo",
)
async def reverse_proxy(request: Request, path: str):
    allowed_hosts = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn", "mmecoa.qpic.cn"}

    path = path.replace("https://", "http://").replace("https:/", "http://")
    if path.startswith("http:/") and not path.startswith("http://"):
        path = "http://" + path[6:]

    from urllib.parse import urlparse
    parsed_url = urlparse(path)
    host = parsed_url.netloc
    if host not in allowed_hosts:
        return Response(
            content="只允许访问微信公众号图标，请使用正确的域名。",
            status_code=301,
            headers={"Location": path},
        )

    cache_key = f"{request.method}_{path}".encode("utf-8")
    cache_filename = os.path.join(CACHE_DIR, hashlib.sha256(cache_key).hexdigest())

    content, headers = await asyncio.to_thread(_read_cache, cache_filename)
    if content is not None:
        return Response(
            content=content,
            status_code=200,
            headers=headers,
            media_type=headers.get("Content-Type"),
        )

    request_data = await request.body()
    resp = await _client.request(
        method=request.method,
        url=path,
        content=request_data,
    )

    content = resp.content
    status_code = resp.status_code
    resp_headers = dict(resp.headers)
    media_type = resp.headers.get("Content-Type")

    await asyncio.to_thread(_write_cache, cache_filename, content, resp_headers)

    return Response(
        content=content,
        status_code=status_code,
        headers=resp_headers,
        media_type=media_type,
    )
