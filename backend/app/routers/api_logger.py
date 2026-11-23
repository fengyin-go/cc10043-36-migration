"""耗时迁移与结案接口：在通用资源路由之上增加只读视图、迁移运维和结案动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for
from app.services.api_logger import ApiLoggerService
from app.services.elapsed import DEFAULT_SLOW_THRESHOLD_MS

router = APIRouter(prefix="/api", tags=["耗时迁移"])


def _logger() -> ApiLoggerService:
    service = service_for("api_logger")
    assert isinstance(service, ApiLoggerService)
    return service


class MigratePayload(BaseModel):
    batch_size: int = Field(default=2, ge=1)
    stop_after: int | None = Field(default=None, ge=0)


class RollbackPayload(BaseModel):
    batch_no: int | None = Field(default=None, ge=1)


class CorrectPayload(BaseModel):
    elapsed_ms: Any = None
    log_id: int | None = None


class CallPayload(BaseModel):
    api_name: str
    params: str = ""
    status_code: int = 200
    elapsed_ms: Any = 0
    run_id: int | None = None


class ClosePayload(BaseModel):
    slow_threshold_ms: int = Field(default=DEFAULT_SLOW_THRESHOLD_MS, ge=0)
    revision_of: int | None = Field(default=None, ge=1)


@router.get("/api_logger/migration")
def migration_status() -> dict[str, Any]:
    """迁移进度：已转换行数、剩余行数和批次边界。"""
    return _logger().migration_status()


@router.post("/api_logger/migration")
def migrate(payload: MigratePayload | None = None) -> dict[str, Any]:
    """执行一批存量耗时转换；中断后再次调用即从剩余行续跑。"""
    body = payload or MigratePayload()
    return _logger().migrate_elapsed(batch_size=body.batch_size, stop_after=body.stop_after)


@router.post("/api_logger/migration/rollback")
def rollback(payload: RollbackPayload | None = None) -> dict[str, Any]:
    """回滚迁移批次：不带批次号回滚全部，带批次号只回滚该批。"""
    body = payload or RollbackPayload()
    return _logger().rollback_elapsed_migration(body.batch_no)


@router.get("/api_logger/legacy")
def legacy_view(
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20_000, ge=1),
) -> dict[str, Any]:
    """旧读入口：仍返回迁移前的字符串耗时，条数和分页字段保持兼容。"""
    return _logger().list_entries_legacy(
        keyword=keyword, status=status, page=page, size=size)


@router.get("/api_logger/slow-requests")
def slow_requests(
    api_name: str | None = Query(default=None),
    threshold_ms: int = Query(default=DEFAULT_SLOW_THRESHOLD_MS, ge=0),
) -> dict[str, Any]:
    """慢请求统计：有效样本与未知数量分列，并给出逐行耗时视图。"""
    return _logger().slow_request_stats(api_name, threshold_ms=threshold_ms)


@router.get("/api_logger/elapsed")
def elapsed_view() -> dict[str, Any]:
    """逐行核对：原始文本、可运算毫秒、状态和未知原因。"""
    return {
        "items": _logger().elapsed_view(),
        "migration": _logger().migration_status(),
    }


@router.post("/api_logger/calls")
def record_call(payload: CallPayload) -> dict[str, Any]:
    """采集一条调用日志；耗时按整数毫秒写入，轮次用于后续结案关联。"""
    return _logger().log_api_call(
        payload.api_name, payload.params, payload.status_code,
        payload.elapsed_ms, run_id=payload.run_id,
    )


@router.post("/api_logger/{log_id}/elapsed")
def correct_elapsed(log_id: int, payload: CorrectPayload) -> dict[str, Any]:
    """补正单条日志的耗时，例如把文字占位改成真实毫秒数。"""
    return _logger().correct_elapsed(log_id, payload.elapsed_ms)


@router.post("/api_scheduler/{entry_id}/close")
def close_execution(entry_id: int, payload: ClosePayload | None = None) -> dict[str, Any]:
    """结案本轮执行：固化输入快照与慢请求统计；可指定 revision_of 出修订。"""
    service = service_for("api_scheduler")
    body = payload or ClosePayload()
    result = service.close_execution(
        entry_id,
        slow_threshold_ms=body.slow_threshold_ms,
        revision_of=body.revision_of,
    )
    if not result.get("ok"):
        status_code = 409 if "revision_of" in str(result.get("message", "")) else 404
        raise HTTPException(status_code=status_code, detail=result.get("message", "结案失败"))
    return result


@router.get("/api_scheduler/{entry_id}/closures")
def closure_history(entry_id: int) -> dict[str, Any]:
    """结案版本历史，每版附上统计与输入快照的复核结果。"""
    service = service_for("api_scheduler")
    history = service.closure_history(entry_id)
    if not history.get("versions"):
        raise HTTPException(status_code=404, detail="该计划尚未结案")
    return history


@router.get("/api_scheduler/{entry_id}/closures/{version}")
def closure_detail(entry_id: int, version: int) -> dict[str, Any]:
    """读取指定结案版本及其输入快照。"""
    service = service_for("api_scheduler")
    doc = service.get_closure(entry_id, version)
    if doc is None:
        raise HTTPException(status_code=404, detail="结案版本不存在")
    return doc
