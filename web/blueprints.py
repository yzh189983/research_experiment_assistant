"""Blueprint 定义（集中放置，避免循环导入）。"""
from flask import Blueprint

# 所有 /api/* 路由收编到此 blueprint；url_prefix 为空，保持原 URL 不变
api_bp = Blueprint("api", __name__)
