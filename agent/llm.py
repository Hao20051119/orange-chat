# -*- coding: utf-8 -*-
"""DeepSeek / OpenAI 兼容接口封装 + 连通性自检。

用法：
    python llm.py            # 自检：读 config.json 打一次最小请求
    python llm.py "你好"      # 直接问一句
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
CONFIG = os.environ.get("WXBOT_CONFIG") or os.path.join(ROOT, "config.json")


def load_config() -> dict:
    """统一走 config.py：带校验、半写入保护、环境变量覆盖。"""
    try:
        from config import load as _load
        return _load()
    except Exception:
        with open(CONFIG, "r", encoding="utf-8-sig") as f:
            return json.load(f)


USAGE_FILE = os.environ.get("WXBOT_USAGE_LOG") or os.path.join(BASE, "usage.jsonl")


def _log_usage(usage: dict | None, elapsed: float, model: str):
    """记录每次调用用量，便于统计成本（不含聊天内容）。"""
    try:
        if os.path.exists(USAGE_FILE) and os.path.getsize(USAGE_FILE) > 5 * 1024 * 1024:
            os.replace(USAGE_FILE, USAGE_FILE + "." + time.strftime("%Y%m%d-%H%M%S"))
        rec = {
            "ts": int(time.time()),
            "model": model,
            "elapsed": elapsed,
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "completion_tokens": (usage or {}).get("completion_tokens"),
            "total_tokens": (usage or {}).get("total_tokens"),
        }
        with open(USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def usage_summary(days: int = 1) -> dict:
    """统计最近 N 天的调用次数与 token 用量。"""
    import datetime
    cutoff = time.time() - days * 86400
    calls = 0
    tokens = 0
    if not os.path.exists(USAGE_FILE):
        return {"calls": 0, "tokens": 0}
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("ts") or 0) >= cutoff:
                    calls += 1
                    tokens += r.get("total_tokens") or 0
    except Exception:
        pass
    return {"calls": calls, "tokens": tokens}


def chat(messages, config: dict | None = None, timeout: int = 60,
         max_tokens: int | None = None) -> dict:
    """调一次 chat/completions，返回 {ok, text, usage, error, truncated}。

    * 429 优先按 ``Retry-After`` 等待；5xx / 超时按 1s、2s… 退避并加抖动
    * HTTP 200 但 ``finish_reason=length`` → 提高 max_tokens 重试一次；
      仍被截断就标记 ``truncated``，由上层决定「不把它当完整回复发出去」
    * 每日调用数超过 ``llm.daily_budget`` 直接拒绝，避免意外烧额度
    """
    import random as _random

    cfg = config or load_config()
    llm = cfg.get("llm", {})
    key = (llm.get("api_key") or "").strip()
    if not key:
        return {"ok": False, "error": "config.json 里 llm.api_key 为空", "fatal": True}

    budget = int(llm.get("daily_budget", 0) or 0)
    if budget > 0:
        used = usage_summary(1).get("calls", 0)
        if used >= budget:
            return {"ok": False, "fatal": True,
                    "error": "今日 LLM 调用已达上限（%d/%d 次），已停止调用" % (used, budget)}

    url = llm.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    tokens = int(max_tokens or llm.get("max_tokens", 800))
    token_cap = max(tokens, int(llm.get("max_tokens_cap", 1200)))
    retries = int(llm.get("retries", 2))
    t0 = time.time()

    attempt = 0
    trunc_retried = False
    while attempt <= retries:
        payload = {
            "model": llm.get("model", "deepseek-chat"),
            "messages": messages,
            "temperature": llm.get("temperature", 1.0),
            "max_tokens": tokens,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            # HTTP 200 不代表内容可用：必须校验 choices / content
            choices = body.get("choices") or []
            if not choices:
                return {"ok": False, "error": "响应里没有 choices"}
            choice = choices[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            finish = choice.get("finish_reason")
            if not str(text).strip():
                return {"ok": False, "error": "模型返回空内容",
                        "finish_reason": finish, "usage": body.get("usage")}

            # 被截断：先加长重试一次，仍截断就交给上层处理（不发半句）
            if finish == "length" and not trunc_retried and tokens < token_cap:
                trunc_retried = True
                tokens = min(token_cap, int(tokens * 1.6) + 1)
                continue

            out = {
                "ok": True,
                "text": text,
                "usage": body.get("usage"),
                "finish_reason": finish,
                "elapsed": round(time.time() - t0, 2),
            }
            _log_usage(body.get("usage"), out["elapsed"], payload.get("model", ""))
            if finish == "length":
                out["truncated"] = True
            return out

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                detail = ""
            # 除 429 外的 4xx 是配置/请求问题，重试无意义
            if e.code != 429 and 400 <= e.code < 500:
                return {"ok": False, "error": "HTTP %s: %s" % (e.code, detail),
                        "status": e.code, "fatal": True}
            if attempt >= retries:
                return {"ok": False, "error": "HTTP %s: %s" % (e.code, detail),
                        "status": e.code}
            wait = 1.5 * (attempt + 1)
            if e.code == 429:
                ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                try:
                    wait = max(float(ra), 1.0) if ra else wait
                except (TypeError, ValueError):
                    pass
            attempt += 1
            time.sleep(wait + _random.uniform(0, 0.4))
            continue

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt >= retries:
                return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            attempt += 1
            time.sleep(1.0 * attempt + _random.uniform(0, 0.4))
            continue

        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    return {"ok": False, "error": "重试耗尽"}


def main():
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_config()
    llm = cfg.get("llm", {})
    print("base_url : %s" % llm.get("base_url"))
    print("model    : %s" % llm.get("model"))
    key = llm.get("api_key") or ""
    print("api_key  : %s" % (key[:6] + "..." + key[-4:] if len(key) > 12 else "(未设置)"))
    print("")

    prompt = sys.argv[1] if len(sys.argv) > 1 else "用一句话打个招呼"
    r = chat([
        {"role": "system", "content": cfg.get("agent", {}).get("persona", "")},
        {"role": "user", "content": prompt},
    ], cfg)
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
