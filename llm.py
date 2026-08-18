"""
LLM PROVIDER LAYER — routes chat calls to Ollama (local, default) or Groq
(cloud, fast mode). Both providers support native function calling, so the
ReAct loop in orchestrator.py works unchanged with either.

To enable fast mode set two environment variables:

    setx LLM_PROVIDER "groq"
    setx GROQ_API_KEY "gsk-..."

and restart the terminal. Without LLM_PROVIDER=groq (or without a key) the
system falls back to Ollama automatically.
"""

import json
import os
import re
import sys
import time
from typing import Any

import ollama
import requests

# Windows consoles default to cp1252, which cannot encode characters Groq
# models emit (e.g. U+2011 non-breaking hyphen). Reconfigure std streams to
# UTF-8 with a safe fallback so print() never crashes the pipeline. When the
# stream doesn't support reconfigure (tests/IDE), wrap prints instead.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def _safe(text: str) -> str:
    """Return a version of text that can be printed on any console encoding."""
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, AttributeError):
        return text.encode("utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace")

GROQ_MODEL: str = "openai/gpt-oss-120b"
GROQ_URL: str = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MIN_INTERVAL: float = 20.0
GROQ_BACKOFF_SECONDS: float = 300.0
GROQ_BACKOFF_MAX: float = 180.0
GROQ_LONG_RESET_SECONDS: float = 15.0
GROQ_DAILY_LIMIT_TOKENS: int = 100_000

OLLAMA_OPTIONS: dict[str, Any] = {"num_ctx": 8192, "cache_prompt": True}

_last_groq_ts: float = 0.0
_groq_blocked_until: float = 0.0


def _getenv(name: str) -> str:
    """Read an env var, falling back to the Windows user registry so the
    Groq settings work even in a terminal that was opened before setx ran."""
    val = os.environ.get(name, "")
    if val.strip():
        return val.strip()
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, _ = winreg.QueryValueEx(k, name)
            return str(value).strip()
    except (OSError, FileNotFoundError, ImportError):
        return ""


def provider() -> str:
    """Active provider: 'groq' if configured with a key, else 'ollama'."""
    if _getenv("LLM_PROVIDER").lower() == "groq":
        if _getenv("GROQ_API_KEY"):
            return "groq"
        raise RuntimeError(
            "LLM_PROVIDER=groq is set but GROQ_API_KEY is missing. "
            "Set GROQ_API_KEY (e.g. setx GROQ_API_KEY \"gsk-...\") or remove "
            "LLM_PROVIDER to use local Ollama."
        )
    return "ollama"


def _normalize_tool_calls(groq_message: dict) -> list[dict] | None:
    """Convert Groq/OpenAI-style tool_calls to our unified shape.
    Arguments are stored as a dict for the tool layer, plus the RAW JSON
    string so we can replay byte-identical arguments back to Groq (it
    insists the echoed assistant tool_calls match the original exactly)."""
    tcs = groq_message.get("tool_calls")
    if not tcs:
        return None
    out: list[dict] = []
    for tc in tcs:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {}
        else:
            args = args_raw
            args_raw = json.dumps(args_raw)
        out.append({
            "id": tc.get("id") or f"call_{len(out)}",
            "function": {"name": fn.get("name", ""), "arguments": args,
                         "arguments_raw": args_raw},
        })
    return out


def _to_groq_messages(messages: list[dict]) -> list[dict]:
    """Convert our unified message history into Groq/OpenAI format.

    Ollama-style tool result messages {"role":"tool", "content", "name"}
    become {"role":"tool", "tool_call_id", "content"} using the assistant
    tool-call id that triggered them."""
    out: list[dict] = []
    pending_tc_ids: list[str] = []
    used_ids: set[str] = set()
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            gm: dict = {"role": "assistant", "content": m.get("content") or ""}
            tcs = m.get("tool_calls")
            if tcs:
                groq_tcs = []
                fresh_ids: list[str] = []
                for i, tc in enumerate(tcs):
                    raw = tc["function"].get("arguments_raw")
                    if raw is None:
                        raw = json.dumps(tc["function"]["arguments"])
                    tid = tc.get("id") or f"call_{i}"
                    fresh_ids.append(tid)
                    groq_tcs.append({
                        "id": tid,
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": raw,
                        },
                    })
                gm["tool_calls"] = groq_tcs
                pending_tc_ids, used_ids = fresh_ids, set()
            out.append(gm)
        elif role == "tool":
            tid = m.get("tool_call_id")
            if not tid and pending_tc_ids:
                tid = next((x for x in pending_tc_ids if x not in used_ids), None)
                if tid:
                    used_ids.add(tid)
            tm = {"role": "tool", "content": m.get("content") or ""}
            tm["tool_call_id"] = tid or f"call_{len(out)}"
            out.append(tm)
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def _to_ollama_messages(messages: list[dict]) -> list[dict]:
    """Strip tool_call_id before sending to Ollama (it pairs tool results
    by name, not by id)."""
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "tool":
            out.append({"role": "tool", "content": m.get("content") or "",
                        "name": m.get("name", "")})
        else:
            out.append(m)
    return out


def _body_error_msg(r: requests.Response) -> str:
    if not getattr(r, "text", None):
        return ""
    try:
        body = r.json()
    except (ValueError, AttributeError):
        return ""
    return (body.get("error") or {}).get("message", "") or ""


def _retry_after(r: requests.Response) -> float:
    """Seconds to wait after a 429, honoring the real wait in the error body.
    The minute-level headers (e.g. 'x-ratelimit-reset-tokens: 1ms') can say
    'ready now' while the ACTUAL blocker is a daily quota that Groq reports
    only in the body message ('Please try again in 8m28.896s.').
    Body text wins, then Retry-After, then reset headers, then a default."""
    msg = _body_error_msg(r)
    if msg:
        parsed = _parse_body_retry(msg)
        if parsed is not None:
            return min(parsed, GROQ_BACKOFF_MAX)
    ra = r.headers.get("Retry-After")
    if ra:
        try:
            return min(float(ra), 30.0)
        except ValueError:
            pass
    reset = (r.headers.get("x-ratelimit-reset-tokens")
             or r.headers.get("x-ratelimit-reset-requests"))
    if reset:
        parsed = _parse_reset_seconds(reset)
        if parsed is not None:
            return min(parsed, 30.0)
    return 10.0


def _parse_body_retry(msg: str) -> float | None:
    """Parse Groq's 429 body like 'Please try again in 8m28.896s.' into
    seconds. Handles 'Xm Ys', '8m28.896s', 'in 30s'."""
    m = re.search(r"in\s+(?:(\d+)\s*m(?:in)?(?:utes)?)?\s*(?:and\s+)?(\d+(?:\.\d+)?)\s*s", msg)
    if m:
        minutes = float(m.group(1) or 0)
        seconds = float(m.group(2))
        return minutes * 60 + seconds
    m = re.search(r"in\s+(\d+)\s*m(?:in)?(?:utes)?", msg)
    if m:
        return float(m.group(1)) * 60
    return None


def _parse_reset_seconds(value: str) -> float | None:
    """Parse a Groq reset header like '1m30s', '42s', '1m', or '185ms' into
    seconds. NOTE: Groq sends milliseconds as '185ms' — that is 0.185s,
    NOT 185 minutes. Must be handled before the 'm' branch."""
    v = value.strip().lower()
    if v.endswith("ms"):
        try:
            return float(v[:-2]) / 1000.0
        except ValueError:
            return None
    if "m" in v:
        mins_part = v.split("m")[0]
        rest = v.split("m", 1)[1].replace("s", "").strip()
        try:
            return float(mins_part) * 60 + (float(rest) if rest else 0.0)
        except ValueError:
            return None
    if v.endswith("s"):
        try:
            return float(v[:-1])
        except ValueError:
            return None
    try:
        return float(v)
    except ValueError:
        return None


def _reset_seconds(r: requests.Response) -> float:
    """Uncapped reset duration Groq reported (seconds). Falls back to
    60s when the header is missing so the backoff latch still behaves."""
    msg = _body_error_msg(r)
    if msg:
        parsed = _parse_body_retry(msg)
        if parsed is not None:
            return parsed
    ra = r.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    reset = (r.headers.get("x-ratelimit-reset-tokens")
             or r.headers.get("x-ratelimit-reset-requests"))
    if reset:
        parsed = _parse_reset_seconds(reset)
        if parsed is not None:
            return parsed
    return 60.0


def _groq_chat(messages: list[dict], tools: list[dict] | None,
               model: str = "") -> dict:
    global _last_groq_ts, _groq_blocked_until
    if time.time() < _groq_blocked_until:
        print(f"  [llm] Groq rate-limited recently — using Ollama "
              f"for {_groq_blocked_until - time.time():.0f}s more",
              flush=True)
        return _ollama_chat(messages, tools, model or "qwen2.5:7b-instruct")
    elapsed = time.time() - _last_groq_ts
    if _last_groq_ts and elapsed < GROQ_MIN_INTERVAL:
        wait = GROQ_MIN_INTERVAL - elapsed
        print(f"  [llm] spacing Groq calls — waiting {wait:.0f}s "
              f"({GROQ_MIN_INTERVAL:.0f}s minimum)", flush=True)
        time.sleep(wait)
    _last_groq_ts = time.time()
    payload: dict = {
        "model": GROQ_MODEL,
        "messages": _to_groq_messages(messages),
    }
    if tools:
        payload["tools"] = tools
    headers = {
        "Authorization": f"Bearer {_getenv('GROQ_API_KEY')}",
        "Content-Type": "application/json",
    }
    last_err: Exception | None = None
    resp_json: dict | None = None
    last_429: requests.Response | None = None
    for attempt in range(4):  # retry transient errors / empty bodies / 429
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)
            if r.status_code == 429:
                last_429 = r
                wait = _retry_after(r)
                last_err = requests.exceptions.HTTPError(
                    "429 Too Many Requests", response=r)
                if attempt >= 1 and wait >= GROQ_LONG_RESET_SECONDS:
                    # Second 429 with a genuinely long reset = quota exhausted
                    # for this window. Don't burn another 30s+ — fall back.
                    break
                print(f"  [llm] rate-limited by Groq — waiting {wait:.0f}s "
                      f"(attempt {attempt+1})", flush=True)
                time.sleep(wait)
                continue
            if r.status_code == 200:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                if body.get("choices"):
                    resp_json = body
                    break
                last_err = RuntimeError(f"Groq returned no result: {r.text[:200]}")
            else:
                r.raise_for_status()
        except Exception as e:
            last_err = e
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None \
               and 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                break  # deterministic client error — no point retrying
        if attempt < 2:
            time.sleep(2 * (attempt + 1))

    if resp_json is None:
        reset = (_reset_seconds(last_429) + 20.0
                 if last_429 is not None else 60.0)
        cooldown = min(max(reset, 30.0), GROQ_BACKOFF_MAX)
        print(f"  [llm] Groq unavailable (quota exhausted/error) — "
              f"falling back to Ollama (model='{model or GROQ_MODEL}'); "
              f"Groq cooling down for {cooldown:.0f}s",
              flush=True)
        try:
            result = _ollama_chat(messages, tools, model or "qwen2.5:7b-instruct")
        except Exception as oe:
            raise RuntimeError(
                f"Groq request failed: {last_err}; Ollama fallback also "
                f"failed: {oe}") from oe
        _groq_blocked_until = time.time() + cooldown
        return result

    gm = resp_json["choices"][0]["message"]
    norm: dict = {"role": "assistant", "content": gm.get("content") or ""}
    tcs = _normalize_tool_calls(gm)
    if tcs:
        norm["tool_calls"] = tcs
    return {"message": norm}


def _ollama_chat(messages: list[dict], tools: list[dict] | None,
                 model: str) -> dict:
    resp = ollama.chat(model=model, messages=_to_ollama_messages(messages),
                       tools=tools, options=OLLAMA_OPTIONS)
    m = resp["message"]
    norm: dict = {"role": "assistant", "content": m.get("content") or ""}
    tcs = m.get("tool_calls")
    if tcs:
        norm["tool_calls"] = _normalize_tool_calls(
            {"tool_calls": [tc.model_dump() for tc in tcs]})
    return {"message": norm}


def chat(messages: list[dict], tools: list[dict] | None = None,
         model: str = "") -> dict:
    """Unified chat call. Returns {"message": {"role", "content",
    "tool_calls"}} in a provider-agnostic shape."""
    if provider() == "groq":
        return _groq_chat(messages, tools, model)
    return _ollama_chat(messages, tools, model)