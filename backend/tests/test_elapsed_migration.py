"""耗时迁移、慢请求统计与结案修订的单元测试（不依赖 HTTP 服务）。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.api_logger import ApiLoggerService
from app.services.api_scheduler import ApiSchedulerService
from app.services.elapsed import compute_stats, parse_elapsed, snapshot_rows
from app.store import store


class ParseElapsedTests(unittest.TestCase):
    def test_integer_text_reads_as_milliseconds(self) -> None:
        self.assertEqual(parse_elapsed("12"), (12, "有效", None))
        self.assertEqual(parse_elapsed("+7"), (7, "有效", None))
        self.assertEqual(parse_elapsed(105), (105, "有效", None))

    def test_valid_zero_is_a_measurement(self) -> None:
        self.assertEqual(parse_elapsed("0"), (0, "有效", None))
        self.assertEqual(parse_elapsed(0), (0, "有效", None))

    def test_empty_and_text_placeholders_are_unknown(self) -> None:
        for value, reason in (
            (None, "空值"),
            ("", "空值"),
            ("   ", "空值"),
            ("未记录", "无法识别的文本"),
            ("n/a", "无法识别的文本"),
            ("12ms", "无法识别的文本"),
            (True, "无法识别的文本"),
            (-5, "负值"),
            ("-5", "负值"),
        ):
            with self.subTest(value=value):
                parsed, state, why = parse_elapsed(value)
                self.assertIsNone(parsed)
                self.assertEqual(state, "未知")
                self.assertEqual(why, reason)

    def test_unknown_never_becomes_zero(self) -> None:
        stats = compute_stats(snapshot_rows([
            {"id": 1, "API名称": "a", "耗时毫秒": "0"},
            {"id": 2, "API名称": "a", "耗时毫秒": "12"},
            {"id": 3, "API名称": "a", "耗时毫秒": "105"},
            {"id": 4, "API名称": "a", "耗时毫秒": "未记录"},
        ]), slow_threshold_ms=100)
        self.assertEqual(stats["valid_samples"], 3)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["fast_responses"], 1)
        self.assertEqual(stats["slow_count"], 1)
        self.assertEqual(stats["slow_sample_denominator"], 3)
        self.assertEqual(stats["min_ms"], 0)
        self.assertIsNone(
            [row for row in snapshot_rows([
                {"id": 4, "API名称": "a", "耗时毫秒": "未记录"}])
             if row["id"] == 4][0]["elapsed_ms"]
        )


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        # 单元测试从空日志表开始，示例行不参与迁移计数。
        store.rows("api_logger").clear()
        store.rows("elapsed_migration").clear()
        self.logger = ApiLoggerService()
        self.ids = []
        for value in ("0", "12", "105", "未记录"):
            result = self.logger.log_api_call("迁移API", f"p-{value}", 200, value, run_id=1)
            self.ids.append(result["log_id"])

    def rows(self) -> dict[int, dict]:
        return {row["id"]: row for row in store.rows("api_logger")}

    def test_interrupted_migration_resumes_without_touching_new_rows(self) -> None:
        first = self.logger.migrate_elapsed(batch_size=2, stop_after=2)
        self.assertEqual(first["remaining"], 2)
        self.assertEqual(self.logger.migration_status()["state"], "迁移中")

        self.logger.log_api_call("迁移API", "p-50", 200, 50, run_id=1)
        resumed = self.logger.migrate_elapsed()
        self.assertEqual(resumed["state"], "已完成")
        self.assertEqual(resumed["remaining"], 0)

        rows = self.rows()
        self.assertEqual(rows[self.ids[0]]["耗时毫秒"], 0)
        self.assertEqual(rows[self.ids[1]]["耗时毫秒"], 12)
        self.assertEqual(rows[self.ids[2]]["耗时毫秒"], 105)
        self.assertIsNone(rows[self.ids[3]]["耗时毫秒"])
        self.assertEqual(rows[self.ids[3]]["耗时原文"], "未记录")
        new_row = [row for row in rows.values() if row.get("请求参数摘要") == "p-50"][0]
        self.assertEqual(new_row["耗时毫秒"], 50)

    def test_rollback_restores_legacy_text_and_keeps_corrections(self) -> None:
        self.logger.migrate_elapsed()
        self.logger.correct_elapsed(self.ids[3], 200)
        rolled = self.logger.rollback_elapsed_migration()
        self.assertTrue(rolled["ok"])
        self.assertEqual(rolled["kept_corrections"], [self.ids[3]])

        rows = self.rows()
        self.assertEqual(rows[self.ids[0]]["耗时毫秒"], "0")
        self.assertEqual(rows[self.ids[1]]["耗时毫秒"], "12")
        self.assertEqual(rows[self.ids[2]]["耗时毫秒"], "105")
        # 迁移后被业务补正过的行，回滚只退回文本表示，不抹掉补正值。
        self.assertEqual(rows[self.ids[3]]["耗时毫秒"], "200")
        for log_id in self.ids:
            self.assertNotIn("耗时状态", rows[log_id])

        legacy = self.logger.list_entries_legacy()
        self.assertEqual(legacy["total"], len(legacy["items"]))
        self.assertTrue(all(isinstance(item["耗时毫秒"], str) for item in legacy["items"]))
        self.assertEqual(
            [item["耗时毫秒"] for item in legacy["items"] if item["id"] == self.ids[0]], ["0"]
        )


class ClosureRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        store.rows("api_logger").clear()
        store.rows("elapsed_migration").clear()
        self.logger = ApiLoggerService()
        self.scheduler = ApiSchedulerService()
        plan, _missing = self.scheduler.create_entry({
            "计划名称": "结案计划",
            "关联API": "结案API",
            "Cron表达式": "0 * * * *",
            "下次执行时间": "2026-11-23T00:00:00+00:00",
            "上次状态": "待执行",
        })
        assert plan is not None
        self.plan_id = plan["id"]
        triggered = self.scheduler.trigger_execution(self.plan_id)
        self.run_id = triggered["run_id"]
        self.ids = []
        for value in ("0", "12", "105", "未记录"):
            result = self.logger.log_api_call(
                "结案API", f"p-{value}", 200, value, run_id=self.run_id)
            self.ids.append(result["log_id"])
        self.logger.migrate_elapsed()

    def test_revision_matches_its_own_snapshot_and_keeps_first_version(self) -> None:
        first = self.scheduler.close_execution(self.plan_id, slow_threshold_ms=100)
        self.assertTrue(first["ok"])
        stats1 = first["closure"]["stats"]
        self.assertEqual(stats1["slow_count"], 1)
        self.assertEqual(stats1["valid_samples"], 3)
        self.assertEqual(stats1["unknown"], 1)
        self.assertEqual(stats1["slow_sample_denominator"], 3)

        self.logger.correct_elapsed(self.ids[3], 200)
        second = self.scheduler.close_execution(
            self.plan_id, slow_threshold_ms=100, revision_of=1)
        self.assertTrue(second["ok"])
        doc2 = second["closure"]
        self.assertEqual(doc2["version"], 2)
        self.assertEqual(doc2["stats"]["slow_count"], 2)
        self.assertEqual(doc2["stats"]["valid_samples"], 4)
        self.assertEqual(doc2["stats"]["unknown"], 0)
        self.assertFalse(doc2["same_input_as_previous"])
        self.assertEqual([item["id"] for item in doc2["corrected_since_previous"]], [self.ids[3]])

        history = self.scheduler.closure_history(self.plan_id)
        self.assertEqual(len(history["versions"]), 2)
        for version in history["versions"]:
            with self.subTest(version=version["version"]):
                self.assertTrue(version["stats_match_snapshot"])
                self.assertEqual(version["stats_mismatches"], [])
        self.assertEqual(history["versions"][0]["superseded_by"], 2)

    def test_resubmitting_unchanged_input_reuses_version(self) -> None:
        first = self.scheduler.close_execution(self.plan_id, slow_threshold_ms=100)
        again = self.scheduler.close_execution(
            self.plan_id, slow_threshold_ms=100, revision_of=1)
        self.assertTrue(again["ok"])
        self.assertTrue(again.get("reissued"))
        self.assertEqual(again["closure"]["version"], first["closure"]["version"])
        self.assertEqual(again["closure"]["stats"], first["closure"]["stats"])
        self.assertEqual(len(self.scheduler.closure_history(self.plan_id)["versions"]), 1)

    def test_reclosing_without_version_is_refused(self) -> None:
        self.scheduler.close_execution(self.plan_id, slow_threshold_ms=100)
        refused = self.scheduler.close_execution(self.plan_id, slow_threshold_ms=100)
        self.assertFalse(refused["ok"])
        self.assertIn("revision_of", refused["message"])


if __name__ == "__main__":
    unittest.main()
