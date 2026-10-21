#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect credentials that must not be published in generated configurations."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any


SENSITIVE_NAMES = frozenset({
    "accountinfo",
    "accesstoken",
    "apikey",
    "auth",
    "authorization",
    "authtoken",
    "bearer",
    "clientsecret",
    "cookie",
    "key",
    "password",
    "passwd",
    "pwd",
    "refreshtoken",
    "secret",
    "session",
    "sessionid",
    "sig",
    "sign",
    "signature",
    "token",
})

URL_FIELDS = frozenset({
    "api",
    "epg",
    "ext",
    "header",
    "headers",
    "jar",
    "live",
    "logo",
    "parse",
    "playurl",
    "spider",
    "url",
})

QUERY_PARAMETER = re.compile(r"(?:[?&;])([A-Za-z0-9_.-]+)=([^&#\s\"'<>]*)")
KEY_VALUE = re.compile(
    r"(?:^|[\s,{;\"'])([A-Za-z][A-Za-z0-9_.-]*)\s*[:=]\s*[\"']?([^\"'\s,}]+)",
    re.I,
)
HEADER_CREDENTIAL = re.compile(
    r"(?i)\b(?:authorization|cookie|set-cookie|x-api-key|api-key)\b\s*[:=]\s*[^\s,;]+"
)
BEARER_CREDENTIAL = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
REDACTABLE_HEADER = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api-key)\b\s*[:=]\s*[^\s,;]+"
)
URL_USERINFO = re.compile(r"(?i)\b(https?://)[^/@\s]+@")
REDACTABLE_QUERY = re.compile(r"([?&;])([A-Za-z0-9_.-]+)=([^&#\s\"'<>]*)")
PLACEHOLDER = re.compile(
    r"^(?:\{\{.*\}\}|\$\{.*\}|%[A-Z0-9_]+%|<[^>]+>|\[[A-Z0-9_]+\])$",
    re.I,
)
EMPTY_OR_PUBLIC = frozenset({
    "",
    "0",
    "false",
    "none",
    "null",
    "public",
    "true",
    "undefined",
})


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _meaningful_secret(value: str) -> bool:
    candidate = urllib.parse.unquote(value.strip().strip("\"'"))
    if candidate.lower() in EMPTY_OR_PUBLIC or PLACEHOLDER.fullmatch(candidate):
        return False
    if candidate.startswith(("{{", "${")) or candidate.endswith(("}}", "}")):
        return False
    return len(candidate) >= 3


def _scan_string(value: str, path: str, field: str) -> list[str]:
    findings: list[str] = []
    decoded = urllib.parse.unquote(value)

    # TVBox site.key is a public identifier, not an API credential.
    if (
        _normalized_name(field) in SENSITIVE_NAMES
        and _normalized_name(field) != "key"
        and _meaningful_secret(value)
    ):
        findings.append("%s: field:%s" % (path, field))

    if "://" in decoded:
        try:
            parts = urllib.parse.urlsplit(decoded)
            if parts.username or parts.password:
                findings.append("%s: url-userinfo" % path)
        except ValueError:
            pass

    for match in QUERY_PARAMETER.finditer(decoded):
        name, candidate = match.groups()
        if _normalized_name(name) in SENSITIVE_NAMES and _meaningful_secret(candidate):
            findings.append("%s: query:%s" % (path, name))

    if _normalized_name(field) in URL_FIELDS or any(token in decoded for token in ("=", ":")):
        for match in KEY_VALUE.finditer(decoded):
            name, candidate = match.groups()
            if _normalized_name(name) in SENSITIVE_NAMES and _meaningful_secret(candidate):
                findings.append("%s: value:%s" % (path, name))

    if HEADER_CREDENTIAL.search(decoded):
        findings.append("%s: credential-header" % path)
    if BEARER_CREDENTIAL.search(decoded):
        findings.append("%s: bearer-token" % path)
    return findings


def find_sensitive_credentials(value: Any, path: str = "$") -> list[str]:
    """Return safe finding labels (parameter names and paths, never values)."""
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = "%s.%s" % (path, key)
            if isinstance(item, str):
                findings.extend(_scan_string(item, child_path, str(key)))
            else:
                findings.extend(find_sensitive_credentials(item, child_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            findings.extend(find_sensitive_credentials(item, "%s[%d]" % (path, index)))
    elif isinstance(value, str):
        findings.extend(_scan_string(value, path, ""))
    return findings


def contains_sensitive_credentials(value: Any) -> bool:
    return bool(find_sensitive_credentials(value))


def redact_sensitive_text(value: Any) -> str:
    """Redact credentials from a URL or error string before publishing it."""
    text = "" if value is None else str(value)
    if not text:
        return text

    text = URL_USERINFO.sub(lambda match: "%s<redacted>@" % match.group(1), text)

    def redact_query(match: re.Match[str]) -> str:
        name = match.group(2)
        if _normalized_name(name) not in SENSITIVE_NAMES:
            return match.group(0)
        return "%s%s=<redacted>" % (match.group(1), name)

    text = REDACTABLE_QUERY.sub(redact_query, text)

    def redact_header(match: re.Match[str]) -> str:
        return "%s: <redacted>" % match.group(1)

    text = REDACTABLE_HEADER.sub(redact_header, text)
    text = BEARER_CREDENTIAL.sub("Bearer <redacted>", text)
    return text
