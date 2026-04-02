# WeRSS - WeChat Official Account RSS Subscription Assistant

## Overview
WeRSS is a tool for subscribing to and managing WeChat Official Account content, providing RSS subscription functionality. It uses a front-end and back-end separation architecture:
- **Backend**: Python 3.12 + FastAPI + uvicorn
- **Frontend**: Vue 3 + Vite (pre-built, served as static files from `static/`)
- **Database**: SQLite in development (`data/db.db`), auto-switches to PostgreSQL (`DATABASE_URL`) in Replit Autoscale production (detected via `K_REVISION` env var)

## Architecture
- The FastAPI backend serves the pre-built Vue frontend from `static/` as static files.
- API routes are prefixed with `/api/v1/wx`.
- The app runs on port 5000 (configured in `config.yaml`).

## Key Files
- `main.py` - Application entry point (starts uvicorn, job scheduler, cascade sync)
- `web.py` - FastAPI app setup, router registration, static file serving
- `config.yaml` - Main configuration file (created from `config.example.yaml`)
- `requirements.txt` - Python dependencies
- `static/` - Pre-built Vue frontend assets
- `web_ui/` - Vue 3 frontend source code (uses Vite build system)
- `apis/` - FastAPI route handlers
- `core/` - Core utilities (config, database, auth, logging)
- `driver/` - Authentication drivers
- `jobs/` - Scheduled background jobs
- `views/` - Server-side view handlers
- `data/` - Runtime data (SQLite database, cache)

## Running the Application
```bash
python main.py -job True -init True
```
- `-job True`: Enable scheduled jobs
- `-init True`: Initialize database tables and default admin user on first run

## Configuration
Configuration is in `config.yaml` (env variable substitution supported via `${VAR:-default}` syntax).

Key config values:
- `port`: Server port (default: 5000)
- `db`: Database connection string (default: `sqlite:///data/db.db`)
- `server.auto_reload`: Auto-reload on code changes (default: True, uses StatReload)

## Default Credentials
On first run with `-init True`, an admin user is created with:
- Username: `admin`
- Password: (set during initialization - check logs)

## Notion Sync
Articles are automatically synced to a Notion database when their content is fetched.
- **Module**: `driver/notion_sync.py`
- **Integration**: Replit Notion OAuth connector (`conn_notion_01KM89HEYZY9GVGED9X9XSGQZK`)
- **Env vars**: `NOTION_TOKEN`, `NOTION_DATABASE_ID`
- **Database**: "公众号监控" (`ae94ec621f5c46ac9dfafff5cc22dd44`)
- **Fields synced**: 文章标题, 文章链接, 发布时间, 公众号, 状态(默认"待审核"), 备注
- **Deduplication**: Checks by URL before creating, skips duplicates
- **Non-blocking**: Runs in a daemon thread, never blocks the main article pipeline
- **Hook**: Called in `core/article_content.py::sync_article_content` after successful save

## Workflow
- **Workflow**: "Start application" - runs `python main.py -job True -init True` on port 5000 (webview)
- **Deployment**: VM target - always running to support background jobs and WebSocket connections
