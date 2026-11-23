"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import api_logger, resource

# 耗时迁移等固定子路径必须排在通用 /api/{module} 之前注册，
# 否则会被动态路径当成 module 或 entry_id 抢走。
ROUTERS = [api_logger, resource]
