"""LM Studio client + CLI wrappers. Python 3 stdlib only."""

import json
import subprocess
import time
import urllib.error
import urllib.request

BASE = "http://172.24.144.1:1234"
LMS = "/mnt/c/Users/Obhi/.lmstudio/bin/lms.exe"


class ChatError(Exception):
    def __init__(self, status, body, url):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"chat HTTP {status} {url}: {str(body)[:300]}")


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def list_llms(base=BASE):
    _, body = _get(base + "/api/v1/models")
    data = json.loads(body.decode("utf-8", "replace"))
    out = []
    for m in data.get("models", []):
        if m.get("type") == "llm":
            out.append({
                "key": m["key"],
                "display": m.get("display_name"),
                "ctx": m.get("max_context_length"),
            })
    return out


def probe_endpoint(base=BASE, model="probe"):
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 1, "temperature": 0.0, "stream": False}
    req = urllib.request.Request(
        base + "/api/v0/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return "v0"
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        if e.code == 404:
            return "v1"
        return "v0"
    except Exception:
        return "v1"


def chat(base, model, messages, endpoint=None, max_tokens=2048,
         temperature=0.0, stream=True, timeout=600):
    if endpoint is None:
        endpoint = probe_endpoint(base)
    url = base + ("/api/v0/chat/completions" if endpoint == "v0" else "/v1/chat/completions")
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    first_byte_ms = None
    ttft_ms = None
    deltas = []
    content = []
    finish = None
    usage = None
    stats = None
    rid = created = model_ret = None
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ChatError(e.code, body, url)
    except Exception as e:
        raise ChatError(0, str(e), url)

    try:
        ctype = resp.headers.get("Content-Type", "")
        if stream and "text/event-stream" in ctype:
            while True:
                line = resp.readline()
                if not line:
                    break
                if first_byte_ms is None:
                    first_byte_ms = (time.monotonic() - t0) * 1000
                sline = line.decode("utf-8", "replace").strip()
                if not sline.startswith("data:"):
                    continue
                data = sline[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if not rid:
                    rid = obj.get("id")
                    created = obj.get("created")
                    model_ret = obj.get("model")
                ch = obj.get("choices") or []
                if not ch:
                    if obj.get("usage"):
                        usage = obj.get("usage")
                    if obj.get("stats"):
                        stats = obj.get("stats")
                    continue
                c0 = ch[0]
                d = (c0.get("delta") or {}).get("content")
                if d:
                    content.append(d)
                    ms = (time.monotonic() - t0) * 1000
                    if ttft_ms is None:
                        ttft_ms = ms
                    deltas.append(round(ms, 1))
                if c0.get("finish_reason"):
                    finish = c0["finish_reason"]
        else:
            body = resp.read().decode("utf-8", "replace")
            obj = json.loads(body)
            rid = obj.get("id")
            created = obj.get("created")
            model_ret = obj.get("model")
            ch = obj.get("choices") or []
            if ch:
                content = [(ch[0].get("message") or {}).get("content") or ""]
                finish = ch[0].get("finish_reason")
            usage = obj.get("usage")
            stats = obj.get("stats")
    finally:
        try:
            resp.close()
        except Exception:
            pass

    wall_ms = (time.monotonic() - t0) * 1000
    return {
        "endpoint": endpoint, "id": rid, "created": created, "model": model_ret,
        "content_raw": "".join(content), "finish_reason": finish,
        "usage": usage, "stats": stats,
        "first_byte_ms": round(first_byte_ms, 1) if first_byte_ms else None,
        "ttft_ms": round(ttft_ms, 1) if ttft_ms else None,
        "wall_ms": round(wall_ms, 1), "deltas": deltas, "ok": True,
    }


def _lms(*args, timeout=180):
    try:
        p = subprocess.run([LMS, *[str(a) for a in args]],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as e:
        return -1, "", str(e)


def lms_load(key, parallel=1):
    return _lms("load", key, "--parallel", parallel, "-y")


def lms_unload(key):
    return _lms("unload", key)


def lms_unload_all():
    return _lms("unload", "--all")


def lms_ps():
    return _lms("ps", "--json")


def lms_server_status():
    return _lms("server", "status")


def lms_server_start():
    return _lms("server", "start")


def _server_up(base=BASE, tries=4, wait=6):
    for _ in range(tries):
        try:
            _get(base + "/v1/models", timeout=8)
            return True
        except Exception:
            lms_server_start()
            time.sleep(wait)
    return False