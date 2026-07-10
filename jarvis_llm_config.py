#!/usr/bin/env python3
"""贾维斯 JARVIS — 统一 LLM 配置层。

解决"用户必须手改 .env 才能配大模型"的问题：
  - 桌面端设置页把 API Key / Base URL / 模型名 写入 ~/.vibe-trading/llm_config.json
  - 本模块统一向 evolve / dashboard / reasoning / strategy_gen 提供生效配置

优先级（高 → 低）：
  1. ~/.vibe-trading/llm_config.json（UI 保存，api_key 非空才生效）
  2. 环境变量 / .env（DEEPSEEK_API_KEY / JARVIS_LLM_API_KEY / OPENAI_API_KEY
     + JARVIS_LLM_BASE_URL + JARVIS_LLM_MODEL，与历史行为一致）

返回结构与历史 _llm_config() 同构：{"key": str, "base": str, "model": str}
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
LLM_CONFIG_PATH = os.path.join(CONFIG_DIR, "llm_config.json")

# provider 预设：填了 key 不填 base/model 时的默认值
PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "custom": {"base_url": "", "model": ""},
}

# 用户可调的生成参数（借鉴 QuantDinger：模型行为不写死在代码里）
PARAM_DEFAULTS: dict[str, Any] = {
    "temperature": 0.75,      # 问答默认温度：偏活一点让语气有人味（UI 配置优先；推理/JSON 场景调用方显式覆盖）
    "max_tokens": 900,        # 单次回答长度上限
    "system_prompt_extra": "",  # 追加到内置 system prompt 之后的用户自定义人格/偏好
}

_PARAM_BOUNDS = {
    "temperature": (0.0, 2.0),
    "max_tokens": (100, 8000),
}


class LLMNotConfigured(RuntimeError):
    """未配置任何可用的 API Key。"""


class LLMCallError(RuntimeError):
    """LLM 调用在拿到首包前失败（网络/鉴权/4xx 等）。"""


def _record_usage(
    module: str,
    cfg: dict | None,
    t0: float,
    ok: bool,
    *,
    usage: dict | None = None,
    messages: list[dict] | None = None,
    output_text: str | None = None,
    error: str | None = None,
) -> None:
    """LLM 用量记账钩子（jarvis_llm_usage）。记账失败静默，绝不影响主调用。"""
    try:
        import jarvis_llm_usage as jlu

        jlu.record_call(
            module=module,
            model=(cfg or {}).get("model"),
            base=(cfg or {}).get("base"),
            usage=usage,
            messages=messages,
            output_text=output_text,
            latency_ms=int((time.time() - t0) * 1000),
            ok=ok,
            error=error,
        )
    except Exception:  # noqa: BLE001
        pass


def _read_file_config() -> dict[str, Any]:
    if os.path.exists(LLM_CONFIG_PATH):
        try:
            with open(LLM_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _infer_base_and_model(base_url: str, model: str, provider: str) -> tuple[str, str]:
    """按 provider 预设/base 特征补全缺省的 base_url 与 model。"""
    preset = PROVIDER_PRESETS.get((provider or "").lower(), {})
    base = (base_url or preset.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if not model:
        model = preset.get("model") or (
            "deepseek-chat" if "deepseek" in base.lower() else "gpt-4o-mini"
        )
    return base, model


def _env_config() -> dict[str, str] | None:
    """历史口径：环境变量读取（.env 已由入口脚本加载进 os.environ）。"""
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    key = os.environ.get("JARVIS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ds_key
    if not key:
        return None
    base = os.environ.get("JARVIS_LLM_BASE_URL")
    if not base:
        base = "https://api.deepseek.com" if ds_key else "https://api.openai.com/v1"
    base = base.rstrip("/")
    is_deepseek = "deepseek" in base.lower()
    model = os.environ.get("JARVIS_LLM_MODEL") or ("deepseek-chat" if is_deepseek else "gpt-4o-mini")
    return {"key": key, "base": base, "model": model}


def get_llm_config() -> dict[str, str] | None:
    """返回生效的 LLM 配置 {key, base, model}；两处都未配置返回 None。"""
    file_cfg = _read_file_config()
    api_key = str(file_cfg.get("api_key", "") or "").strip()
    if api_key:
        base, model = _infer_base_and_model(
            str(file_cfg.get("base_url", "") or "").strip(),
            str(file_cfg.get("model", "") or "").strip(),
            str(file_cfg.get("provider", "") or "").strip(),
        )
        return {"key": api_key, "base": base, "model": model}
    return _env_config()


def _clamp_param(key: str, value: Any) -> Any:
    try:
        v = float(value) if key == "temperature" else int(value)
    except (TypeError, ValueError):
        return PARAM_DEFAULTS[key]
    lo, hi = _PARAM_BOUNDS[key]
    return max(lo, min(hi, v))


def get_params() -> dict[str, Any]:
    """用户可调生成参数（temperature / max_tokens / system_prompt_extra），带护栏。"""
    file_cfg = _read_file_config()
    return {
        "temperature": _clamp_param("temperature", file_cfg.get("temperature", PARAM_DEFAULTS["temperature"])),
        "max_tokens": _clamp_param("max_tokens", file_cfg.get("max_tokens", PARAM_DEFAULTS["max_tokens"])),
        "system_prompt_extra": str(file_cfg.get("system_prompt_extra", "") or "").strip()[:1000],
    }


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 6}{key[-4:]}"


def read_config_masked() -> dict[str, Any]:
    """给前端设置页用：key 只回脱敏值，并说明当前生效来源。"""
    file_cfg = _read_file_config()
    file_key = str(file_cfg.get("api_key", "") or "").strip()
    env_cfg = _env_config()
    effective = get_llm_config()
    params = get_params()
    source = "none"
    if effective:
        source = "file" if file_key else "env"
    return {
        "provider": file_cfg.get("provider", "deepseek"),
        "base_url": file_cfg.get("base_url", ""),
        "model": file_cfg.get("model", ""),
        "api_key_masked": _mask_key(file_key),
        "has_key": bool(file_key),
        "env_fallback_available": bool(env_cfg) and not file_key,
        "configured": bool(effective),
        "source": source,
        "effective_base": effective["base"] if effective else "",
        "effective_model": effective["model"] if effective else "",
        "temperature": params["temperature"],
        "max_tokens": params["max_tokens"],
        "system_prompt_extra": params["system_prompt_extra"],
        "presets": {k: dict(v) for k, v in PROVIDER_PRESETS.items()},
    }


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """合并保存配置；api_key 留空表示保持原值。传 clear_key=True 清空 key。"""
    cfg = _read_file_config()
    for field in ("provider", "base_url", "model"):
        if field in updates and updates[field] is not None:
            cfg[field] = str(updates[field]).strip()
    for field in ("temperature", "max_tokens"):
        if field in updates and updates[field] is not None:
            cfg[field] = _clamp_param(field, updates[field])
    if "system_prompt_extra" in updates and updates["system_prompt_extra"] is not None:
        cfg["system_prompt_extra"] = str(updates["system_prompt_extra"]).strip()[:1000]
    key = updates.get("api_key")
    if isinstance(key, str) and key.strip():
        cfg["api_key"] = key.strip()
    if updates.get("clear_key"):
        cfg["api_key"] = ""
    cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LLM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(LLM_CONFIG_PATH, 0o600)  # 配置里有 key，收紧权限
    except OSError:
        pass
    return read_config_masked()


def _build_request(cfg: dict[str, str], messages: list[dict], *, stream: bool,
                   temperature: float, max_tokens: int,
                   json_mode: bool = False) -> urllib.request.Request:
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        },
    )


def call_llm(
    prompt: str,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 2000,
    timeout: int = 90,
    cfg: dict[str, str] | None = None,
    module: str = "unknown",
) -> str:
    """OpenAI 兼容 chat/completions 调用，返回文本；未配置/失败抛异常。"""
    cfg = cfg or get_llm_config()
    if not cfg:
        raise RuntimeError(
            "未配置大模型。请在桌面端「设置 → 大模型 (LLM)」填入 API Key，"
            "或设置环境变量 DEEPSEEK_API_KEY / JARVIS_LLM_API_KEY。"
        )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    req = _build_request(cfg, messages, stream=False,
                         temperature=temperature, max_tokens=max_tokens)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _record_usage(module, cfg, t0, ok=False, messages=messages,
                      error=repr(e)[:150])
        raise
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    _record_usage(module, cfg, t0, ok=True, usage=body.get("usage"),
                  messages=messages, output_text=text)
    return text


def chat(
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
    timeout: int = 60,
    cfg: dict[str, str] | None = None,
    module: str = "unknown",
) -> str | None:
    """多轮 messages 非流式调用；未配置/失败返回 None（上层自行降级），永不抛出。

    temperature/max_tokens 不传时用设置页保存的用户参数（get_params）。
    """
    cfg = cfg or get_llm_config()
    if not cfg:
        return None
    params = get_params()
    req = _build_request(
        cfg, messages, stream=False,
        temperature=params["temperature"] if temperature is None else _clamp_param("temperature", temperature),
        max_tokens=params["max_tokens"] if max_tokens is None else _clamp_param("max_tokens", max_tokens),
        json_mode=json_mode,
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        _record_usage(module, cfg, t0, ok=True, usage=body.get("usage"),
                      messages=messages, output_text=text)
        return text or None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as e:
        _record_usage(module, cfg, t0, ok=False, messages=messages,
                      error=repr(e)[:150])
        return None


def chat_stream(
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int = 90,
    cfg: dict[str, str] | None = None,
    module: str = "unknown",
) -> Iterator[str]:
    """多轮 messages 流式调用：逐段 yield 文本增量（SSE delta）。

    首包前失败抛 LLMNotConfigured / LLMCallError（上层回退非流式或规则兜底）；
    流中途断线静默终止（已 yield 的内容有效）。
    """
    cfg = cfg or get_llm_config()
    if not cfg:
        raise LLMNotConfigured("未配置大模型 API Key")
    params = get_params()
    req = _build_request(
        cfg, messages, stream=True,
        temperature=params["temperature"] if temperature is None else _clamp_param("temperature", temperature),
        max_tokens=params["max_tokens"] if max_tokens is None else _clamp_param("max_tokens", max_tokens),
    )
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        _record_usage(module, cfg, t0, ok=False, messages=messages,
                      error=f"HTTP {e.code}: {(detail or str(e.reason))[:120]}")
        raise LLMCallError(f"HTTP {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _record_usage(module, cfg, t0, ok=False, messages=messages,
                      error=f"网络错误: {repr(e)[:120]}")
        raise LLMCallError(f"网络错误: {repr(e)[:150]}") from e

    # 流式响应通常不带 usage，按输入/输出文本估算记账；
    # 个别 OpenAI 兼容实现会在末尾 chunk 附 usage，取到就用真实值。
    out_parts: list[str] = []
    stream_usage: dict | None = None
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(obj.get("usage"), dict):
                    stream_usage = obj["usage"]
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    out_parts.append(delta)
                    yield delta
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return  # 流中途断开：保留已输出部分，静默收尾（finally 仍记账）
    finally:
        _record_usage(module, cfg, t0, ok=True, usage=stream_usage,
                      messages=messages, output_text="".join(out_parts))


def test_connection(cfg: dict[str, str] | None = None) -> dict[str, Any]:
    """真实调一次模型验证连通性；返回 {ok, latency_ms, model, reply/error}。"""
    cfg = cfg or get_llm_config()
    if not cfg:
        return {"ok": False, "error": "未配置：请先填入 API Key"}
    t0 = time.time()
    try:
        reply = call_llm(
            "请只回复两个字：在线", temperature=0.0, max_tokens=8, timeout=30, cfg=cfg,
            module="test",
        )
        return {
            "ok": True,
            "latency_ms": int((time.time() - t0) * 1000),
            "model": cfg["model"],
            "base": cfg["base"],
            "reply": reply[:40],
        }
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {detail or e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="统一 LLM 配置层")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("show", help="查看当前配置（脱敏）")
    sub.add_parser("test", help="测试连通性")
    args = parser.parse_args()
    if args.cmd == "show":
        print(json.dumps(read_config_masked(), ensure_ascii=False, indent=2))
    elif args.cmd == "test":
        print(json.dumps(test_connection(), ensure_ascii=False, indent=2))
    else:
        parser.print_help()
