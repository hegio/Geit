#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for classification and conservative health decisions."""

from __future__ import annotations

import unittest

import check_health
import fetch_tvbox


class ClassificationTests(unittest.TestCase):
    def classify(self, name: str, group: str = "DE=3", section: str = "") -> str:
        return fetch_tvbox.normalize_group_with_section(group, name, section)

    def test_cctv_wins_over_provider_section(self) -> None:
        self.assertEqual(self.classify("CCTV1", "海南移动"), "央视频道")
        self.assertEqual(self.classify("CCTV5体育", "运营商/内网"), "央视频道")
        self.assertEqual(self.classify("CCTV15音乐", "音乐点播"), "央视频道")

    def test_cctv_editions_and_overseas_feeds_are_distinct(self) -> None:
        self.assertEqual(self.classify("CCTV1港澳版", "港澳频道"), "港澳台")
        self.assertEqual(self.classify("CCTV-4 America", "美国UA(挂梯子)"), "海外频道")
        self.assertEqual(self.classify("CCTV-4美洲", "咪咕央视2"), "海外频道")

    def test_phoenix_and_satellite_priority(self) -> None:
        self.assertEqual(self.classify("凤凰卫视", "DE=3"), "港澳台")
        self.assertEqual(self.classify("湖南卫视", "DE=3"), "卫视频道")
        self.assertEqual(self.classify("广州南国都市", "DE=3"), "地方频道")

    def test_short_id_mapping_and_unknown_review(self) -> None:
        self.assertEqual(self.classify("ystq", "央卫63"), "央视频道")
        self.assertEqual(self.classify("lbcl", "央卫63"), "影视轮播")
        self.assertEqual(self.classify("未知缩写", "未知组"), "待人工确认")

    def test_private_group_is_preserved(self) -> None:
        self.assertEqual(
            self.classify("任意频道", "私密频道2_169169", "私密频道2_169169"),
            "私密频道2_169169",
        )


class HealthTests(unittest.TestCase):
    def item(self, status: str, http_status: int | None = None, detail: str = "") -> check_health.Item:
        return check_health.Item(
            kind="live",
            name="测试直播",
            url="https://example.com/live.m3u8",
            status=status,
            http_status=http_status,
            detail=detail,
            checked_at="2026-10-22T00:00:00Z",
            source="https://example.com/source",
        )

    def test_soft_failures_are_not_decisive(self) -> None:
        self.assertFalse(check_health.is_decisive_failure(self.item("http_error", 403, "HTTP 403")))
        self.assertFalse(check_health.is_decisive_failure(self.item("probe_failed", detail="timed out")))
        self.assertTrue(check_health.is_decisive_failure(self.item("http_error", 404, "HTTP 404")))

    def test_only_probe_ok_clears_history(self) -> None:
        original = check_health.load_streaks
        check_health.load_streaks = lambda: {
            "https://example.com/live.m3u8": {
                "url": "https://example.com/live.m3u8",
                "name": "测试直播",
                "source": "https://example.com/source",
                "count": 3,
                "history": [{"checked_at": "old", "status": "http_error", "detail": "HTTP 404"}],
                "removable": False,
            }
        }
        try:
            for status in ("reachable", "manifest_ok", "playback_unverified"):
                streaks = check_health.update_streaks([self.item(status)])
                self.assertEqual(streaks["https://example.com/live.m3u8"]["count"], 3, status)
            streaks = check_health.update_streaks([self.item("probe_ok")])
            self.assertEqual(streaks["https://example.com/live.m3u8"]["count"], 0)
        finally:
            check_health.load_streaks = original


if __name__ == "__main__":
    unittest.main()
