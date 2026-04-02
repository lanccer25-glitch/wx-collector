"""
Notion 同步模块 - 将微信文章同步到 Notion 数据库
使用 Replit Notion OAuth 集成（NOTION_TOKEN 环境变量）
"""
from __future__ import annotations

import os
import re
import html
from datetime import datetime, timezone
from typing import Optional

import requests

from core.print import print_info, print_warning

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
_BLOCK_MAX = 2000
_BLOCKS_PER_REQUEST = 100


def _get_token() -> Optional[str]:
    # Prefer manually set NOTION_TOKEN, fall back to NOTION_OAUTH_TOKEN
    return (
        os.environ.get("NOTION_TOKEN", "").strip()
        or os.environ.get("NOTION_OAUTH_TOKEN", "").strip()
        or None
    )


def _get_db_id() -> Optional[str]:
    return os.environ.get("NOTION_DATABASE_ID", "").strip() or None


def _headers() -> dict:
    token = _get_token()
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _is_configured() -> bool:
    return bool(_get_token() and _get_db_id())


def _get_notion_page_id(article_url: str) -> Optional[str]:
    """通过文章链接查找已有 Notion 页面，返回 page_id 或 None"""
    db_id = _get_db_id()
    try:
        resp = requests.post(
            f"{NOTION_API_BASE}/databases/{db_id}/query",
            headers=_headers(),
            json={
                "filter": {
                    "property": "文章链接",
                    "url": {"equals": article_url},
                },
                "page_size": 1,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            return results[0]["id"] if results else None
        return None
    except Exception as e:
        print_warning(f"[Notion] 检查重复失败: {e}")
        return None


def _article_exists_in_notion(article_url: str) -> bool:
    return _get_notion_page_id(article_url) is not None


def _clear_page_blocks(page_id: str) -> None:
    """删除页面下所有现有 block（用于覆盖更新正文）"""
    hdrs = _headers()
    try:
        resp = requests.get(
            f"{NOTION_API_BASE}/blocks/{page_id}/children?page_size=100",
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            return
        blocks = resp.json().get("results", [])
        for block in blocks:
            block_id = block.get("id")
            if block_id:
                requests.delete(
                    f"{NOTION_API_BASE}/blocks/{block_id}",
                    headers=hdrs,
                    timeout=10,
                )
    except Exception as e:
        print_warning(f"[Notion] 清除旧正文失败: {e}")


def _format_date(ts) -> Optional[str]:
    """将 Unix 时间戳转为 Notion date 格式 (ISO 8601)"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


def _notion_len(s: str) -> int:
    """Notion uses JavaScript-style string length: characters above U+FFFF count as 2."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _notion_safe_slice(s: str, max_len: int) -> tuple:
    """Return (head, tail) where _notion_len(head) <= max_len."""
    count = 0
    for i, c in enumerate(s):
        step = 2 if ord(c) > 0xFFFF else 1
        if count + step > max_len:
            return s[:i], s[i:]
        count += step
    return s, ""


def _para_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _image_block(url: str) -> dict:
    return {
        "object": "block",
        "type": "image",
        "image": {"type": "external", "external": {"url": url}},
    }


def _text_to_para_blocks(text: str) -> list:
    """将纯文本按换行切成 paragraph block 列表，每块 ≤ _BLOCK_MAX（Notion 计数方式）"""
    blocks = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        while _notion_len(para) > _BLOCK_MAX:
            chunk, para = _notion_safe_slice(para, _BLOCK_MAX)
            blocks.append(_para_block(chunk))
        if para:
            blocks.append(_para_block(para))
    return blocks


_IMG_PLACEHOLDER = "\x00IMG\x00"


def _html_to_blocks(raw: str) -> list:
    """
    将微信文章 HTML 转成 Notion block 列表，图片以 image block 写入，其余为 paragraph block。
    顺序与原文保持一致。
    """
    if not raw:
        return []

    # 1. 删除 script / style / noscript / svg / 注释 整块内容
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<svg[^>]*>.*?</svg>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # 2. 把 <img> 替换成占位符，记录 URL 顺序
    img_urls: list = []

    def _replace_img(m: re.Match) -> str:
        tag = m.group(0)
        # 优先取 src，其次 data-src
        url_m = re.search(r'\bsrc="(https?://[^"]+)"', tag)
        if not url_m:
            url_m = re.search(r"\bsrc='(https?://[^']+)'", tag)
        if not url_m:
            url_m = re.search(r'\bdata-src="(https?://[^"]+)"', tag)
        if not url_m:
            url_m = re.search(r"\bdata-src='(https?://[^']+)'", tag)
        if url_m:
            img_urls.append(url_m.group(1))
            return f"\n{_IMG_PLACEHOLDER}{len(img_urls) - 1}\n"
        return ""  # 无法提取 URL 则忽略

    text = re.sub(r"<img[^>]*>", _replace_img, text, flags=re.IGNORECASE | re.DOTALL)

    # 3. 去掉其余 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 4. 按行拼块，遇到占位符生成 image block，其余文本生成 paragraph block
    blocks: list = []
    buffer_lines: list = []

    def _flush_buffer():
        if buffer_lines:
            para_text = "\n".join(buffer_lines).strip()
            buffer_lines.clear()
            if para_text:
                blocks.extend(_text_to_para_blocks(para_text))

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(_IMG_PLACEHOLDER):
            _flush_buffer()
            try:
                idx = int(stripped[len(_IMG_PLACEHOLDER):])
                url = img_urls[idx]
                # 过滤掉 data URI 和超长 URL（Notion 不支持）
                if url.startswith("http") and len(url) <= 2000:
                    blocks.append(_image_block(url))
            except (ValueError, IndexError):
                pass
        else:
            buffer_lines.append(line)

    _flush_buffer()
    return blocks


def _append_blocks(page_id: str, blocks: list) -> None:
    """向已创建的 Notion 页面追加 block（分批，每批最多 _BLOCKS_PER_REQUEST 块）"""
    import time
    hdrs = _headers()
    for i in range(0, len(blocks), _BLOCKS_PER_REQUEST):
        batch = blocks[i: i + _BLOCKS_PER_REQUEST]
        for attempt in range(3):
            try:
                resp = requests.patch(
                    f"{NOTION_API_BASE}/blocks/{page_id}/children",
                    headers=hdrs,
                    json={"children": batch},
                    timeout=15,
                )
                if resp.status_code == 200:
                    break
                # 409/400/404 有时是 Notion 处理延迟，稍等后重试
                if attempt < 2:
                    time.sleep(1.5)
                else:
                    print_warning(f"[Notion] 追加正文失败 {resp.status_code}: {resp.text[:200]}")
                    return
            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5)
                else:
                    print_warning(f"[Notion] 追加正文异常: {e}")
                    return


def sync_article_to_notion(article_data: dict, mp_name: str = "", force: bool = False) -> bool:
    """
    将单篇文章同步到 Notion 数据库。
    article_data: 纯 Python dict，包含 url/title/publish_time/description/content
    mp_name: 公众号名称（纯字符串）
    force: True 时跳过去重检查，直接写入（用于刷新按钮强制重写）
    返回: True=成功, False=跳过或失败
    """
    if not _is_configured():
        return False

    article_url = (article_data.get("url") or "").strip()
    if not article_url:
        return False

    title = (article_data.get("title") or "").strip() or "无标题"
    publish_time = article_data.get("publish_time")
    digest = (article_data.get("description") or "").strip()
    content_raw = (article_data.get("content") or "").strip()
    pub_date = _format_date(publish_time)

    all_blocks = _html_to_blocks(content_raw) if content_raw else []

    # 检查是否已有 Notion 页面
    existing_page_id = None if force else _get_notion_page_id(article_url)

    if existing_page_id:
        if content_raw:
            # 已有页面 + 有真实正文 → 更新页面内容
            _clear_page_blocks(existing_page_id)
            first_batch = all_blocks[:_BLOCKS_PER_REQUEST]
            remaining_blocks = all_blocks[_BLOCKS_PER_REQUEST:]
            _append_blocks(existing_page_id, first_batch)
            if remaining_blocks:
                _append_blocks(existing_page_id, remaining_blocks)
            print_info(f"[Notion] 正文已更新: {title[:40]} (正文 {len(all_blocks)} 块)")
        else:
            print_info(f"[Notion] 页面已存在，暂无正文: {title[:40]}")
        # 确保 DB 里的 notion_page_id 已记录
        if existing_page_id and article_url:
            try:
                from core.db import DB
                from core.models.article import Article
                _session = DB.get_session()
                try:
                    _art = _session.query(Article).filter(Article.url == article_url).first()
                    if _art and not _art.notion_page_id:
                        _art.notion_page_id = existing_page_id
                        _session.commit()
                except Exception:
                    _session.rollback()
                finally:
                    _session.close()
            except Exception:
                pass
        return True

    properties: dict = {
        "文章标题": {"title": [{"text": {"content": title[:2000]}}]},
        "文章链接": {"url": article_url},
        "状态": {"status": {"name": "待审核"}},
    }

    if pub_date:
        properties["发布时间"] = {"date": {"start": pub_date}}

    if mp_name:
        properties["公众号"] = {"select": {"name": mp_name[:100]}}

    first_batch = all_blocks[:_BLOCKS_PER_REQUEST]
    remaining_blocks = all_blocks[_BLOCKS_PER_REQUEST:]

    db_id = _get_db_id()
    payload: dict = {"parent": {"database_id": db_id}, "properties": properties}
    if first_batch:
        payload["children"] = first_batch

    try:
        resp = requests.post(
            f"{NOTION_API_BASE}/pages",
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            page_id = resp.json().get("id", "")
            if content_raw:
                print_info(f"[Notion] 文章已同步: {title[:40]} (正文 {len(all_blocks)} 块)")
                if remaining_blocks and page_id:
                    _append_blocks(page_id, remaining_blocks)
            else:
                print_info(f"[Notion] 文章已创建(无正文): {title[:40]}")
            # 将 notion_page_id 写回 DB
            if page_id and article_url:
                try:
                    from core.db import DB
                    from core.models.article import Article
                    _session = DB.get_session()
                    try:
                        _art = _session.query(Article).filter(Article.url == article_url).first()
                        if _art:
                            _art.notion_page_id = page_id
                            _session.commit()
                    except Exception:
                        _session.rollback()
                    finally:
                        _session.close()
                except Exception:
                    pass
            return True
        else:
            err = resp.json().get("message", resp.text[:200])
            print_warning(f"[Notion] 同步失败 {resp.status_code}: {err}")
            return False
    except Exception as e:
        print_warning(f"[Notion] 同步异常: {e}")
        return False
