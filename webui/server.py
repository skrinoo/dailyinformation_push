"""本地控制台：把 config.json 变成能点的界面，并提供一键部署。

设计约束：
1. 零第三方依赖（只用标准库），因为这个工程的卖点之一就是 `pip install` 都不需要。
2. 只监听 127.0.0.1，并校验 Host / Origin：配置文件里有飞书 chat_id、
   模型密钥的环境变量名，不能被随便一个网页跨站读走。
3. 写配置前先备份，校验不过不落盘——不能让 UI 把一套跑不起来的配置写进去。
4. 「一键部署」= 真的在后台起 `main.py schedule`，而不是只生成一句命令。
   进程信息写 data/deploy.json，服务重启后仍能显示状态、仍能停掉。
   系统服务（开机自启）另走 `deploy service`，只生成文件不代执行提权操作。

接口一览：
  GET  /                    控制台页面
  GET  /api/overview        状态 + 今日统计 + 关注项 + 检查项
  GET  /api/config          完整配置 + 两段系统提示词（只读）
  POST /api/config          保存配置（校验 + 备份）
  POST /api/topics          关注项增删改
  POST /api/run             试跑一轮（后台任务）
  GET  /api/job?id=         任务进度与日志
  POST /api/brief           生成晚间简报
  POST /api/deploy          一键部署（常驻 / 生成服务文件）
  POST /api/stop            停止常驻进程
  GET  /api/logs            常驻进程日志尾部
  GET  /api/sources         已配置源 + 兜底选源开关 + 上次自动选源结果
  POST /api/sources/auto    现在挑一次（模型或目录 → 白名单 → 真抓校验）
  GET  /api/review          最近收录的条目 + 已给过的反馈 + 生效权重 + 原因分布
  POST /api/feedback        记一次反馈（up/down/save/open；down 可带 reason）
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import autosource, feedback, judge, studio  # noqa: E402
from intel.briefing import run_briefing  # noqa: E402
from intel.config import backup_path, load_config, validate  # noqa: E402
from intel.pipeline import run_once  # noqa: E402
from intel.scheduler import describe as describe_schedule, next_run_at  # noqa: E402
from intel.store import Store  # noqa: E402
from intel.topics import effective as topic_effective, list_topics  # noqa: E402
from intel.util import now_local  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
UI_PATH = Path(__file__).with_name("ui.html")
DEPLOY_PATH = DATA_DIR / "deploy.json"
SUPERVISOR_LOG = DATA_DIR / "supervisor.log"
SERVICE_DIR = DATA_DIR / "deploy"

RUN_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
MAX_LOG_LINES = 1200


# ---------------------------------------------------------------- 小工具
def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    return str(obj)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_deploy() -> dict[str, Any]:
    if not DEPLOY_PATH.exists():
        return {}
    try:
        return json.loads(DEPLOY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_deploy(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pid_alive(pid: int) -> bool:
    """Windows 上不能用 os.kill(pid, 0) 探活——它会真的去终止进程。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _scheduler_cfg() -> dict[str, Any]:
    cfg = load_config(CONFIG_PATH)
    return cfg.get("schedule") or {}


def _next_run_text(cfg: dict[str, Any]) -> str:
    try:
        nxt = next_run_at(cfg.get("schedule") or {})
        return nxt.strftime("%m-%d %H:%M")
    except Exception:
        return "—"


def _today_window(cfg: dict[str, Any]) -> tuple[str, str]:
    tz = (cfg.get("schedule") or {}).get("timezone", "Asia/Shanghai")
    now = now_local(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            now.astimezone(timezone.utc).isoformat(timespec="seconds"))


def _tail(path: Path, lines: int = 120) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()[-lines:]
        return [ln.rstrip("\n") for ln in data]
    except OSError:
        return []


# ---------------------------------------------------------------- 配置
def _safe_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回给前端前做一次深拷贝，顺手把可能为空的结构补齐，避免前端到处判空。"""
    out = json.loads(json.dumps(cfg, default=_json_default, ensure_ascii=False))
    out.setdefault("topics", [])
    out.setdefault("profile", {})
    channels = out.setdefault("channels", {})
    for name in ("feishu", "dingtalk", "telegram"):
        channels.setdefault(name, {})
    return out


def _checks(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    errors = validate(cfg)
    checks.append({"name": "配置校验", "ok": not errors,
                   "detail": "通过" if not errors else "；".join(errors[:3])})

    llm = cfg.get("llm") or {}
    key_env = str(llm.get("api_key_env") or "")
    has_key = bool(os.environ.get(key_env)) if key_env else False
    if not llm.get("enabled"):
        checks.append({"name": "模型", "ok": True, "detail": "未启用，走规则打分"})
    else:
        checks.append({"name": "模型", "ok": has_key,
                       "detail": f"{llm.get('model', '')} · "
                                 + ("密钥已设置" if has_key else f"环境变量 {key_env} 为空")})

    feishu = (cfg.get("channels") or {}).get("feishu") or {}
    if not feishu.get("enabled"):
        checks.append({"name": "飞书", "ok": True, "detail": "未启用，只生成文件"})
    else:
        from intel import feishu as feishu_mod

        ok = feishu_mod.configured(cfg)
        checks.append({"name": "飞书", "ok": ok,
                       "detail": ("已配置 " + str(feishu.get("mode", "webhook"))) if ok
                                 else "凭据环境变量为空"})

    dingtalk = (cfg.get("channels") or {}).get("dingtalk") or {}
    if not dingtalk.get("enabled"):
        checks.append({"name": "钉钉", "ok": True, "detail": "未启用"})
    else:
        from intel import dingtalk as dingtalk_mod

        ok = dingtalk_mod.configured(cfg)
        env_name = dingtalk.get("webhook_env") or "DINGTALK_WEBHOOK"
        checks.append({"name": "钉钉", "ok": ok,
                       "detail": "已配置 webhook" if ok else f"环境变量 {env_name} 为空"})

    topics = [tp for tp in list_topics(cfg) if tp.get("enabled", True)]
    checks.append({"name": "关注项", "ok": bool(topics),
                   "detail": f"{len(topics)} 个启用" if topics else "没有启用的关注项，跑起来只会空转"})

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append({"name": "存储", "ok": True, "detail": "数据目录可写（data/）"})
    except OSError as exc:
        checks.append({"name": "存储", "ok": False, "detail": f"不可写：{exc}"})
    return checks


def _save_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """先校验再落盘，并留一份带时间戳的备份。"""
    errors = validate(cfg)
    if errors:
        return {"ok": False, "errors": errors}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = backup_path(CONFIG_PATH, stamp)
        backup.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        for old in sorted(backup.parent.glob(f"{CONFIG_PATH.name}.bak-*"))[:-10]:
            try:
                old.unlink()
            except OSError:
                pass
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return {"ok": True, "errors": [], "path": str(CONFIG_PATH)}


# ---------------------------------------------------------------- 常驻进程
def _start_supervisor(dry_run: bool = False) -> dict[str, Any]:
    state = _read_deploy()
    if state.get("pid") and pid_alive(int(state["pid"])):
        return {"ok": False, "detail": f"已经在运行（pid {state['pid']}）"}

    cfg = load_config(CONFIG_PATH)
    errors = validate(cfg)
    if errors:
        return {"ok": False, "detail": "配置未通过校验：" + "；".join(errors[:3])}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ROOT / "main.py"), "schedule"]
    if dry_run:
        cmd.append("--dry-run")
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP
                 | getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {_now_iso()} 启动：{' '.join(cmd)} =====\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                                creationflags=flags, close_fds=True)
    _write_deploy({"pid": proc.pid, "started_at": _now_iso(), "mode": "process",
                   "dry_run": dry_run, "cmd": cmd})
    time.sleep(0.8)  # 给解释器和配置加载一点时间，别让前端立刻看到「已退出」
    if not pid_alive(proc.pid):
        return {"ok": False, "detail": "进程启动后立即退出，查看日志：" + str(SUPERVISOR_LOG)}
    return {"ok": True, "detail": f"已启动（pid {proc.pid}）", "pid": proc.pid}


def _stop_supervisor() -> dict[str, Any]:
    state = _read_deploy()
    pid = int(state.get("pid") or 0)
    if not pid or not pid_alive(pid):
        _write_deploy({**state, "pid": 0, "stopped_at": _now_iso()})
        return {"ok": True, "detail": "当前没有运行中的进程"}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"停止失败：{exc}"}
    _write_deploy({**state, "pid": 0, "stopped_at": _now_iso()})
    return {"ok": True, "detail": "已停止"}


def _write_service_files(cfg: dict[str, Any]) -> dict[str, Any]:
    """生成三平台的开机自启文件，但不替用户执行（提权操作必须由用户自己确认）。"""
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    main = str(ROOT / "main.py")
    written: list[str] = []
    cmds: list[str] = []

    # 用 Register-ScheduledTask 而不是 schtasks /TR：路径含空格时后者要套三层引号，
    # 复制粘贴极易坏掉；PowerShell 的 -Execute / -Argument 是两个独立参数，不会歧义。
    # 动作包一层 cmd /c 把 stdout/stderr 追加进 supervisor.log——计划任务本身不收集输出，
    # 不重定向的话 main.py schedule 的日志（含每次失败的堆栈）就无处可看。
    win = SERVICE_DIR / "install-task.ps1"
    log = str(SUPERVISOR_LOG)
    win.write_text(
        "# Start at logon; restart every minute on failure. Run as Administrator:\n"
        "#   powershell -ExecutionPolicy Bypass -File install-task.ps1\n"
        "$action = New-ScheduledTaskAction -Execute 'cmd.exe' "
        "-Argument '/c \"\"" + py + "\" \"" + main + "\" schedule >> \"" + log + "\" 2>&1\"' "
        '-WorkingDirectory "' + str(ROOT) + '"\n'
        '$trigger = New-ScheduledTaskTrigger -AtLogOn\n'
        '$settings = New-ScheduledTaskSettingsSet -RestartCount 3 '
        '-RestartInterval (New-TimeSpan -Minutes 1) '
        '-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries\n'
        'Register-ScheduledTask -TaskName "IntelAgent" -Action $action -Trigger $trigger '
        '-Settings $settings -Description "Intel Agent scheduled run" -ErrorAction Stop -Force\n'
        'Write-Host "Installed scheduled task: IntelAgent"\n',
        encoding="utf-8")
    written.append(str(win))
    cmds.append('powershell -ExecutionPolicy Bypass -File "' + str(win) + '"')

    unit = SERVICE_DIR / "intel-agent.service"
    unit.write_text(
        "[Unit]\nDescription=Intel Agent scheduled run\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\nWorkingDirectory=" + str(ROOT) + "\n"
        "ExecStart=" + py + " " + main + " schedule\nRestart=on-failure\n"
        "RestartSec=30\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
    written.append(str(unit))
    cmds.append("mkdir -p ~/.config/systemd/user && cp "
                f"'{unit}' ~/.config/systemd/user/ && systemctl --user enable --now intel-agent")

    plist = SERVICE_DIR / "com.intel.agent.plist"
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>com.intel.agent</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        f"    <string>{py}</string><string>{main}</string><string>schedule</string>\n"
        "  </array>\n"
        f"  <key>WorkingDirectory</key><string>{ROOT}</string>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        "  <key>StandardOutPath</key><string>" + str(SUPERVISOR_LOG) + "</string>\n"
        "  <key>StandardErrorPath</key><string>" + str(SUPERVISOR_LOG) + "</string>\n"
        "</dict></plist>\n", encoding="utf-8")
    written.append(str(plist))
    cmds.append(f"cp '{plist}' ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.intel.agent.plist")

    return {"ok": True, "detail": "已生成三平台服务文件", "files": written,
            "commands": cmds, "note": "开机自启需要管理员/用户级权限，命令由你确认后执行。"}


# ---------------------------------------------------------------- 任务
class _Collector:
    """把 print 与 log_fn 都收进任务日志；前端轮询就能看到进度。"""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def write(self, text: str) -> int:
        if not text:
            return 0
        job = JOBS.get(self.job_id)
        if job is None:
            return len(text)
        for line in str(text).splitlines():
            if line.strip():
                job["lines"].append(line.rstrip())
        if len(job["lines"]) > MAX_LOG_LINES:
            del job["lines"][:-MAX_LOG_LINES]
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def _run_job(job_id: str, topic: str, dry_run: bool, reset: bool) -> None:
    job = JOBS[job_id]
    sink = _Collector(job_id)
    try:
        with RUN_LOCK:
            job["status"] = "running"
            cfg = load_config(CONFIG_PATH)
            errors = validate(cfg)
            if errors:
                job["status"] = "failed"
                job["error"] = "配置未通过校验：" + "；".join(errors[:3])
                return
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = run_once(cfg, topic=topic, dry_run=dry_run,
                                  reset_watermark=reset, log_fn=sink.write)
            job["result"] = {
                "run_id": result.get("run_id"),
                "fetched": result.get("fetched"),
                "after_watermark": result.get("after_watermark"),
                "after_dedupe": result.get("after_dedupe"),
                "after_triage": result.get("after_triage"),
                "clusters": result.get("clusters"),
                "coarse_kept": result.get("coarse_kept"),
                "gated": result.get("gated"),
                "kept": result.get("kept"),
                "elapsed_s": result.get("elapsed_s"),
                "tokens": (result.get("budget") or {}).get("used_run"),
                "push": result.get("push"),
                "html": result.get("html"),
                "md": result.get("md"),
                "errors": result.get("errors"),
            }
            job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["finished_at"] = _now_iso()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "IntelConsole/1.0"

    # ---- 基础 ----
    def log_message(self, fmt: str, *args: Any) -> None:  # 静音，避免终端刷屏
        return

    def _host_ok(self) -> bool:
        host = str(self.headers.get("Host") or "")
        return bool(re.match(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", host))

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8"))

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 4 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json({"ok": False, "error": "host not allowed"}, 403)
            return
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        try:
            if path in ("/", "/index.html"):
                self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                self._send(204, b"")
            elif path == "/api/overview":
                self._json(_overview())
            elif path == "/api/config":
                cfg = load_config(CONFIG_PATH)
                self._json({"ok": True, "config": _safe_cfg(cfg),
                            "prompts": {"judge": judge.SYSTEM_PROMPT,
                                        "studio": studio.SYSTEM_PROMPT},
                            "checks": _checks(cfg)})
            elif path == "/api/job":
                job_id = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("id", "")
                job = JOBS.get(job_id)
                self._json({"ok": bool(job), "job": job} if job else
                           {"ok": False, "error": "任务不存在"})
            elif path == "/api/logs":
                self._json({"ok": True, "lines": _tail(SUPERVISOR_LOG, 160)})
            elif path == "/api/sources":
                self._json(_sources_view())
            elif path == "/api/review":
                q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                self._json(_review(q.get("topic", ""), q.get("limit", "40")))
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json({"ok": False, "error": "host not allowed"}, 403)
            return
        origin = str(self.headers.get("Origin") or "")
        if origin and not re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", origin):
            self._json({"ok": False, "error": "origin not allowed"}, 403)
            return
        path = self.path.split("?", 1)[0]
        try:
            payload = self._body()
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"请求解析失败：{exc}"}, 400)
            return

        try:
            if path == "/api/config":
                cfg = payload.get("config") or {}
                self._json(_save_cfg(cfg))
            elif path == "/api/topics":
                self._json(_topic_action(payload))
            elif path == "/api/run":
                if RUN_LOCK.locked():
                    self._json({"ok": False, "error": "已有一轮在跑，等它结束"}, 409)
                    return
                job_id = uuid.uuid4().hex[:10]
                with JOBS_LOCK:
                    JOBS[job_id] = {"id": job_id, "status": "queued", "lines": [],
                                    "started_at": _now_iso(), "topic": payload.get("topic", ""),
                                    "dry_run": bool(payload.get("dry_run", True))}
                threading.Thread(target=_run_job, daemon=True, args=(
                    job_id, str(payload.get("topic") or ""),
                    bool(payload.get("dry_run", True)), bool(payload.get("reset", False)),
                )).start()
                self._json({"ok": True, "job_id": job_id})
            elif path == "/api/brief":
                cfg = load_config(CONFIG_PATH)
                result = run_briefing(cfg, Store(cfg["storage"]["db"]),
                                      day=payload.get("day") or "today",
                                      dry_run=bool(payload.get("dry_run", True)),
                                      verbose=False)
                self._json({"ok": True, "summary": result["summary"],
                            "html": result["html"], "push": result["push"]})
            elif path == "/api/deploy":
                mode = str(payload.get("mode") or "process")
                if mode == "service":
                    self._json(_write_service_files(load_config(CONFIG_PATH)))
                else:
                    self._json(_start_supervisor(bool(payload.get("dry_run", False))))
            elif path == "/api/sources/auto":
                self._json(_sources_auto(payload))
            elif path == "/api/feedback":
                self._json(_feedback(payload))
            elif path == "/api/stop":
                self._json(_stop_supervisor())
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def _review(topic: str = "", limit: str = "40") -> dict[str, Any]:
    """「反馈」页：最近收录的条目 + 已经给过的动作 + 当前生效权重 + 原因分布。"""
    cfg = load_config(CONFIG_PATH)
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 40
    store = Store(cfg["storage"]["db"])
    try:
        items = store.recent_items(limit=n, topic=topic)
        weights = store.topic_weights(topic) if topic else []
        reasons = store.reason_stats(topic)
    finally:
        store.close()
    return {"ok": True, "topic": topic, "items": items, "topic_weights": weights,
            "reasons": reasons, "reason_options": feedback.options(),
            "topics": [t["id"] for t in (cfg.get("topics") or []) if t.get("enabled")]}


def _feedback(payload: dict[str, Any]) -> dict[str, Any]:
    """记一次反馈。action 只认四个；down 可带 reason；topic 留空则按条目归属。"""
    cfg = load_config(CONFIG_PATH)
    item_id = str(payload.get("item_id") or "")
    action = str(payload.get("action") or "")
    topic = str(payload.get("topic") or "")
    reason = str(payload.get("reason") or "")
    if not item_id:
        return {"ok": False, "error": "缺少 item_id"}
    store = Store(cfg["storage"]["db"])
    try:
        out = store.add_feedback(item_id, action, topic=topic, reason=reason)
    except KeyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        store.close()
    return {"ok": True, **out}


def _sources_view() -> dict[str, Any]:
    """给「采集源」页用：现有源、兜底开关、上次自动选源的结果。"""
    cfg = load_config(CONFIG_PATH)
    src = cfg.get("sources") or {}
    auto = {**autosource.DEFAULTS, **(src.get("auto") or {})}
    entries = list((autosource.load_cache(cfg).get("entries") or {}).values())
    latest = max(entries, key=lambda e: float(e.get("created_at") or 0)) if entries else None
    return {
        "ok": True,
        "count": autosource.enabled_source_count(cfg),
        "needs_discovery": autosource.needs_discovery(cfg),
        "auto": auto,
        "rss": [{"name": f.get("name"), "url": f.get("url"), "lang": f.get("lang"),
                 "auto": bool(f.get("auto"))} for f in (src.get("rss") or [])],
        "last": latest,
    }


def _sources_auto(payload: dict[str, Any]) -> dict[str, Any]:
    """现在挑一次。挑源会真抓校验，所以这个请求可能要几秒到十几秒。"""
    cfg = load_config(CONFIG_PATH)
    topic = str(payload.get("topic") or "")
    if topic:
        try:
            cfg = topic_effective(cfg, topic)
        except KeyError:
            return {"ok": False, "error": f"未知关注项：{topic}"}
    api_key = os.environ.get(cfg["llm"].get("api_key_env") or "LLM_API_KEY", "").strip()
    try:
        res = autosource.ensure(cfg, api_key, force=bool(payload.get("refresh")))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "source": res.get("source"),
        "cached": bool(res.get("cached")),
        "feeds": res.get("feeds") or [],
        "rejected": res.get("rejected") or [],
    }


def _topic_action(payload: dict[str, Any]) -> dict[str, Any]:
    """关注项增删改。全部走整份配置校验，避免单点改动绕过 validate。"""
    cfg = load_config(CONFIG_PATH)
    topics = cfg.setdefault("topics", [])
    action = str(payload.get("action") or "save")
    topic = payload.get("topic") or {}
    tid = str(topic.get("id") or "").strip()

    if action == "delete":
        if not tid:
            return {"ok": False, "errors": ["缺少关注项 id"]}
        cfg["topics"] = [tp for tp in topics if tp.get("id") != tid]
        return {**_save_cfg(cfg), "topics": cfg["topics"]}

    if not tid:
        return {"ok": False, "errors": ["关注项 id 不能为空"]}
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,31}$", tid):
        return {"ok": False, "errors": ["id 只能用小写字母、数字和连字符（2-32 位）"]}

    clean = {k: v for k, v in topic.items() if k in {
        "id", "name", "enabled", "keywords", "interests", "avoid", "custom_instructions",
        "sources", "schedule", "filter", "output", "max_items_per_run",
    }}
    clean["id"] = tid
    clean.setdefault("enabled", True)
    existing = next((i for i, tp in enumerate(topics) if tp.get("id") == tid), None)
    if existing is None:
        topics.append(clean)
    else:
        topics[existing] = clean
    return {**_save_cfg(cfg), "topics": cfg["topics"]}


# ---------------------------------------------------------------- 概览
def _overview() -> dict[str, Any]:
    cfg = load_config(CONFIG_PATH)
    state = _read_deploy()
    pid = int(state.get("pid") or 0)
    running = pid_alive(pid)
    tz = (cfg.get("schedule") or {}).get("timezone", "Asia/Shanghai")

    store = Store(cfg["storage"]["db"])
    try:
        start_iso, now_iso = _today_window(cfg)
        runs = [r for r in store.runs_between(start_iso, now_iso) if r.get("status") != "running"]
        today = {
            "runs": len(runs),
            "fetched": sum(int(r.get("fetched") or 0) for r in runs),
            "kept": sum(int(r.get("kept") or 0) for r in runs),
            "pushed": sum(int(r.get("pushed") or 0) for r in runs),
            "tokens": sum(int(r.get("tokens") or 0) for r in runs),
        }
        recent = [{
            "id": r.get("id"), "topic": r.get("topic") or "", "started_at": r.get("started_at"),
            "fetched": r.get("fetched"), "kept": r.get("kept"), "tokens": r.get("tokens"),
            "status": r.get("status"), "note": r.get("note"),
        } for r in store.recent_runs(8)]

        topic_rows = []
        for tp in list_topics(cfg):
            tid = tp["id"]
            topic_rows.append({
                "id": tid,
                "name": tp.get("name") or tid,
                "enabled": bool(tp.get("enabled", True)),
                "schedule": describe_schedule(tp.get("schedule") or {}),
                "next_run": _next_run_text({"schedule": tp.get("schedule") or {}}),
                "kept_today": len(store.topic_items(start_iso, now_iso, topic=tid,
                                                    only_picked=True, limit=200)),
                "last_run": (store.topic_runs(tid, 1) or [{}])[0].get("started_at", ""),
            })
    finally:
        store.close()

    channels = cfg.get("channels") or {}
    return {
        "ok": True,
        "status": {
            "running": running,
            "pid": pid if running else 0,
            "since": state.get("started_at") or "",
            "dry_run": bool(state.get("dry_run")),
            "next_run": _next_run_text(cfg),
            "schedule": describe_schedule(cfg.get("schedule") or {}),
            "timezone": tz,
            "last_log": next((ln for ln in reversed(_tail(SUPERVISOR_LOG, 40))
                              if ln.strip() and not ln.startswith("=====")), ""),
        },
        "today": today,
        "recent": recent,
        "topics": topic_rows,
        "channels": {
            "feishu": {"enabled": bool((channels.get("feishu") or {}).get("enabled")),
                       "mode": (channels.get("feishu") or {}).get("mode", "webhook")},
            "telegram": {"enabled": bool((channels.get("telegram") or {}).get("enabled"))},
            "dingtalk": {"enabled": bool((channels.get("dingtalk") or {}).get("enabled"))},
        },
        "checks": _checks(cfg),
        "paths": {"config": str(CONFIG_PATH), "log": str(SUPERVISOR_LOG),
                  "out": str(ROOT / (cfg["storage"].get("out_dir") or "out"))},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="本地控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    if not UI_PATH.exists():
        print(f"缺少界面文件：{UI_PATH}", file=sys.stderr)
        return 1
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"控制台已启动：{url}（Ctrl+C 退出）")
    if not args.no_open:
        try:
            import webbrowser

            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
