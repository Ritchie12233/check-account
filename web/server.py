"""Local-only UI: fixed routes, bounded jobs, no shell or arbitrary URL inputs."""
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import sherlock_project

STATIC = Path(__file__).parent
DATA = Path(os.environ.get("CHECK_ACCOUNT_DATA", "/data"))
PORT = 18765
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
JOBS = {}
ACTIVE = None
PROCESS = None
SLOTS = threading.BoundedSemaphore(24)
ID = re.compile(r"[a-f0-9]{32}")
USERNAME = re.compile(r"[\w][\w.\-]{0,63}", re.UNICODE)
manifest = json.loads((Path(sherlock_project.__file__).parent / "resources/data.json").read_text())
SITES = sorted(k for k, v in manifest.items() if isinstance(v, dict) and "url" in v and not v.get("isNSFW"))


def save(job):
    target = DATA / (job["id"] + ".json")
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def load():
    DATA.mkdir(parents=True, exist_ok=True)
    for path in sorted(DATA.glob("*.json")):
        if not ID.fullmatch(path.stem) or path.is_symlink() or path.stat().st_size > 2_000_000:
            continue
        try:
            job = json.loads(path.read_text())
            if job["id"] != path.stem or not isinstance(job.get("rows"), list):
                continue
            if job["state"] == "running":
                job.update(state="interrupted", message="服务已重启；已完成的部分结果保留，可重新查询。")
                save(job)
            JOBS[job["id"]] = job
        except (ValueError, KeyError, OSError, TypeError):
            continue


def safe_url(value):
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username else ""
    except ValueError:
        return ""


def run_job(job):
    global ACTIVE, PROCESS
    proc = None
    timer = None
    try:
        with LOCK:
            if job["state"] != "running":
                return
            proc = subprocess.Popen(
                [sys.executable, str(STATIC / "worker.py"), job["username"], json.dumps(job["sites"])],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONUNBUFFERED": "1"})
            PROCESS = proc
        def deadline():
            with LOCK:
                if job["state"] == "running":
                    job.update(state="timeout", message="查询超过 10 分钟，已停止；部分结果已保存。")
                    proc.kill()
        timer = threading.Timer(600, deadline)
        timer.daemon = True
        timer.start()
        done = False
        for line in proc.stdout:
            if len(line) > 65536:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with LOCK:
                if job["state"] != "running":
                    break
                kind = event.get("type")
                if kind == "start":
                    job["total"] = event["total"]
                elif kind == "row" and len(job["rows"]) < len(SITES):
                    job["rows"].append({"site": str(event["site"])[:150],
                        "url": safe_url(str(event["url"])[:4096]),
                        "status": str(event["status"])[:30],
                        "seconds": event.get("seconds"), "detail": str(event.get("detail", ""))[:200]})
                elif kind == "done":
                    done = True
                elif kind == "error":
                    job["message"] = event["message"]
                save(job)
        code = proc.wait(timeout=5)
        with LOCK:
            if job["state"] == "running":
                job["state"] = "completed" if code == 0 and done else "failed"
                if job["state"] == "failed" and not job["message"]:
                    job["message"] = "查询进程未正常结束；已收到的部分结果仍可查看。"
    except Exception:
        with LOCK:
            if job["state"] == "running":
                job.update(state="failed", message="查询未能完成，请重试。")
    finally:
        if timer:
            timer.cancel()
        if proc:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
        with LOCK:
            ACTIVE = None
            PROCESS = None
            job["finished"] = time.time()
            save(job)


def summary(job):
    return {k: v for k, v in job.items() if k != "rows"} | {
        "checked": len(job["rows"]), "found": sum(r["status"] == "Claimed" for r in job["rows"])}


def csv_cell(value):
    value = str(value if value is not None else "")
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export(job, fmt):
    if fmt == "json":
        return json.dumps(job, ensure_ascii=False, indent=2).encode(), "application/json"
    if fmt == "txt":
        lines = [f"用户名: {job['username']}", f"任务状态: {job['state']}", "同名账号需要人工核实归属。", ""]
        lines += [f"{r['site']}\t{r['url']}" for r in job["rows"] if r["status"] == "Claimed"]
        return "\n".join(lines).encode("utf-8"), "text/plain; charset=utf-8"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["用户名", "站点", "状态", "链接", "耗时（秒）", "说明"])
    for row in job["rows"]:
        writer.writerow([csv_cell(v) for v in (job["username"], row["site"], row["status"], row["url"], row.get("seconds"), row.get("detail"))])
    return stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"


class Handler(BaseHTTPRequestHandler):
    server_version = "CheckAccount"

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, *args):
        pass  # Do not log usernames or query history.

    def reply(self, code, body, mime="application/json", download=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self):
        if self.headers.get_all("Host", []) not in ([f"127.0.0.1:{PORT}"], [f"localhost:{PORT}"]):
            self.reply(403, {"error": "仅允许通过本机地址访问。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.reply(403, {"error": "不允许跨站请求。"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + self.headers["Host"]:
            self.reply(403, {"error": "请求来源不匹配。"})
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        if path in assets:
            name, mime = assets[path]
            return self.reply(200, (STATIC / name).read_bytes(), mime)
        with LOCK:
            if path == "/api/bootstrap":
                return self.reply(200, {"token": TOKEN, "sites": SITES})
            if path == "/api/jobs":
                return self.reply(200, [summary(j) for j in sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)])
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/export\.(csv|txt|json))?", path)
            if match and match[1] in JOBS:
                job = JOBS[match[1]]
                if match[2]:
                    body, mime = export(job, match[2])
                    return self.reply(200, body, mime, f"check-account-{job['id']}.{match[2]}")
                return self.reply(200, summary(job) | {"rows": job["rows"]})
        self.reply(404, {"error": "未找到记录。"})

    def do_POST(self):
        global ACTIVE
        if not self.trusted():
            return
        if not secrets.compare_digest(self.headers.get("X-Local-Token", ""), TOKEN):
            return self.reply(403, {"error": "页面已过期，请刷新后重试。"})
        if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
            return self.reply(415, {"error": "请求必须使用 JSON。"})
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 4096:
                raise ValueError()
            payload = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, OSError):
            return self.reply(400, {"error": "请求格式无效或内容过长。"})
        with LOCK:
            if self.path == "/api/jobs":
                username = payload.get("username")
                sites = payload.get("sites", [])
                if not isinstance(username, str) or not USERNAME.fullmatch(username) or username in (".", ".."):
                    return self.reply(400, {"error": "用户名须为 1–64 个字母、数字、下划线、点或短横线，并以字母、数字或下划线开头。"})
                if not isinstance(sites, list) or len(sites) > 30 or any(not isinstance(s, str) or s not in SITES for s in sites):
                    return self.reply(400, {"error": "查询范围无效。"})
                if ACTIVE:
                    return self.reply(409, {"error": "已有查询正在进行，请等待完成或先停止。", "id": ACTIVE})
                if len(JOBS) >= 500:
                    return self.reply(409, {"error": "历史记录已达到 500 条，请先备份并清理本地 web-data 目录后重启。"})
                job = {"id": uuid.uuid4().hex, "username": username, "sites": sorted(set(sites)),
                       "created": time.time(), "state": "running", "total": len(sites) or len(SITES), "rows": [], "message": ""}
                save(job)
                JOBS[job["id"]] = job
                ACTIVE = job["id"]
                threading.Thread(target=run_job, args=(job,), daemon=True).start()
                return self.reply(202, summary(job))
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", self.path)
            if match and match[1] in JOBS:
                job = JOBS[match[1]]
                if job["state"] == "running":
                    job.update(state="cancelled", message="查询已停止；部分结果已保存。")
                    if PROCESS and ACTIVE == job["id"]:
                        PROCESS.kill()
                    save(job)
                return self.reply(200, summary(job))
        self.reply(404, {"error": "未找到操作。"})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        if not SLOTS.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            SLOTS.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            SLOTS.release()


if __name__ == "__main__":
    load()
    print("Check Account listening on port 18765", flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
