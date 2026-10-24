"""Provider routing: NVIDIA NIM, OpenRouter and local Ollama (all OpenAI-compatible).

Ordered fallback, credential pools, per-key cooldowns, retries with backoff, tolerant tool-call
parsing, task-based model choice (light / vision), private (local-only) mode and usage callbacks.
Standard library only. Keys are read from the environment, never stored.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field


class ProviderError(Exception):
    def __init__(self, msg, status=None, retryable=False, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class AllProvidersFailed(Exception):
    pass


def http_post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"error": {"message": raw[:500]}}
        return e.code, data, dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"network: {e}", retryable=True)


def http_stream(url, headers, body, timeout=120):
    """POST and return (status, iterator of text lines, headers). Non-200 bodies come back as one line."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, iter([e.read().decode(errors="replace")]), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"network: {e}", retryable=True)

    def gen():
        try:
            for raw in r:
                yield raw.decode("utf-8", "replace")
        except (OSError, TimeoutError) as e:
            raise ProviderError(f"stream interrupted: {e}", retryable=True)
        finally:
            r.close()
    return r.status, gen(), dict(r.headers)


def http_get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), {}
    except urllib.error.HTTPError as e:
        return e.code, {"error": {"message": e.read().decode(errors="replace")[:300]}}, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"network: {e}", retryable=True)


@dataclass
class Provider:
    name: str
    base_url: str
    key_env: str
    model: str
    headers: dict = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)
    local: bool = False
    requires_key: bool = True
    light_model: str | None = None  # cheaper model for summaries, planning, verification
    vision_model: str | None = None

    def __post_init__(self):
        self._cool: dict[str, float] = {}
        self._rr = itertools.count()
        self._lock = threading.Lock()

    def keys(self) -> list[str]:
        raw = os.environ.get(self.key_env, "")
        found = [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
        return found or ([] if self.requires_key else ["local"])

    def has_keys(self) -> bool:
        return bool(self.keys())

    def pick_key(self):
        with self._lock:
            now = time.time()
            live = [k for k in self.keys() if self._cool.get(k, 0) <= now]
            return live[next(self._rr) % len(live)] if live else None

    def cool(self, key, seconds):
        with self._lock:
            self._cool[key] = time.time() + seconds

    def model_for(self, task):
        if task == "light":
            return self.light_model or self.model
        if task == "vision":
            return self.vision_model  # None -> provider skipped
        return self.model


@dataclass
class Response:
    content: str
    tool_calls: list
    message: dict
    usage: dict
    provider: str
    model: str
    local: bool = False


def default_providers(cfg: dict | None = None) -> dict[str, Provider]:
    cfg = cfg or {}
    nv, orr, ol = cfg.get("nvidia", {}), cfg.get("openrouter", {}), cfg.get("ollama", {})
    return {
        "nvidia": Provider(
            "nvidia", nv.get("base_url", "https://integrate.api.nvidia.com/v1"), "NVIDIA_API_KEY",
            os.environ.get("COURIER_NVIDIA_MODEL") or nv.get("model", "meta/llama-3.3-70b-instruct"),
            light_model=nv.get("light_model"), vision_model=nv.get("vision_model")),
        "openrouter": Provider(
            "openrouter", orr.get("base_url", "https://openrouter.ai/api/v1"), "OPENROUTER_API_KEY",
            os.environ.get("COURIER_OPENROUTER_MODEL") or orr.get("model", "meta-llama/llama-3.3-70b-instruct:free"),
            headers={"HTTP-Referer": "https://localhost/courier", "X-Title": "Courier Agent"},
            extra_body=orr.get("extra_body", {}), light_model=orr.get("light_model"),
            vision_model=orr.get("vision_model")),
        "ollama": Provider(
            "ollama", ol.get("base_url", "http://localhost:11434/v1"), "OLLAMA_API_KEY",
            os.environ.get("COURIER_OLLAMA_MODEL") or ol.get("model", "qwen2.5:7b-instruct"),
            local=True, requires_key=False, light_model=ol.get("light_model"), vision_model=ol.get("vision_model")),
    }


class Router:
    def __init__(self, providers: list[Provider], transport=http_post, sleep=time.sleep,
                 retries=2, timeout=120, all_providers: dict | None = None, on_usage=None,
                 stream_transport=http_stream, cache=None):
        self.providers = providers
        self.all = all_providers or {p.name: p for p in providers}
        self.transport = transport
        self.sleep = sleep
        self.retries = retries
        self.timeout = timeout
        self.on_usage = on_usage
        self.stream_transport = stream_transport
        self.cache = cache
        self.last_used: tuple[str, str] | None = None

    @classmethod
    def from_config(cls, cfg: dict, **kw):
        provs = default_providers(cfg)
        order = os.environ.get("COURIER_ROUTE")
        order = [s.strip() for s in order.split(",")] if order else cfg.get("route", ["nvidia", "openrouter"])
        return cls([provs[n] for n in order if n in provs], all_providers=provs, **kw)

    # -- public ---------------------------------------------------------
    def chat(self, messages, tools=None, temperature=0.3, max_tokens=None, task=None, private=False,
             on_token=None) -> Response:
        """task: None | 'light' | 'vision'. private=True uses local providers only (nothing leaves the PC).
        on_token: callback for streamed text. The cache only serves tool-free, non-streaming calls."""
        ckey = None
        if self.cache and not tools and not on_token and task != "vision":
            ckey = self.cache.key(messages, task)
            hit = self.cache.get(ckey)
            if hit:
                return Response(hit[0], [], {"role": "assistant", "content": hit[0]}, {}, "cache", hit[1], True)
        pool = [p for p in self.all.values() if p.local] if private else self.providers
        if private and not pool:
            raise AllProvidersFailed("private mode needs a local provider (Ollama)")
        errors = []
        for p in pool:
            model = p.model_for(task)
            if model is None:
                errors.append(f"{p.name}: no {task} model configured")
                continue
            if not p.has_keys():
                errors.append(f"{p.name}: no key (set {p.key_env})")
                continue
            try:
                resp = self._try(p, model, messages, tools, temperature, max_tokens, on_token)
                self.last_used = (resp.provider, resp.model)
                if ckey and not resp.tool_calls:
                    self.cache.put(ckey, resp.content, resp.model)
                return resp
            except ProviderError as e:
                errors.append(f"{p.name}: {e}")
        raise AllProvidersFailed("; ".join(errors) or "no providers configured")

    # -- internals ------------------------------------------------------
    def _emit(self, p, model, usage, t0, ok, status):
        if self.on_usage:
            try:
                self.on_usage({"provider": p.name, "model": model, "local": p.local, "usage": usage,
                               "latency": time.time() - t0, "ok": ok, "status": status})
            except Exception:
                pass  # accounting must never break a chat

    def _send(self, url, headers, body, on_token):
        if not on_token:
            return self.transport(url, headers, body, self.timeout)
        status, lines, hdrs = self.stream_transport(url, headers, {**body, "stream": True}, self.timeout)
        if status != 200:
            raw = "".join(lines)
            try:
                return status, json.loads(raw), hdrs
            except ValueError:
                return status, {"error": {"message": raw[:300]}}, hdrs
        return 200, self._consume(lines, on_token, body["model"]), hdrs

    @staticmethod
    def _consume(lines, on_token, model):
        """Assemble OpenAI-style server-sent events (text and tool-call fragments) into one normal response."""
        text, calls, usage, mdl = [], {}, {}, model
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ch = json.loads(payload)
            except ValueError:
                continue
            usage = ch.get("usage") or usage
            mdl = ch.get("model") or mdl
            for choice in ch.get("choices") or []:
                d = choice.get("delta") or {}
                if d.get("content"):
                    text.append(d["content"])
                    on_token(d["content"])
                for tc in d.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                    c["id"] = tc.get("id") or c["id"]
                    fn = tc.get("function") or {}
                    c["name"] += fn.get("name") or ""
                    c["args"] += fn.get("arguments") or ""
        msg = {"role": "assistant", "content": "".join(text)}
        if calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                 for _, c in sorted(calls.items())]
        if not text and not calls:
            return {"error": {"message": "empty stream", "code": 502}}
        return {"choices": [{"message": msg}], "usage": usage, "model": mdl}

    def _try(self, p, model, messages, tools, temperature, max_tokens, on_token=None):
        body = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(p.extra_body)
        last = None
        for attempt in range(self.retries + 1):
            key = p.pick_key()
            if key is None:
                raise ProviderError("all keys cooling down", retryable=True)
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **p.headers}
            t0 = time.time()
            try:
                status, data, hdrs = self._send(p.base_url.rstrip("/") + "/chat/completions", headers, body, on_token)
            except ProviderError as e:
                last = e
                self._emit(p, model, {}, t0, False, "network")
                self.sleep(min(2 ** attempt, 8))
                continue
            err = data.get("error") if isinstance(data, dict) else None
            if status == 200 and data.get("choices"):
                self._emit(p, model, data.get("usage", {}), t0, True, "200")
                return self._normalize(p, data, model)
            msg = (err or {}).get("message", str(data)[:200]) if isinstance(err, dict) else str(err or data)[:200]
            code = status if status != 200 else (err or {}).get("code", 502) if isinstance(err, dict) else 502
            code = code if isinstance(code, int) else 502
            self._emit(p, model, {}, t0, False, str(code))
            if code == 429:
                ra = _retry_after(hdrs)
                p.cool(key, ra or 30)
                last = ProviderError(f"429 rate limited: {msg}", 429, True, ra)
                self.sleep(0 if len(p.keys()) > 1 else min(2 ** attempt, 4))
                continue
            if code in (401, 402, 403):
                p.cool(key, 3600)
                last = ProviderError(f"{code} auth/credit: {msg}", code)
                continue
            if code >= 500:
                last = ProviderError(f"{code} server: {msg}", code, True)
                self.sleep(min(2 ** attempt, 8))
                continue
            raise ProviderError(f"{code}: {msg}", code)  # 400/404/422: wrong model/params -> next provider
        raise last or ProviderError("failed")

    @staticmethod
    def _normalize(p, data, model) -> Response:
        m = data["choices"][0].get("message") or {}
        content = m.get("content") or ""
        calls, wire = [], []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                args = {"_parse_error": raw}
            cid = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            calls.append({"id": cid, "name": fn.get("name", ""), "arguments": args})
            wire.append({"id": cid, "type": "function",
                         "function": {"name": fn.get("name", ""),
                                      "arguments": raw if isinstance(raw, str) else json.dumps(raw)}})
        msg = {"role": "assistant", "content": content}
        if wire:
            msg["tool_calls"] = wire
        return Response(content, calls, msg, data.get("usage", {}), p.name, data.get("model", model), p.local)


def _retry_after(hdrs):
    for k, v in (hdrs or {}).items():
        if k.lower() == "retry-after":
            try:
                return min(float(v), 300)
            except ValueError:
                return None
    return None


def list_models(provider: Provider, transport=http_get):
    key = (provider.keys() or [""])[0]
    status, data, _ = transport(provider.base_url.rstrip("/") + "/models",
                                {"Authorization": f"Bearer {key}", **provider.headers})
    if status != 200:
        raise ProviderError(f"{status}: {data}", status)
    return [m.get("id") for m in data.get("data", [])]
