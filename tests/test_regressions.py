#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for classification and conservative health decisions."""

from __future__ import annotations

import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import check_health
import fetch_tvbox


class _FakeResponse:
    def __init__(self, data: bytes, status: int = 200, content_type: str = "text/plain") -> None:
        self._data = data
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, _size: int = -1) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class SourceRelayTests(unittest.TestCase):
    def test_relay_is_opt_in(self) -> None:
        failure = fetch_tvbox.urllib.error.HTTPError(
            "https://source.example/list.m3u", 403, "Forbidden", None, None
        )
        with mock.patch.object(fetch_tvbox, "SOURCE_RELAY", ""), \
             mock.patch.object(fetch_tvbox.urllib.request, "urlopen",
                               side_effect=failure) as urlopen, \
             mock.patch.object(fetch_tvbox.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "HTTP Error 403"):
                fetch_tvbox.fetch("https://source.example/list.m3u")
        self.assertEqual(urlopen.call_count, fetch_tvbox.RETRY)

    def test_direct_success_never_calls_relay(self) -> None:
        with mock.patch.object(fetch_tvbox, "SOURCE_RELAY",
                               "https://relay.example/fetch?url="), \
             mock.patch.object(fetch_tvbox.urllib.request, "urlopen",
                               return_value=_FakeResponse(b"#EXTM3U\n")) as urlopen:
            result = fetch_tvbox.fetch("https://source.example/list.m3u")
        self.assertFalse(result.via_relay)
        self.assertEqual(result.effective_url, "https://source.example/list.m3u")
        self.assertEqual(urlopen.call_count, 1)

    def test_403_falls_back_to_encoded_relay_url(self) -> None:
        failure = fetch_tvbox.urllib.error.HTTPError(
            "https://source.example/list.m3u", 403, "Forbidden", None, None
        )
        with mock.patch.object(fetch_tvbox, "SOURCE_RELAY",
                               "https://relay.example/fetch?url="), \
             mock.patch.object(fetch_tvbox.urllib.request, "urlopen",
                               side_effect=[failure] * fetch_tvbox.RETRY
                               + [_FakeResponse(b"#EXTM3U\n")]) as urlopen, \
             mock.patch.object(fetch_tvbox.time, "sleep"):
            result = fetch_tvbox.fetch("https://source.example/list.m3u")
        self.assertTrue(result.via_relay)
        self.assertEqual(result.effective_url, "https://source.example/list.m3u")
        relay_url = urlopen.call_args_list[-1].args[0].full_url
        self.assertEqual(
            relay_url,
            "https://relay.example/fetch?url=https%3A%2F%2Fsource.example%2Flist.m3u",
        )

    def test_relay_template_placeholder_is_supported(self) -> None:
        with mock.patch.object(fetch_tvbox, "SOURCE_RELAY",
                               "https://relay.example/fetch/{url}/raw"):
            relay = fetch_tvbox.source_relay_url("https://source.example/list.m3u")
        self.assertEqual(
            relay,
            "https://relay.example/fetch/https%3A%2F%2Fsource.example%2Flist.m3u/raw",
        )

    def test_github_sources_keep_existing_mirror_chain(self) -> None:
        with mock.patch.object(fetch_tvbox, "SOURCE_RELAY",
                               "https://relay.example/fetch?url="), \
             mock.patch.object(fetch_tvbox.urllib.request, "urlopen",
                               return_value=_FakeResponse(b"{}")) as urlopen:
            result = fetch_tvbox.fetch(
                "https://raw.githubusercontent.com/hegio/Geit/main/config.json"
            )
        self.assertFalse(result.via_relay)
        self.assertEqual(urlopen.call_count, 1)


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

    def test_third_failure_is_removable(self) -> None:
        """A channel URL is dropped after 3 decisive failures."""
        original = check_health.load_streaks
        prior = {
            "url": "https://example.com/live.m3u8",
            "name": "测试直播",
            "source": "https://example.com/source",
            "count": 2,
            "history": [],
            "removable": False,
        }
        check_health.load_streaks = lambda: {"https://example.com/live.m3u8": dict(prior)}
        try:
            item = self.item("http_error", 404, "HTTP 404")
            item.failure_class = "hard"
            third = check_health.update_streaks([item])["https://example.com/live.m3u8"]
            self.assertEqual(third["count"], 3)
            self.assertTrue(third["removable"])
        finally:
            check_health.load_streaks = original

    def test_second_failure_is_report_only(self) -> None:
        original = check_health.load_streaks
        prior = {
            "url": "https://example.com/live.m3u8",
            "name": "测试直播",
            "source": "https://example.com/source",
            "count": 1,
            "history": [],
            "removable": False,
        }
        check_health.load_streaks = lambda: {"https://example.com/live.m3u8": dict(prior)}
        try:
            item = self.item("http_error", 404, "HTTP 404")
            item.failure_class = "hard"
            second = check_health.update_streaks([item])["https://example.com/live.m3u8"]
            self.assertEqual(second["count"], 2)
            self.assertFalse(second["removable"])
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
        # Every UA gets one retry after a hard status, so a persistent 404
        # costs 2 x len(USER_AGENTS) requests.
        with mock.patch.object(check_health, "_request_once", return_value=hard), \
             mock.patch.object(check_health.time, "sleep"):
            result = check_health.http_check("https://example.com/live.m3u8")
            self.assertEqual(result.failure_class, "hard")
        responses = [hard, soft, hard, hard]
        responses += [hard] * (len(check_health.USER_AGENTS) * 2 - len(responses))
        with mock.patch.object(check_health, "_request_once", side_effect=responses), \
             mock.patch.object(check_health.time, "sleep"):
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


class FalsePositiveTests(unittest.TestCase):
    """A playable link must never be labelled dead.

    Each case below was measured against the real host: the link serves a valid
    stream, yet the previous classifier counted it toward auto-removal.
    """

    def test_extensionless_hls_is_routed_to_manifest_probe(self) -> None:
        # fast.hidns.vip serves HLS with Content-Type: text/plain and no
        # extension. ffprobe refuses it, so it must go through probe_manifest(),
        # which resolves the media lines and probes a real segment.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = b"#EXTM3U\n#EXT-X-TARGETDURATION:8\n/api/hoy/1\n/api/hoy/2\n"
            original = (check_health.HEALTH_JSON, check_health.HEALTH_CSV,
                        check_health.DEAD_CHANNELS_JSON,
                        check_health.CHANNEL_HEALTH_JSON)
            check_health.HEALTH_JSON = root / "health.json"
            check_health.HEALTH_CSV = root / "health.csv"
            check_health.DEAD_CHANNELS_JSON = root / "dead_channels.json"
            check_health.CHANNEL_HEALTH_JSON = root / "channel_health.json"
            try:
                with mock.patch.object(check_health, "http_check",
                                       return_value=check_health.HttpResult(
                                           status=200,
                                           detail="ok",
                                           body=body,
                                           content_type="text/plain",
                                           user_agent="Mozilla/5.0",
                                           final_url="https://fast.hidns.vip/163189/hoy")), \
                     mock.patch.object(check_health, "probe_manifest",
                                       return_value=("probe_ok", "segment decoded")):
                    item = check_health.Item(
                        kind="channel", name="HOY TV",
                        url="https://fast.hidns.vip/163189/hoy",
                        status="unverified", source="s",
                    )
                    result = check_health.check(item, "ffprobe", True)
                self.assertEqual(result.status, "probe_ok")
                self.assertEqual(result.failure_class, "")
            finally:
                (check_health.HEALTH_JSON, check_health.HEALTH_CSV,
                 check_health.DEAD_CHANNELS_JSON,
                 check_health.CHANNEL_HEALTH_JSON) = original

    def test_auth_gate_body_is_soft(self) -> None:
        # "644368714 由于版权原因，请登录后观看" arrives with HTTP 200. It is an
        # auth requirement, not a dead source, so it must not count toward
        # removal (873 channels depend on this).
        body = "644368714 由于版权原因，请登录后观看".encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = (check_health.HEALTH_JSON, check_health.HEALTH_CSV,
                        check_health.DEAD_CHANNELS_JSON,
                        check_health.CHANNEL_HEALTH_JSON)
            check_health.HEALTH_JSON = root / "health.json"
            check_health.HEALTH_CSV = root / "health.csv"
            check_health.DEAD_CHANNELS_JSON = root / "dead_channels.json"
            check_health.CHANNEL_HEALTH_JSON = root / "channel_health.json"
            try:
                with mock.patch.object(check_health, "http_check",
                                       return_value=check_health.HttpResult(
                                           status=200,
                                           detail="ok",
                                           body=body,
                                           content_type="application/json",
                                           user_agent="Mozilla/5.0",
                                           final_url="http://my1nas.dpdns.org:1234/644368714")), \
                     mock.patch.object(check_health, "probe",
                                       return_value=("probe_failed",
                                                     "Invalid data found when processing input")):
                    item = check_health.Item(
                        kind="channel", name="CHC动作电影",
                        url="http://my1nas.dpdns.org:1234/644368714",
                        status="unverified", source="s",
                    )
                    result = check_health.check(item, "ffprobe", True)
                self.assertEqual(result.failure_class, "soft")
                self.assertTrue(check_health.is_soft_failure(result))
                self.assertFalse(check_health.is_decisive_failure(result))
            finally:
                (check_health.HEALTH_JSON, check_health.HEALTH_CSV,
                 check_health.DEAD_CHANNELS_JSON,
                 check_health.CHANNEL_HEALTH_JSON) = original

    def test_probe_forwards_referer(self) -> None:
        # The 163189 family only serves segments when a Referer is present.
        seen = {}

        def fake_run(command, capture_output, timeout):
            seen["command"] = command
            return subprocess.CompletedProcess(command, 0, b"video\naudio\n", b"")

        with mock.patch.object(check_health.subprocess, "run", side_effect=fake_run):
            status, detail = check_health.probe(
                "https://fast.hidns.vip/api/hoy/1", "ffprobe",
                "Mozilla/5.0", "https://fast.hidns.vip/163189/hoy")
        self.assertEqual(status, "probe_ok")
        joined = " ".join(seen["command"])
        self.assertIn("Referer: https://fast.hidns.vip/163189/hoy", joined)

    def test_offline_programme_is_still_decisive(self) -> None:
        # "节目已下线" means the channel really is gone; it must stay decisive.
        body = "961930269 节目已下线，请尝试其他节目".encode("utf-8")
        self.assertFalse(check_health._soft_failure_text(body.decode("utf-8")))

    def test_404_flap_is_retried_before_being_decisive(self) -> None:
        """A 404 that a retry turns into 200 is a CDN flap, not a dead source.

        Measured: v.lfthirtytwo.com and vip.lzcdn2.com return 404 for 25-50%
        of single requests while the stream plays. Four UAs hitting the same
        bad node in one instant used to look like "all clients failed" and
        count toward removal.
        """
        hard = check_health.HttpResult(404, "HTTP 404", user_agent="ua")
        ok = check_health.HttpResult(200, "", user_agent="ua")
        with mock.patch.object(check_health, "_request_once",
                               side_effect=[hard, ok]), \
             mock.patch.object(check_health.time, "sleep"):
            result = check_health.http_check("https://example.com/live.m3u8")
        self.assertEqual(result.status, 200)

    def test_persistent_404_stays_decisive(self) -> None:
        hard = check_health.HttpResult(404, "HTTP 404", user_agent="ua")
        with mock.patch.object(check_health, "_request_once", return_value=hard), \
             mock.patch.object(check_health.time, "sleep"):
            result = check_health.http_check("https://example.com/live.m3u8")
        self.assertEqual(result.status, 404)
        self.assertEqual(result.failure_class, "hard")

    def test_retry_does_not_rescue_a_soft_failure(self) -> None:
        # 5xx, 403/429/451 and timeouts are already environmental; they must
        # not spend an extra request, and must never become decisive.
        soft = check_health.HttpResult(503, "HTTP 503", user_agent="ua")
        with mock.patch.object(check_health, "_request_once",
                               side_effect=[soft] * 4), \
             mock.patch.object(check_health.time, "sleep") as sleep_mock:
            result = check_health.http_check("https://example.com/live.m3u8")
        self.assertEqual(result.failure_class, "soft")
        sleep_mock.assert_not_called()

    def test_hard_verdict_requires_every_attempt_to_be_hard(self) -> None:
        hard = check_health.HttpResult(404, "HTTP 404", user_agent="ua")
        timeout = check_health.HttpResult(None, "timed out", user_agent="ua")
        # First UA 404s twice, second UA times out. The verdict must be soft,
        # because a hard status from one client cannot prove the source is
        # gone when another client could not get an answer at all.
        responses = [hard, hard, timeout, timeout]
        responses += [timeout] * (len(check_health.USER_AGENTS) * 2 - len(responses))
        with mock.patch.object(check_health, "_request_once",
                               side_effect=responses), \
             mock.patch.object(check_health.time, "sleep"):
            result = check_health.http_check("https://example.com/live.m3u8")
        self.assertEqual(result.failure_class, "soft")



class RemovedChannelTests(unittest.TestCase):
    """The removal record is the only place a human can see what was dropped."""

    def setUp_whitelist(self, text: str) -> None:
        self._wl = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                              encoding="utf-8")
        self._wl.write(text)
        self._wl.close()
        self._orig_wl = check_health.WHITELIST_PATH
        check_health.WHITELIST_PATH = Path(self._wl.name)

    def tearDown_whitelist(self) -> None:
        check_health.WHITELIST_PATH = self._orig_wl
        Path(self._wl.name).unlink(missing_ok=True)

    def test_whitelisted_url_is_never_candidate_remove(self) -> None:
        """A hand-curated URL survives repeated decisive failures."""
        self.setUp_whitelist("http://gone.example/live.m3u8\n")
        try:
            for _ in range(5):
                item = self._channel("http://gone.example/live.m3u8",
                                     "probe_failed", 404)
                rows = {r["url"]: r for r in
                        check_health._channel_health_rows([item])}
            row = rows["http://gone.example/live.m3u8"]
            self.assertEqual(row["verdict"], "whitelisted")
            self.assertEqual(row["decisive_failures"], 0)
        finally:
            self.tearDown_whitelist()

    def test_whitelisted_host_protects_other_paths(self) -> None:
        """A bare host entry protects every URL on that host or a subdomain."""
        self.setUp_whitelist("gone.example\n")
        try:
            item = self._channel("http://cdn.gone.example/other/live.m3u8",
                                  "probe_failed", 404)
            rows = check_health._channel_health_rows([item])
            self.assertEqual(rows[0]["verdict"], "whitelisted")
        finally:
            self.tearDown_whitelist()

    def test_whitelisted_url_never_enters_removal_record(self) -> None:
        self.setUp_whitelist("http://gone.example/live.m3u8\n")
        try:
            check_health.write_removed_channels([
                self._channel("http://gone.example/live.m3u8", "probe_failed", 404)
            ])
            data = json.loads(check_health.REMOVED_CHANNELS_JSON.read_text(encoding="utf-8"))
            self.assertEqual(data["summary"]["removed_total"], 0)
        finally:
            self.tearDown_whitelist()

    def test_whitelisted_live_source_reports_no_streak(self) -> None:
        original = check_health.load_streaks
        check_health.load_streaks = lambda: {}
        self.setUp_whitelist("gone.example\n")
        try:
            item = check_health.Item(
                kind="live", name="测试直播",
                url="http://gone.example/live.m3u8",
                status="http_error", http_status=404, detail="HTTP 404",
                checked_at="2026-10-23T00:00:00Z",
                source="https://example.com/source", host="gone.example",
            )
            item.failure_class = "hard"
            streaks = check_health.update_streaks([item])
            self.assertTrue(streaks["http://gone.example/live.m3u8"]["whitelisted"])
            self.assertFalse(streaks["http://gone.example/live.m3u8"]["removable"])
        finally:
            self.tearDown_whitelist()
            check_health.load_streaks = original

    def test_missing_whitelist_disables_the_feature(self) -> None:
        self.setUp_whitelist("")
        try:
            self.assertFalse(check_health.whitelisted("http://gone.example/live.m3u8"))
            urls, hosts = check_health.load_whitelist()
            self.assertEqual((urls, hosts), (set(), set()))
        finally:
            self.tearDown_whitelist()

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._json = check_health.REMOVED_CHANNELS_JSON
        self._csv = check_health.REMOVED_CHANNELS_CSV
        self._health = check_health.CHANNEL_HEALTH_JSON
        check_health.REMOVED_CHANNELS_JSON = self.root / "removed_channels.json"
        check_health.REMOVED_CHANNELS_CSV = self.root / "removed_channels.csv"
        check_health.CHANNEL_HEALTH_JSON = self.root / "channel_health.json"

    def tearDown(self) -> None:
        check_health.REMOVED_CHANNELS_JSON = self._json
        check_health.REMOVED_CHANNELS_CSV = self._csv
        check_health.CHANNEL_HEALTH_JSON = self._health
        self._dir.cleanup()

    def _seed(self, entries: list[dict]) -> None:
        check_health.REMOVED_CHANNELS_JSON.write_text(
            json.dumps({"updated": "x", "channels": entries}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_removal_record_keeps_channel_name_and_source(self) -> None:
        self._fail_three_times("http://gone.example/live.m3u8")
        check_health.write_removed_channels([self._channel("http://gone.example/live.m3u8", "probe_failed", 404)])
        data = json.loads(check_health.REMOVED_CHANNELS_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["removed_total"], 1)
        row = data["channels"][0]
        self.assertEqual(row["name"], "CCTV-测试")
        self.assertEqual(row["url"], "http://gone.example/live.m3u8")
        self.assertEqual(row["source"], "https://example.com/source")
        self.assertTrue(row["removed_at"])

    def test_unsampled_history_is_retained(self) -> None:
        self._seed([{
            "name": "旧记录", "url": "http://old.example/live.m3u8",
            "removed_at": "2026-10-20T00:00:00Z",
        }])
        item = self._channel("http://other.example/live.m3u8", "probe_ok")
        check_health.write_removed_channels([item])
        data = json.loads(check_health.REMOVED_CHANNELS_JSON.read_text(encoding="utf-8"))
        urls = [row["url"] for row in data["channels"]]
        self.assertIn("http://old.example/live.m3u8", urls)

    def test_recovered_url_leaves_the_record(self) -> None:
        self._seed([{
            "name": "恢复频道", "url": "http://back.example/live.m3u8",
            "removed_at": "2026-10-20T00:00:00Z",
        }])
        item = self._channel("http://back.example/live.m3u8", "probe_ok")
        check_health.write_removed_channels([item])
        data = json.loads(check_health.REMOVED_CHANNELS_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["channels"], [])

    def test_csv_header_names_every_field(self) -> None:
        self._fail_three_times("http://gone.example/live.m3u8")
        check_health.write_removed_channels([self._channel("http://gone.example/live.m3u8", "probe_failed", 404)])
        text = check_health.REMOVED_CHANNELS_CSV.read_text(encoding="utf-8-sig")
        header = text.splitlines()[0]
        for field in ("name", "url", "host", "source", "removed_at", "evidence"):
            self.assertIn(field, header)

    def test_unsampled_removed_url_is_rechecked(self) -> None:
        """A removed URL the cursor never reached must still be probed.

        Removal is permanent until the URL is sampled again. With a 6500 of
        9816 sample that can take several runs, so a transient failure would
        keep a working channel out of merged.m3u far longer than intended.
        """
        self._seed([{
            "name": "恢复频道", "url": "http://back.example/live.m3u8",
            "removed_at": "2026-10-20T00:00:00Z",
        }])
        # An unrelated channel that WAS sampled this run.
        sampled = [self._channel("http://sampled.example/live.m3u8", "probe_ok")]
        with mock.patch.object(check_health, "check",
                               side_effect=lambda item, fp, dp: self._channel(
                                   item.url, "probe_ok")) as check_mock:
            recovered = check_health.recover_removed_channels(sampled, "ffprobe", True)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].url, "http://back.example/live.m3u8")
        self.assertEqual(recovered[0].status, "probe_ok")
        self.assertEqual(check_mock.call_count, 1)

    def test_removed_url_sampled_this_run_is_not_rechecked(self) -> None:
        self._seed([{
            "name": "已抽样", "url": "http://back.example/live.m3u8",
            "removed_at": "2026-10-20T00:00:00Z",
        }])
        sampled = [self._channel("http://back.example/live.m3u8", "probe_ok")]
        with mock.patch.object(check_health, "check") as check_mock:
            recovered = check_health.recover_removed_channels(sampled, "ffprobe", True)
        self.assertEqual(recovered, [])
        check_mock.assert_not_called()

    def test_recovered_url_leaves_the_record_after_recheck(self) -> None:
        """A recovered URL must leave the record and re-enter merged.m3u."""
        self._seed([{
            "name": "恢复频道", "url": "http://back.example/live.m3u8",
            "removed_at": "2026-10-20T00:00:00Z",
        }])
        recovered = [self._channel("http://back.example/live.m3u8", "probe_ok")]
        check_health.write_removed_channels(recovered)
        data = json.loads(check_health.REMOVED_CHANNELS_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["channels"], [])

    def test_internal_removed_url_is_not_rechecked(self) -> None:
        self._seed([{
            "name": "内网频道", "url": "http://rrs03.hw.gmcc.net/live/cctv1.m3u8",
            "removed_at": "2026-10-20T00:00:00Z",
        }])
        with mock.patch.object(check_health, "check") as check_mock:
            recovered = check_health.recover_removed_channels([], "ffprobe", True)
        self.assertEqual(recovered, [])
        check_mock.assert_not_called()

    def _fail_three_times(self, url: str) -> None:
        for _ in range(3):
            check_health.write_channel_health([self._channel(url, "probe_failed", 404)])

    def _channel(self, url: str, status: str, http_status: int | None = None):
        return check_health.Item(
            kind="channel",
            name="CCTV-测试",
            url=url,
            status=status,
            http_status=http_status,
            detail="HTTP 404",
            checked_at="2026-10-23T00:00:00Z",
            source="https://example.com/source",
            category="央视频道",
            evidence="HTTP 404",
            failure_class="hard",
            host="gone.example",
        )


class WhitelistAggregationTests(unittest.TestCase):
    """The aggregator must honour the whitelist even against a stale report.

    docs/dead_channels.json is only rewritten by the health workflow, so a
    URL added to tvbox/whitelist.txt right now would otherwise keep being
    dropped from merged.m3u until the next health run completed.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._dead = fetch_tvbox.DEAD_CHANNELS_PATH
        self._wl = fetch_tvbox.WHITELIST_PATH
        fetch_tvbox.DEAD_CHANNELS_PATH = self.root / "dead_channels.json"
        fetch_tvbox.WHITELIST_PATH = self.root / "whitelist.txt"

    def tearDown(self) -> None:
        fetch_tvbox.DEAD_CHANNELS_PATH = self._dead
        fetch_tvbox.WHITELIST_PATH = self._wl
        self._dir.cleanup()

    def _seed_dead(self, urls: list[str]) -> None:
        fetch_tvbox.DEAD_CHANNELS_PATH.write_text(json.dumps({
            "updated": "x",
            "removed_urls": urls,
        }), encoding="utf-8")

    def _write_whitelist(self, text: str) -> None:
        fetch_tvbox.WHITELIST_PATH.write_text(text, encoding="utf-8")

    def test_stale_dead_list_does_not_remove_whitelisted_url(self) -> None:
        target = "http://gone.example/live/cctv1.m3u8"
        self._seed_dead([target, "http://other.example/live.m3u8"])
        self._write_whitelist("gone.example\n")
        dead = fetch_tvbox.load_dead_channel_urls()
        self.assertNotIn(target, dead)
        self.assertIn("http://other.example/live.m3u8", dead)

    def test_missing_whitelist_keeps_legacy_behaviour(self) -> None:
        target = "http://gone.example/live/cctv1.m3u8"
        self._seed_dead([target])
        # No whitelist file at all.
        self.assertIn(target, fetch_tvbox.load_dead_channel_urls())

    def test_url_entry_protects_host_and_subdomains(self) -> None:
        self._write_whitelist("http://gone.example/live/cctv1.m3u8\n")
        whitelist = fetch_tvbox.load_whitelist()
        self.assertTrue(fetch_tvbox.is_whitelisted(
            "http://cdn.gone.example/live/other.m3u8", whitelist))
        self.assertFalse(fetch_tvbox.is_whitelisted(
            "http://gone.example.evil.com/live.m3u8", whitelist))


class CredentialRedactionTests(unittest.TestCase):
    """A credential shape the redactor missed must never kill the run.

    CI run 39850087046 aborted at write_report() with "TOKEN" after 53 minutes
    of probing, losing the whole report. The cause was a bare "token=value"
    printed by ffprobe: REDACTABLE_QUERY only matches a URL query, so a pair
    with no ? & or ; in front of it slipped through both the redactor and the
    pre-write check.
    """

    def test_bare_key_value_is_redacted(self) -> None:
        from config_security import redact_sensitive_text
        self.assertEqual(redact_sensitive_text("token=abc123"), "token=<redacted>")
        self.assertEqual(
            redact_sensitive_text("Token: abc123"), "Token=<redacted>"
        )
        self.assertEqual(
            redact_sensitive_text("sessionid=abc"), "sessionid=<redacted>"
        )

    def test_bare_redaction_leaves_plain_text_alone(self) -> None:
        from config_security import redact_sensitive_text
        for text in (
            "ffprobe(Mozilla) 检测到媒体流: video,audio",
            "HTTP 200；UA=Lavf；Content-Type=video/mp4；重定向 2 次",
            "Content-Type=text/html",
            "UA=Mozilla",
            "站点播放依赖影视 App 与 spider 解析",
        ):
            self.assertEqual(redact_sensitive_text(text), text)

    def test_url_query_is_still_redacted(self) -> None:
        from config_security import redact_sensitive_text
        self.assertIn("token=<redacted>", redact_sensitive_text(
            "Opening 'http://a.com/live/1.m3u8?token=ZmFrZQ' for reading"
        ))

    def test_residual_credential_does_not_abort_the_report(self) -> None:
        """A shape the redactor still misses must warn, not raise."""
        report = {
            "updated": "2026-10-23T00:00:00Z",
            "items": [{"kind": "channel", "detail": "token=ZmFrZQx9Kq2"}],
        }
        with mock.patch.object(check_health, "find_sensitive_credentials",
                               return_value=["$.items[0].detail: value:token"]):
            check_health.assert_no_credentials(report, "健康报告")
        # The run survived and the value is gone from what gets written.
        self.assertNotIn("ZmFrZQx9Kq2", json.dumps(report, ensure_ascii=False))
        self.assertIn("token=<redacted>", json.dumps(report, ensure_ascii=False))

    def test_clean_report_is_untouched(self) -> None:
        report = {"items": [{"detail": "HTTP 404；尝试 2 次"}]}
        before = json.dumps(report, ensure_ascii=False)
        with mock.patch.object(check_health, "find_sensitive_credentials",
                               return_value=[]):
            check_health.assert_no_credentials(report, "健康报告")
        self.assertEqual(json.dumps(report, ensure_ascii=False), before)


if __name__ == "__main__":
    unittest.main()
