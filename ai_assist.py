#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional AI-assisted review for classification and incident summaries.

The model is deliberately kept outside the health decision path:

* it never marks a stream as dead;
* it never writes ``removable.json`` or edits ``merged.m3u``;
* it never receives full URLs containing credentials;
* any API failure is fail-open and only produces an ``unavailable`` report.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from config_security import find_sensitive_credentials, redact_sensitive_text


HERE = Path(__file__).resolve().parent
DOCS_DIR = HERE / "docs"
AI_REVIEW_JSON = DOCS_DIR / "ai_review.json"
CHANNEL_HEALTH_JSON = DOCS_DIR / "channel_health.json"
CLASSIFICATION_REVIEW_JSON = DOCS_DIR / "classification_review.json"
REMOVABLE_JSON = DOCS_DIR / "removable.json"

ALLOWED_CATEGORIES = {
    "央视频道",
    "卫视频道",
    "地方频道",
    "港澳台",
    "海外频道",
    "体育频道",
    "音乐频道",
    "音乐点播",
    "影视少儿",
    "影视轮播",
    "教育纪实",
    "运营商/内网",
    "综合直播",
    "待人工确认",
}


def _log(message: str) -> None:
    print("[%s] AI %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message))


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _provider_configs(mode: str) -> list[dict[str, str]]:
    mode = (mode or "none").strip().lower()
    local = {
        "provider": "local-strata",
        "base_url": os.environ.get("STRATA_BASE_URL", "").strip(),
        "api_key": os.environ.get("STRATA_API_KEY", "").strip(),
        "model": os.environ.get("STRATA_MODEL", "").strip() or "qwen3.8-flash-next-iq2_xs",
        "wire_api": os.environ.get("STRATA_WIRE_API", "chat").strip().lower(),
    }
    cloud = {
        "provider": "cloud",
        "base_url": os.environ.get("AI_BASE_URL", "").strip(),
        "api_key": (
            os.environ.get("AI_API_KEY", "").strip()
            or os.environ.get("MAAS_API_KEY", "").strip()
        ),
        "model": os.environ.get("AI_MODEL", "").strip(),
        "wire_api": "chat",
    }
    if mode == "none":
        return []
    if mode == "local":
        return [local] if local["base_url"] else []
    if mode == "cloud":
        return [cloud] if cloud["base_url"] and cloud["api_key"] and cloud["model"] else []
    if mode == "auto":
        result = []
        if local["base_url"]:
            result.append(local)
        if cloud["base_url"] and cloud["api_key"] and cloud["model"]:
            result.append(cloud)
        return result
    return []


def _endpoint(base_url: str) -> str:
    value = base_url.rstrip("/")
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", value, re.S | re.I)
    if fenced:
        value = fenced.group(1)
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        value = value[start:end + 1]
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return parsed


def _call_chat(config: dict[str, str], prompt: str) -> dict[str, Any]:
    if config.get("wire_api") == "responses":
        return _call_responses(config, prompt)
    payload = {
        "model": config["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 TVBox 直播源治理助手。你只做分类建议、错误聚类和中文解释。"
                    "你不能判断直播源是否失效，不能建议直接删除，不能输出或猜测凭据。"
                    "必须只返回一个 JSON 对象。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1800,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config["api_key"]:
        headers["Authorization"] = "Bearer " + config["api_key"]
    request = urllib.request.Request(
        _endpoint(config["base_url"]),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read(2 * 1024 * 1024)
    data = json.loads(raw.decode("utf-8", "replace"))
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("模型响应缺少 choices")
    content = choices[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型响应内容为空")
    return _extract_json(content)


def _call_responses(config: dict[str, str], prompt: str) -> dict[str, Any]:
    """Call an OpenAI-compatible Responses endpoint such as the local bridge."""
    payload = {
        "model": config["model"],
        "input": prompt,
        "max_output_tokens": 1800,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config["api_key"]:
        headers["Authorization"] = "Bearer " + config["api_key"]
    base = config["base_url"].rstrip("/")
    endpoint = base + "/responses" if base.endswith("/v1") else base + "/v1/responses"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read(2 * 1024 * 1024)
    data = json.loads(raw.decode("utf-8", "replace"))
    texts: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                texts.append(str(content.get("text", "")))
    if not texts:
        raise ValueError("Responses 模型响应缺少 output_text")
    return _extract_json("\n".join(texts))


def _pending_classifications() -> list[dict[str, Any]]:
    data = _read_json(CLASSIFICATION_REVIEW_JSON, {})
    pending = data.get("pending", []) if isinstance(data, dict) else []
    result = []
    for entry in pending[:120]:
        if not isinstance(entry, dict):
            continue
        result.append({
            "channel_key": redact_sensitive_text(str(entry.get("channel_key", "")))[:120],
            "name": redact_sensitive_text(str(entry.get("name", "")))[:160],
            "groups_raw": [
                redact_sensitive_text(str(value))[:80]
                for value in entry.get("groups_raw", [])[:5]
            ],
            "source_sections": [
                redact_sensitive_text(str(value))[:80]
                for value in entry.get("source_sections", [])[:5]
            ],
            "line_count": int(entry.get("line_count", 0) or 0),
        })
    return result


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _error_clusters() -> list[dict[str, Any]]:
    data = _read_json(CHANNEL_HEALTH_JSON, {})
    rows = data.get("lines", []) if isinstance(data, dict) else []
    counter: Counter[tuple[str, str, str, str, str]] = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("verdict") not in {"observe", "observe_soft", "candidate_remove"}:
            continue
        counter[(
            str(row.get("status", "")),
            str(row.get("failure_class", "")),
            str(row.get("http_status", "")),
            _host(str(row.get("url", ""))),
            str(row.get("category", "")),
        )] += 1
    result = []
    for (status, failure_class, http_status, host, category), count in counter.most_common(30):
        result.append({
            "status": status,
            "failure_class": failure_class,
            "http_status": http_status,
            "host": host,
            "category": category,
            "count": count,
        })
    return result


def _removable_summary() -> dict[str, Any]:
    data = _read_json(REMOVABLE_JSON, {})
    if not isinstance(data, dict):
        return {}
    return {
        "flagged": len(data.get("removable", [])),
        "pending": len(data.get("pending", [])),
        "policy": data.get("policy", {}),
    }


def _build_prompt(pending: list[dict[str, Any]], clusters: list[dict[str, Any]]) -> str:
    allowed = ", ".join(sorted(ALLOWED_CATEGORIES))
    return (
        "请处理下面两类辅助任务。\n"
        "第一，给待归类频道提出分类建议；只能从以下类别选择："
        + allowed
        + "。不确定就填“待人工确认”，不要编造频道身份。\n"
        "第二，把失败聚类翻译成简短中文，指出可能原因和人工复核方向。"
        "不要给出删除结论，也不要把超时、5xx、403/429 当作失效。\n\n"
        "只返回如下 JSON 结构：\n"
        '{"classification_suggestions":[{"channel_key":"...","category":"...",'
        '"confidence":"high|medium|low","reason":"..."}],'
        '"incident_summary":"...","recommended_actions":["..."],"warnings":["..."]}\n\n'
        "待归类频道：\n"
        + json.dumps(pending, ensure_ascii=False)
        + "\n\n失败聚类：\n"
        + json.dumps(clusters, ensure_ascii=False)
    )


def _sanitize_result(value: dict[str, Any]) -> dict[str, Any]:
    suggestions = []
    for item in value.get("classification_suggestions", []):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        if category not in ALLOWED_CATEGORIES:
            continue
        suggestions.append({
            "channel_key": redact_sensitive_text(str(item.get("channel_key", "")))[:120],
            "category": category,
            "confidence": str(item.get("confidence", "")).lower()[:12],
            "reason": redact_sensitive_text(str(item.get("reason", "")))[:500],
        })
    actions = [
        redact_sensitive_text(str(value))[:500]
        for value in value.get("recommended_actions", [])[:20]
    ]
    warnings = [
        redact_sensitive_text(str(value))[:500]
        for value in value.get("warnings", [])[:20]
    ]
    return {
        "classification_suggestions": suggestions[:120],
        "incident_summary": redact_sensitive_text(str(value.get("incident_summary", "")))[:1500],
        "recommended_actions": actions,
        "warnings": warnings,
    }


def _write_review(path: Path, result: dict[str, Any]) -> None:
    """Write a review only after the same credential gate used by reports."""
    findings = find_sensitive_credentials(result)
    if findings:
        raise ValueError("AI 辅助报告包含疑似凭据，已中止写入")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_ai_review(mode: str, output_dir: Path | None = None) -> dict[str, Any]:
    """Write an optional AI review and return a small status summary."""
    target = (output_dir or DOCS_DIR) / AI_REVIEW_JSON.name
    pending = _pending_classifications()
    clusters = _error_clusters()
    # A disabled optional review must not create a timestamp-only commit on
    # every health run. Keep the last useful report for manual inspection.
    if (mode or "none").strip().lower() == "none":
        return {"status": "skipped", "provider": "", "suggestions": 0}
    base = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "safety": (
            "AI 只生成分类建议和错误解释；不参与失效判定、不修改 merged.m3u、"
            "不写入 removable.json、不接收含凭据的完整 URL。"
        ),
        "inputs": {
            "pending_channels": len(pending),
            "failure_clusters": len(clusters),
            "removable": _removable_summary(),
        },
    }
    configs = _provider_configs(mode)
    if not configs:
        result = {
            **base,
            "status": "skipped",
            "reason": "未配置可用的 AI 提供方，或运行模式为 none",
        }
        _write_review(target, result)
        return {"status": "skipped", "provider": "", "suggestions": 0}

    prompt = _build_prompt(pending, clusters)
    errors: list[str] = []
    for config in configs:
        try:
            _log("尝试 %s / %s" % (config["provider"], config["model"]))
            parsed = _sanitize_result(_call_chat(config, prompt))
            result = {
                **base,
                "status": "ok",
                "provider": config["provider"],
                "model": config["model"],
                "result": parsed,
            }
            _write_review(target, result)
            return {
                "status": "ok",
                "provider": config["provider"],
                "suggestions": len(parsed["classification_suggestions"]),
            }
        except Exception as exc:
            message = redact_sensitive_text(str(exc))[:300]
            errors.append("%s: %s" % (config["provider"], message))
            _log("%s 不可用，继续尝试下一个: %s" % (config["provider"], message))

    result = {
        **base,
        "status": "unavailable",
        "errors": errors,
    }
    _write_review(target, result)
    return {"status": "unavailable", "provider": "", "suggestions": 0}
