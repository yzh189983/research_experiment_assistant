"""Web 层：Flask Blueprint 与共享视图工具。

拆分策略：所有 /api/* 路由归入 api_bp，页面路由与 WebSocket 仍由 app.py 承载。
前端全部用固定 URL 调用 API，不依赖 Flask endpoint 名，因此加 blueprint 前缀不会破坏调用。
"""
