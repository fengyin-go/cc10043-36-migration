"""API调用调度领域服务：按Cron表达式调度API调用、管理执行计划和调用链路编排。

数据链路位置：调度与编排层，驱动API调用计划的定时触发和调用链路的编排执行。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.elapsed import compare_stats, compute_stats, fingerprint, snapshot_rows
from app.store import store

CLOSURE_TABLE = "call_closure_rounds"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApiSchedulerService(BaseModuleService):
    MODULE = "api_scheduler"
    ENTITY = "调用计划"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["计划名称", "关联API", "Cron表达式", "下次执行时间", "上次状态"]
    REQUIRED_FIELDS = ["计划名称", "关联API", "Cron表达式"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "执行失败"]
    ACTION_RULES = {"立即执行": "执行中", "暂停计划": "待执行"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：调度编排与调用链路 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """动作入口：立即执行要真正开一轮，不能只改状态。"""
        if action == "立即执行":
            result = self.trigger_execution(entry_id)
            if not result.get("ok"):
                return None, str(result.get("message", "计划不存在"))
            return store.find(self.MODULE, entry_id), "调用计划已立即执行"
        return super().run_action(entry_id, action)

    def trigger_execution(self, entry_id: int) -> dict[str, Any]:
        """触发计划的立即执行：轮次加一，随后写入的日志都归入本轮。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "调用计划不存在"}
        now = _now()
        entry["当前执行轮次"] = int(entry.get("当前执行轮次") or 0) + 1
        entry["上次状态"] = "执行中"
        entry["status"] = "执行中"
        entry["下次执行时间"] = now
        return {
            "ok": True,
            "message": "已触发执行",
            "started_at": now,
            "run_id": entry["当前执行轮次"],
        }

    def validate_schedule(self, entry_id: int) -> list[str]:
        """校验Cron表达式和调度参数的合法性。"""
        errors = []
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return ["计划不存在"]
        cron = entry.get("Cron表达式", "")
        if not cron:
            errors.append("Cron表达式不能为空")
        parts = cron.split()
        if len(parts) < 5:
            errors.append("Cron表达式格式无效，需5个字段")
        api_connectors = store.rows("api_connector")
        api_name = entry.get("关联API", "")
        matched = [c for c in api_connectors if api_name in str(c.get("连接器名称", ""))]
        if api_name and not matched:
            errors.append(f"关联API '{api_name}' 未找到可用连接器")
        return errors

    def route_to_rate_guard(self, entry_id: int) -> str | None:
        """调度计划匹配频控规则，防止过载。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        api_name = entry.get("关联API", "")
        rules = store.rows("rate_guard")
        for rule in rules:
            if rule.get("status") == "已生效" and api_name in rule.get("限流维度", ""):
                return rule.get("规则名称", "")
        return None

    def handle_execution_failure(self, entry_id: int, reason: str) -> dict[str, Any]:
        """执行失败时更新状态并路由到失败隔离区。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "计划不存在"}
        entry["status"] = "执行失败"
        entry["上次状态"] = f"失败: {reason}"
        now = datetime.now(timezone.utc).isoformat()
        isolation_rows = store.rows("failure_isolation")
        fid = max((int(r.get("id", 0)) for r in isolation_rows), default=0) + 1
        isolation_rows.append({
            "id": fid, "失败消息ID": f"sched-err-{entry_id}",
            "失败原因": reason, "原始Topic": entry.get("关联API", ""),
            "隔离时间": now, "重试次数": "0",
            "status": "待处理", "pending": True, "abnormal": False,
        })
        return {"ok": True, "message": "已记录失败隔离", "isolation_id": fid}

    def complete_with_audit(self, entry_id: int, record_count: int) -> None:
        """执行完成时归档审计和数据落地配置检查。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return
        entry["status"] = "已完成"
        entry["上次状态"] = f"完成: {record_count}条"
        entry["pending"] = False
        now = _now()
        audit_rows = store.rows("stream_audit")
        aid = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        audit_rows.append({
            "id": aid, "处理时间": now, "数据源": entry.get("关联API", ""),
            "消息总数": str(record_count), "成功数": str(record_count),
            "失败原因分布": "", "status": "正常", "pending": True, "abnormal": False,
        })

    # ---- 业务扩展：执行结案与慢请求统计关联 ----

    def close_execution(
        self,
        entry_id: int,
        *,
        slow_threshold_ms: int = 100,
        revision_of: int | None = None,
    ) -> dict[str, Any]:
        """结案本轮调度：把本轮日志固化成输入快照，连同慢请求统计一起归档。

        - 首轮结案生成版本 1；``revision_of`` 指向已结案版本时生成修订版本。
        - 每版都保存完整的输入快照和统计结果，统计只从快照重算；
          日志行日后被补正，也只影响下一版，不会让已归档的统计与快照对不上。
        - 修订会说明与首次统计的差异，并指出冲突来自哪些日志的更正。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "调用计划不存在"}

        api_name = str(entry.get("关联API", ""))
        run_id = entry.get("当前执行轮次") or 0
        rows = [
            row for row in store.rows("api_logger")
            if str(row.get("API名称", "")) == api_name
            and int(row.get("执行轮次") or 0) == int(run_id)
        ]
        snapshot = snapshot_rows(rows)
        if not snapshot:
            return {"ok": False, "message": "本轮没有可结案的调用日志"}

        stats = compute_stats(snapshot, slow_threshold_ms=slow_threshold_ms)
        digest = fingerprint(snapshot)

        rounds = store.rows(CLOSURE_TABLE)
        versions = [r for r in rounds if int(r.get("scheduler_id", 0)) == entry_id]
        if versions and revision_of is None:
            return {
                "ok": False,
                "message": "本轮已有结案版本；确认输入未变请按同版结果再结，"
                           "输入被更正请出修订，均需指定 revision_of",
                "versions": sorted(int(r.get("version", 0)) for r in versions),
            }
        previous = None
        if revision_of is not None:
            previous = next(
                (r for r in versions if int(r.get("version", 0)) == int(revision_of)), None
            )
            if previous is None:
                return {"ok": False, "message": f"没有可修订的结案版本 {revision_of}"}

        if previous is not None and previous.get("snapshot_fingerprint") == digest:
            # 输入没变：直接复用同版结果，不新增版本，避免历史里出现重复结论。
            ok, mismatches = compare_stats(previous.get("stats", {}), stats)
            previous["last_reissued_at"] = _now()
            return {
                "ok": True,
                "message": "输入未变化，沿用同版结果",
                "closure": previous,
                "reissued": True,
                "stats_match_snapshot": ok and not mismatches,
            }

        version = max((int(r.get("version", 0)) for r in versions), default=0) + 1
        corrected: list[dict[str, Any]] = []
        if previous is not None:
            prior = {int(item["id"]): item for item in previous.get("snapshot", [])}
            for item in snapshot:
                old = prior.get(int(item["id"]))
                if old is None or old.get("raw") != item.get("raw"):
                    corrected.append({
                        "id": item["id"],
                        "before": None if old is None else old.get("raw"),
                        "after": item.get("raw"),
                        "corrected_at": item.get("corrected_at"),
                    })
            for item in previous.get("snapshot", []):
                if int(item["id"]) not in {int(x["id"]) for x in snapshot}:
                    corrected.append({"id": item["id"], "before": item.get("raw"),
                                      "after": None, "corrected_at": None})
        doc: dict[str, Any] = {
            "id": max((int(r.get("id", 0)) for r in rounds), default=0) + 1,
            "scheduler_id": entry_id,
            "plan_name": entry.get("计划名称", ""),
            "api_name": api_name,
            "run_id": int(run_id),
            "version": version,
            "revision_of": revision_of,
            "closed_at": _now(),
            "slow_threshold_ms": slow_threshold_ms,
            "stats": stats,
            "snapshot": snapshot,
            "snapshot_fingerprint": digest,
            "input_count": len(snapshot),
            "corrected_since_previous": corrected,
            "supersedes": [int(previous["version"])] if previous else [],
            "status": "已结案",
        }
        if previous is not None:
            ok, mismatches = compare_stats(previous.get("stats", {}), stats)
            doc["revision_reason"] = "输入已更正，出修订结果"
            doc["same_input_as_previous"] = False
            doc["stats_match_previous"] = ok
            doc["stats_mismatches"] = mismatches
            previous["superseded_by"] = version
        rounds.append(doc)

        entry["结案版本"] = version
        entry["最近结案时间"] = doc["closed_at"]
        return {
            "ok": True,
            "message": doc.get("revision_reason", "本轮已结案"),
            "closure": doc,
            "recomputed_matches": (
                None if previous is None
                else compare_stats(doc["stats"], compute_stats(snapshot, slow_threshold_ms=slow_threshold_ms))[0]
            ),
        }

    def closure_history(self, entry_id: int) -> dict[str, Any]:
        """列出某计划的全部结案版本，并现场复核每版统计与输入快照是否一致。"""
        rounds = [
            r for r in store.rows(CLOSURE_TABLE)
            if int(r.get("scheduler_id", 0)) == entry_id
        ]
        versions = []
        for doc in sorted(rounds, key=lambda r: int(r.get("version", 0))):
            recomputed = compute_stats(
                doc.get("snapshot", []),
                slow_threshold_ms=int(doc.get("slow_threshold_ms", 100)),
            )
            ok, mismatches = compare_stats(doc.get("stats", {}), recomputed)
            versions.append({
                "version": doc.get("version"),
                "revision_of": doc.get("revision_of"),
                "closed_at": doc.get("closed_at"),
                "input_count": doc.get("input_count"),
                "snapshot_fingerprint": doc.get("snapshot_fingerprint"),
                "superseded_by": doc.get("superseded_by"),
                "stats": doc.get("stats"),
                "stats_match_snapshot": ok,
                "stats_mismatches": mismatches,
                "corrected_since_previous": doc.get("corrected_since_previous", []),
            })
        return {"ok": True, "scheduler_id": entry_id, "versions": versions,
                "latest_version": versions[-1]["version"] if versions else None}

    def get_closure(self, entry_id: int, version: int | None = None) -> dict[str, Any] | None:
        """读取一版结案记录，默认最新版；附上从快照复核的结果。"""
        rounds = [
            r for r in store.rows(CLOSURE_TABLE)
            if int(r.get("scheduler_id", 0)) == entry_id
        ]
        if not rounds:
            return None
        if version is None:
            doc = max(rounds, key=lambda r: int(r.get("version", 0)))
        else:
            doc = next((r for r in rounds if int(r.get("version", 0)) == version), None)
            if doc is None:
                return None
        recomputed = compute_stats(
            doc.get("snapshot", []),
            slow_threshold_ms=int(doc.get("slow_threshold_ms", 100)),
        )
        ok, mismatches = compare_stats(doc.get("stats", {}), recomputed)
        return {**doc, "recomputed": recomputed,
                "stats_match_snapshot": ok, "stats_mismatches": mismatches}

    def check_data_sink(self, entry_id: int) -> list[str]:
        """检查数据落地配置是否就绪。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        sinks = store.rows("data_sink")
        ready = [s.get("配置名称", "") for s in sinks if s.get("status") == "已启用"]
        return ready