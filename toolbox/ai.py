# -*- coding: utf-8 -*-
"""DeepSeek function calling 封装：API Key 安全保存 + 工具调用循环。

本模块不直接操作设备：所有真实动作都由上层传入的 executor 执行。
API Key 用 Windows DPAPI 加密后存到 data/secrets.json，绝不明文落盘。
"""
from __future__ import annotations

import base64
import ctypes
import http.client
import json
import os
import time
import urllib.error
import urllib.request

from .config import DIR_DATA

BASE_URL = "https://api.deepseek.com"
# 现行默认模型；旧模型名仍兼容，模型不存在时会自动按 LEGACY_MODELS 顺序兜底。
MODEL = "deepseek-flash"
LEGACY_MODELS = ["deepseek-v4-flash", "deepseek-chat"]
MODELS = [MODEL] + LEGACY_MODELS
# 兜底成功后记住真正可用的模型名，后续请求直接用它（空串表示用默认 MODEL）。
_active_model: str = ""
SECRETS_FILE = DIR_DATA / "secrets.json"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


# ================================================================== DPAPI
class _DataBlob(ctypes.Structure):
    """Win32 DATA_BLOB；cbData 对应 DWORD，固定 4 字节。"""

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi(protect: bool, data: bytes) -> bytes:
    """调用 CryptProtectData / CryptUnprotectData，失败抛 RuntimeError。"""
    if os.name != "nt":
        raise RuntimeError("DPAPI 只支持 Windows，当前系统无法安全保存 API Key")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise RuntimeError(f"系统没有可用的 DPAPI：{exc}") from None

    blob_ptr = ctypes.POINTER(_DataBlob)
    crypt32.CryptProtectData.argtypes = [
        blob_ptr, ctypes.c_wchar_p, blob_ptr, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, blob_ptr,
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        blob_ptr, ctypes.c_void_p, blob_ptr, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, blob_ptr,
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(data, len(data) + 1)
    in_blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    try:
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(in_blob),
                "Mi6Toolbox DeepSeek API Key",
                None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(out_blob),
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(in_blob),
                None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(out_blob),
            )
        if not ok:
            code = ctypes.get_last_error()
            raise RuntimeError(f"DPAPI {'加密' if protect else '解密'}失败（Windows 错误码 {code}）")
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            try:
                kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
            except Exception:  # noqa: BLE001
                pass


def encrypt(text: str) -> str:
    """明文 → base64(DPAPI blob)；只在 Windows 上可用，失败抛 RuntimeError。"""
    if not isinstance(text, str) or not text:
        raise RuntimeError("要加密的内容不能为空")
    try:
        blob = _dpapi(True, text.encode("utf-8"))
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"DPAPI 加密失败：{type(exc).__name__}: {exc}") from None
    return base64.b64encode(blob).decode("ascii")


def decrypt(blob: str) -> str:
    """base64(DPAPI blob) → 明文；失败抛 RuntimeError。"""
    if not isinstance(blob, str) or not blob:
        raise RuntimeError("解密失败：密文为空或格式不正确")
    try:
        raw = base64.b64decode(blob.encode("ascii"), validate=True)
    except Exception:  # noqa: BLE001
        raise RuntimeError("解密失败：密文不是有效的 Base64") from None
    try:
        plain = _dpapi(False, raw)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"DPAPI 解密失败：{type(exc).__name__}: {exc}") from None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("解密失败：解密结果不是有效的 UTF-8 文本") from None


# ================================================================== API Key
def _secrets_read() -> dict:
    """读 secrets.json；文件不存在、损坏或结构不对都当成空配置。"""
    try:
        obj = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def key_save(key: str) -> dict:
    """校验并加密保存 DeepSeek API Key，绝不把明文写到磁盘。"""
    raw = str(key or "").strip()
    if not raw.startswith("sk-") or len(raw) < 20:
        return {"ok": False, "error": "API Key 格式不对：应以 sk- 开头，且长度不少于 20 个字符"}
    try:
        encrypted = encrypt(raw)
        payload = {
            "deepseek_key": encrypted,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SECRETS_FILE.with_name(SECRETS_FILE.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, SECRETS_FILE)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"保存 API Key 失败：{exc}"}


def key_load() -> str:
    """读出并解密 API Key；没有、损坏或不是当前用户加密的都返回空串。"""
    try:
        blob = _secrets_read().get("deepseek_key")
        if not isinstance(blob, str) or not blob:
            return ""
        return decrypt(blob)
    except Exception:  # noqa: BLE001
        return ""


def key_has() -> bool:
    """是否已经保存了可用的 API Key。"""
    return bool(key_load())


def key_mask() -> str:
    """返回 "sk-…ab12" 形式的掩码；没有可用 Key 时返回空串。"""
    key = key_load()
    if not key:
        return ""
    tail = key[-4:] if len(key) >= 4 else key
    return f"sk-…{tail}"


def key_clear() -> dict:
    """删除本机保存的 API Key。"""
    try:
        SECRETS_FILE.unlink(missing_ok=True)
        tmp = SECRETS_FILE.with_name(SECRETS_FILE.name + ".tmp")
        tmp.unlink(missing_ok=True)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"清除 API Key 失败：{exc}"}


# ================================================================== HTTP
def _http_error_text(code: int) -> str:
    """把常见 HTTP 状态码翻译成用户能看懂的中文错误。"""
    fixed = {
        400: "请求参数不对，DeepSeek 拒绝了请求",
        401: "API Key 不对或已失效",
        402: "账户余额不足",
        403: "这个 API Key 没有访问权限",
        404: "API 地址不对（HTTP 404），检查 base_url",
        422: "DeepSeek 不接受这些请求参数，检查模型名和消息格式",
        429: "请求太频繁，稍后再试",
    }
    if code in fixed:
        return fixed[code]
    if code >= 500:
        return f"DeepSeek 服务暂时出错（HTTP {code}），稍后再试"
    return f"请求 DeepSeek 失败（HTTP {code}）"


class _NetError(Exception):
    """网络层错误（连不上、超时等），只在模块内部使用。"""


def _net_error_text(exc: BaseException) -> str:
    """把网络异常翻译成中文提示。"""
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "连接 DeepSeek 超时，请求没有完成"
    text = str(reason or exc).strip()
    if text:
        return f"连不上 api.deepseek.com，检查网络（{text[:120]}）"
    return "连不上 api.deepseek.com，检查网络"


def _error_summary(err) -> str:
    """把 API 的 error 字段收敛成一句可读描述。"""
    if isinstance(err, dict):
        err = err.get("message") or err.get("type") or "未知错误"
    text = str(err).strip()
    return text[:300] if text else "未知错误"


def _as_text(value) -> str:
    """把可能是 None / 字符串 / 结构体的字段统一成字符串。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return _json_text(value)


def _http_error_detail(raw: bytes) -> str:
    """从错误响应体里尽量取出可读原因（截断），取不到返回空串。"""
    try:
        text = raw.decode("utf-8", "replace").lstrip("\ufeff").strip()
    except Exception:  # noqa: BLE001
        return ""
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        return text[:300]
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            reason = err.get("message") or err.get("type") or ""
            return str(reason).strip()[:300]
        if isinstance(err, str):
            return err.strip()[:300]
    return ""


def _http_error_message(code: int, raw: bytes) -> str:
    """HTTP 状态码 + 响应体里的原因，拼成中文错误。"""
    msg = _http_error_text(code)
    detail = _http_error_detail(raw)
    if detail and detail not in msg:
        msg = f"{msg}（{detail}）"
    return msg


_MODEL_ERROR_HINTS = (
    "not found", "not_found", "does not exist", "invalid", "unknown",
    "unsupported", "模型不存在", "模型无效", "未知模型", "不支持的模型",
)


def _model_rejected(status: int, raw: bytes) -> bool:
    """判断 400/404 是否属于"模型名不存在"这类错误。

    只有响应里同时出现 model（或"模型"）和 not found/invalid 之类的字样才重试；
    涉及 reasoning/thinking 的参数错误不重试，避免把真正的请求错误当成模型名问题。
    """
    if status not in (400, 404):
        return False
    text = raw.decode("utf-8", "replace").lower()
    if "model" not in text and "模型" not in text:
        return False
    if "reasoning" in text or "thinking" in text:
        return False
    return any(hint in text for hint in _MODEL_ERROR_HINTS)


def _model_candidates(model: str | None) -> list[str]:
    """按优先级列出要尝试的模型：显式/记住的 → 默认 → 兼容旧名。"""
    first = str(model or _active_model or MODEL).strip() or MODEL
    out = [first]
    for name in MODELS:
        if name not in out:
            out.append(name)
    return out


def _remember_model(model: str) -> None:
    """记住兜底成功的模型名，后续请求直接用它。"""
    global _active_model
    if model:
        _active_model = str(model)


def _normalize_effort(effort) -> str:
    """reasoning_effort 只接受 low/high/max，其它值一律当 high。"""
    value = str(effort or "").strip().lower()
    return value if value in ("low", "high", "max") else "high"


def _prepare_messages(messages: list, with_tools: bool) -> list:
    """复制一份用于请求的 messages；带 tools 时补齐 assistant 的 reasoning_content。

    思考模式下只要请求带 tools，历史里每一轮 assistant 都必须原样带回
    reasoning_content（没有发生工具调用的轮次也要带，空串也要带），否则 API 返回 400。
    这里只补空串、不覆盖已有的真实思考内容，也不修改调用方原始列表。
    """
    prepared = []
    for item in messages:
        if not isinstance(item, dict):
            prepared.append(item)
            continue
        copy = dict(item)
        if with_tools and copy.get("role") == "assistant":
            reasoning = copy.get("reasoning_content")
            copy["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
        prepared.append(copy)
    return prepared


def _build_payload(messages: list, tools: list | None, *, model: str, thinking: bool,
                   reasoning_effort: str, temperature: float | None,
                   stream: bool) -> dict:
    """构造请求体：思考模式不带 temperature，关闭思考时才带。"""
    payload = {
        "model": model,
        "messages": _prepare_messages(messages, bool(tools)),
    }
    if thinking:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = _normalize_effort(reasoning_effort)
    else:
        payload["thinking"] = {"type": "disabled"}
        if temperature is not None:
            payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
    return payload


def _request_open(url: str, api_key: str, body: bytes, timeout: int,
                  accept: str) -> tuple[object | None, tuple[int, bytes] | None]:
    """POST 一次，返回 (响应对象, None) 或 (None, (状态码, 响应体))。

    网络层错误抛 _NetError。HTTP 错误时响应体已经读完，响应对象已关闭。
    """
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": accept,
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp, None
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        try:
            exc.close()
        except Exception:  # noqa: BLE001
            pass
        return None, (int(exc.code or 0), raw)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _NetError(_net_error_text(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise _NetError(f"请求 DeepSeek 时出错：{type(exc).__name__}: {exc}") from None


# ---------------------------------------------------------------- 回调记录
_CALLBACK_ERRORS: list[dict] = []
_CALLBACK_ERRORS_LIMIT = 20


def _record_callback_error(kind: str, exc: BaseException) -> None:
    """记录 on_delta / on_event 回调里抛出的异常，最多保留 20 条。

    回调是上层 UI 代码，出错不能影响 AI 主流程，所以这里只记录不抛出。
    """
    try:
        _CALLBACK_ERRORS.append({
            "time": time.time(),
            "kind": kind,
            "error": f"{type(exc).__name__}: {exc}",
        })
        del _CALLBACK_ERRORS[:-_CALLBACK_ERRORS_LIMIT]
    except Exception:  # noqa: BLE001
        pass


def _parse_chat_response(raw: bytes, status: int, model: str) -> dict:
    """解析非流式响应；成功时在顶层补 reasoning_content。"""
    try:
        text = raw.decode("utf-8", "replace").lstrip("\ufeff")
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return {"error": "DeepSeek 返回的内容不是合法 JSON，可能服务异常", "status": status}
    if not isinstance(data, dict):
        return {"error": "DeepSeek 返回的数据格式不对（不是 JSON 对象）", "status": status}
    if data.get("error"):
        return {"error": f"DeepSeek 返回错误：{_error_summary(data.get('error'))}", "status": status}
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return {"error": "DeepSeek 返回的数据缺少 choices，可能服务异常", "status": status}
    reasoning = ""
    first = choices[0]
    if isinstance(first, dict):
        message = first.get("message")
        if isinstance(message, dict):
            reasoning = _as_text(message.get("reasoning_content"))
    data["reasoning_content"] = reasoning
    data.setdefault("model", model)
    return data


def chat(messages: list, tools: list | None = None, *, key: str | None = None,
         base_url: str = BASE_URL, model: str | None = None, thinking: bool = True,
         reasoning_effort: str = "high", timeout: int = 180,
         temperature: float = 0.2) -> dict:
    """POST {base_url}/chat/completions，成功返回原始 JSON，失败返回中文 error。

    默认走思考模式：请求体带 thinking.type=enabled 和 reasoning_effort，不发送
    temperature；thinking=False 时发送 thinking.type=disabled，并允许 temperature。
    返回数据顶层额外带 reasoning_content（取不到就是空串），方便上层展示。
    模型名不存在（400/404）时自动按 LEGACY_MODELS 顺序重试，成功后记住可用模型。
    """
    try:
        api_key = str(key if key is not None else key_load()).strip()
        if not api_key:
            return {"error": "还没设置 DeepSeek API Key，请先在 AI 页保存", "status": 0}
        if not isinstance(messages, list):
            return {"error": "messages 必须是列表", "status": 0}
        url = str(base_url or BASE_URL).rstrip("/") + "/chat/completions"
        candidates = _model_candidates(model)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"请求 DeepSeek 时出错：{type(exc).__name__}: {exc}", "status": 0}

    last_error = {"error": "请求 DeepSeek 失败", "status": 0}
    for index, candidate in enumerate(candidates):
        try:
            payload = _build_payload(
                messages, tools, model=candidate, thinking=thinking,
                reasoning_effort=reasoning_effort, temperature=temperature, stream=False,
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            resp, http_error = _request_open(url, api_key, body, timeout, "application/json")
        except _NetError as exc:
            return {"error": str(exc), "status": 0}
        except (TypeError, ValueError) as exc:
            return {"error": f"消息或工具定义无法序列化成 JSON：{exc}", "status": 0}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"请求 DeepSeek 时出错：{type(exc).__name__}: {exc}", "status": 0}

        if resp is None:
            status, raw = http_error or (0, b"")
            last_error = {"error": _http_error_message(status, raw), "status": status}
            if _model_rejected(status, raw) and index + 1 < len(candidates):
                continue
            return last_error
        try:
            with resp:
                status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read()
        except TimeoutError:
            return {"error": f"等待 DeepSeek 回复超时（超过 {timeout} 秒）", "status": 0}
        except (urllib.error.URLError, OSError):
            return {"error": "连不上 api.deepseek.com，检查网络", "status": 0}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"读取 DeepSeek 回复时出错：{type(exc).__name__}: {exc}", "status": 0}

        data = _parse_chat_response(raw, status, candidate)
        if not data.get("error") and index > 0:
            _remember_model(candidate)
        return data
    return last_error


# ---------------------------------------------------------------- 流式 SSE
def _charset_of(resp) -> str:
    """从响应头里取 charset，取不到返回空串。"""
    try:
        ctype = str(resp.headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return ""
    pos = ctype.lower().find("charset=")
    if pos < 0:
        return ""
    value = ctype[pos + 8:].split(";")[0].strip().strip("\"'")
    return value if value else ""


def _decoders(charset: str) -> list[str]:
    """按优先级给出解码方式：响应声明的 charset → UTF-8 → GBK/GB18030。"""
    out = [charset.lower()] if charset else []
    for enc in ("utf-8", "gbk", "gb18030"):
        if enc not in out:
            out.append(enc)
    return out


def _decode_line(raw: bytes, charset: str) -> str:
    """逐行解码，兼容同一路流里 UTF-8 与 GBK 混用。"""
    for enc in _decoders(charset):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _emit_delta(on_delta, kind: str, text: str) -> None:
    """真正按增量回调 on_delta；回调抛异常只记录，不影响流。"""
    if not callable(on_delta) or not text:
        return
    try:
        on_delta(kind, text)
    except Exception as exc:  # noqa: BLE001
        _record_callback_error("delta", exc)


def _stream_call_key(fragment: dict, fallback: int) -> int:
    """取 tool_call 分片的 index；缺失或非法时退回顺序号。"""
    index = fragment.get("index")
    if isinstance(index, bool):
        return fallback
    if isinstance(index, int) and index >= 0:
        return index
    if isinstance(index, str) and index.strip().isdigit():
        return int(index.strip())
    return fallback


def _merge_stream_tool_calls(acc: dict[int, dict], fragments) -> None:
    """把流式 tool_calls 分片按 index 聚合；arguments 是字符串拼接。"""
    if not isinstance(fragments, list):
        return
    fallback = max(acc) + 1 if acc else 0
    for position, fragment in enumerate(fragments):
        if not isinstance(fragment, dict):
            continue
        key = _stream_call_key(fragment, fallback + position)
        slot = acc.get(key)
        if slot is None:
            slot = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            acc[key] = slot
        call_id = fragment.get("id")
        if isinstance(call_id, str) and call_id and not slot["id"]:
            slot["id"] = call_id
        function = fragment.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name and not slot["function"]["name"]:
            slot["function"]["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["function"]["arguments"] += arguments
        elif isinstance(arguments, dict):
            slot["function"]["arguments"] += json.dumps(arguments, ensure_ascii=False)


def _consume_stream(resp, *, status: int, model: str, on_delta, timeout: int,
                    charset: str, should_stop=None) -> dict:
    """读 SSE、逐片聚合，返回和 chat 同构的结果；任何异常都收敛成中文 error。"""
    state = {
        "content": [],
        "reasoning": [],
        "calls": {},
        "usage": {},
        "model": model,
        "got": False,
        "done": False,
        "error": "",
        "stopped": False,
    }
    buf = b""

    def handle_line(raw_line: bytes) -> None:
        line = _decode_line(raw_line, charset).rstrip("\r\n")
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff")
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            return
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload:
            return
        if payload == "[DONE]":
            state["done"] = True
            state["got"] = True
            return
        try:
            obj = json.loads(payload)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(obj, dict):
            return
        state["got"] = True
        err = obj.get("error")
        if err:
            state["error"] = _error_summary(err)
            return
        got_model = obj.get("model")
        if isinstance(got_model, str) and got_model:
            state["model"] = got_model
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage:
            state["usage"] = usage
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            return
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            state["reasoning"].append(reasoning)
            _emit_delta(on_delta, "reasoning", reasoning)
        content = delta.get("content")
        if isinstance(content, str) and content:
            state["content"].append(content)
            _emit_delta(on_delta, "content", content)
        _merge_stream_tool_calls(state["calls"], delta.get("tool_calls"))

    reader = getattr(resp, "read1", None)
    try:
        while not state["done"]:
            if callable(should_stop) and should_stop():  # 用户点了停止：立刻别读了
                state["stopped"] = True
                break
            chunk = reader(4096) if callable(reader) else resp.read(1)
            if not chunk:
                break
            buf += chunk
            while not state["done"]:
                pos = buf.find(b"\n")
                if pos < 0:
                    break
                line, buf = buf[:pos], buf[pos + 1:]
                handle_line(line)
        if not state["done"] and buf.strip():
            handle_line(buf)
    except TimeoutError:
        return {"error": f"读取流式回复超时（超过 {timeout} 秒）", "status": status}
    except http.client.IncompleteRead:
        return {"error": "流式连接中途断开，回复不完整", "status": status}
    except (urllib.error.URLError, OSError) as exc:
        return {"error": f"读取流式回复时连接中断：{exc}", "status": status}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"解析流式回复出错：{type(exc).__name__}: {exc}", "status": status}

    if state["error"]:
        return {"error": f"DeepSeek 返回错误：{state['error']}", "status": status}
    if state["stopped"]:
        return {"error": "已停止生成", "stopped": True, "status": status,
                "message": {"role": "assistant",
                            "content": "".join(state["content"]),
                            "reasoning_content": "".join(state["reasoning"]),
                            "tool_calls": [state["calls"][k] for k in sorted(state["calls"])]}}
    if not state["got"]:
        return {"error": "没有收到流式数据（可能不是 SSE 或连接已中断），检查 API 地址和网络",
                "status": status}
    if not state["done"]:
        return {"error": "流式连接提前中断（没有收到 [DONE]），回复可能不完整", "status": status}
    calls = [state["calls"][key] for key in sorted(state["calls"])]
    return {
        "message": {
            "role": "assistant",
            "content": "".join(state["content"]),
            "reasoning_content": "".join(state["reasoning"]),
            "tool_calls": calls,
        },
        "usage": state["usage"],
        "model": state["model"],
    }


def chat_stream(messages: list, tools: list | None = None, *, key: str | None = None,
                base_url: str = BASE_URL, model: str | None = None,
                thinking: bool = True, reasoning_effort: str = "high",
                timeout: int = 180, on_delta=None, should_stop=None) -> dict:
    """流式调用 DeepSeek，逐片回调 on_delta(kind, text)，返回和 chat 同构的结果。

    kind 是 "reasoning" 或 "content"，text 是本次真正新增的那一小段字符串。
    成功结果是 {"message": {...}, "usage": {...}, "model": ...}；出错返回中文 error。
    """
    try:
        api_key = str(key if key is not None else key_load()).strip()
        if not api_key:
            return {"error": "还没设置 DeepSeek API Key，请先在 AI 页保存", "status": 0}
        if not isinstance(messages, list):
            return {"error": "messages 必须是列表", "status": 0}
        url = str(base_url or BASE_URL).rstrip("/") + "/chat/completions"
        candidates = _model_candidates(model)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"请求 DeepSeek 时出错：{type(exc).__name__}: {exc}", "status": 0}

    last_error = {"error": "请求 DeepSeek 失败", "status": 0}
    for index, candidate in enumerate(candidates):
        try:
            payload = _build_payload(
                messages, tools, model=candidate, thinking=thinking,
                reasoning_effort=reasoning_effort, temperature=None, stream=True,
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            resp, http_error = _request_open(url, api_key, body, timeout, "text/event-stream")
        except _NetError as exc:
            return {"error": str(exc), "status": 0}
        except (TypeError, ValueError) as exc:
            return {"error": f"消息或工具定义无法序列化成 JSON：{exc}", "status": 0}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"请求 DeepSeek 时出错：{type(exc).__name__}: {exc}", "status": 0}

        if resp is None:
            status, raw = http_error or (0, b"")
            last_error = {"error": _http_error_message(status, raw), "status": status}
            if _model_rejected(status, raw) and index + 1 < len(candidates):
                continue
            return last_error
        try:
            with resp:
                status = int(getattr(resp, "status", 200) or 200)
                charset = _charset_of(resp)
                outcome = _consume_stream(
                    resp, status=status, model=candidate, on_delta=on_delta,
                    timeout=timeout, charset=charset, should_stop=should_stop,
                )
        except TimeoutError:
            return {"error": f"连接 DeepSeek 超时（超过 {timeout} 秒）", "status": 0}
        except (urllib.error.URLError, OSError):
            return {"error": "连不上 api.deepseek.com，检查网络", "status": 0}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"读取流式回复时出错：{type(exc).__name__}: {exc}", "status": 0}
        if not outcome.get("error") and index > 0:
            _remember_model(candidate)
        return outcome
    return last_error


# ================================================================== 工具定义
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_device_state",
            "description": (
                "读取当前小米6的设备状态：连接模式（ADB/TWRP/Fastboot/未连接）、型号和代号、"
                "系统版本、BL 解锁状态、root、分区数量、电量、存储。刷机或重启前先调用它确认环境。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_device",
            "description": (
                "等手机以指定模式重新连上，最多等 timeout 秒（默认 90）。"
                "重启到 recovery / TWRP / Fastboot 之后必须先调用它：这期间 ADB 会断开，"
                "不要因为一次 get_device_state 没连上就断定用户拔线了。"
                "expect 可填 any / adb / twrp / fastboot / sideload。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expect": {
                        "type": "string",
                        "description": "期望的模式：any（默认，任意模式都算）、adb、twrp、fastboot、sideload。",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "最多等多少秒，默认 90，最大 300。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_partitions",
            "description": "列出手机上的分区名（by-name）。需要手机处于 ADB 或 TWRP 模式。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_local_packages",
            "description": "列出本机 downloads/（或指定子目录）里可刷的全量包，不解包、不碰手机。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir": {
                        "type": "string",
                        "description": "要列出的本地目录（相对程序根目录），默认 downloads。",
                        "default": "downloads",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_package",
            "description": (
                "查看一个本机全量刷机包里有哪些分区镜像，只读文件、不解包、不碰手机。"
                "path 必须是本机已存在的路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "本机全量包的路径，必须是本机已存在的路径。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_package",
            "description": (
                "把本机全量包解包到电脑的 backups/，可用 parts 只解出指定分区。"
                "只写电脑，不碰手机；path 必须是本机已存在的路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "本机全量包的路径，必须是本机已存在的路径。",
                    },
                    "parts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只解出这些分区，例如 [\"boot\"]；省略表示全部解包。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flash_local_image",
            "description": (
                "【危险】把一个本机 img 用 fastboot 刷进指定分区。会修改手机，需用户确认。"
                "调用前先用 get_device_state 确认已进 Fastboot 且分区正确；"
                "path 必须是本机已存在的路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "part": {
                        "type": "string",
                        "description": "目标分区名，例如 boot、recovery；必须确认分区真实存在。",
                    },
                    "path": {
                        "type": "string",
                        "description": "本机 img 文件路径，必须是本机已存在的路径。",
                    },
                },
                "required": ["part", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flash_package",
            "description": (
                "【危险】把本机全量包解包并整包刷入手机，可用 parts 只刷部分分区。"
                "会修改手机，需用户确认；path 必须是本机已存在的路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "本机全量包的路径，必须是本机已存在的路径。",
                    },
                    "parts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只刷这些分区；省略表示按整包处理。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sideload_package",
            "description": (
                "【危险】在 TWRP/Sideload 模式下用 adb sideload 刷入本机卡刷包。"
                "会修改手机，需用户确认；path 必须是本机已存在的路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "本机卡刷包路径，必须是本机已存在的路径。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_partition",
            "description": (
                "【危险】从手机 dump 一个分区到电脑 backups/ 做备份。会访问手机存储，需用户确认；"
                "分区名要准确，例如 boot、recovery、persist。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "part": {
                        "type": "string",
                        "description": "要 dump 的分区名，例如 boot、recovery、persist。",
                    },
                },
                "required": ["part"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reboot_device",
            "description": (
                "【危险】重启手机到 system/recovery/bootloader/poweroff，会断开当前连接，需用户确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["system", "recovery", "bootloader", "poweroff"],
                        "description": "重启目标，默认 system。",
                        "default": "system",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_apk",
            "description": (
                "【危险】安装本机 apk/ 目录里的一个 APK，会修改手机，需用户确认。"
                "手机需要处于系统模式并开启 USB 调试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "APK 文件名（位于本机 apk/ 目录，只填文件名）；"
                            "该文件必须是本机已存在的路径。"
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adb_shell",
            "description": (
                "【危险】在手机上执行一条 adb shell 命令，需用户确认。只在确有必要时使用；"
                "禁止自行执行删除、格式化、dd 写分区等破坏性命令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "要执行的命令，例如 getprop ro.product.model。",
                    },
                },
                "required": ["cmd"],
            },
        },
    },
]

RISKY_TOOLS: set[str] = {
    "flash_local_image",
    "flash_package",
    "sideload_package",
    "extract_partition",
    "reboot_device",
    "install_apk",
    "adb_shell",
}


def tool_names() -> list[str]:
    """按 TOOLS 的顺序返回工具名。"""
    return [item["function"]["name"] for item in TOOLS]


# ================================================================== 提示词与智能体
def system_prompt(device_ctx: str) -> str:
    """生成中文 system prompt，并把上层提供的设备/环境摘要插进去。"""
    ctx = str(device_ctx or "").strip() or "（上层暂时没有提供设备摘要，先调用 get_device_state）"
    return f"""你是「小米6 刷机工具箱」内置的 AI 助手。你服务的是一台小米6（代号 sagit）。
你的职责是理解用户想做的刷机/备份/排障操作，并在必要时调用工具箱提供的工具真正完成操作。

你可以调用的工具（只有工具执行成功才算完成，绝不能假装执行或编造结果）：
- get_device_state：查看设备模式、型号、系统、BL、root、分区数、电量、存储。
- wait_for_device：等设备以指定模式重新连上（重启后必用，别把重启当成拔线）。
- list_partitions：列出手机分区名。
- list_local_packages：列出本机 downloads/ 里可刷的全量包。
- inspect_package：查看本机全量包里有哪些镜像（不解包）。
- extract_package：只把全量包解到电脑 backups/，不碰手机。
- flash_local_image：fastboot 刷一个本地 img（危险，需用户确认）。
- flash_package：解包并整包刷入（危险，需用户确认）。
- sideload_package：TWRP sideload 刷卡刷包（危险，需用户确认）。
- extract_partition：从手机 dump 分区到电脑（危险，需用户确认）。
- reboot_device：重启到 system/recovery/bootloader/poweroff（危险，需用户确认）。
- install_apk：安装 apk/ 目录里的应用（危险，需用户确认）。
- adb_shell：执行一条 adb shell 命令（危险，需用户确认）。

设备断连与重启（很重要，别误判）：
- 小米6 一重启（reboot recovery / reboot bootloader / 进 TWRP / 进 Fastboot），ADB 会立刻断开 10~60 秒，
  这期间 get_device_state 会显示"未连接"。这是正常的重启过程，**不是用户拔了数据线**。
- 判断"真的没连"的步骤：① 看 get_device_state 里的 recentAction（最近是不是刚重启/刚刷机）；
  ② 调用 wait_for_device 等它自己回来；③ 只有 wait_for_device 超时了，才可以提示用户检查数据线 / USB 调试 /
  手机上有没有停在开机动画或 recovery 主界面。
- 一次 get_device_state 没连上，**不许**直接说"数据线松了/手机拔了/连不上手机"，先等再判断。
- 模式会变，不同操作要求的模式不一样：整包线刷 → Fastboot；sideload 卡刷 → TWRP 的 sideload；
  提取分区、列分区 → TWRP 或 root 的 ADB；装应用 → 正常系统。
- 刷完机通常要 reboot_device(target=system)，再 wait_for_device(expect=adb) 确认能正常开机。
- reboot_device(target=poweroff) 之后设备不会自己回来，要提示用户长按电源键开机，再 wait_for_device。

安全规则（必须遵守）：
1. 优先用工具获取事实，不要靠猜测。设备状态不明时先 get_device_state 探状态；如果它是"未连接"而
   recentAction 显示刚重启过，用 wait_for_device 等，而不是催用户去拔插数据线。
2. 刷任何分区前，先确认设备已进入 Fastboot（或明确支持写入的 TWRP/root 模式），并核对型号、
   解锁状态、分区名和本机文件确实存在。没有把握时先停下来问用户。
3. 绝对不要主动刷 persist、modemst1、modemst2、fsg、misc 等敏感分区；IMEI、基带、传感器
   校准数据一旦损坏很难恢复。用户即使要求，也要先说明风险并确认已备份。
4. 不要为了“试试”执行破坏性 adb shell 命令（rm、format、dd 写分区等）。只执行解决当前问题
   所必需、且用户明确同意的命令。
5. 危险操作会由工具箱交给用户确认。在拿到工具结果之前，不要声称已经刷好或重启完成。
6. 工具返回 ok=false 时，如实解释失败原因并给出下一步；不要编造成功。遇到缺文件、未连接、
   模式不对、权限不足时，先解决前置条件，不要硬刷。
7. 回答用简体中文，简洁、可操作。不确定时只问一个最关键的问题，不要一次抛出一堆猜测。
8. 你的思考过程（reasoning）会实时显示给用户看，所以思考时也尽量用简体中文，别用英文独白。

当前设备/环境摘要（由工具箱提供，可能不完整）：
{ctx}
"""


def _agent_result() -> dict:
    """run_agent 的固定返回骨架。"""
    return {
        "ok": False,
        "reply": "",
        "steps": [],
        "pending": [],
        "error": "",
        "rounds": 0,
        "usage": {},
    }


def _json_text(obj) -> str:
    """把工具结果转成给模型看的 JSON 文本，绝不因为序列化失败而崩。"""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return json.dumps({"ok": False, "error": "工具返回结果无法转成 JSON"}, ensure_ascii=False)


def _tool_message(call_id: str, content: str) -> dict:
    """构造回写给模型的 tool 消息。"""
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _safe_execute(executor, name: str, args: dict) -> dict:
    """调用上层 executor，把异常收敛成错误 dict。"""
    try:
        result = executor(name, args)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"工具 {name} 执行出错：{type(exc).__name__}: {exc}"}
    if isinstance(result, dict):
        return result
    return {"ok": True, "result": result}


def _parse_tool_args(function: dict) -> tuple[dict, str]:
    """解析 tool_call.function.arguments，返回 (参数, 错误信息)。"""
    raw = function.get("arguments")
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, dict):
        return raw, ""
    if not isinstance(raw, str):
        return {}, "工具参数必须是 JSON 字符串对象"
    try:
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return {}, f"工具参数不是合法 JSON：{exc}"
    if not isinstance(parsed, dict):
        return {}, "工具参数 JSON 必须是对象（{...}）"
    return parsed, ""


def _emit(on_event, event: dict) -> None:
    """安全上报事件；on_event 抛异常只记录，绝不影响 agent 主流程。"""
    if not callable(on_event):
        return
    try:
        on_event(event)
    except Exception as exc:  # noqa: BLE001
        _record_callback_error("event", exc)


def _agent_fail(result: dict, error: str, on_event) -> dict:
    """统一设置错误并上报 error 事件。"""
    result["ok"] = False
    result["error"] = error
    _emit(on_event, {"type": "error", "error": error})
    return result


def _agent_finish(result: dict, on_event) -> dict:
    """统一上报 final 事件；pending 只带 name/args，不把内部 id 暴露给界面。"""
    pending = [
        {"name": item.get("name"), "args": item.get("args")}
        for item in result.get("pending") or []
        if isinstance(item, dict)
    ]
    _emit(on_event, {
        "type": "final",
        "text": result.get("reply") or "",
        "steps": list(result.get("steps") or []),
        "pending": pending,
    })
    return result


def _one_line(text: str) -> str:
    """取多行文本里最后一行有内容的部分。"""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _tool_text(res) -> str:
    """把工具结果收敛成一行给界面看的文字。"""
    if not isinstance(res, dict):
        return str(res)[:500]
    if res.get("ok") is False:
        for key in ("error", "log", "note", "out"):
            value = res.get(key)
            if isinstance(value, str) and value.strip():
                return ("失败：" + value.strip())[:500]
        return "失败"
    for key in ("log", "out", "note", "reply", "path", "dir", "result"):
        value = res.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value)[:500]
    return "完成"


def _emit_tool_result(on_event, name: str, res) -> None:
    """上报 tool_result 事件；ok 只认显式 False。"""
    ok = not (isinstance(res, dict) and res.get("ok") is False)
    _emit(on_event, {
        "type": "tool_result",
        "name": name,
        "ok": bool(ok),
        "text": _tool_text(res),
    })


def _delta_emitter(on_event):
    """把 chat_stream 的 on_delta 转成 run_agent_stream 的 reasoning/content 事件。"""
    if not callable(on_event):
        return None

    def on_delta(kind: str, text: str) -> None:
        if kind in ("reasoning", "content"):
            _emit(on_event, {"type": kind, "delta": text})

    return on_delta


_STOPPED_MSG = "已停止生成（你点了停止按钮）"


def _user_stopped(should_stop) -> bool:
    """安全地问一下"用户是不是点了停止"。"""
    try:
        return bool(callable(should_stop) and should_stop())
    except Exception:  # noqa: BLE001
        return False


def _agent_loop(messages: list, executor, *, allow_risky: bool = False,
                max_rounds: int = 8, key: str | None = None,
                thinking: bool = True, reasoning_effort: str = "high",
                on_event=None, streaming: bool = False, should_stop=None) -> dict:
    """run_agent / run_agent_stream 共用的工具调用循环。"""
    result = _agent_result()
    try:
        delta_callback = _delta_emitter(on_event)
        if not isinstance(messages, list):
            return _agent_fail(result, "messages 必须是列表", on_event)
        try:
            limit = int(max_rounds)
        except (TypeError, ValueError):
            return _agent_fail(result, "max_rounds 必须是正整数", on_event)
        if limit < 1:
            return _agent_fail(result, "max_rounds 必须是正整数", on_event)

        for round_no in range(1, limit + 1):
            if _user_stopped(should_stop):
                return _agent_fail(result, _STOPPED_MSG, on_event)
            result["rounds"] = round_no
            _emit(on_event, {"type": "round", "n": round_no})
            if streaming:
                resp = chat_stream(
                    messages, TOOLS, key=key, thinking=thinking,
                    reasoning_effort=reasoning_effort, on_delta=delta_callback,
                    should_stop=should_stop,
                )
                if not isinstance(resp, dict):
                    return _agent_fail(result, "DeepSeek 返回的数据格式不对", on_event)
                if resp.get("stopped"):
                    partial_msg = resp.get("message") or {}
                    partial = partial_msg.get("content") or ""
                    # 把「被打断」也写进上下文，下一轮模型知道上一条没说完
                    messages.append({
                        "role": "assistant",
                        "content": partial or "（用户中断了这次回答）",
                        "reasoning_content": partial_msg.get("reasoning_content") or "",
                    })
                    if partial:
                        result["reply"] = partial
                    return _agent_fail(result, _STOPPED_MSG, on_event)
                if resp.get("error"):
                    return _agent_fail(result, str(resp.get("error")), on_event)
                message = resp.get("message")
                if not isinstance(message, dict):
                    return _agent_fail(
                        result, "DeepSeek 返回的数据格式不对（没有 message）", on_event
                    )
            else:
                resp = chat(messages, TOOLS, key=key)
                if not isinstance(resp, dict):
                    return _agent_fail(result, "DeepSeek 返回的数据格式不对", on_event)
                if resp.get("error"):
                    return _agent_fail(result, str(resp.get("error")), on_event)
                choices = resp.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    return _agent_fail(
                        result, "DeepSeek 返回的数据格式不对（没有有效的 choices）", on_event
                    )
                message = choices[0].get("message")
                if not isinstance(message, dict):
                    return _agent_fail(
                        result, "DeepSeek 返回的数据格式不对（没有 message）", on_event
                    )

            usage = resp.get("usage")
            if isinstance(usage, dict):
                result["usage"] = usage

            reasoning = _as_text(message.get("reasoning_content"))
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                content = _json_text(content)
            content = content or ""
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning,
                })
                result["reply"] = content
                result["ok"] = True
                return _agent_finish(result, on_event)

            if not isinstance(tool_calls, list):
                return _agent_fail(result, "DeepSeek 返回的 tool_calls 格式不对", on_event)
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning,
            })

            for index, call in enumerate(tool_calls):
                if _user_stopped(should_stop):
                    return _agent_fail(result, _STOPPED_MSG, on_event)
                if isinstance(call, dict):
                    call_id = str(call.get("id") or f"call_{round_no}_{index}")
                    function = call.get("function")
                    if not isinstance(function, dict):
                        function = {}
                    name = str(function.get("name") or "")
                else:
                    call_id = f"call_{round_no}_{index}"
                    function = {}
                    name = ""

                if not name:
                    tool_result = {"ok": False, "error": "模型给出的工具调用缺少工具名，已跳过"}
                    messages.append(_tool_message(call_id, _json_text(tool_result)))
                    result["steps"].append({"name": "", "args": {}, "result": tool_result})
                    continue

                args, args_error = _parse_tool_args(function)
                if args_error:
                    _emit(on_event, {"type": "tool_call", "name": name, "args": {}})
                    tool_result = {"ok": False, "error": args_error}
                    messages.append(_tool_message(call_id, _json_text(tool_result)))
                    result["steps"].append({"name": name, "args": {}, "result": tool_result})
                    _emit_tool_result(on_event, name, tool_result)
                    continue

                _emit(on_event, {"type": "tool_call", "name": name, "args": args})
                if name in RISKY_TOOLS and not allow_risky:
                    result["pending"].append({"name": name, "args": args, "id": call_id})
                    continue

                tool_result = _safe_execute(executor, name, args)
                result["steps"].append({"name": name, "args": args, "result": tool_result})
                messages.append(_tool_message(call_id, _json_text(tool_result)))
                _emit_tool_result(on_event, name, tool_result)

            if result["pending"]:
                names = "、".join(item["name"] for item in result["pending"])
                result["ok"] = True
                result["reply"] = f"有 {len(result['pending'])} 个危险操作需要你确认后才会执行：{names}"
                pending_items = [
                    {"name": item["name"], "args": item["args"]} for item in result["pending"]
                ]
                _emit(on_event, {"type": "pending", "items": pending_items})
                return _agent_finish(result, on_event)

        error = f"AI 连续调用工具 {limit} 轮仍未给出最终回复，已停止以避免死循环"
        return _agent_fail(result, error, on_event)
    except Exception as exc:  # noqa: BLE001
        return _agent_fail(result, f"AI 工具循环出错：{type(exc).__name__}: {exc}", on_event)


def run_agent(messages: list, executor, *, allow_risky: bool = False,
              max_rounds: int = 8, key: str | None = None,
              should_stop=None) -> dict:
    """运行 DeepSeek 工具调用循环；危险工具默认只收集到 pending，等上层确认。

    messages 会被就地追加 assistant / tool 消息，上层保存后即可续聊。
    pending 非空时返回 ok=True，reply 是待确认提示；上层确认后先调 approve_pending，
    再调用本函数让模型基于工具结果继续。usage 记录最后一轮 API 的用量。
    带 tools 的请求会把 reasoning_content 一起写进 assistant 消息并原样回传。
    should_stop 是可调用的「用户点了停止」判断函数，会在每轮开始和每次工具调用前检查。
    """
    return _agent_loop(
        messages, executor, allow_risky=allow_risky, max_rounds=max_rounds,
        key=key, streaming=False, on_event=None, should_stop=should_stop,
    )


def run_agent_stream(messages: list, executor, *, allow_risky: bool = False,
                     max_rounds: int = 8, key: str | None = None,
                     thinking: bool = True, reasoning_effort: str = "high",
                     on_event=None, should_stop=None) -> dict:
    """流式版 run_agent：行为和返回字段与 run_agent 一致，额外实时上报 on_event。

    事件格式：round / reasoning / content / tool_call / tool_result / pending /
    final / error。on_event 里抛出的异常会被吞掉并记录到模块内，不会弄崩主流程。
    should_stop 一旦返回 True，流式读取会立刻停止，并返回 error="已停止生成"。
    """
    return _agent_loop(
        messages, executor, allow_risky=allow_risky, max_rounds=max_rounds,
        key=key, thinking=thinking, reasoning_effort=reasoning_effort,
        on_event=on_event, streaming=True, should_stop=should_stop,
    )


def approve_pending(messages: list, pending: list, executor) -> dict:
    """执行用户已确认的 pending 操作，并把每个结果作为 tool 消息写回 messages。

    单个工具失败只反映在该条 tool 消息里，整批仍返回 ok=True。用户拒绝执行时，
    上层应自行给每个 pending 的 id 补一条 tool 消息，否则后续请求会缺少工具回复。
    """
    try:
        if not isinstance(messages, list):
            return {"ok": False, "error": "messages 必须是列表"}
        if not isinstance(pending, list):
            return {"ok": False, "error": "pending 必须是列表"}
        steps = []
        for index, item in enumerate(pending):
            if isinstance(item, dict):
                name = str(item.get("name") or "")
                call_id = str(item.get("id") or f"call_approved_{index}")
                args = item.get("args")
            else:
                name = ""
                call_id = f"call_approved_{index}"
                args = None

            if not name:
                tool_result = {"ok": False, "error": "待确认操作缺少工具名，已跳过"}
                step_args = {}
            elif not isinstance(args, dict):
                tool_result = {"ok": False, "error": "待确认操作的参数格式不对，已跳过"}
                step_args = {}
            else:
                step_args = args
                tool_result = _safe_execute(executor, name, args)

            messages.append(_tool_message(call_id, _json_text(tool_result)))
            steps.append({"name": name, "args": step_args, "result": tool_result})
        return {"ok": True, "steps": steps}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"执行待确认操作时出错：{type(exc).__name__}: {exc}"}


def reset_session() -> None:
    """清空模块内可能缓存的会话。当前实现不缓存会话，保留此接口给上层调用。"""
    return None
