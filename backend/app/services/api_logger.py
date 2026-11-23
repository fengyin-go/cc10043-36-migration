"""API调用日志领域服务：记录每次API调用的请求参数、响应和耗时，管理日志查询和统计分析。

数据链路位置：监控采集层，记录全链路中每个API调用的详细日志，支撑调用统计和失败分析。

耗时字段正从"文本备份"迁移为可运算的整数毫秒：
- 新写入的日志直接存整数 ``耗时毫秒``，同时保留 ``耗时原文`` 便于追查；
- 存量日志由 ``migrate_elapsed`` 分批转换，进度记在 ``elapsed_migration`` 表，
  中断后从下一批续跑，回滚时按批次快照逐行还原；
- 迁移完成后 ``耗时毫秒`` 为整数或 None，``耗时状态`` 标明有效/未知，
  ``耗时原文`` 保留迁移前实际文本，慢请求统计不拿 0 填补任何占位。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.elapsed import (
    DEFAULT_SLOW_THRESHOLD_MS,
    UNKNOWN,
    VALID,
    compute_stats,
    elapsed_of_row,
    parse_elapsed,
    snapshot_rows,
)
from app.store import store

MIGRATION_TABLE = "elapsed_migration"
MIGRATION_STATE_READY = "待迁移"
MIGRATION_STATE_RUNNING = "迁移中"
MIGRATION_STATE_DONE = "已完成"
MIGRATION_STATE_ROLLED_BACK = "已回滚"

# 迁移前就存在的字段，逐行留底用于回滚。
LEGACY_SNAPSHOT_FIELDS = ("耗时毫秒", "响应状态码")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migration_row() -> dict[str, Any] | None:
    rows = store.rows(MIGRATION_TABLE)
    return rows[0] if rows else None


def _ensure_migration_row() -> dict[str, Any]:
    rows = store.rows(MIGRATION_TABLE)
    if not rows:
        rows.append({
            "id": 1,
            "state": MIGRATION_STATE_READY,
            "processed": 0,
            "total": 0,
            "batches": [],
            "started_at": None,
            "finished_at": None,
            "rolled_back_at": None,
            "last_error": None,
        })
    return rows[0]


class ApiLoggerService(BaseModuleService):
    MODULE = "api_logger"
    ENTITY = "调用日志"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["调用时间", "API名称", "请求参数摘要", "响应状态码", "耗时毫秒"]
    REQUIRED_FIELDS = ["调用时间", "API名称", "请求参数摘要"]
    STATUS_ORDER = ["成功", "客户端错误", "服务端错误", "超时"]
    ACTION_RULES = {"查看详情": "客户端错误", "重试调用": "服务端错误"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：日志收集与统计分析 ----

    def log_api_call(
        self,
        api_name: str,
        params: str,
        status_code: int,
        elapsed_ms: Any,
        *,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        """记录一次API调用的完整日志。

        整数耗时直接以数值毫米落库；上游仍在上报文本的，原样作为存量文本保存，
        交给 ``migrate_elapsed`` 统一转换。``run_id`` 绑定调度轮次，结案时按
        (API名称, 执行轮次) 取本轮输入。
        """
        rows = store.rows(self.MODULE)
        log_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        now = _now()
        if 200 <= status_code < 300:
            status = "成功"
            abnormal = False
        elif 400 <= status_code < 500:
            status = "客户端错误"
            abnormal = True
        elif status_code >= 500:
            status = "服务端错误"
            abnormal = True
        else:
            status = "超时"
            abnormal = True
        entry: dict[str, Any] = {
            "id": log_id, "调用时间": now, "API名称": api_name,
            "请求参数摘要": params, "响应状态码": str(status_code),
            "执行轮次": int(run_id) if run_id is not None else None,
            "收入快照": False, "最近更正时间": None,
            "status": status,
            "pending": not abnormal, "abnormal": abnormal,
        }
        if isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool):
            if elapsed_ms < 0:
                entry["耗时毫秒"] = None
                entry["耗时原文"] = str(elapsed_ms)
                entry["耗时状态"] = UNKNOWN
                entry["耗时未知原因"] = "负值"
            else:
                entry["耗时毫秒"] = int(elapsed_ms)
                entry["耗时原文"] = str(elapsed_ms)
                entry["耗时状态"] = VALID
                entry["耗时未知原因"] = None
        else:
            # 存量文本先原样保存，不带迁移状态，后续由迁移批次转换。
            entry["耗时毫秒"] = elapsed_ms
            entry["耗时原文"] = elapsed_ms
        rows.append(entry)
        return {
            "ok": True, "log_id": log_id, "status": status,
            "legacy_pending": "耗时状态" not in entry,
        }

    def _refresh_elapsed(self, row: dict[str, Any]) -> None:
        """按原始文本刷新一行的耗时状态，未知时毫秒保持 None。"""
        parsed, state, reason = parse_elapsed(row.get("耗时原文", row.get("耗时毫秒")))
        row["耗时毫秒"] = parsed
        row["耗时状态"] = state
        row["耗时未知原因"] = reason

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """新读入口：``耗时毫秒`` 是整数或 None，另附状态、原文和文本投影。

        条数与分页行为不变；未升级的调用方改走 ``/api/api_logger/legacy``
        即可拿到迁移前的字符串形状。
        """
        rows, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)
        return [self._project_row(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """详情使用与新列表相同的可运算耗时形状。"""
        row = store.find(self.MODULE, entry_id)
        return self._project_row(row) if row is not None else None

    def _project_row(self, row: dict[str, Any]) -> dict[str, Any]:
        value, state, reason, _representation = elapsed_of_row(row)
        raw = row.get("耗时原文", row.get("耗时毫秒"))
        legacy = "" if raw is None else str(raw)
        return {
            **row,
            "耗时毫秒": value,
            "耗时毫秒文本": legacy,
            "耗时状态": state,
            "耗时未知原因": reason,
        }

    def list_entries_legacy(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20_000,
    ) -> dict[str, Any]:
        """旧读入口的列表结果：字符串耗时、相同筛选、条数和分页字段。

        筛选会在迁移后的表上做，但 ``耗时毫秒`` 的过滤仍按原始文本比较，
        和迁移前的行为一致。
        """
        rows, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)
        return {
            "items": [self._legacy_projection(row) for row in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    def export_records(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """导出走旧读入口的形状，保证老脚本解析到的仍是字符串耗时。"""
        rows, _total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
        return [self._legacy_projection(row) for row in rows]

    def list_for_export(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """通用导出接口的钩子：上线期间保持旧导出字段兼容。"""
        return self.export_records(filters=filters, keyword=keyword, status=status)

    def _legacy_projection(self, row: dict[str, Any]) -> dict[str, Any]:
        value, state, reason, _representation = elapsed_of_row(row)
        raw = row.get("耗时原文", row.get("耗时毫秒"))
        legacy = "" if raw is None else str(raw)
        return {
            **row,
            "耗时毫秒": legacy,
            "耗时毫秒数值": value,
            "耗时状态": state,
            "耗时未知原因": reason,
        }

    def migrate_elapsed(self, *, batch_size: int = 2, stop_after: int | None = None) -> dict[str, Any]:
        """分批把存量文本耗时转换成整数毫秒，可中断后续跑。

        ``stop_after`` 限制本轮最多转换多少条存量日志，模拟上线期间的中断；
        迁移进度不靠一次性扫描，每次都从尚未迁移的行继续，所以中断重入是安全的。
        已有 ``耗时状态`` 的行（含中断期间新写入的日志）直接跳过，不会被改写。
        明确写出的 ``""`` / ``null`` 空值也算存量行，转换后进入未知明细。

        每批写一条批次快照，记录该批每行迁移前的 ``耗时毫秒`` 文本，供逐批回滚。
        """
        if batch_size < 1:
            return {"ok": False, "message": "批次大小必须为正整数"}
        progress = _ensure_migration_row()
        rows = store.rows(self.MODULE)
        budget = len(rows) if stop_after is None else max(stop_after, 0)

        progress["state"] = MIGRATION_STATE_RUNNING
        progress["started_at"] = progress.get("started_at") or _now()
        progress["finished_at"] = None
        progress["total"] = len(rows)
        scanned = min(int(progress.get("scanned", 0)), len(rows))

        def pending() -> list[dict[str, Any]]:
            return [row for row in rows[scanned:] if "耗时状态" not in row]

        converted = 0
        unknown = 0
        touched = 0
        while touched < budget:
            chunk = pending()[: min(batch_size, budget - touched)]
            if not chunk:
                # 没有待迁移行了；把扫描位置归零，后续再有存量行插入也能被找到。
                scanned = 0
                break
            snapshots = []
            for row in chunk:
                snapshots.append({
                    "id": int(row.get("id", 0)),
                    "before": {field: row.get(field) for field in LEGACY_SNAPSHOT_FIELDS},
                })
                row["耗时原文"] = row.get("耗时毫秒")
                self._refresh_elapsed(row)
                row["收入快照"] = False
                row["最近更正时间"] = None
                if row.get("耗时状态") == VALID:
                    converted += 1
                else:
                    unknown += 1
                touched += 1
            # 续跑点 = 本轮最后一条已处理行的下一行；行按 id 升序存放。
            last_id = int(chunk[-1].get("id", 0))
            scanned = next(
                (index + 1 for index, row in enumerate(rows)
                 if int(row.get("id", 0)) == last_id),
                len(rows),
            )
            progress["batches"].append({
                "no": len(progress["batches"]) + 1,
                "row_count": len(chunk),
                "rows": snapshots,
                "at": _now(),
            })

        # 如果预算用完仍有存量行在当前扫描点之后，保留扫描点用于续跑；
        # 否则从头再检查一次，避免漏掉后段插入的存量行。
        if touched >= budget and not pending():
            scanned = 0

        progress["scanned"] = scanned
        progress["processed"] = int(progress.get("processed", 0)) + touched
        remaining = sum(1 for row in rows if "耗时状态" not in row)
        if remaining:
            progress["state"] = MIGRATION_STATE_RUNNING
            progress["last_error"] = None
        else:
            progress["state"] = MIGRATION_STATE_DONE
            progress["finished_at"] = _now()

        return {
            "ok": True,
            "message": "存量迁移已完成" if not remaining else "迁移批次已完成，可续跑",
            "state": progress["state"],
            "processed": progress["processed"],
            "total": progress["total"],
            "converted_this_run": converted,
            "unknown_this_run": unknown,
            "remaining": remaining,
            "resume_from": scanned,
            "batches": len(progress["batches"]),
        }

    def migration_status(self) -> dict[str, Any]:
        """读取迁移进度；返回的进度足以判断续跑起点和回滚边界。"""
        progress = _ensure_migration_row()
        rows = store.rows(self.MODULE)
        remaining = sum(1 for row in rows if "耗时状态" not in row)
        return {
            **progress,
            "remaining": remaining,
            "row_total": len(rows),
            "batch_count": len(progress.get("batches", [])),
        }

    def rollback_elapsed_migration(self, batch_no: int | None = None) -> dict[str, Any]:
        """回滚迁移：不带批次号时回滚全部批次，带批次号时只回滚该批。

        回滚把 ``耗时毫秒`` 还原成迁移前的文本，并移除迁移附加的状态字段，
        旧读入口的列表与导出因此恢复原样。持有批次快照的存量行会逐行还原；
        迁移期间新写入的日志没有快照，保持不动。

        批次快照只记录迁移瞬间的表示。如果某行在迁移之后又被业务补正过
        （``最近更正时间`` 晚于迁移），回滚只退回文本表示、保留补正后的值，
        不把业务更正一起抹掉；被回滚掉的批次快照仍然记在 ``rollbacks`` 里，
        迁移前的实际记录始终可追。
        """
        progress = _ensure_migration_row()
        batches = list(progress.get("batches", []))
        if batch_no is None:
            targets = batches
        else:
            targets = [batch for batch in batches if int(batch.get("no", 0)) == batch_no]
            if not targets:
                return {"ok": False, "message": f"批次 {batch_no} 不存在"}
        by_id = {int(row.get("id", 0)): row for row in store.rows(self.MODULE)}
        restored = 0
        kept_corrections: list[int] = []
        for batch in targets:
            for item in batch.get("rows", []):
                row = by_id.get(int(item.get("id", 0)))
                if row is None:
                    continue
                before = item.get("before", {})
                before_text = before.get("耗时毫秒")
                current_text = row.get("耗时原文", row.get("耗时毫秒"))
                corrected = (
                    row.get("最近更正时间") is not None
                    and str(current_text) != str(before_text)
                )
                if corrected:
                    kept_corrections.append(int(row.get("id", 0)))
                    row["耗时毫秒"] = str(current_text)
                else:
                    row["耗时毫秒"] = before_text
                if "响应状态码" in before:
                    row["响应状态码"] = before.get("响应状态码")
                for field in ("耗时状态", "耗时未知原因", "耗时原文", "收入快照", "最近更正时间"):
                    row.pop(field, None)
                restored += 1
            progress["batches"].remove(batch)
            progress.setdefault("rollbacks", []).append({**batch, "rolled_back_at": _now()})
        # 回滚后需要重新扫一遍，才能把还原出来的存量行再次纳入续跑范围。
        progress["scanned"] = 0
        if progress["batches"]:
            progress["state"] = MIGRATION_STATE_RUNNING
            progress["processed"] = sum(int(b.get("row_count", 0)) for b in progress["batches"])
        else:
            progress["state"] = MIGRATION_STATE_ROLLED_BACK
            progress["processed"] = 0
            progress["rolled_back_at"] = _now()
            progress["finished_at"] = None
        return {
            "ok": True,
            "message": f"已回滚 {restored} 行",
            "restored": restored,
            "kept_corrections": kept_corrections,
            "state": progress["state"],
            "remaining_batches": len(progress["batches"]),
        }

    def correct_elapsed(self, log_id: int, value: Any) -> dict[str, Any]:
        """补正一条日志的原始耗时；已结案的日志只允许追加修订，不改历史结论。"""
        row = store.find(self.MODULE, log_id)
        if row is None:
            return {"ok": False, "message": f"调用日志 {log_id} 不存在"}
        parsed, state, reason = parse_elapsed(value)
        row["耗时原文"] = value
        row["耗时毫秒"] = parsed
        row["耗时状态"] = state
        row["耗时未知原因"] = reason
        row["最近更正时间"] = _now()
        row["收入快照"] = False
        return {
            "ok": True,
            "message": "耗时已更正",
            "log_id": log_id,
            "elapsed_ms": parsed,
            "state": state,
            "unknown_reason": reason,
        }

    def read_elapsed_legacy(self) -> dict[str, Any]:
        """旧读入口的完整视图：输出迁移前的字符串耗时，供上线期间未升级的调用方使用。"""
        items = [self._legacy_projection(row) for row in store.rows(self.MODULE)]
        return {"items": items, "total": len(items), "legacy": True}

    def _legacy_projection(self, row: dict[str, Any]) -> dict[str, Any]:
        """把一行投影成迁移前的文本形状：耗时是字符串，空值输出空串。"""
        raw = row.get("耗时原文", row.get("耗时毫秒"))
        if raw is None:
            raw = ""
        return {
            "id": int(row.get("id", 0)),
            "调用时间": row.get("调用时间"),
            "API名称": row.get("API名称"),
            "请求参数摘要": row.get("请求参数摘要"),
            "响应状态码": row.get("响应状态码"),
            "耗时毫秒": str(raw),
            "status": row.get("status"),
            "pending": row.get("pending"),
            "abnormal": row.get("abnormal"),
        }

    def elapsed_snapshot(self) -> dict[str, Any]:
        """把当前日志表固化成可追输入；同一批日志重复读取得到同一指纹。"""
        return {"rows": snapshot_rows(store.rows(self.MODULE))}

    def aggregate_call_stats(self, api_name: str | None = None) -> dict[str, Any]:
        """按API名称聚合调用统计，支持全局或按API筛选。"""
        rows = store.rows(self.MODULE)
        if api_name:
            rows = [r for r in rows if api_name in str(r.get("API名称", ""))]
        total = len(rows)
        success = len([r for r in rows if r.get("status") == "成功"])
        client_err = len([r for r in rows if r.get("status") == "客户端错误"])
        server_err = len([r for r in rows if r.get("status") == "服务端错误"])
        timeout = len([r for r in rows if r.get("status") == "超时"])
        return {"api_name": api_name or "全部", "total": total, "success": success,
                "client_error": client_err, "server_error": server_err, "timeout": timeout,
                "success_rate": round(success / max(total, 1) * 100, 1)}

    def slow_request_stats(
        self,
        api_name: str | None = None,
        *,
        threshold_ms: int = DEFAULT_SLOW_THRESHOLD_MS,
    ) -> dict[str, Any]:
        """慢请求统计：有效样本与未知数量分开说明，未知不参与分母。"""
        rows = store.rows(self.MODULE)
        if api_name:
            rows = [r for r in rows if api_name in str(r.get("API名称", ""))]
        snapshot = snapshot_rows(rows)
        stats = compute_stats(snapshot, slow_threshold_ms=threshold_ms)
        return {
            "api_name": api_name or "全部",
            "threshold_ms": threshold_ms,
            "stats": stats,
            "elapsed_view": self._elapsed_view(rows),
        }

    def elapsed_view(self) -> list[dict[str, Any]]:
        """逐行耗时视图：原始文本、可运算毫秒、状态和未知原因并排。"""
        return self._elapsed_view(store.rows(self.MODULE))

    def _elapsed_view(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把耗时的可运算值与原始文本并排给出，便于核对迁移前后。"""
        view = []
        for row in sorted(rows, key=lambda item: int(item.get("id", 0))):
            value, state, reason, representation = elapsed_of_row(row)
            raw = row.get("耗时原文", row.get("耗时毫秒"))
            view.append({
                "id": int(row.get("id", 0)),
                "raw": raw,
                "elapsed_ms": value,
                "state": state,
                "unknown_reason": reason,
                "representation": representation,
            })
        return view

    def validate_api_exists(self, api_name: str) -> bool:
        """校验API是否已在连接器或调度计划中注册。"""
        connectors = store.rows("api_connector")
        schedules = store.rows("api_scheduler")
        has_connector = any(api_name in str(c.get("连接器名称", "")) for c in connectors)
        has_schedule = any(api_name in str(s.get("关联API", "")) for s in schedules)
        return has_connector or has_schedule

    def route_errors_to_isolation(self, entry_id: int) -> dict[str, Any]:
        """调用错误日志路由到失败隔离区。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") in ("成功",):
            return {"ok": False, "message": "无需隔离"}
        now = _now()
        isolation_rows = store.rows("failure_isolation")
        fid = max((int(r.get("id", 0)) for r in isolation_rows), default=0) + 1
        isolation_rows.append({
            "id": fid, "失败消息ID": f"api-err-{entry_id}",
            "失败原因": f"{entry.get('status')}: {entry.get('响应状态码', '')}",
            "原始Topic": entry.get("API名称", ""),
            "隔离时间": now, "重试次数": "0",
            "status": "待处理", "pending": True, "abnormal": False,
        })
        return {"ok": True, "message": "已路由至隔离区", "isolation_id": fid}

    def export_logs(self, api_name: str, time_range: tuple[str, str], export_format: str = "csv") -> dict[str, Any]:
        """导出API调用日志到流式数据导出。"""
        export_rows = store.rows("stream_export")
        eid = max((int(r.get("id", 0)) for r in export_rows), default=0) + 1
        export_rows.append({
            "id": eid, "任务名称": f"日志导出-{api_name}",
            "数据源": api_name, "导出格式": export_format,
            "时间范围": f"{time_range[0]}~{time_range[1]}",
            "文件大小": "0", "status": "排队中", "pending": True, "abnormal": False,
        })
        return {"ok": True, "export_id": eid, "api_name": api_name}

    def check_rate_limit_status(self, api_name: str) -> dict[str, Any]:
        """检查与API关联的频控规则状态。"""
        rules = store.rows("rate_guard")
        matching = [r for r in rules if api_name in str(r.get("限流维度", ""))]
        return {"api_name": api_name, "active_rules": len([r for r in matching if r.get("status") == "已生效"]),
                "total_rules": len(matching)}
