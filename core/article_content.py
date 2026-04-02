from __future__ import annotations

import threading
from typing import Any, Tuple

from core.config import cfg
from core.models.base import DATA_STATUS
from core.print import print_info, print_warning

# 全局并发限制：最多同时启动 2 个浏览器实例，防止线程资源耗尽
_web_fetch_semaphore = threading.Semaphore(2)


def normalize_content_mode(mode: str | None = None) -> str:
    normalized = (mode or cfg.get("gather.content_mode", "api") or "api").strip().lower()
    if normalized not in {"web", "api"}:
        return "api"
    return normalized


def extract_origin_article_id(article_id: str, mp_id: str | None = None) -> str:
    if not article_id:
        return ""

    mp_prefix = (mp_id or "").replace("MP_WXS_", "").strip()
    if mp_prefix:
        prefixed = f"{mp_prefix}-"
        if article_id.startswith(prefixed):
            return article_id[len(prefixed):]

    return article_id


def build_article_url(article: Any) -> str:
    article_url = (getattr(article, "url", "") or "").strip()
    if article_url:
        return article_url

    origin_id = extract_origin_article_id(
        getattr(article, "id", ""),
        getattr(article, "mp_id", ""),
    )
    if not origin_id:
        return ""

    return f"https://mp.weixin.qq.com/s/{origin_id}"


def _fetch_with_web(url: str) -> dict:
    from driver.wxarticle import Web

    with _web_fetch_semaphore:
        result = Web.get_article_content(url) or {}
    return result


def _fetch_with_api(url: str) -> str:
    from core.wx.model.api import MpsApi

    fetcher = MpsApi()
    return (fetcher.content_extract(url) or "").strip()


def fetch_article_content(url: str, preferred_mode: str | None = None) -> Tuple[str, str, dict]:
    """
    Returns (content, mode, extra_data).
    extra_data may contain: read_num, like_num, old_like_num, share_num.
    """
    mode = normalize_content_mode(preferred_mode)
    modes = [mode] + [item for item in ("web", "api") if item != mode]

    for current_mode in modes:
        try:
            if current_mode == "api":
                content = _fetch_with_api(url)
                extra = {}
            else:
                result = _fetch_with_web(url)
                content = (result.get("content") or "").strip()
                extra = {
                    "read_num": result.get("read_num", 0),
                    "like_num": result.get("like_num", 0),
                    "old_like_num": result.get("old_like_num", 0),
                    "share_num": result.get("share_num", 0),
                }
        except Exception as exc:
            print_warning(f"fetch article content failed in {current_mode} mode: {exc}")
            continue

        if content == "DELETED":
            return content, current_mode, {}
        if content:
            return content, current_mode, extra

    return "", mode, {}


def sync_article_content(
    session,
    article: Any,
    preferred_mode: str | None = None,
    force: bool = False,
) -> Tuple[bool, str]:
    existing_content = (getattr(article, "content", "") or "").strip()
    if existing_content and not force:
        return False, "cached"

    article_url = build_article_url(article)
    if not article_url:
        print_warning(f"article {getattr(article, 'id', '')} has no valid url")
        return False, "missing_url"

    content, mode, extra = fetch_article_content(article_url, preferred_mode)
    if not content:
        return False, mode

    try:
        if content == "DELETED":
            article.content = ""
            article.content_html = ""
            article.status = DATA_STATUS.DELETED
            session.commit()
            session.refresh(article)
            print_info(f"article {article.id} marked as deleted via {mode}")
            return True, mode

        from driver.wxarticle import Web
        from tools.fix import fix_html

        article.content = content
        article.content_html = fix_html(content)
        article.status = DATA_STATUS.ACTIVE
        if not (getattr(article, "description", "") or "").strip():
            article.description = Web.get_description(content)

        if extra.get("read_num"):
            article.read_num = extra["read_num"]
        if extra.get("like_num"):
            article.like_num = extra["like_num"]
        if extra.get("old_like_num"):
            article.old_like_num = extra["old_like_num"]
        if extra.get("share_num"):
            article.share_num = extra["share_num"]

        session.commit()
        session.refresh(article)
        print_info(f"article {article.id} content synced via {mode}, stats: read={extra.get('read_num',0)} like={extra.get('like_num',0)}")

        # 同步到 Notion（非阻塞，失败不影响主流程）
        try:
            from driver.notion_sync import sync_article_to_notion
            from core.models.feed import Feed
            import threading
            mp_name = ""
            feed = session.query(Feed).filter(Feed.id == getattr(article, "mp_id", None)).first()
            if feed:
                mp_name = getattr(feed, "mp_name", "") or ""
            # 提取为纯 Python 值再传给子线程，避免 SQLAlchemy session 跨线程竞态
            article_data = {
                "url": (article.url or "").strip(),
                "title": (article.title or "").strip(),
                "publish_time": article.publish_time,
                "description": (article.description or "").strip(),
                "content": (article.content or "").strip(),
            }
            threading.Thread(
                target=sync_article_to_notion,
                args=(article_data, mp_name),
                daemon=True,
            ).start()
        except Exception as _notion_err:
            print_warning(f"Notion 同步启动失败: {_notion_err}")

        return True, mode
    except Exception:
        session.rollback()
        raise
