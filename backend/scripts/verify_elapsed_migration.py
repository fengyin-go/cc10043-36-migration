"""耗时迁移与慢请求结案的端到端核对脚本。

用标准库起一个真实的 uvicorn 实例，按验收场景逐步调用接口：
0、12、105、文字占位四笔 → 转换到一半写入 50 → 结案 → 补正占位为 200
→ 出修订 → 回滚 → 核对旧入口兼容。

用法：backend/.venv/bin/python scripts/verify_elapsed_migration.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8123")
FAILURES: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


def call_raw(method: str, path: str) -> tuple[int, str]:
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def check(name: str, condition: bool, detail: object = None) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + ("" if detail is None else f"  -> {detail}"))
    if not condition:
        FAILURES.append(name)


def rows_by_id() -> dict[int, dict]:
    _status, payload = call("GET", "/api/api_logger/legacy")
    return {item["id"]: item for item in payload["items"]}


def compute_from(doc: dict) -> dict:
    """用与后端相同规则从结案快照重算慢请求统计（独立实现，用于交叉核对）。"""
    valid = [
        int(item["elapsed_ms"]) for item in doc["snapshot"]
        if item["state"] == "有效" and item["elapsed_ms"] is not None
    ]
    reasons: dict[str, int] = {}
    for item in doc["snapshot"]:
        if item["state"] != "有效":
            reasons[item["unknown_reason"]] = reasons.get(item["unknown_reason"], 0) + 1
    threshold = doc.get("slow_threshold_ms", 100)
    slow = [value for value in valid if value >= threshold]
    return {
        "total": len(doc["snapshot"]),
        "valid_samples": len(valid),
        "unknown": len(doc["snapshot"]) - len(valid),
        "unknown_reasons": reasons,
        "fast_responses": len([value for value in valid if value == 0]),
        "slow_count": len(slow),
        "slow_threshold_ms": threshold,
        "slow_sample_denominator": len(valid),
        "slow_rate_percent": round(len(slow) / len(valid) * 100, 1) if valid else None,
        "unknown_rate_percent": round((len(doc["snapshot"]) - len(valid)) / len(doc["snapshot"]) * 100, 1) if doc["snapshot"] else 0.0,
        "avg_ms": round(sum(valid) / len(valid), 1) if valid else None,
        "min_ms": min(valid) if valid else None,
        "max_ms": max(valid) if valid else None,
        "has_valid_sample": bool(valid),
    }


def main() -> int:
    _status, health = call("GET", "/api/health")
    check("健康检查可用", health.get("ok") is True)

    # 先把示例数据里的存量文本一次性迁完，让后续计数只反映本次场景。
    _status, seeded = call("POST", "/api/api_logger/migration", {})
    check("示例存量先完成迁移", seeded.get("remaining") == 0, seeded)

    # 1. 准备四笔存量日志：0、12、105、文字占位（都是迁移前的文本形状）。
    #    API 名称每次运行唯一，避免内存态服务重复跑脚本时把历次日志算进同一轮。
    api_name = f"验收API-{os.getpid()}"
    _status, plan = call("POST", "/api/api_scheduler", {
        "values": {
            "计划名称": "验收计划",
            "关联API": api_name,
            "Cron表达式": "0 * * * *",
            "下次执行时间": "2026-11-23T00:00:00+00:00",
            "上次状态": "待执行",
        }
    })
    plan_id = int(plan["entry"]["id"])
    _status, trigger = call("POST", f"/api/api_scheduler/{plan_id}/actions",
                            {"action": "立即执行"})
    check("调度动作可用，轮次已切到 1", trigger.get("ok") is True,
          trigger.get("entry", {}).get("当前执行轮次"))
    run_id = 1

    baseline_ids = set(rows_by_id())
    for value in ("0", "12", "105", "未记录"):
        _status, result = call("POST", "/api/api_logger/calls", {
            "api_name": api_name, "params": f"p-{value}",
            "status_code": 200, "elapsed_ms": value, "run_id": run_id,
        })
        check(f"存量日志 {value} 按文本待迁移", result.get("legacy_pending") is True, result)

    scenario_ids = sorted(set(rows_by_id()) - baseline_ids)
    text_total = sum(
        1 for row_id, row in rows_by_id().items()
        if row_id in scenario_ids and isinstance(row["耗时毫秒"], str)
    )
    check("场景共 4 条存量文本", text_total == 4, text_total)

    # 2. 迁移只跑一半：4 条存量里先转 2 条。
    _status, first = call("POST", "/api/api_logger/migration",
                          {"batch_size": 2, "stop_after": 2})
    check("首批迁移只处理 2 行，剩 2 行", first.get("remaining") == 2, first)

    # 3. 转换到一半写入新的 50ms：新日志已是整数，不参与存量改写。
    _status, inserted = call("POST", "/api/api_logger/migration", {"batch_size": 2})
    _status, call_result = call("POST", "/api/api_logger/calls", {
        "api_name": api_name, "params": "p-50",
        "status_code": 200, "elapsed_ms": 50, "run_id": run_id,
    })
    check("中断期间写入的 50ms 日志成功", call_result.get("ok") is True, call_result)
    scenario_ids.append(call_result["log_id"])

    _status, resumed = call("POST", "/api/api_logger/migration", {})
    check("续跑完成剩余存量", resumed.get("state") == "已完成" and resumed.get("remaining") == 0,
          resumed)

    _status, view = call("GET", "/api/api_logger/elapsed")
    numeric = {item["id"]: item for item in view["items"] if item["id"] in scenario_ids}
    values = sorted(
        item["elapsed_ms"] for item in numeric.values() if item["state"] == "有效"
    )
    unknown = [item for item in numeric.values() if item["state"] == "未知"]
    check("有效毫秒按整数读取：0、12、50、105", values == [0, 12, 50, 105], values)
    check("文字占位进入未知明细", len(unknown) == 1 and unknown[0]["elapsed_ms"] is None, unknown)
    check("未知行保留原始文本", unknown[0]["raw"] == "未记录", unknown[0].get("raw"))

    # 导出是旧脚本的读入口：迁移后仍输出字符串耗时。
    status_code, csv_text = call_raw("GET", "/api/api_logger/export")
    header, *lines = [line for line in csv_text.splitlines() if line]
    check("导出表头仍是旧字段", header.startswith("﻿id,") and "耗时毫秒" in header, header)
    check("导出条数与旧入口一致", len(lines) == len(rows_by_id()), (len(lines), len(rows_by_id())))

    # 4. 首次结案：慢请求阈值 100ms。
    _status, closure1 = call("POST", f"/api/api_scheduler/{plan_id}/close",
                             {"slow_threshold_ms": 100})
    stats1 = closure1["closure"]["stats"]
    check("首版慢请求数 1（105ms）", stats1["slow_count"] == 1, stats1)
    check("首版有效样本 4，未知 1", stats1["valid_samples"] == 4 and stats1["unknown"] == 1, stats1)
    check("首版慢请求分母为有效样本 4", stats1["slow_sample_denominator"] == 4, stats1)
    check("0ms 计为快速响应而非未知", stats1["fast_responses"] == 1, stats1)

    # 4b. 输入未变的再次结案：按同一版本再结，沿用同版结果，不新增版本。
    _status, repeat = call("POST", f"/api/api_scheduler/{plan_id}/close",
                           {"slow_threshold_ms": 100, "revision_of": 1})
    repeat_doc = repeat["closure"]
    check("同版再结复用版本 1", repeat_doc["version"] == 1 and not repeat.get("versions"),
          repeat)
    check("同版再结统计与首版一致", repeat_doc["stats"] == stats1)
    check("同版再结统计与自己的快照一致", repeat_doc["stats"] == compute_from(repeat_doc))

    # 4c. 未带 revision_of 的再次结案会被拒绝，避免静默覆盖历史结论。
    _status, refused = call("POST", f"/api/api_scheduler/{plan_id}/close",
                            {"slow_threshold_ms": 100})
    check("重复结案需显式指定版本", _status == 409 and "revision_of" in refused.get("detail", ""),
          refused)

    # 5. 补正文字占位为 200ms，再次结案出修订。
    _status, corrected = call("POST", f"/api/api_logger/{unknown[0]['id']}/elapsed",
                              {"elapsed_ms": 200})
    check("补正成功且解析为 200ms", corrected.get("elapsed_ms") == 200, corrected)

    _status, closure2 = call("POST", f"/api/api_scheduler/{plan_id}/close",
                             {"slow_threshold_ms": 100, "revision_of": 1})
    doc2 = closure2["closure"]
    stats2 = doc2["stats"]
    check("修订版慢请求数 2（105、200）", stats2["slow_count"] == 2, stats2)
    check("修订版有效样本 5，未知 0", stats2["valid_samples"] == 5 and stats2["unknown"] == 0, stats2)
    check("修订版说明输入已更正", doc2["same_input_as_previous"] is False, doc2.get("revision_reason"))
    check("修订版指出被更正的日志", [c["id"] for c in doc2["corrected_since_previous"]] == [unknown[0]["id"]])

    # 6. 两版统计都必须与本版快照对得上。
    _status, history = call("GET", f"/api/api_scheduler/{plan_id}/closures")
    check("版本历史只有两版，同版再结未新增", len(history["versions"]) == 2,
          [v["version"] for v in history["versions"]])
    check("两版统计与各自快照一致",
          all(v["stats_match_snapshot"] for v in history["versions"]),
          [(v["version"], v["stats_match_snapshot"], v["stats_mismatches"]) for v in history["versions"]])
    check("首版仍指向 1 条慢请求，未被修订改写", history["versions"][0]["stats"]["slow_count"] == 1)
    check("修订说明首版被取代", history["versions"][0]["superseded_by"] == 2)

    # 7. 回滚：旧入口输出恢复原样，条数与文本形状不变。
    before_rollback = rows_by_id()
    corrected_id = unknown[0]["id"]
    _status, rolled = call("POST", "/api/api_logger/migration/rollback", {})
    check("回滚覆盖迁移行", rolled.get("restored", 0) >= 4, rolled)
    after_rollback = rows_by_id()
    check("回滚后旧入口条数不变", len(after_rollback) == len(before_rollback),
          (len(before_rollback), len(after_rollback)))
    # 未更正的行逐行还原成迁移前的文本；补正过的占位保留 200 的值，只退回文本表示。
    expected = {
        row_id: before_rollback[row_id]["耗时毫秒"]
        for row_id in scenario_ids if row_id != corrected_id
    }
    expected[corrected_id] = "200"
    restored_ok = all(
        after_rollback[row_id]["耗时毫秒"] == text
        for row_id, text in expected.items()
    )
    check("回滚后存量文本逐行还原、补正保留", restored_ok,
          {row_id: after_rollback[row_id]["耗时毫秒"] for row_id in scenario_ids})
    check("回滚报告保留的补正行", rolled.get("kept_corrections") == [corrected_id], rolled)
    new_row = [
        row for row_id, row in after_rollback.items()
        if row_id in scenario_ids and row["请求参数摘要"] == "p-50"
    ]
    check("回滚不改写迁移期间新写入的 50ms", new_row and new_row[0]["耗时毫秒"] == "50", new_row)

    _status, legacy_after = call("GET", "/api/api_logger/legacy")
    check("旧入口 total 与列表条数一致",
          legacy_after["total"] == len(legacy_after["items"]) == len(after_rollback))
    all_text = all(isinstance(item["耗时毫秒"], str) for item in legacy_after["items"])
    check("旧入口耗时全部是字符串", all_text)

    _status, slow_after = call("GET", "/api/api_logger/slow-requests?threshold_ms=100")
    check("回滚后慢请求统计仍可复算", slow_after["stats"]["valid_samples"] >= 2,
          slow_after["stats"])

    _status, detail = call("GET", f"/api/api_scheduler/{plan_id}/closures/1")
    check("回滚不影响已归档结案快照", detail["stats_match_snapshot"] is True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("全部核对通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
