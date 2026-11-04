"""LLM 调用公共层：协议适配 + 配置读取（settings 表 + 环境变量覆盖）。

环境变量（优先级高于设置页，供 Docker 部署 / 测试实例使用）：
    LLM_PROTOCOL / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
"""
from __future__ import annotations

import json
import os

_LLM_SETTING_KEY = "llm"


def llm_cfg() -> dict:
    import database
    raw = database.get_setting(_LLM_SETTING_KEY, "")
    try:
        cfg = json.loads(raw) if raw else {}
    except Exception:
        cfg = {}
    out = {"protocol": (cfg.get("protocol") or "anthropic").strip().lower(),
           "base_url": (cfg.get("base_url") or "").strip(),
           "model": (cfg.get("model") or "").strip(),
           "api_key": (cfg.get("api_key") or "").strip()}
    # 环境变量覆盖（已设置的项优先于设置页保存值）
    env_map = {"protocol": "LLM_PROTOCOL", "base_url": "LLM_BASE_URL",
               "model": "LLM_MODEL", "api_key": "LLM_API_KEY"}
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            out[k] = v.strip()
    if out["protocol"] not in ("anthropic", "openai"):
        out["protocol"] = "anthropic"
    return out


def llm_request(cfg: dict, messages: list, system: str = "",
                max_tokens: int = 1024, timeout: int = 90) -> str:
    """按配置的协议调用 LLM，返回回复文本；未配置或失败抛 HTTPException(400/502)。

    messages = [{"role": "user"|"assistant", "content": "..."}]，system 为指令/人设。
    """
    import urllib.error
    import urllib.request
    from fastapi import HTTPException
    if not (cfg["base_url"] and cfg["model"] and cfg["api_key"]):
        raise HTTPException(status_code=400,
                            detail="请先在设置页配置 LLM（接口地址、模型、API Key）")
    base = cfg["base_url"].rstrip("/")
    if cfg["protocol"] == "anthropic":          # 例：https://api.minimax.cn/anthropic
        url, headers = base + "/v1/messages", {
            "content-type": "application/json", "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01"}
        body: dict = {"model": cfg["model"], "max_tokens": max_tokens, "messages": messages}
        if system:
            body["system"] = system
    else:                                       # openai 兼容：例 https://api.minimax.cn/v1
        url, headers = base + "/v1/chat/completions", {
            "content-type": "application/json", "authorization": "Bearer " + cfg["api_key"]}
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        body = {"model": cfg["model"], "max_tokens": max_tokens, "messages": msgs}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail="LLM HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
    except Exception as e:
        raise HTTPException(status_code=502, detail="LLM 请求失败：" + str(e)[:200])
    text = ""
    if isinstance(data, dict):
        if cfg["protocol"] == "anthropic":
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        else:
            choices = data.get("choices") or []
            if choices:
                text = (choices[0].get("message") or {}).get("content") or ""
    return text.strip()


def retryable_llm_error(detail: str) -> bool:
    """哪些错误值得退避重试：429 限流 / 5xx 服务端错误 / 网络(超时、连接)失败。"""
    d = detail or ""
    return ("LLM HTTP 429" in d or "LLM HTTP 5" in d or "LLM 请求失败" in d
            or "timed out" in d.lower() or "timeout" in d.lower())
