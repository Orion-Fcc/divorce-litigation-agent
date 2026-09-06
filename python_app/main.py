# -*- coding: utf-8 -*-
"""
婚讼管家
本地服务端：法律检索 + 经验学习 + 智能问答 + 文件解析 + 桌面窗口。

运行：
    python main.py            # 启动本地服务并打开窗口（无 pywebview 时自动用浏览器）
    python main.py --no-window  # 只起服务，手动打开浏览器访问提示的地址

配置：
    复制 config.example.json 为 config.json，填入 API Key。
    config.json 不会上传到 git（已加入 .gitignore）。
"""
import base64
import io
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(BASE_DIR, "ui", "index.html")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
VERSION = "2.2.2"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
VISION_PRESETS = {
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            "model": "glm-4v-flash"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
             "model": "qwen-vl-plus"},
}
OCR_PROMPT = ("请完整识别并转写这张图片中的所有文字内容（可能是合同、法律文书、聊天记录、票据、证件等）。"
              "要求：1) 保持原有结构和条款顺序；2) 不得遗漏金额、日期、姓名、证件号、账号等关键信息；"
              "3) 对非文字的重要信息（如签字、手印、盖章位置）简要说明；4) 只输出识别结果，不要评论分析。")
LAW_DB_MAX_AGE_DAYS = 7  # 法律库超过 7 天自动后台更新

sys.path.insert(0, BASE_DIR)
import legal_db
import learning

# ---------------------------------------------------------------- 配置
_config = {}


def load_config():
    global _config
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            _config = json.load(f)
    except Exception:
        _config = {}
    return _config


def cfg(key, default=""):
    return _config.get(key, default) or default


# ---------------------------------------------------------------- LLM 调用
def _requests():
    import requests
    return requests


def llm_complete(messages, max_tokens=800, temperature=0.3, model=None):
    """非流式调用 DeepSeek（用于经验蒸馏等后台任务）。"""
    if not cfg("deepseek_key"):
        return None
    r = _requests().post(DEEPSEEK_URL, json={
        "model": model or cfg("model", "deepseek-chat"),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }, headers={"Authorization": "Bearer " + cfg("deepseek_key"),
                "Content-Type": "application/json"}, timeout=120)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError("DeepSeek 错误 %s: %s" % (r.status_code, str(data)[:200]))
    return data["choices"][0]["message"]["content"]


def _now_line():
    """LLM 自身没有实时时钟，必须把当前日期时间注入上下文。"""
    weekdays = "一二三四五六日"
    now = datetime.now()
    return ("【当前时间】今天是 %d年%d月%d日 星期%s %02d:%02d。"
            "回答涉及期限、时效、日期推算（如冷静期、上诉期、起诉间隔）的问题时，"
            "以此刻日期为基准计算。" % (now.year, now.month, now.day,
                                        weekdays[now.weekday()], now.hour, now.minute))


def build_chat_messages(client_messages, user_query):
    """在客户端消息基础上注入当前时间 + 法律检索结果 + 历史经验。"""
    msgs = [dict(m) for m in client_messages]
    law_block = legal_db.format_context(user_query)
    exp_block = learning.format_context(user_query)
    inject = "\n\n".join(b for b in (_now_line(), law_block, exp_block) if b)
    if inject:
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = msgs[0]["content"] + "\n\n" + inject
        else:
            msgs.insert(0, {"role": "system", "content": inject})
    return msgs


def call_vision(image_data_url, prompt=OCR_PROMPT):
    provider = cfg("vision_provider", "glm")
    preset = VISION_PRESETS.get(provider, VISION_PRESETS["glm"])
    key = cfg("vision_key")
    if not key:
        raise RuntimeError("服务端未配置视觉模型 Key（config.json 的 vision_key）")
    r = _requests().post(preset["base_url"], json={
        "model": cfg("vision_model", preset["model"]) or preset["model"],
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_data_url}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 4096,
    }, headers={"Authorization": "Bearer " + key,
                "Content-Type": "application/json"}, timeout=180)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError("视觉模型错误 %s: %s" % (r.status_code, str(data)[:200]))
    return data["choices"][0]["message"]["content"]


def parse_document(name, data_b64):
    """解析 PDF / Word / 文本类文件，返回纯文本。"""
    raw = base64.b64decode(data_b64)
    ext = (os.path.splitext(name)[1] or "").lower().lstrip(".")
    if ext in ("txt", "md", "csv", "json", "log"):
        return raw.decode("utf-8", errors="replace")
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("未安装 pypdf，请先 pip install -r requirements.txt")
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if ext in ("docx", "doc"):
        if ext == "doc":
            raise RuntimeError("暂不支持 .doc 旧格式，请另存为 .docx 后上传")
        try:
            import docx
        except ImportError:
            raise RuntimeError("未安装 python-docx，请先 pip install -r requirements.txt")
        d = docx.Document(io.BytesIO(raw))
        return "\n".join(p.text for p in d.paragraphs).strip()
    raise RuntimeError("不支持的文件类型：." + ext)


# ---------------------------------------------------------------- 法律库自动更新
def maybe_update_legal_db(force=False):
    """法律库过旧时在后台重新构建（自主学习能力之一：跟踪法律修订）。"""
    def _worker():
        try:
            import subprocess
            script = os.path.join(BASE_DIR, "tools", "build_legal_db.py")
            r = subprocess.run([sys.executable, script], capture_output=True,
                               text=True, timeout=1800)
            print("[法律库更新]", r.stdout[-500:] if r.returncode == 0 else r.stderr[-500:])
            legal_db.reload()
        except Exception as e:
            print("[法律库更新失败]", e)

    manifest_path = os.path.join(BASE_DIR, "legal_db", "manifest.json")
    need = force
    if not need:
        try:
            age = time.time() - os.path.getmtime(manifest_path)
            need = age > LAW_DB_MAX_AGE_DAYS * 86400
        except OSError:
            need = True
    if need:
        threading.Thread(target=_worker, daemon=True).start()
        return True
    return False


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[服务] " + fmt % args + "\n")

    # ---- 工具 ----
    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, status=400):
        self._send_json({"error": msg}, status)

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._serve_ui()
        elif path.startswith("/assets/"):
            self._serve_asset(path)
        elif path == "/api/ping":
            self._send_json({"ok": True, "mode": "python", "version": VERSION,
                             "laws": len(legal_db.manifest()),
                             "experiences": learning.count()})
        elif path == "/api/config":
            self._send_json({
                "has_deepseek_key": bool(cfg("deepseek_key")),
                "has_vision_key": bool(cfg("vision_key")),
                "model": cfg("model", "deepseek-chat"),
                "vision_provider": cfg("vision_provider", "glm"),
            })
        elif path == "/api/laws":
            self._send_json({"laws": legal_db.manifest(),
                             "built_at": self._legal_db_built_at()})
        elif path == "/api/law":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            law = legal_db.get_law((q.get("id") or [""])[0])
            if not law:
                self._send_error("未找到该法律", 404)
            else:
                self._send_json({"law": law})
        elif path == "/api/law_search":
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            query = unquote((q.get("q") or [""])[0])
            law_id = (q.get("law") or [""])[0] or None
            self._send_json({"results": legal_db.search(query, law_id)})
        elif path == "/api/law_retrieve":
            # 对话引用展示：返回与问题最相关的条文（与注入 LLM 的同一检索结果）及官方来源
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            query = unquote((q.get("q") or [""])[0])
            refs = []
            law_meta = {l["id"]: l for l in legal_db.manifest()}
            for h in legal_db.retrieve(query, max_items=6):
                meta = law_meta.get(h["law_id"], {})
                refs.append({"law_short": h["law_short"], "marker": h["marker"],
                             "text": h["text"],
                             "source_name": meta.get("source_name"),
                             "source_url": meta.get("source_url"),
                             "effective": meta.get("effective")})
            self._send_json({"refs": refs})
        elif path == "/api/learn/stats":
            self._send_json({"experiences": learning.count()})
        else:
            self._send_error("Not Found", 404)

    def _legal_db_built_at(self):
        try:
            with open(os.path.join(BASE_DIR, "legal_db", "manifest.json"),
                      encoding="utf-8") as f:
                return json.load(f).get("built_at", "")
        except Exception:
            return ""

    def _serve_ui(self):
        try:
            with open(UI_FILE, "rb") as f:
                body = f.read()
        except OSError:
            self._send_error("ui/index.html 不存在", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    _ASSET_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

    def _serve_asset(self, path):
        """服务 ui/assets/ 下的静态资源（仅允许单层文件名，防路径穿越）。"""
        rel = path[len("/assets/"):]
        if not rel or rel != os.path.basename(rel) or ".." in rel:
            self._send_error("Bad Request", 400)
            return
        fp = os.path.join(BASE_DIR, "ui", "assets", rel)
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            self._send_error("Not Found", 404)
            return
        ext = os.path.splitext(rel)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", self._ASSET_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/chat":
            self._handle_chat()
        elif path == "/api/vision":
            self._handle_vision()
        elif path == "/api/parse":
            self._handle_parse()
        elif path == "/api/learn":
            self._handle_learn()
        elif path == "/api/laws/update":
            started = maybe_update_legal_db(force=True)
            self._send_json({"started": started})
        else:
            self._send_error("Not Found", 404)

    def _handle_chat(self):
        if not cfg("deepseek_key"):
            self._send_error("服务端未配置 DeepSeek Key（python_app/config.json）", 500)
            return
        body = self._json_body()
        client_messages = body.get("messages") or []
        # 找最后一条用户消息做检索
        user_query = ""
        for m in reversed(client_messages):
            if m.get("role") == "user":
                user_query = m.get("content", "")
                break
        messages = build_chat_messages(client_messages, user_query)

        # SSE 转发
        try:
            r = _requests().post(DEEPSEEK_URL, json={
                "model": body.get("model") or cfg("model", "deepseek-chat"),
                "messages": messages,
                "stream": True,
            }, headers={"Authorization": "Bearer " + cfg("deepseek_key"),
                        "Content-Type": "application/json"},
                timeout=(15, 300), stream=True)
        except Exception as e:
            self._send_error("无法连接 DeepSeek：%s" % e, 502)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            if r.status_code != 200:
                err = r.text[:300]
                self.wfile.write(("data: " + json.dumps(
                    {"error": "DeepSeek 错误 %s: %s" % (r.status_code, err)},
                    ensure_ascii=False) + "\n\n").encode("utf-8"))
            else:
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    self.wfile.write((line + "\n\n").encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            # DeepSeek 流中断/读超时：尽力把原因以 SSE 事件发给前端，避免界面卡在“思考中”
            try:
                self.wfile.write(("data: " + json.dumps(
                    {"error": "服务端回答中断：%s" % str(e)[:150]},
                    ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
        finally:
            r.close()

    def _handle_vision(self):
        body = self._json_body()
        image = body.get("image", "")
        if not image:
            self._send_error("缺少图片数据")
            return
        try:
            text = call_vision(image, body.get("prompt") or OCR_PROMPT)
            self._send_json({"text": text})
        except Exception as e:
            self._send_error(str(e), 500)

    def _handle_parse(self):
        body = self._json_body()
        name = body.get("name", "")
        data_b64 = body.get("data_b64", "")
        if not name or not data_b64:
            self._send_error("缺少文件名或内容")
            return
        try:
            text = parse_document(name, data_b64)
            self._send_json({"text": text})
        except Exception as e:
            self._send_error(str(e), 500)

    def _handle_learn(self):
        """会话沉淀：前端在完成一轮回答后上报会话，服务端节流并蒸馏经验。"""
        body = self._json_body()
        session_id = str(body.get("session_id") or "default")
        messages = body.get("messages") or []
        if not cfg("deepseek_key"):
            self._send_json({"learned": False, "reason": "no_key"})
            return
        if not learning.should_learn(session_id, messages):
            self._send_json({"learned": False, "reason": "unchanged"})
            return

        def _worker():
            exp = learning.distill(messages, llm_complete)
            if exp:
                learning.add_experience(exp)
                learning.mark_learned(session_id, messages)
                print("[学习] 新经验：", exp["topics"], exp["situation"][:40])

        threading.Thread(target=_worker, daemon=True).start()
        self._send_json({"learned": True})


# ---------------------------------------------------------------- 自动初始化
def auto_init():
    """首次运行自动初始化（桌面壳 / 浏览器版一键启动都会调用）：
    装依赖 → 生成 config.json → 构建法律库。每一步幂等，可反复执行。"""
    import subprocess

    # 1) 依赖检查与安装
    print("[初始化] 检查 Python 依赖…", flush=True)
    try:
        import requests  # noqa: F401
        print("[初始化] 依赖已就绪。", flush=True)
    except ImportError:
        req = os.path.join(BASE_DIR, "requirements.txt")
        print("[初始化] 正在安装依赖（pip install -r requirements.txt），请稍候…", flush=True)
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req],
                               capture_output=True, text=True, timeout=900)
            if r.returncode == 0:
                print("[初始化] 依赖安装完成。", flush=True)
            else:
                print("[初始化] 依赖安装失败：%s" % (r.stderr or r.stdout)[-300:], flush=True)
        except Exception as e:
            print("[初始化] 依赖安装异常：%s" % e, flush=True)

    # 2) 生成配置（存在则不覆盖）
    if not os.path.exists(CONFIG_FILE):
        try:
            import shutil
            shutil.copyfile(os.path.join(BASE_DIR, "config.example.json"), CONFIG_FILE)
            print("[初始化] 已生成 config.json（请用记事本填入你的 DeepSeek Key，然后重启本应用）。", flush=True)
        except Exception as e:
            print("[初始化] 生成 config.json 失败：%s" % e, flush=True)
    else:
        print("[初始化] config.json 已存在。", flush=True)

    # 3) 构建法律库（首次构建约 1-2 分钟）
    manifest_path = os.path.join(BASE_DIR, "legal_db", "manifest.json")
    laws_ready = False
    try:
        with open(manifest_path, encoding="utf-8") as f:
            laws_ready = bool(json.load(f).get("laws"))
    except Exception:
        laws_ready = False
    if laws_ready:
        print("[初始化] 法律库已就绪。", flush=True)
        return
    print("[初始化] 法律库为空，开始首次构建（从政府/法院官网抓取官方文本，约 1-2 分钟）…", flush=True)
    try:
        script = os.path.join(BASE_DIR, "tools", "build_legal_db.py")
        r = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=1800)
        tail = (r.stdout or "")[-600:]
        print("[初始化] 法律库构建%s。%s" % ("完成" if r.returncode == 0 else "失败", tail), flush=True)
    except Exception as e:
        print("[初始化] 法律库构建异常：%s" % e, flush=True)


def _parse_args(argv):
    """极简参数解析：--no-window / --auto-init / --open-browser / --port N / --port-file PATH"""
    args = {"no_window": False, "auto_init": False, "open_browser": False,
            "port": 0, "port_file": ""}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-window":
            args["no_window"] = True
        elif a == "--auto-init":
            args["auto_init"] = True
        elif a == "--open-browser":
            args["open_browser"] = True
        elif a == "--port" and i + 1 < len(argv):
            try:
                args["port"] = int(argv[i + 1])
            except ValueError:
                pass
            i += 1
        elif a == "--port-file" and i + 1 < len(argv):
            args["port_file"] = argv[i + 1]
            i += 1
        i += 1
    return args


# ---------------------------------------------------------------- 启动
def start_server(port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def main():
    opts = _parse_args(sys.argv[1:])
    load_config()
    if opts["auto_init"]:
        auto_init()  # 桌面壳/浏览器版一键启动：自动装依赖→生成配置→构建法律库
    legal_db.load()
    if not legal_db.is_loaded():
        print("[提示] 法律库为空，可在启动时加 --auto-init 自动构建。")
    maybe_update_legal_db()  # 过旧自动更新（自主学习能力）

    server, port = start_server(opts["port"])
    if opts["port_file"]:
        try:
            with open(opts["port_file"], "w", encoding="ascii") as f:
                f.write(str(port))
        except OSError as e:
            print("[警告] 无法写端口文件：%s" % e)
    url = "http://127.0.0.1:%d/" % port
    print("=" * 56)
    print("  婚讼管家 v%s" % VERSION)
    print("  本地服务：%s" % url)
    print("  法律库：%d 部 | 经验库：%d 条" % (len(legal_db.manifest()),
                                                  learning.count()))
    print("  DeepSeek Key：%s | 视觉 Key：%s" % (
        "已配置" if cfg("deepseek_key") else "未配置（请在 config.json 填写）",
        "已配置" if cfg("vision_key") else "未配置"))
    print("=" * 56)

    if opts["no_window"]:
        print("已启动（--no-window），按 Ctrl+C 停止。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    if opts["open_browser"]:
        if os.environ.get("HSG_NO_BROWSER"):
            print("[浏览器版] 服务已就绪（测试模式跳过打开浏览器）。")
        else:
            print("[浏览器版] 正在打开浏览器…")
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    try:
        import webview  # pywebview
        webview.create_window("婚讼管家", url, width=1200, height=800,
                              min_size=(800, 600))
        webview.start()
    except ImportError:
        print("[提示] 未安装 pywebview，已改用系统浏览器打开。")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
