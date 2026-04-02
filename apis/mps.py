from logging import info
from fastapi import APIRouter, Depends, HTTPException, status, Query, Body, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.background import BackgroundTasks
from core.auth import get_current_user_or_ak
from core.db import DB
from core.wx import search_Biz
from driver.wx import Wx
from .base import success_response, error_response
from datetime import datetime
from core.config import cfg
from core.res import save_avatar_locally
from core.models.feed import FEATURED_MP_ID, FEATURED_MP_NAME, FEATURED_MP_INTRO
from core.models.base import DATA_STATUS
from core.cache import clear_cache_pattern
import io
import os
from jobs.article import UpdateArticle, UpdateArticleRecent
from driver.wxarticle import WXArticleFetcher
import threading
from uuid import uuid4
router = APIRouter(prefix=f"/mps", tags=["公众号管理"])
# import core.db as db
# UPDB=db.Db("数据抓取")
# def UpdateArticle(art:dict):
#             return UPDB.add_article(art)


def build_featured_mp_item():
    now = datetime.now().isoformat()
    return {
        "id": FEATURED_MP_ID,
        "mp_name": FEATURED_MP_NAME,
        "mp_cover": "/static/logo.svg",
        "mp_intro": FEATURED_MP_INTRO,
        "status": 1,
        "created_at": now,
        "is_system": True
    }


_featured_article_tasks = {}
_featured_article_tasks_lock = threading.Lock()


def _set_featured_article_task(task_id: str, data: dict):
    with _featured_article_tasks_lock:
        _featured_article_tasks[task_id] = data


def _ensure_featured_feed(session):
    from core.models.feed import Feed

    featured_feed = session.query(Feed).filter(Feed.id == FEATURED_MP_ID).first()
    if featured_feed:
        return featured_feed

    now = datetime.now()
    featured_feed = Feed(
        id=FEATURED_MP_ID,
        mp_name=FEATURED_MP_NAME,
        mp_cover="logo.svg",
        mp_intro=FEATURED_MP_INTRO,
        status=1,
        sync_time=0,
        update_time=0,
        created_at=now,
        updated_at=now,
        faker_id=FEATURED_MP_ID
    )
    session.add(featured_feed)
    return featured_feed


def _run_add_featured_article_task(task_id: str, url: str):
    session = DB.get_session()
    fetcher = None
    try:
        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": url,
            "status": "running",
            "message": "任务执行中"
        })

        from core.models.article import Article

        target_url = str(url or "").strip()
        if not target_url:
            raise ValueError("请输入文章链接")

        fetcher = WXArticleFetcher()
        info = fetcher.get_article_content(target_url)
        if not info or info.get("fetch_error"):
            raise ValueError(info.get("fetch_error") or "文章抓取失败，请检查链接或登录状态")

        if info.get("content") == "DELETED":
            raise ValueError("该文章暂不可访问或已删除")

        raw_article_id = info.get("id") or fetcher.extract_id_from_url(target_url)
        if not raw_article_id:
            raise ValueError("无法解析文章ID，请确认链接格式")

        _ensure_featured_feed(session)

        article_id = f"{FEATURED_MP_ID}-{raw_article_id}".replace("MP_WXS_", "")
        now = datetime.now()
        publish_time = info.get("publish_time")
        if not isinstance(publish_time, int):
            try:
                publish_time = int(publish_time)
            except Exception:
                publish_time = int(now.timestamp())

        article_data = {
            "title": info.get("title") or target_url,
            "description": info.get("description") or fetcher.get_description(info.get("content") or ""),
            "content": info.get("content") or "",
            "publish_time": publish_time,
            "url": target_url,
            "pic_url": info.get("topic_image") or info.get("pic_url") or "",
        }

        existing = session.query(Article).filter(Article.id == article_id).first()
        if existing:
            existing.mp_id = FEATURED_MP_ID
            existing.title = article_data["title"]
            existing.description = article_data["description"]
            existing.content = article_data["content"]
            existing.publish_time = article_data["publish_time"]
            existing.url = article_data["url"]
            existing.pic_url = article_data["pic_url"]
            existing.status = DATA_STATUS.ACTIVE
            existing.updated_at = int(now.timestamp())
            existing.updated_at_millis = int(now.timestamp() * 1000)
            created = False
        else:
            session.add(Article(
                id=article_id,
                mp_id=FEATURED_MP_ID,
                title=article_data["title"],
                description=article_data["description"],
                content=article_data["content"],
                publish_time=article_data["publish_time"],
                url=article_data["url"],
                pic_url=article_data["pic_url"],
                status=DATA_STATUS.ACTIVE,
                created_at=now,
                updated_at=int(now.timestamp()),
                updated_at_millis=int(now.timestamp() * 1000),
                is_read=0,
                is_favorite=0
            ))
            created = True

        session.commit()
        clear_cache_pattern("articles_list")
        clear_cache_pattern("article_detail")
        clear_cache_pattern("home_page")
        clear_cache_pattern("tag_detail")

        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": target_url,
            "status": "success",
            "message": "精选文章添加成功" if created else "精选文章更新成功",
            "id": article_id,
            "mp_id": FEATURED_MP_ID,
            "mp_name": FEATURED_MP_NAME,
            "title": article_data["title"],
            "created": created
        })
    except Exception as e:
        session.rollback()
        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": url,
            "status": "failed",
            "message": str(e)
        })
    finally:
        if fetcher is not None:
            try:
                fetcher.Close()
            except Exception:
                pass
        session.close()


@router.post("/refresh-all", summary="立刻采集所有公众号最新文章")
async def refresh_all_mps(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """触发立即采集所有公众号的最新文章，后台异步执行，接口立即返回。
    cascade.enabled=True 时分发给子节点，否则本地执行。"""

    if cfg.get("cascade.enabled", False) and cfg.get("cascade.node_type", "parent") == "parent":
        def _do_cascade():
            from jobs.cascade_task_dispatcher import cascade_task_dispatcher
            from core.models.message_task import MessageTask
            import uuid
            session = DB.get_session()
            try:
                tasks = session.query(MessageTask).filter(MessageTask.status == 0).all()
                if not tasks:
                    print("[refresh-all cascade] 未找到启用的级联任务，无法分发")
                    return
                run_id = str(uuid.uuid4())
                for task in tasks:
                    cascade_task_dispatcher.dispatch_task_to_children(task, run_id)
                print(f"[refresh-all cascade] 已分发 {len(tasks)} 个任务给子节点 (run_id: {run_id})")
            except Exception as e:
                print(f"[refresh-all cascade] 分发失败: {e}")
            finally:
                session.close()

        threading.Thread(target=_do_cascade, daemon=True).start()
        return success_response({"message": "已通过 Cascade 分发给子节点，请稍后刷新查看采集进度"})

    def _do_refresh():
        from core.models.feed import Feed
        from core.wx import WxGather
        session = DB.get_session()
        try:
            feeds = session.query(Feed).filter(Feed.status != 1000).all()
            for feed in feeds:
                try:
                    wx = WxGather().Model()
                    wx.get_Articles(
                        feed.faker_id,
                        CallBack=UpdateArticleRecent,
                        Mps_id=feed.id,
                        Mps_title=feed.mp_name,
                        MaxPage=1,
                    )
                except Exception as e:
                    print(f"[refresh-all] {feed.mp_name} 采集失败: {e}")
        finally:
            session.close()

    threading.Thread(target=_do_refresh, daemon=True).start()
    return success_response({"message": "已开始后台采集，请稍后刷新查看最新文章"})


@router.get("/search/{kw}", summary="搜索公众号")
async def search_mp(
    kw: str = "",
    limit: int = 10,
    offset: int = 0,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        result = search_Biz(kw,limit=limit,offset=offset)
        data={
            'list':result.get('list') if result is not None else [],
            'page':{
                'limit':limit,
                'offset':offset
            },
            'total':result.get('total') if result is not None else 0
        }
        return success_response(data)
    except Exception as e:
        print(f"搜索公众号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message=f"搜索公众号失败,请重新扫码授权！",
            )
        )

@router.get("", summary="获取公众号列表")
async def get_mps(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    kw: str = Query(""),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        query = session.query(Feed).filter(Feed.id != FEATURED_MP_ID)
        if kw:
            query = query.filter(Feed.mp_name.ilike(f"%{kw}%"))
        total = query.count() + 1
        mps = query.order_by(Feed.created_at.desc()).limit(limit).offset(offset).all()
        mps_list = [{
                "id": mp.id,
                "mp_name": mp.mp_name,
                "mp_cover": mp.mp_cover,
                "mp_intro": mp.mp_intro,
                "status": mp.status,
                "created_at": mp.created_at.isoformat()
            } for mp in mps]
        if offset == 0:
            mps_list.insert(0, build_featured_mp_item())
        return success_response({
            "list": mps_list,
            "page": {
                "limit": limit,
                "offset": offset,
                "total": total
            },
            "total": total
        })
    except Exception as e:
        print(f"获取公众号列表错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="获取公众号列表失败"
            )
        )


@router.post("/featured/article", summary="添加精选文章")
async def add_featured_article(
    url: str = Body(..., embed=True, min_length=1),
    current_user: dict = Depends(get_current_user_or_ak)
):
    try:
        target_url = str(url or "").strip()
        if not target_url:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40001,
                    message="请输入文章链接"
                )
            )
        if "mp.weixin.qq.com/s/" not in target_url:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40002,
                    message="请输入有效的公众号文章链接"
                )
            )

        task_id = str(uuid4())
        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": target_url,
            "status": "pending",
            "message": "任务已创建"
        })
        threading.Thread(
            target=_run_add_featured_article_task,
            args=(task_id, target_url),
            daemon=True
        ).start()

        return success_response({
            "task_id": task_id,
            "url": target_url,
            "status": "pending"
        }, message="已开始添加/抓取，请稍后刷新查看结果")
    except HTTPException:
        raise
    except Exception as e:
        print(f"添加精选文章任务启动失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="添加精选文章失败"
            )
        )


@router.get("/featured/article/tasks/{task_id}", summary="查询精选文章添加任务状态")
async def get_featured_article_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    with _featured_article_tasks_lock:
        task = _featured_article_tasks.get(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response(
                code=40404,
                message="任务不存在"
            )
        )
    return success_response(task)

@router.get("/update/{mp_id}", summary="更新公众号文章")
async def update_mps(
     mp_id: str,
     start_page: int = 0,
     end_page: int = 1,
     since_ts: int = 0,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
           return error_response(
                    code=40401,
                    message="请选择一个公众号"
                )
        import time
        sync_interval=cfg.get("sync_interval",60)
        if mp.update_time is None:
            mp.update_time=int(time.time())-sync_interval
        time_span=int(time.time())-int(mp.update_time)
        print(f"[账号刷新] mp_id={mp_id} mp_name={mp.mp_name} time_span={time_span}s sync_interval={sync_interval}s since_ts={since_ts}")
        if time_span<sync_interval:
            wait_sec = sync_interval - time_span
            print(f"[账号刷新] 拒绝：距上次更新仅 {time_span}s，需再等 {wait_sec}s")
            return error_response(
                    code=40402,
                    message=f"操作过于频繁，请 {wait_sec} 秒后再试",
                    data={"time_span":time_span,"wait_sec":wait_sec}
                )

        from jobs.article import UpdateArticle as _UpdateArticle, make_time_filtered_callback
        callback = _UpdateArticle
        effective_max_page = end_page
        if since_ts > 0:
            days_back = max(1, (time.time() - since_ts) / 86400)
            # 取前端传入的 end_page 与按天数估算页数的较大值，不再硬性封顶 50
            effective_max_page = max(end_page, int(days_back / 5) + 2)
            callback = make_time_filtered_callback(since_ts=since_ts)

        print(f"[账号刷新] 开始抓取 {mp.mp_name}，MaxPage={effective_max_page}，since_ts={since_ts}")

        # Cascade 模式：单账号也分发给子节点
        if cfg.get("cascade.enabled", False) and cfg.get("cascade.node_type", "parent") == "parent":
            _mp_id = mp.id
            _mp_name = mp.mp_name
            def _do_cascade_single():
                from jobs.cascade_task_dispatcher import cascade_task_dispatcher
                from core.models.message_task import MessageTask
                from core.models.feed import Feed as _Feed
                import uuid
                inner_session = DB.get_session()
                try:
                    task = inner_session.query(MessageTask).filter(MessageTask.status == 0).first()
                    if not task:
                        print(f"[单账号cascade] 未找到启用任务，跳过 {_mp_name}")
                        return
                    feed = inner_session.query(_Feed).filter(_Feed.id == _mp_id).first()
                    if not feed:
                        return
                    run_id = str(uuid.uuid4())
                    allocation = cascade_task_dispatcher.create_pending_allocation(task, [feed], run_id)
                    if allocation:
                        cascade_task_dispatcher.notify_children_new_task(allocation.id, 1)
                        print(f"[单账号cascade] 已创建待认领任务: {_mp_name} (allocation: {allocation.id})")
                except Exception as e:
                    print(f"[单账号cascade] 分发失败: {e}")
                finally:
                    inner_session.close()

            import threading as _threading
            _threading.Thread(target=_do_cascade_single, daemon=True).start()
            return success_response({
                "time_span": time_span,
                "list": [],
                "total": 0,
                "mps": mp,
                "cascade": True,
                "message": f"已通过 Cascade 分发 {mp.mp_name} 给子节点"
            })

        result=[]
        def UpArt(mp):
            from core.wx import WxGather
            print(f"[账号刷新-线程] 启动 Playwright 抓取 {mp.mp_name}")
            wx=WxGather().Model()
            wx.get_Articles(mp.faker_id,Mps_id=mp.id,Mps_title=mp.mp_name,CallBack=callback,start_page=start_page,MaxPage=effective_max_page)
            print(f"[账号刷新-线程] 完成 {mp.mp_name}，共 {len(wx.articles)} 篇")
            result=wx.articles
        import threading
        threading.Thread(target=UpArt,args=(mp,)).start()
        return success_response({
            "time_span":time_span,
            "list":result,
            "total":len(result),
            "mps":mp
        })
    except Exception as e:
        print(f"更新公众号文章: {str(e)}",e)
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message=f"更新公众号文章{str(e)}"
            )
        )

@router.get("/{mp_id}", summary="获取公众号详情")
async def get_mp(
    mp_id: str,
    # current_user: dict = Depends(get_current_user)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="公众号不存在"
                )
            )
        return success_response(mp)
    except Exception as e:
        print(f"获取公众号详情错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="获取公众号详情失败"
            )
        )
@router.post("/by_article", summary="通过文章链接获取公众号详情")
async def get_mp_by_article(
    url: str=Query(..., min_length=1),
    current_user: dict = Depends(get_current_user_or_ak)
):
    try:
        info =await WXArticleFetcher().async_get_article_content(url)
        
        if not info:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="公众号不存在"
                )
            )
        return success_response(info)
    except Exception as e:
        print(f"获取公众号详情错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="请输入正确的公众号文章链接"
            )
        )


def _do_add_mp(session, mp_name: str, fakeid: str, avatar: str, mp_intro: str):
    """内部添加公众号逻辑，供批量订阅复用"""
    from core.models.feed import Feed
    import base64
    now = datetime.now()
    mpx_id = base64.b64decode(fakeid).decode("utf-8")
    local_avatar_path = save_avatar_locally(avatar) or ""
    existing_feed = session.query(Feed).filter(Feed.faker_id == fakeid).first()
    if existing_feed:
        return existing_feed, False
    new_feed = Feed(
        id=f"MP_WXS_{mpx_id}",
        mp_name=mp_name,
        mp_cover=local_avatar_path,
        mp_intro=mp_intro,
        status=1,
        created_at=now,
        updated_at=now,
        faker_id=fakeid,
        update_time=0,
        sync_time=0,
    )
    session.add(new_feed)
    session.commit()
    return new_feed, True


_MP_LOCATION_PREFIXES = [
    '\u9ed1\u9f99\u6c5f\u7701', '\u5185\u8499\u53e4\u81ea\u6cbb\u533a', '\u65b0\u7586\u7ef4\u543e\u5c14\u81ea\u6cbb\u533a', '\u897f\u85cf\u81ea\u6cbb\u533a',
    '\u5e7f\u4e1c\u7701', '\u56db\u5ddd\u7701', '\u6d59\u6c5f\u7701', '\u6c5f\u82cf\u7701', '\u5c71\u4e1c\u7701', '\u6cb3\u5357\u7701', '\u6e56\u5317\u7701', '\u6e56\u5357\u7701',
    '\u798f\u5efa\u7701', '\u5b89\u5fbd\u7701', '\u6c5f\u897f\u7701', '\u6cb3\u5317\u7701', '\u5c71\u897f\u7701', '\u9655\u897f\u7701', '\u4e91\u5357\u7701', '\u8d35\u5dde\u7701',
    '\u7518\u8083\u7701', '\u6d77\u5357\u7701', '\u5409\u6797\u7701', '\u8fbd\u5b81\u7701',
    '\u5317\u4eac\u5e02', '\u4e0a\u6d77\u5e02', '\u5929\u6d25\u5e02', '\u91cd\u5e86\u5e02',
    '\u6df1\u5733\u5e02', '\u5e7f\u5dde\u5e02', '\u6210\u90fd\u5e02', '\u676d\u5dde\u5e02', '\u6b66\u6c49\u5e02', '\u5357\u4eac\u5e02', '\u897f\u5b89\u5e02', '\u5b81\u6ce2\u5e02',
    '\u82cf\u5dde\u5e02', '\u9752\u5c9b\u5e02', '\u957f\u6c99\u5e02', '\u90d1\u5dde\u5e02', '\u6c88\u9633\u5e02', '\u6d4e\u5357\u5e02', '\u5408\u80a5\u5e02', '\u53a6\u95e8\u5e02',
    '\u54c8\u5c14\u6ee8\u5e02', '\u4f5b\u5c71\u5e02', '\u4e1c\u839e\u5e02', '\u65e0\u9521\u5e02', '\u6606\u660e\u5e02', '\u5927\u8fde\u5e02', '\u6e29\u5dde\u5e02',
    '\u5317\u4eac', '\u4e0a\u6d77', '\u5929\u6d25', '\u91cd\u5e86', '\u6df1\u5733', '\u5e7f\u5dde', '\u6210\u90fd', '\u676d\u5dde', '\u6b66\u6c49',
    '\u5408\u80a5', '\u4f5b\u5c71', '\u4e1c\u839e', '\u5b81\u6ce2', '\u82cf\u5dde', '\u9752\u5c9b', '\u957f\u6c99', '\u90d1\u5dde', '\u897f\u5b89',
]
_MP_COMPANY_SUFFIXES = [
    '\u80a1\u4efd\u6709\u9650\u516c\u53f8', '\u6709\u9650\u8d23\u4efb\u516c\u53f8', '\u6709\u9650\u5408\u4f19\u4f01\u4e1a', '\u96c6\u56e2\u80a1\u4efd\u6709\u9650\u516c\u53f8',
    '\u6709\u9650\u516c\u53f8', '\u96c6\u56e2', '\u4f01\u4e1a',
]
_MP_INDUSTRY_WORDS = [
    '\u79d1\u6280', '\u533b\u7597', '\u751f\u7269', '\u6750\u6599', '\u5de5\u4e1a', '\u7535\u5b50', '\u673a\u68b0', '\u5236\u9020',
    '\u536b\u751f', '\u9f7f\u79d1', '\u8f74\u627f', '\u77ff\u4e1a', '\u5316\u5de5', '\u91d1\u878d', '\u8d38\u6613', '\u6295\u8d44',
    '\u5efa\u8bbe', '\u5efa\u7b51', '\u4fe1\u606f', '\u6280\u672f', '\u6570\u5b57', '\u667a\u80fd', '\u4e92\u8054\u7f51', '\u7f51\u7edc',
    '\u5de5\u7a0b', '\u73af\u4fdd', '\u80fd\u6e90', '\u533b\u836f', '\u836f\u4e1a', '\u5065\u5eb7', '\u517b\u8001', '\u6559\u80b2',
    '\u4f20\u5a92', '\u6587\u5316', '\u4f20\u64ad', '\u5e7f\u544a', '\u548b\u8be2', '\u7ba1\u7406', '\u670d\u52a1', '\u4f9b\u5e94\u94fe',
    '\u7269\u6d41', '\u822a\u7a7a', '\u6c7d\u8f66', '\u65b0\u6750\u6599', '\u534a\u5bfc\u4f53', '\u828b\u7247', '\u8f6f\u4ef6', '\u786c\u4ef6',
    '\u5668\u68b0', '\u8bbe\u5907', '\u4eea\u5668', '\u5149\u7535', '\u673a\u5668\u4eba', '\u81ea\u52a8\u5316',
]

def _mp_extract_core(name: str) -> str:
    n = name.strip()
    for loc in sorted(_MP_LOCATION_PREFIXES, key=len, reverse=True):
        if n.startswith(loc):
            n = n[len(loc):]
            break
    for suf in sorted(_MP_COMPANY_SUFFIXES, key=len, reverse=True):
        if n.endswith(suf):
            n = n[:-len(suf)]
            break
    return n.strip() or name.strip()

def _mp_extract_brand(core: str) -> str:
    brand = core
    for w in sorted(_MP_INDUSTRY_WORDS, key=len, reverse=True):
        brand = brand.replace(w, '')
    brand = brand.strip()
    return brand if len(brand) >= 2 else core

def _mp_char_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)

def _mp_score(query: str, candidate: str) -> float:
    core_q = _mp_extract_core(query)
    brand_q = _mp_extract_brand(core_q)
    core_c = _mp_extract_core(candidate)
    brand_c = _mp_extract_brand(core_c)
    if len(brand_q) >= 2 and brand_q in candidate:
        return 0.9
    if len(brand_q) >= 2 and brand_q in core_c:
        return 0.85
    sim_brand = _mp_char_sim(brand_q, brand_c)
    sim_core = _mp_char_sim(core_q, core_c)
    sim_full = _mp_char_sim(core_q, candidate)
    return max(sim_brand * 0.6, sim_core * 0.5, sim_full * 0.4)

def _score_candidates(results: list, keyword: str, top_n: int = 5):
    scored = []
    for item in results:
        nick = item.get('nickname', '')
        score = _mp_score(keyword, nick)
        cert = item.get('verify_status', -1) >= 1
        scored.append((score, cert, item))
    scored.sort(key=lambda x: (x[0] >= 0.4 and x[1], x[0] >= 0.4, x[1], x[0]), reverse=True)
    candidates = []
    for score, cert, item in scored[:top_n]:
        if score >= 0.7:
            confidence = 'high'
        elif score >= 0.4:
            confidence = 'medium'
        else:
            confidence = 'low'
        candidates.append({
            'nickname': item.get('nickname', ''),
            'fakeid': item.get('fakeid', ''),
            'avatar': item.get('round_head_img', ''),
            'intro': item.get('signature', ''),
            'verify_status': item.get('verify_status', -1),
            'service_type': item.get('service_type', 0),
            'score': round(score, 2),
            'confidence': confidence,
        })
    recommended = next((i for i, c in enumerate(candidates) if c['score'] >= 0.4), None)
    return candidates, recommended


@router.post("/batch_subscribe", summary="批量搜索并订阅公众号")
async def batch_subscribe(
    companies: list = Body(..., description="公司名称列表"),
    auto_add: bool = Body(False, description="是否自动订阅找到的账号"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    import asyncio
    session = DB.get_session()
    results = []
    for company in companies:
        company = (company or "").strip()
        if not company:
            continue
        row = {"company": company, "status": "not_found", "candidates": [], "selected_index": None, "subscribed": False, "message": ""}
        try:
            search_result = await asyncio.to_thread(search_Biz, company, limit=20, offset=0)
            if search_result is None:
                row["status"] = "rate_limited"
                row["message"] = "微信搜索频率限制，请稍后再试"
                results.append(row)
                await asyncio.sleep(2.0)
                continue
            items = (search_result or {}).get("list", [])
            candidates, recommended = _score_candidates(items, company, top_n=5)
            row["candidates"] = candidates
            row["selected_index"] = recommended
            if candidates:
                row["status"] = "found" if recommended is not None else "candidates_only"
                if auto_add and recommended is not None:
                    best = candidates[recommended]
                    feed, created = _do_add_mp(
                        session,
                        mp_name=best["nickname"],
                        fakeid=best["fakeid"],
                        avatar=best["avatar"],
                        mp_intro=best["intro"],
                    )
                    if created:
                        from core.queue import TaskQueue
                        from core.wx import WxGather
                        Max_page = int(cfg.get("max_page", "2"))
                        TaskQueue.add_task(
                            WxGather().Model().get_Articles,
                            faker_id=feed.faker_id,
                            Mps_id=feed.id,
                            CallBack=UpdateArticleRecent,
                            MaxPage=Max_page,
                            Mps_title=feed.mp_name,
                        )
                        row["subscribed"] = True
                        row["message"] = "订阅成功"
                    else:
                        row["subscribed"] = False
                        row["message"] = "已订阅（跳过）"
            else:
                row["status"] = "not_found"
                row["message"] = "未找到相关账号"
        except Exception as e:
            row["status"] = "error"
            row["message"] = str(e)[:100]
        results.append(row)
        await asyncio.sleep(0.5)

    return success_response({"results": results})


@router.post("", summary="添加公众号")
async def add_mp(
    mp_name: str = Body(..., min_length=1, max_length=255),
    mp_cover: str = Body(None, max_length=255),
    mp_id: str = Body(None, max_length=255),
    avatar: str = Body(None, max_length=500),
    mp_intro: str = Body(None, max_length=255),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        import time
        now = datetime.now()
        
        import base64
        mpx_id = base64.b64decode(mp_id).decode("utf-8")
        local_avatar_path = f"{save_avatar_locally(avatar)}"
        
        # 检查公众号是否已存在
        existing_feed = session.query(Feed).filter(Feed.faker_id == mp_id).first()
        
        if existing_feed:
            # 更新现有记录
            existing_feed.mp_name = mp_name
            existing_feed.mp_cover = local_avatar_path
            existing_feed.mp_intro = mp_intro
            existing_feed.updated_at = now
        else:
            # 创建新的Feed记录
            new_feed = Feed(
                id=f"MP_WXS_{mpx_id}",
                mp_name=mp_name,
                mp_cover= local_avatar_path,
                mp_intro=mp_intro,
                status=1,  # 默认启用状态
                created_at=now,
                updated_at=now,
                faker_id=mp_id,
                update_time=0,
                sync_time=0,
            )
            session.add(new_feed)
           
        session.commit()
        
        feed = existing_feed if existing_feed else new_feed
         #在这里实现第一次添加获取公众号文章
        if not existing_feed:
            from core.queue import TaskQueue
            from core.wx import WxGather
            Max_page=int(cfg.get("max_page","2"))
            TaskQueue.add_task( WxGather().Model().get_Articles,faker_id=feed.faker_id,Mps_id=feed.id,CallBack=UpdateArticleRecent,MaxPage=Max_page,Mps_title=mp_name)
            
        return success_response({
            "id": feed.id,
            "mp_name": feed.mp_name,
            "mp_cover": feed.mp_cover,
            "mp_intro": feed.mp_intro,
            "status": feed.status,
            "faker_id":mp_id,
            "created_at": feed.created_at.isoformat()
        })
    except Exception as e:
        session.rollback()
        print(f"添加公众号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="添加公众号失败"
            )
        )


@router.delete("/{mp_id}", summary="删除订阅号")
async def delete_mp(
    mp_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="订阅号不存在"
                )
            )
        
        session.delete(mp)
        session.commit()
        return success_response({
            "message": "订阅号删除成功",
            "id": mp_id
        })
    except Exception as e:
        session.rollback()
        print(f"删除订阅号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="删除订阅号失败"
            )
        )

@router.put("/{mp_id}", summary="更新订阅号状态")
async def update_mp_status(
    mp_id: str,
    mp_name: str = Body(None),
    mp_cover: str = Body(None),
    mp_intro: str = Body(None),
    status: int = Body(None),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="订阅号不存在"
                )
            )
        
        if mp_name is not None:
            mp.mp_name = mp_name
        if mp_cover is not None:
            mp.mp_cover = mp_cover
        if mp_intro is not None:
            mp.mp_intro = mp_intro
        if status is not None:
            mp.status = status
        
        mp.updated_at = datetime.now()
        session.commit()
        
        return success_response({
            "message": "更新成功",
            "id": mp_id,
            "status": mp.status
        })
    except Exception as e:
        session.rollback()
        print(f"更新订阅号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="更新订阅号失败"
            )
        )
