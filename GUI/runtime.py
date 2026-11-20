"""Launch and control one explicitly selected Orange preview session."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(__file__).resolve().parent / ".runtime"
ACCOUNTS = STATE / "accounts.json"          # 账号 -> 上次读到的微信昵称
SERVICE = ROOT / "wxbot" / "wx_service.py"
WORKER = ROOT / "agent" / "reply_worker.py"

if str(ROOT / "agent") not in sys.path:
    sys.path.insert(0, str(ROOT / "agent"))
import apikeys  # noqa: E402  本机 Key 记录库（api_keys.local.json）
import runtime_lock  # noqa: E402


class RuntimeError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_config(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(path: Path, config: dict):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".session-", suffix=".tmp", delete=False) as file:
        staged = Path(file.name)
        json.dump(config, file, ensure_ascii=False, indent=2)
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


class OrangeRuntime:
    def __init__(self):
        self._lock = threading.RLock()
        self._session_lock = None
        self._job = None
        self._children: list[subprocess.Popen] = []
        self._base_url = ""
        self._token = ""
        self.exit_reason = ""
        self._local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        atexit.register(self.stop)

    @property
    def running(self) -> bool:
        children = tuple(self._children)
        return len(children) == 2 and all(child.poll() is None for child in children)

    def discover_accounts(self) -> list[dict]:
        agent_dir = str(ROOT / "agent")
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        try:
            from wx_identity import discover_accounts
            return discover_accounts()
        except ImportError as exc:
            raise RuntimeError("微信组件加载失败，请使用已安装项目依赖的 Python 启动。") from exc
        except OSError as exc:
            raise RuntimeError("无法读取本机微信的运行状态，请确认微信已登录。") from exc

    def account_nicks(self, accounts) -> dict:
        """连接页补昵称：只读密钥已缓存的账号，读不到就返回空表（不影响选账号）。"""
        agent_dir = str(ROOT / "agent")
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        try:
            from wx_identity import read_nicks
            return read_nicks(accounts)
        except Exception:                     # noqa: BLE001 昵称只是显示用
            return {}

    def known_nicks(self) -> dict:
        """上次成功读到的昵称（读库失败时连接页仍能显示）。"""
        try:
            data = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if str(v).strip()}

    def remember_nick(self, account: str, nick: str) -> None:
        """把读到的昵称记在本机（按账号），下次连接页不用再开库。"""
        nick = " ".join(str(nick or "").split())
        if not account or not nick:
            return
        data = self.known_nicks()
        if data.get(account) == nick:
            return
        data[account] = nick
        try:
            ACCOUNTS.parent.mkdir(parents=True, exist_ok=True)
            staged = ACCOUNTS.with_name(ACCOUNTS.name + ".tmp")
            staged.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(staged, ACCOUNTS)
        except OSError:
            pass

    def _original_model(self) -> dict:
        value = read_config(ROOT / "config.json").get("llm")
        configured = value if isinstance(value, dict) else {}
        base_url = configured.get("base_url") or "https://api.deepseek.com"
        return {
            "base_url": base_url,
            "model": configured.get("model") or "deepseek-chat",
            # 优先级与配置加载一致：环境变量 > 匹配接口的本机 Key > 旧文件里的 Key
            "api_key": (os.environ.get("DSHWX_API_KEY", "").strip()
                        or apikeys.key_for_url(base_url)
                        or configured.get("api_key") or ""),
        }

    def configured_model(self) -> dict:
        model = self._original_model()
        return {"base_url": model["base_url"], "model": model["model"],
                "key_available": bool(model["api_key"])}

    # ---------------------------------------------------------- 本机 Key 记录
    def api_keys(self) -> dict:
        """设置面板用：已保存的 Key 记录 + 默认那条的完整信息。"""
        return apikeys.for_gui()

    def save_api_key(self, label: str, base_url: str, model: str, key: str) -> dict:
        """记下一条 Key 并设为默认；同一个 Key 重复填只更新标签。"""
        saved = apikeys.add(label, base_url, model, key)
        if not saved:
            raise RuntimeError("API Key 为空，没有保存。")
        return {"saved": saved, **apikeys.for_gui()}

    def select_api_key(self, id_: str) -> dict:
        apikeys.select(id_)
        return apikeys.for_gui()

    def api_key_secret(self, id_: str) -> str:
        """取某条记录的完整 Key，只用于填充输入框，不显示在列表里。"""
        return apikeys.secret(id_)

    def delete_api_key(self, id_: str) -> dict:
        apikeys.remove(id_)
        return apikeys.for_gui()

    def _model(self, selection: dict) -> dict:
        mode = selection.get("mode")
        if mode not in {"configured", "custom"}:
            raise RuntimeError("请选择模型 API。")
        values = self._original_model() if mode == "configured" else selection
        url = str(values.get("base_url") or "").strip().rstrip("/")
        if url.endswith("/chat/completions"):
            url = url.removesuffix("/chat/completions").rstrip("/")
        name = str(values.get("model") or "").strip()
        key = str(values.get("api_key") or "").strip()
        try:
            target = urlsplit(url)
            port = target.port
        except ValueError as exc:
            raise RuntimeError("API 地址格式无效。") from exc
        loopback = target.hostname in ("localhost", "127.0.0.1", "::1")
        if (not target.hostname or target.username is not None or target.password is not None
                or "?" in url or "#" in url or port == 0
                or any(c.isspace() or ord(c) < 32 for c in url)
                or not (target.scheme == "https" or (target.scheme == "http" and loopback))):
            raise RuntimeError("请输入 HTTPS API 地址。本机模型可使用回环 HTTP 地址。")
        if not name or len(name) > 256 or any(c.isspace() for c in name):
            raise RuntimeError("请输入有效的模型 ID。")
        if not key and loopback:
            key = "local"
        if not key or len(key) > 4096 or not key.isascii() or any(c.isspace() for c in key):
            raise RuntimeError("请输入有效的 API Key。")
        return {"base_url": url, "model": name, "api_key": key}

    def test_model(self, selection: dict) -> dict:
        """Only a fixed test prompt goes to the selected provider."""
        try:
            model = self._model(selection)
            payload = {"model": model["model"], "messages": [
                {"role": "user", "content": "Reply with OK."}], "max_tokens": 128,
                "stream": False}
            req = urllib.request.Request(
                model["base_url"] + "/chat/completions", json.dumps(payload).encode("utf-8"),
                {"Authorization": "Bearer " + model["api_key"], "Content-Type": "application/json"},
                method="POST")
            start = time.monotonic()
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
                data = json.load(response)
            choices = data.get("choices") or []
            if not choices or not (choices[0].get("message") or {}).get("content"):
                return {"ok": False, "error": "接口响应未包含可用的聊天回复。"}
            return {"ok": True, "elapsed": round(time.monotonic() - start, 2)}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"API 返回 HTTP {exc.code}，请检查地址、模型 ID 和密钥。"}
        except (urllib.error.URLError, OSError, TimeoutError):
            return {"ok": False, "error": "连接模型 API 失败或超时。"}
        except (ValueError, TypeError, AttributeError):
            return {"ok": False, "error": "接口响应与聊天补全格式不兼容。"}

    def _reserve_session(self):
        import psutil
        import win32job

        STATE.mkdir(parents=True, exist_ok=True)
        handle = runtime_lock.acquire(ROOT, "gui")
        if handle is None:
            raise RuntimeError("已有 Orange 后台在运行，请先关闭对应的旧链路或 GUI 会话。")
        self._session_lock = handle

        # 兼容尚未退出、仍使用旧版锁的本项目后台。
        project_entries = {SERVICE, WORKER}
        for process in psutil.process_iter(["cmdline"]):
            try:
                argv = process.info.get("cmdline") or []
                for arg in argv[1:]:
                    candidate = Path(arg)
                    if candidate.name.casefold() not in {SERVICE.name, WORKER.name}:
                        continue
                    full = candidate if candidate.is_absolute() else Path(process.cwd()) / candidate
                    if full.resolve() in project_entries:
                        raise RuntimeError("旧版或另一个 Orange 后台正在运行。请先用对应界面停止它。")
            except (OSError, psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                continue

        self._job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation, limits)

    def _launch(self, entry: Path, args: list[str], env: dict, log_path: Path):
        import win32api
        import win32con
        import win32job

        executable = Path(sys.executable)
        console_executable = executable.with_name("python.exe")
        if executable.name.lower() == "pythonw.exe" and console_executable.exists():
            executable = console_executable
        with log_path.open("ab") as output:
            child = subprocess.Popen(
                [str(executable), str(entry), *args], cwd=log_path.parent, env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        self._children.append(child)
        try:
            access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
            handle = win32api.OpenProcess(access, False, child.pid)
            try:
                win32job.AssignProcessToJobObject(self._job, handle)
            finally:
                handle.Close()
        except OSError as exc:
            raise RuntimeError("无法托管后台进程，启动已取消。") from exc
        return child

    def _session_config(self, folder: Path, account: str, port: int, model: dict) -> Path:
        path = folder / "config.json"
        config = read_config(path)
        if not config:
            original = read_config(ROOT / "config.json")
            config = original if original.get("account") == account else read_config(ROOT / "config.example.json")
        config.update(account=account, port=port, token="")
        config.setdefault("llm", {}).update(base_url=model["base_url"], model=model["model"], api_key="")
        config.setdefault("agent", {}).update(auto_reply=False, group_member_memory=False)
        config.setdefault("logging", {})["chat_content"] = False
        proactive = config.setdefault("proactive", {})
        proactive["enabled"] = False
        proactive.setdefault("reminders", {})["enabled"] = False
        kb = config.setdefault("kb", {})
        kb.update(inbox=str(folder / "inbox"), db_path=str(folder / "knowledge.db"))
        write_config(path, config)
        return path

    def start(self, account: str, selection: dict, confirmed: bool) -> dict:
        if confirmed is not True:
            raise RuntimeError("请确认选择的是要授权给 Orange 的微信小号。")
        model = self._model(selection)
        with self._lock:
            if self._children:
                if self.running:
                    raise RuntimeError("请先停止当前绑定，再切换账号或模型。")
                self.stop()
            self.exit_reason = ""
            selected = [item for item in self.discover_accounts() if item["account"] == account]
            if len(selected) != 1:
                raise RuntimeError("所选小号已离线或身份不明确，请刷新账号列表。")
            item = selected[0]
            try:
                self._reserve_session()
                folder = STATE / hashlib.sha256(account.encode()).hexdigest()[:24]
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "inbox").mkdir(exist_ok=True)
                ready = folder / "worker.ready"
                ready.unlink(missing_ok=True)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                session_config = self._session_config(folder, account, port, model)
                self._base_url = f"http://127.0.0.1:{port}"
                self._token = secrets.token_urlsafe(32)
                env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                           PYTHONDONTWRITEBYTECODE="1", WXBOT_GUI_SESSION="1",
                           WXBOT_CONFIG=str(session_config),
                           WXBOT_ACCOUNT=account, WXBOT_ACCOUNT_WXID=item["wxid"],
                           WXBOT_ACCOUNT_DIR=item["path"], WXBOT_PORT=str(port),
                           DSHWX_TOKEN=self._token, DSHWX_API_KEY=model["api_key"],
                           WXBOT_QUEUE_DB=str(folder / "queue.db"),
                           WXBOT_MEMORY_DB=str(folder / "memory.db"),
                           WXBOT_KB_DB=str(folder / "knowledge.db"),
                           WXBOT_LOG=str(folder / "reply.log"),
                           WXBOT_MEDIA_CACHE=str(folder / "media_cache"),
                           WXBOT_MEDIA_KEYS=str(folder / "media_keys.json"),
                           WXBOT_USAGE_LOG=str(folder / "usage.jsonl"),
                           WXBOT_WEB_BUDGET=str(folder / "web_budget.json"),
                           WXBOT_READY_FILE=str(ready))
                log_path = folder / "process.log"
                log_path.write_text("", encoding="utf-8")
                service = self._launch(SERVICE, ["--account", account, "--port", str(port)], env, log_path)
                status = self._wait_service(service, account)
                worker = self._launch(WORKER, [], env, log_path)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and worker.poll() is None:
                    if service.poll() is not None:
                        break
                    if ready.is_file():
                        threading.Thread(target=self._watch_children,
                                         args=((service, worker), log_path), daemon=True).start()
                        return {**status, "model": model["model"], "base_url": model["base_url"]}
                    time.sleep(0.2)
                raise RuntimeError(f"小号工作进程未能启动。诊断日志位于 {log_path}")
            except BaseException:
                self.stop()
                raise

    def _watch_children(self, children: tuple, log_path: Path):
        while all(child.poll() is None for child in children):
            time.sleep(0.4)
        with self._lock:
            if tuple(self._children) == children:
                self.exit_reason = f"后台意外退出，本次运行已停止。诊断日志位于 {log_path}"
                self.stop()

    def _wait_service(self, service, account: str) -> dict:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and service.poll() is None:
            try:
                status = self.request("/status", timeout=0.8)
            except RuntimeError:
                time.sleep(0.2)
                continue
            if status.get("account") != account:
                raise RuntimeError("服务返回的账号与选择不符，启动已取消。")
            if status.get("db_error"):
                raise RuntimeError("无法读取所选小号的微信数据库：" + str(status["db_error"]))
            if status.get("account_bound"):
                return status
            time.sleep(0.2)
        raise RuntimeError("无法验证已登录小号的微信窗口，请检查登录状态和运行权限。")

    def request(self, path: str, payload=None, timeout: float = 12) -> dict:
        if not self._base_url or not path.startswith("/") or path.startswith("//"):
            raise RuntimeError("Orange 服务尚未启动。")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self._base_url + path, data=body, headers={"X-Token": self._token,
            "Content-Type": "application/json; charset=utf-8"}, method="GET" if body is None else "POST")
        try:
            with self._local_http.open(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                return json.load(exc)
            except (ValueError, OSError):
                raise RuntimeError(f"服务返回 HTTP {exc.code}。") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError("无法连接 Orange 后台服务。") from exc

    def stop(self):
        with self._lock:
            children = self._children
            self._children = []
            self._base_url = ""
            self._token = ""
            for child in reversed(children):
                if child.poll() is None:
                    with suppress(OSError):
                        child.terminate()
            if self._job is not None:
                self._job.Close()
                self._job = None
            for child in reversed(children):
                with suppress(OSError, subprocess.TimeoutExpired):
                    child.wait(timeout=2)
            if self._session_lock is not None:
                self._session_lock.close()
                self._session_lock = None

