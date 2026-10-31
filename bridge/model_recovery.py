"""Bridge-owned model failure notices; model text never controls recovery."""

from __future__ import annotations

import re


def error_summary(error: Exception | dict) -> str:
    if isinstance(error, dict):
        info = error.get("codexErrorInfo")
        name = info if isinstance(info, str) else ""
        details = {}
        if isinstance(info, dict) and info:
            name, details = next(iter(info.items()))
            details = details if isinstance(details, dict) else {}
        status = details.get("httpStatusCode")
        heading = f"HTTP {status}" if status is not None else name or "模型请求失败"
        message = str(error.get("message") or "Codex 返回失败状态")
    else:
        heading = type(error).__name__
        message = str(error) or "请求未能完成"
    # Provider diagnostics can contain credentials or URLs with query tokens.
    message = re.sub(r"https?://\S+", "[接口地址已省略]", message)
    message = re.sub(r"(?i)\bbearer\s+\S+|\bsk-[\w-]+", "[凭据已隐藏]", message)
    message = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|authorization)([\s\"':=]+)[^\s,}]+",
        r"\1\2[已隐藏]",
        message,
    )
    return f"{heading}\n\n{message[:800]}"
