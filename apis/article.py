import threading
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status as fast_status, Query
from core.auth import get_current_user_or_ak
from core.db import DB
from core.models.base import DATA_STATUS
from core.models.article import Article,ArticleBase
from sqlalchemy import and_, or_, desc
from .base import success_response, error_response
from core.config import cfg
from apis.base import format_search_kw
from core.print import print_warning, print_info, print_error, print_success
from core.cache import clear_cache_pattern
from tools.fix import fix_article
from core.article_content import sync_article_content
from driver.wxarticle import WXArticleFetcher
router = APIRouter(prefix=f"/articles", tags=["文章管理"])

_refresh_tasks = {}
_refresh_tasks_lock = threading.Lock()


def _set_refresh_task(task_id: str, data: dict):
    with _refresh_tasks_lock:
        _refresh_tasks[task_id] = data


def _get_active_refresh_task(article_id: str):
    with _refresh_tasks_lock:
        for task in _refresh_tasks.values():
            if task.get("article_id") != article_id:
                continue
            if task.get("status") in {"pending", "running"}:
                return dict(task)
    return None


def _run_refresh_article_task(task_id: str, article_id: str):
    session = DB.get_session()
    fetcher = None
    try:
        _set_refresh_task(task_id, {
            "task_id": task_id,
            "article_id": article_id,
            "status": "running",
            "message": "任务执行中"
        })

        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            _set_refresh_task(task_id, {
                "task_id": task_id,
                "article_id": article_id,
                "status": "failed",
                "message": "文章不存在"
            })
            return

        target_url = (article.url or "").strip()
        if not target_url:
            _set_refresh_task(task_id, {
                "task_id": task_id,
                "article_id": article_id,
                "status": "failed",
                "message": "文章缺少可抓取链接"
            })
            return

        fetcher = WXArticleFetcher()
        fetched = fetcher.get_article_content(target_url)
        fetched_content = fetched.get("content")

        if fetched_content != "DELETED" and not fetched_content:
            fetch_error = fetched.get("fetch_error") or "文章内容抓取为空"
            _set_refresh_task(task_id, {
                "task_id": task_id,
                "article_id": article_id,
                "status": "failed",
                "message": f"文章刷新失败: {fetch_error}"
            })
            return

        article.title = fetched.get("title") or article.title
        article.url = target_url
        article.publish_time = fetched.get("publish_time") or article.publish_time
        article.content = fetched_content if fetched_content is not None else article.content
        if fetched_content == "DELETED":
            article.description = fetched.get("description") or article.description
        else:
            article.description = fetched.get("description") or fetcher.get_description(article.content or "")
        article.pic_url = fetched.get("topic_image") or fetched.get("pic_url") or article.pic_url
        article.status = DATA_STATUS.DELETED if fetched_content == "DELETED" else DATA_STATUS.ACTIVE

        if fetched.get("read_num"):
            article.read_num = fetched["read_num"]
        if fetched.get("like_num"):
            article.like_num = fetched["like_num"]
        if fetched.get("old_like_num"):
            article.old_like_num = fetched["old_like_num"]
        if fetched.get("share_num"):
            article.share_num = fetched["share_num"]

        now_seconds = int(time.time())
        now_millis = int(time.time() * 1000)
        article.updated_at = now_seconds
        article.updated_at_millis = now_millis
        session.commit()

        clear_cache_pattern("articles_list")
        clear_cache_pattern("article_detail")
        clear_cache_pattern("home_page")
        clear_cache_pattern("tag_detail")

        # 同步到 Notion（非阻塞，失败不影响主流程）
        try:
            from driver.notion_sync import sync_article_to_notion
            from core.models.feed import Feed
            mp_name = ""
            feed = session.query(Feed).filter(Feed.id == article.mp_id).first()
            if feed:
                mp_name = getattr(feed, "mp_name", "") or ""
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
                kwargs={"force": True},
                daemon=True,
            ).start()
        except Exception as _notion_err:
            print_warning(f"Notion 同步启动失败: {_notion_err}")

        _set_refresh_task(task_id, {
            "task_id": task_id,
            "article_id": article_id,
            "status": "success",
            "message": "文章刷新成功",
            "updated_at": now_seconds
        })
    except Exception as e:
        session.rollback()
        _set_refresh_task(task_id, {
            "task_id": task_id,
            "article_id": article_id,
            "status": "failed",
            "message": f"文章刷新失败: {str(e)}"
        })
    finally:
        if fetcher is not None:
            try:
                fetcher.Close()
            except Exception:
                pass
        session.close()


_batch_stats_task: dict = {}
_batch_stats_lock = threading.Lock()


def _run_batch_stats_task():
    with _batch_stats_lock:
        _batch_stats_task.clear()
        _batch_stats_task.update({"status": "running", "done": 0, "total": 0, "failed": 0})
    session = DB.get_session()
    fetcher = None
    try:
        articles = session.query(ArticleBase).filter(
            ArticleBase.status != DATA_STATUS.DELETED,
            ArticleBase.url.isnot(None),
            ArticleBase.url != "",
        ).order_by(desc(ArticleBase.publish_time)).all()
        total = len(articles)
        with _batch_stats_lock:
            _batch_stats_task["total"] = total
        print_info(f"[统计批量刷新] 共 {total} 篇文章")
        for idx, art in enumerate(articles):
            url = (art.url or "").strip()
            if not url:
                continue
            try:
                fetcher = WXArticleFetcher()
                stats = fetcher.fetch_stats_only(url)
                fetcher = None
                updated = False
                if stats.get("read_num"):
                    art.read_num = stats["read_num"]
                    updated = True
                if stats.get("like_num"):
                    art.like_num = stats["like_num"]
                    updated = True
                if stats.get("old_like_num"):
                    art.old_like_num = stats["old_like_num"]
                    updated = True
                if stats.get("share_num"):
                    art.share_num = stats["share_num"]
                    updated = True
                if updated:
                    session.commit()
                    print_info(f"[统计批量刷新] [{idx+1}/{total}] 更新成功: {art.title[:20] if art.title else art.id}")
                else:
                    print_warning(f"[统计批量刷新] [{idx+1}/{total}] 未获取到统计: {art.title[:20] if art.title else art.id}")
                with _batch_stats_lock:
                    _batch_stats_task["done"] = idx + 1
            except Exception as e:
                print_error(f"[统计批量刷新] [{idx+1}/{total}] 失败: {e}")
                with _batch_stats_lock:
                    _batch_stats_task["failed"] += 1
                    _batch_stats_task["done"] = idx + 1
                try:
                    session.rollback()
                except Exception:
                    pass
            finally:
                if fetcher is not None:
                    try:
                        fetcher.Close()
                    except Exception:
                        pass
                    fetcher = None
        clear_cache_pattern("articles_list")
        with _batch_stats_lock:
            _batch_stats_task["status"] = "done"
        print_info(f"[统计批量刷新] 完成")
    except Exception as e:
        print_error(f"[统计批量刷新] 异常: {e}")
        with _batch_stats_lock:
            _batch_stats_task["status"] = "failed"
    finally:
        session.close()


def _sync_article_to_notion_with_fetch(article_id: str, mp_name: str):
    """
    后台线程：如果文章没有正文，先从链接抓取内容，再同步到 Notion。
    如果已有正文，直接同步到 Notion。
    """
    import core.db as _db
    from core.article_content import sync_article_content
    from driver.notion_sync import sync_article_to_notion
    from core.print import print_info, print_warning

    _sess = _db.DB.get_session()
    try:
        article = _sess.query(Article).filter(Article.id == article_id).first()
        if not article:
            return

        content = (article.content or "").strip()

        if not content:
            # 没有正文 → 先尝试抓取；sync_article_content 内部成功后会自动触发 Notion 同步
            print_info(f"[Notion同步] 先抓正文: {(article.title or '')[:40]}")
            updated, mode = sync_article_content(session=_sess, article=article)
            if updated:
                print_info(f"[Notion同步] 正文抓取成功 mode={mode}，Notion同步已在后台触发")
                return  # sync_article_content 已内部触发 Notion 同步，此处直接返回
            else:
                print_warning(f"[Notion同步] 正文抓取失败({mode})，仅同步基本元数据: {(article.title or '')[:40]}")
                # 正文抓取失败也要同步到 Notion（创建无正文页面）
                _sess.refresh(article)

        # 同步到 Notion（有正文用正文，无正文用元数据占位）
        article_data = {
            "url": (article.url or "").strip(),
            "title": (article.title or "").strip(),
            "publish_time": article.publish_time,
            "description": (article.description or "").strip(),
            "content": (article.content or "").strip(),
        }
        sync_article_to_notion(article_data, mp_name)
    except Exception as e:
        print_warning(f"[Notion同步] 异常: {e}")
    finally:
        _sess.close()


@router.post("/notion_sync_mp", summary="同步指定公众号最新10篇文章到Notion")
async def notion_sync_mp(
    mp_id: str = Query(..., description="公众号ID"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed

        feed = session.query(Feed).filter(Feed.id == mp_id).first()
        mp_name = feed.mp_name if feed else ""

        articles = session.query(Article).filter(
            Article.mp_id == mp_id,
            Article.status != DATA_STATUS.DELETED,
        ).order_by(Article.publish_time.desc()).limit(10).all()

        if not articles:
            return success_response({"synced": 0}, message="没有找到文章")

        count = 0
        for article in articles:
            threading.Thread(
                target=_sync_article_to_notion_with_fetch,
                args=(article.id, mp_name),
                daemon=True,
            ).start()
            count += 1

        return success_response({"synced": count}, message=f"已开始同步 {count} 篇文章到Notion（无正文的将自动抓取后同步）")
    except Exception as e:
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(code=50001, message=f"同步到Notion失败: {str(e)}")
        )
    finally:
        session.close()


@router.post("/batch_refresh_stats", summary="批量刷新文章统计(阅读/点赞/在看/转发)")
async def batch_refresh_stats(
    current_user: dict = Depends(get_current_user_or_ak)
):
    with _batch_stats_lock:
        status = _batch_stats_task.get("status")
    if status == "running":
        with _batch_stats_lock:
            return success_response(dict(_batch_stats_task), message="统计刷新任务已在运行中")
    threading.Thread(target=_run_batch_stats_task, daemon=True).start()
    return success_response({"status": "started"}, message="统计批量刷新已启动，将逐篇更新")


@router.get("/batch_refresh_stats/status", summary="查询批量统计刷新进度")
async def get_batch_stats_status(
    current_user: dict = Depends(get_current_user_or_ak)
):
    with _batch_stats_lock:
        return success_response(dict(_batch_stats_task))


@router.delete("/clean", summary="清理无效文章(MP_ID不存在于Feeds表中的文章)")
async def clean_orphan_articles(
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        from core.models.article import Article
        
        # 找出Articles表中mp_id不在Feeds表中的记录
        subquery = session.query(Feed.id).subquery()
        deleted_count = session.query(Article)\
            .filter(~Article.mp_id.in_(subquery))\
            .delete(synchronize_session=False)
        
        session.commit()
        
        # 清除相关缓存
        clear_cache_pattern("articles_list")
        clear_cache_pattern("home_page")
        clear_cache_pattern("tag_detail")
        
        return success_response({
            "message": "清理无效文章成功",
            "deleted_count": deleted_count
        })
    except Exception as e:
        session.rollback()
        print(f"清理无效文章错误: {str(e)}")
        raise HTTPException(
            status_code=fast_status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="清理无效文章失败"
            )
        )

@router.put("/{article_id}/read", summary="改变文章阅读状态")
async def toggle_article_read_status(
    article_id: str,
    is_read: bool = Query(..., description="阅读状态: true为已读, false为未读"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.article import Article
        
        # 检查文章是否存在
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            raise HTTPException(
                status_code=fast_status.HTTP_404_NOT_FOUND,
                detail=error_response(
                    code=40401,
                    message="文章不存在"
                )
            )
        
        # 更新阅读状态
        article.is_read = 1 if is_read else 0
        session.commit()
        
        # 清除相关缓存
        clear_cache_pattern("articles_list")
        clear_cache_pattern("article_detail")
        clear_cache_pattern("tag_detail")
        
        return success_response({
            "message": f"文章已标记为{'已读' if is_read else '未读'}",
            "is_read": is_read
        })
    except HTTPException as e:
        raise e
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"更新文章阅读状态失败: {str(e)}"
            )
        )


@router.put("/{article_id}/favorite", summary="改变文章收藏状态")
async def toggle_article_favorite_status(
    article_id: str,
    is_favorite: bool = Query(..., description="收藏状态: true为收藏, false为取消收藏"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            raise HTTPException(
                status_code=fast_status.HTTP_404_NOT_FOUND,
                detail=error_response(
                    code=40401,
                    message="文章不存在"
                )
            )

        article.is_favorite = 1 if is_favorite else 0
        session.commit()

        clear_cache_pattern("articles_list")
        clear_cache_pattern("article_detail")
        clear_cache_pattern("tag_detail")

        return success_response({
            "message": "文章已收藏" if is_favorite else "已取消收藏",
            "is_favorite": is_favorite
        })
    except HTTPException as e:
        raise e
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"更新文章收藏状态失败: {str(e)}"
            )
        )

@router.delete("/clean_duplicate_articles", summary="清理重复文章")
async def clean_duplicate(
    current_user: dict = Depends(get_current_user_or_ak)
):
    try:
        from tools.clean import clean_duplicate_articles
        (msg, deleted_count) =clean_duplicate_articles()
        return success_response({
            "message": msg,
            "deleted_count": deleted_count
        })
    except Exception as e:
        print(f"清理重复文章: {str(e)}")
        raise HTTPException(
            status_code=fast_status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="清理重复文章"
            )
        )


@router.api_route("", summary="获取文章列表",methods= ["GET", "POST"], operation_id="get_articles_list")
async def get_articles(
    offset: int = Query(0, ge=0),
    limit: int = Query(5, ge=1, le=100),
    status: str = Query(None),
    search: str = Query(None),
    mp_id: str = Query(None),
    only_favorite: bool = Query(False),
    has_content: bool = Query(False),
    sort_by: str = Query("publish_time", description="排序字段: publish_time | updated_at"),
    sort_order: str = Query("desc", description="排序方向: asc | desc"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
      
        
        # 构建查询条件
        query = session.query(ArticleBase)
        if has_content:
            query=session.query(Article)
        if status:
            query = query.filter(Article.status == status)
        else:
            query = query.filter(Article.status != DATA_STATUS.DELETED)
        if mp_id:
            query = query.filter(Article.mp_id == mp_id)
        if only_favorite:
            query = query.filter(Article.is_favorite == 1)
        if search:
            query = query.filter(
               format_search_kw(search)
            )
        
        # 获取总数
        total = query.count()
        # 排序
        _sort_field_map = {
            "publish_time": Article.publish_time,
            "updated_at": Article.updated_at,
        }
        _sort_col = _sort_field_map.get(sort_by, Article.publish_time)
        if sort_order == "asc":
            query = query.order_by(_sort_col.asc())
        else:
            query = query.order_by(_sort_col.desc())
        query = query.offset(offset).limit(limit)
        # 分页查询
        articles = query.all()
        
        # 打印生成的 SQL 语句（包含分页参数）
        print_warning(query.statement.compile(compile_kwargs={"literal_binds": True}))
                       
        # 查询公众号名称
        from core.models.feed import Feed
        mp_names = {}
        for article in articles:
            if article.mp_id and article.mp_id not in mp_names:
                feed = session.query(Feed).filter(Feed.id == article.mp_id).first()
                mp_names[article.mp_id] = feed.mp_name if feed else "未知公众号"
        
        # 合并公众号名称到文章列表
        article_list = []
        for article in articles:
            article_dict = article.__dict__
            article_dict["mp_name"] = mp_names.get(article.mp_id, "未知公众号")
            article_dict["is_favorite"] = int(getattr(article, "is_favorite", 0) or 0)
            article_list.append(article_dict)
        
        from .base import success_response
        return success_response({
            "list": article_list,
            "total": total
        })
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"获取文章列表失败: {str(e)}"
            )
        )

@router.post("/{article_id}/refresh", summary="刷新单篇文章")
async def refresh_article(
    article_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        article_exists = session.query(Article.id).filter(Article.id == article_id).first()
        if not article_exists:
            raise HTTPException(
                status_code=fast_status.HTTP_404_NOT_FOUND,
                detail=error_response(
                    code=40401,
                    message="文章不存在"
                )
            )

        active_task = _get_active_refresh_task(article_id)
        if active_task:
            return success_response(active_task, message="该文章已有刷新任务在执行")

        task_id = str(uuid4())
        task = {
            "task_id": task_id,
            "article_id": article_id,
            "status": "pending",
            "message": "任务已创建"
        }
        _set_refresh_task(task_id, task)

        threading.Thread(
            target=_run_refresh_article_task,
            args=(task_id, article_id),
            daemon=True
        ).start()

        return success_response(task, message="已开始刷新，请稍后查看")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"文章刷新失败: {str(e)}"
            )
        )
    finally:
        session.close()


@router.get("/refresh/tasks/{task_id}", summary="查询文章刷新任务状态")
async def get_refresh_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    with _refresh_tasks_lock:
        task = _refresh_tasks.get(task_id)
    if not task:
        raise HTTPException(
            status_code=fast_status.HTTP_404_NOT_FOUND,
            detail=error_response(
                code=40404,
                message="刷新任务不存在"
            )
        )
    return success_response(task)


@router.get("/{article_id}", summary="获取文章详情")
def get_article_detail(
    article_id: str,
    content: bool = Query(False),
    # current_user: dict = Depends(get_current_user)
):
    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id==article_id).filter(Article.status != DATA_STATUS.DELETED).first()
        if not article:
            raise HTTPException(
                status_code=fast_status.HTTP_404_NOT_FOUND,
                detail=error_response(
                    code=40401,
                    message="文章不存在"
                )
            )
        if content:
            updated, _ = sync_article_content(
                session=session,
                article=article,
                preferred_mode=cfg.get("gather.content_mode", "api"),
            )
            if updated:
                clear_cache_pattern("articles_list")
                clear_cache_pattern("article_detail")
                clear_cache_pattern("home_page")
                clear_cache_pattern("tag_detail")
        return success_response(fix_article(article))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"获取文章详情失败: {str(e)}"
            )
        )   

@router.delete("/{article_id}", summary="删除文章")
async def delete_article(
    article_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.article import Article
        
        # 检查文章是否存在
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            raise HTTPException(
                status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
                detail=error_response(
                    code=40401,
                    message="文章不存在"
                )
            )
        # 逻辑删除文章（更新状态为deleted）
        article.status = DATA_STATUS.DELETED
        if cfg.get("article.true_delete", False):
            session.delete(article)
        session.commit()
        
        return success_response(None, message="文章已标记为删除")
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"删除文章失败: {str(e)}"
            )
        )

@router.get("/{article_id}/next", summary="获取下一篇文章")
def get_next_article(
    article_id: str,
    content: bool = Query(False),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        # 获取当前文章的发布时间
        current_article = session.query(Article).filter(Article.id == article_id).first()
        if not current_article:
            raise HTTPException(
                status_code=fast_status.HTTP_404_NOT_FOUND,
                detail=error_response(
                    code=40401,
                    message="当前文章不存在"
                )
            )
        
        # 查询发布时间更晚的第一篇文章
        next_article = session.query(Article)\
            .filter(Article.publish_time > current_article.publish_time)\
            .filter(Article.status != DATA_STATUS.DELETED)\
            .filter(Article.mp_id == current_article.mp_id)\
            .order_by(Article.publish_time.asc())\
            .first()
        
        if not next_article:
            raise HTTPException(
                status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
                detail=error_response(
                    code=40402,
                    message="没有下一篇文章"
                )
            )
        if content:
            updated, _ = sync_article_content(
                session=session,
                article=next_article,
                preferred_mode=cfg.get("gather.content_mode", "api"),
            )
            if updated:
                clear_cache_pattern("articles_list")
                clear_cache_pattern("article_detail")
                clear_cache_pattern("home_page")
                clear_cache_pattern("tag_detail")
        return success_response(fix_article(next_article))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"获取下一篇文章失败: {str(e)}"
            )
        )

@router.get("/{article_id}/prev", summary="获取上一篇文章")
def get_prev_article(
    article_id: str,
    content: bool = Query(False),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        # 获取当前文章的发布时间
        current_article = session.query(Article).filter(Article.id == article_id).first()
        if not current_article:
            raise HTTPException(
                status_code=fast_status.HTTP_404_NOT_FOUND,
                detail=error_response(
                    code=40401,
                    message="当前文章不存在"
                )
            )
        
        # 查询发布时间更早的第一篇文章
        prev_article = session.query(Article)\
            .filter(Article.publish_time < current_article.publish_time)\
            .filter(Article.status != DATA_STATUS.DELETED)\
            .filter(Article.mp_id == current_article.mp_id)\
            .order_by(Article.publish_time.desc())\
            .first()
        
        if not prev_article:
            raise HTTPException(
                status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
                detail=error_response(
                    code=40403,
                    message="没有上一篇文章"
                )
            )
        if content:
            updated, _ = sync_article_content(
                session=session,
                article=prev_article,
                preferred_mode=cfg.get("gather.content_mode", "api"),
            )
            if updated:
                clear_cache_pattern("articles_list")
                clear_cache_pattern("article_detail")
                clear_cache_pattern("home_page")
                clear_cache_pattern("tag_detail")
        return success_response(fix_article(prev_article))
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=fast_status.HTTP_406_NOT_ACCEPTABLE,
            detail=error_response(
                code=50001,
                message=f"获取上一篇文章失败: {str(e)}"
            )
        )
