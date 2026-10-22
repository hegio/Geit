#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for classification and conservative health decisions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_fifth_failure_is_report_only_and_sixth_is_removable(self) -> None:
        original = check_health.load_streaks
        prior = {
            "url": "https://example.com/live.m3u8",
            "name": "测试直播",
            "source": "https://example.com/source",
            "count": 4,
            "history": [],
            "removable": False,
        }
        check_health.load_streaks = lambda: {"https://example.com/live.m3u8": dict(prior)}
        try:
            item = self.item("http_error", 404, "HTTP 404")
            item.failure_class = "hard"
            fifth = check_health.update_streaks([item])["https://example.com/live.m3u8"]
            self.assertEqual(fifth["count"], 5)
            self.assertFalse(fifth["removable"])
            prior["count"] = 5
            sixth = check_health.update_streaks([item])["https://example.com/live.m3u8"]
            self.assertEqual(sixth["count"], 6)
            self.assertTrue(sixth["removable"])
        finally:
            check_health.load_streaks = original

    def test_hard_label_cannot_override_soft_evidence(self) -> None:
        cases = [
            self.item("probe_failed", detail="Server returned 5XX Server Error reply"),
            self.item("probe_failed", detail="ffprobe 超时"),
            self.item("http_error", 567, "HTTP 567"),
        ]
        for item in cases:
            item.failure_class = "hard"
            self.assertFalse(check_health.is_decisive_failure(item), item.detail)

    def test_404_requires_all_user_agents_to_fail(self) -> None:
        hard = check_health.HttpResult(404, "HTTP 404", user_agent="ua")
        soft = check_health.HttpResult(503, "HTTP 503", user_agent="ua")
        with mock.patch.object(check_health, "_request_once", return_value=hard):
            result = check_health.http_check("https://example.com/live.m3u8")
            self.assertEqual(result.failure_class, "hard")
        with mock.patch.object(check_health, "_request_once", side_effect=[hard, soft, hard, hard]):
            result = check_health.http_check("https://example.com/live.m3u8")
            self.assertEqual(result.failure_class, "soft")

    def test_any_user_agent_success_is_reachable(self) -> None:
        hard = check_health.HttpResult(404, "HTTP 404", user_agent="ua")
        ok = check_health.HttpResult(200, "", user_agent="ua")
        with mock.patch.object(check_health, "_request_once", side_effect=[hard, ok]):
            result = check_health.http_check("https://example.com/live.m3u8")
        self.assertEqual(result.status, 200)

    def test_manifest_relative_variants_and_map(self) -> None:
        text = "\n".join([
            "#EXTM3U",
            '#EXT-X-STREAM-INF:BANDWIDTH=1000',
            "low/index.m3u8",
            '#EXT-X-MAP:URI="init.mp4"',
        ])
        variants, media = check_health._manifest_media_lines(
            text, "https://example.com/live/master.m3u8"
        )
        self.assertEqual(variants, ["https://example.com/live/low/index.m3u8"])
        self.assertEqual(media, ["https://example.com/live/init.mp4"])

    def test_host_outage_never_counts_as_decisive(self) -> None:
        items = []
        for index in range(6):
            item = self.item("http_error", 404, "HTTP 404")
            item.url = "https://outage.example/%d.m3u8" % index
            item.host = "outage.example"
            item.failure_class = "hard"
            items.append(item)
        check_health.apply_host_circuit_breaker(items)
        self.assertTrue(all(item.status == "upstream_incident" for item in items))
        self.assertTrue(all(not check_health.is_decisive_failure(item) for item in items))

    def test_state_cursor_does_not_skip_after_budget(self) -> None:
        items = [self.item("unverified") for _ in range(5)]
        for index, item in enumerate(items):
            item.kind = "channel"
            item.url = "https://example.com/%d.m3u8" % index
            item.url_hash = check_health.url_digest(item.url)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "health_state.json"
            original = check_health.HEALTH_STATE_JSON
            check_health.HEALTH_STATE_JSON = state_path
            try:
                check_health.write_health_state(
                    items, items[:2], 2, 1, 0, "2026-10-22T00:00:00Z"
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["next_channel_offset"], 2)
                self.assertEqual(len(state["urls"]), 2)
                self.assertEqual(state["last_run"]["attempted_at"], "2026-10-22T00:00:00Z")
                check_health.write_health_state(
                    items, [], 2, 1, 0, "2026-10-24T00:00:00Z"
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(len(state["urls"]), 2, "unsampled state must be retained")
            finally:
                check_health.HEALTH_STATE_JSON = original

    def test_group_title_alone_does_not_skip_a_channel(self) -> None:
        """A group title must never decide reachability.

        "私密频道2_169169" is served by the public CDN t33.cdn2020.com, so
        probing it is both possible and required. Only a genuinely unreachable
        target (private IP literal or ISP intranet host) is exempt.
        """
        item = self.item("unverified")
        item.kind = "channel"
        item.category = "私密频道2_169169"
        item.url = "https://t33.cdn2020.com/private/index.m3u8"
        result = check_health.check(item, "", True)
        self.assertNotEqual(result.status, "skip_internal")

    def test_isp_intranet_host_is_skipped(self) -> None:
        item = self.item("unverified")
        item.kind = "channel"
        item.url = "http://rrs03.hw.gmcc.net/live/cctv1.m3u8"
        result = check_health.check(item, "", True)
        self.assertEqual(result.status, "skip_internal")

    def test_private_ip_literal_is_skipped(self) -> None:
        item = self.item("unverified")
        item.kind = "channel"
        item.url = "http://192.168.1.10/live/cctv1.m3u8"
        result = check_health.check(item, "", True)
        self.assertEqual(result.status, "skip_internal")
    def test_ai_none_preserves_report_without_timestamp_commit(self) -> None:
        import ai_assist

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ai_review.json"
            original = '{"status":"ok"}\n'
            target.write_text(original, encoding="utf-8")
            with mock.patch.object(ai_assist, "_pending_classifications", return_value=[]), \
                 mock.patch.object(ai_assist, "_error_clusters", return_value=[]):
                result = ai_assist.write_ai_review("none", Path(directory))
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_report_csv_includes_all_evidence_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = (
                check_health.HEALTH_JSON,
                check_health.HEALTH_CSV,
            )
            check_health.HEALTH_JSON = root / "health.json"
            check_health.HEALTH_CSV = root / "health.csv"
            try:
                item = self.item("http_error", 404, "HTTP 404")
                item.final_url = "https://example.com/final.m3u8"
                item.evidence = "HTTP 404"
                check_health.write_report([item])
                self.assertTrue((root / "health.csv").is_file())
                self.assertTrue((root / "blocked.csv").is_file())
            finally:
                check_health.HEALTH_JSON, check_health.HEALTH_CSV = original


if __name__ == "__main__":
    unittest.main()
