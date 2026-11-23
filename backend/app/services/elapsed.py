"""耗时字段的解析、快照与慢请求统计。

迁移期的数据是混合格式：新记录直接是整数毫秒，存量记录是整数文本，另外还有
"未记录"、"n/a" 这类文字占位。所有读写口都走同一套解析规则，保证迁移前后、
回滚前后对同一批日志得到同样的统计结论。

规则：
- 整数或整数文本（含正负号）按毫秒读取；
- 合法零是有效测量，代表快速响应，不能当成缺失；
- 空值、纯空白和无法识别的文本进入未知明细，值保持为空，绝不补 0；
- 负值不是合法耗时，同样进入未知明细；
- 未知样本不参与平均值、最大值和慢请求分母。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

VALID = "有效"
UNKNOWN = "未知"

REASON_EMPTY = "空值"
REASON_UNRECOGNIZED = "无法识别的文本"
REASON_NEGATIVE = "负值"

_INT_TEXT = re.compile(r"^[+-]?\d+$")

DEFAULT_SLOW_THRESHOLD_MS = 100


def parse_elapsed(value: Any) -> tuple[int | None, str, str | None]:
    """把任意存量/新值解析成 (毫秒, 状态, 未知原因)。

    返回值中的毫秒在未知时为 None，调用方不得用 0 代替。
    """
    if value is None:
        return None, UNKNOWN, REASON_EMPTY
    if isinstance(value, bool):
        # bool 是 int 的子类，但 true/false 不是耗时。
        return None, UNKNOWN, REASON_UNRECOGNIZED
    if isinstance(value, int):
        if value < 0:
            return None, UNKNOWN, REASON_NEGATIVE
        return value, VALID, None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, UNKNOWN, REASON_EMPTY
        if not _INT_TEXT.match(text):
            return None, UNKNOWN, REASON_UNRECOGNIZED
        number = int(text)
        if number < 0:
            return None, UNKNOWN, REASON_NEGATIVE
        return number, VALID, None
    return None, UNKNOWN, REASON_UNRECOGNIZED


def elapsed_of_row(row: dict[str, Any]) -> tuple[int | None, str, str | None, str]:
    """读取一行日志的耗时，并说明它当前是数值字段还是存量文本。

    迁移后的行带 ``耗时状态``；回滚后状态被移除，重新按原文本解析。两种情况下
    得到的毫秒值一致，这就是回滚后统计仍可复核的原因。
    """
    if row.get("耗时状态") in (VALID, UNKNOWN):
        state = str(row["耗时状态"])
        value = row.get("耗时毫秒")
        if state == VALID and isinstance(value, int) and not isinstance(value, bool):
            return value, VALID, None, "numeric"
        if state == UNKNOWN and value is None:
            return None, UNKNOWN, str(row.get("耗时未知原因") or REASON_EMPTY), "legacy_text"
    parsed, state, reason = parse_elapsed(row.get("耗时毫秒"))
    representation = "numeric" if isinstance(row.get("耗时毫秒"), int) and not isinstance(
        row.get("耗时毫秒"), bool
    ) else "legacy_text"
    return parsed, state, reason, representation


def snapshot_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """为一批日志生成可追的输入快照。

    快照保留原始表示，后续统计只从快照重算，不依赖日志行之后是否被更正。
    """
    signed: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item.get("id", 0))):
        value, state, reason, representation = elapsed_of_row(row)
        signed.append(
            {
                "id": int(row.get("id", 0)),
                "api_name": str(row.get("API名称", "")),
                "elapsed_ms": value,
                "state": state,
                "unknown_reason": reason,
                "raw": row.get("耗时原文", row.get("耗时毫秒")),
                "representation": representation,
                "corrected_at": row.get("最近更正时间"),
            }
        )
    return signed


def fingerprint(snapshot: list[dict[str, Any]]) -> str:
    """对快照做稳定指纹，用来判断再次结案的输入是否发生变化。"""
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_stats(
    snapshot: list[dict[str, Any]],
    *,
    slow_threshold_ms: int = DEFAULT_SLOW_THRESHOLD_MS,
) -> dict[str, Any]:
    """从快照计算调用统计，有效样本和未知数量分列。

    慢请求率的分母固定为有效样本数；未知样本既不进分母，也不按 0 毫秒计入
    平均值或快速响应。
    """
    total = len(snapshot)
    valid_values = [
        int(item["elapsed_ms"])
        for item in snapshot
        if item.get("state") == VALID and item.get("elapsed_ms") is not None
    ]
    unknown_items = [item for item in snapshot if item.get("state") != VALID]
    reasons: dict[str, int] = {}
    for item in unknown_items:
        reason = str(item.get("unknown_reason") or REASON_UNRECOGNIZED)
        reasons[reason] = reasons.get(reason, 0) + 1

    slow = [value for value in valid_values if value >= slow_threshold_ms]
    fast = [value for value in valid_values if value == 0]
    valid_count = len(valid_values)
    average = round(sum(valid_values) / valid_count, 1) if valid_count else None
    slow_rate = round(len(slow) / valid_count * 100, 1) if valid_count else None
    unknown_rate = round(len(unknown_items) / total * 100, 1) if total else 0.0

    return {
        "total": total,
        "valid_samples": valid_count,
        "unknown": len(unknown_items),
        "unknown_reasons": reasons,
        "fast_responses": len(fast),
        "slow_count": len(slow),
        "slow_threshold_ms": slow_threshold_ms,
        "slow_sample_denominator": valid_count,
        "slow_rate_percent": slow_rate,
        "unknown_rate_percent": unknown_rate,
        "avg_ms": average,
        "min_ms": min(valid_values) if valid_values else None,
        "max_ms": max(valid_values) if valid_values else None,
        "has_valid_sample": valid_count > 0,
    }


def compare_stats(
    stored: dict[str, Any],
    recomputed: dict[str, Any],
) -> tuple[bool, list[str]]:
    """逐项比对结案时保存的统计与从输入快照重算的统计。"""
    keys = sorted(set(stored) | set(recomputed))
    mismatches = [
        key for key in keys if stored.get(key) != recomputed.get(key)
    ]
    return not mismatches, mismatches
