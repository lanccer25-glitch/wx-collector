
import core.wx as wx 
import core.db as db
from core.config import DEBUG, cfg
from core.models.article import Article
import threading
import time as _time

DB = db.Db(tag="文章采集API")


def _fetch_content_and_sync_notion(article_url: str):
    """
    后台线程：为新入库的文章抓取正文，抓完后自动同步到 Notion。
    复用 sync_article_content（内部已含 Notion 同步逻辑）。
    """
    import core.db as _db
    from core.article_content import sync_article_content
    from core.print import print_warning, print_info

    if not article_url:
        return

    _sess = _db.DB.get_session()
    try:
        article = _sess.query(Article).filter(Article.url == article_url).first()
        if not article:
            return
        updated, mode = sync_article_content(session=_sess, article=article)
        if updated:
            print_info(f"[采集] 新文章正文抓取成功: {(article.title or '')[:40]} mode={mode}")
        else:
            print_warning(f"[采集] 新文章正文抓取失败或跳过: {(article.title or '')[:40]} mode={mode}")
    except Exception as e:
        print_warning(f"[采集] 后台抓取正文异常: {e}")
    finally:
        _sess.close()


def UpdateArticle(art: dict, check_exist=False):
    if DEBUG:
        pass
    if DB.add_article(art, check_exist=check_exist):
        article_url = (art.get("url") or "").strip()
        if article_url:
            threading.Thread(
                target=_fetch_content_and_sync_notion,
                args=(article_url,),
                daemon=True,
            ).start()
        return True
    return False


_ONE_YEAR_SECONDS = 365 * 24 * 3600


def UpdateArticleRecent(art: dict, check_exist=False):
    """
    仅入库近一年内发布的文章（用于新增订阅公众号时的首次抓取）。
    publish_time 是 Unix 时间戳整数，超过一年的直接丢弃。
    """
    publish_ts = art.get("publish_time") or 0
    try:
        publish_ts = int(publish_ts)
    except (TypeError, ValueError):
        publish_ts = 0
    if publish_ts > 0 and publish_ts < (_time.time() - _ONE_YEAR_SECONDS):
        return False
    return UpdateArticle(art, check_exist=check_exist)


def Update_Over(data=None):
    print("更新完成")
    pass


def make_time_filtered_callback(since_ts: int = 0, until_ts: int = 0):
    """
    创建带时间范围过滤的文章回调。
    since_ts: 仅入库该时间戳之后发布的文章 (Unix秒，0表示不限)
    until_ts: 仅入库该时间戳之前发布的文章 (Unix秒，0表示不限)
    """
    def _callback(art: dict, check_exist=False):
        publish_ts = int(art.get("publish_time") or 0)
        if since_ts > 0 and publish_ts > 0 and publish_ts < since_ts:
            return False
        if until_ts > 0 and publish_ts > 0 and publish_ts > until_ts:
            return False
        return UpdateArticle(art, check_exist=check_exist)
    return _callback
