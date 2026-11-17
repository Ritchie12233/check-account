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
from evidence import accounts, safe_url, variants, valid_name, REVIEW_STATES

import sherlock_project

STATIC = Path(__file__).parent
DATA = Path(os.environ.get("CHECK_ACCOUNT_DATA", "/data"))
PORT = 18765
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
JOBS = {}
ACTIVE = None
PROCESS = None
SLOTS = threading.BoundedSemaphore(24)
ID = re.compile(r"[a-f0-9]{32}")
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
        if not ID.fullmatch(path.stem) or path.is_symlink() or path.stat().st_size > 8_000_000:
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


def run_job(job):
    global ACTIVE, PROCESS
    proc = None
    timer = None
    try:
        with LOCK:
            if job["state"] != "running":
                return
            proc = subprocess.Popen(
                [sys.executable, str(STATIC / "worker.py"), json.dumps(job.get("usernames", [job["username"]])), json.dumps(job["sites"])],
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
                elif kind == "row" and len(job["rows"]) < len(SITES) * len(job.get("usernames", [job["username"]])):
                    job["rows"].append({"site": str(event["site"])[:150],
                        "url": safe_url(str(event["url"])[:4096]),
                        "status": str(event["status"])[:30],
                        "seconds": event.get("seconds"), "detail": str(event.get("detail", ""))[:200],
                        "username": event.get("username", job["username"]), "checked_at": event.get("checked_at", "")})
                elif kind == "link" and len(job.setdefault("links", [])) < 120:
                    if not any(link["id"] == event["id"] for link in job["links"]):
                        job["links"].append({k: event[k] for k in ("id", "from_url", "to_url", "site", "username", "observed_at", "evidence", "review")})
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
    return {k: v for k, v in job.items() if k not in ("rows", "links", "reviews")} | {
        "checked": len(job["rows"]), "found": len(accounts(job)), "link_count": len(job.get("links", []))}


def detail(job):
    links = [dict(link, review=job.get("reviews", {}).get("link:" + link["id"], "pending")) for link in job.get("links", [])]
    return summary(job) | {"rows": job["rows"], "accounts": accounts(job), "links": links}


def csv_cell(value):
    value = str(value if value is not None else "")
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export(job, fmt):
    if fmt == "json":
        return json.dumps(detail(job), ensure_ascii=False, indent=2).encode(), "application/json"
    if fmt == "txt":
        lines = [f"用户名: {job['username']}", f"任务状态: {job['state']}", "同名账号需要人工核实归属。", ""]
        for account in accounts(job):
            sources = ", ".join(dict.fromkeys(s["username"] for s in account["sources"]))
            lines.append(f"{account['site']}\t{account['url']}\t{REVIEW_STATES[account['review']]}\t查询来源: {sources}\t{account['evidence']}")
        lines.append("\n公开主页关联（链接出现不代表账号同属一人）：")
        lines += [f"{link['from_url']} -> {link['to_url']}\t{REVIEW_STATES[link['review']]}\t{link['evidence']}" for link in detail(job)["links"]]
        return "\n".join(lines).encode("utf-8"), "text/plain; charset=utf-8"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["记录类型", "站点", "查询用户名", "检测状态", "核实状态", "账号/目标链接", "来源链接", "证据说明", "检测时间"])
    for account in accounts(job):
        writer.writerow([csv_cell(v) for v in ("去重账号", account["site"], ", ".join(dict.fromkeys(s["username"] for s in account["sources"])), "已发现", REVIEW_STATES[account["review"]], account["url"], " | ".join(s["url"] for s in account["sources"]), account["evidence"], " | ".join(s["checked_at"] for s in account["sources"]))])
    for link in detail(job)["links"]:
        writer.writerow([csv_cell(v) for v in ("主页关联", link["site"], link["username"], "页面外链", REVIEW_STATES[link["review"]], link["to_url"], link["from_url"], link["evidence"], link["observed_at"])])
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
                return self.reply(200, detail(job))
        self.reply(404, {"error": "未找到记录。"})

    def do_POST(self):
        global ACTIVE
        if not self.trusted():
            return
        supplied_token = self.headers.get("X-Local-Token", "")
        if not supplied_token.isascii() or not secrets.compare_digest(supplied_token, TOKEN):
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
            if self.path == "/api/variants":
                if not valid_name(payload.get("username")):
                    return self.reply(400, {"error": "请先输入有效用户名。"})
                return self.reply(200, {"usernames": variants(payload["username"])})
            if self.path == "/api/jobs":
                username = payload.get("username")
                sites = payload.get("sites", [])
                if not valid_name(username):
                    return self.reply(400, {"error": "用户名须为 1–64 个字母、数字、下划线、点或短横线，并以字母、数字或下划线开头。"})
                if not isinstance(sites, list) or len(sites) > 30 or any(not isinstance(s, str) or s not in SITES for s in sites):
                    return self.reply(400, {"error": "查询范围无效。"})
                aliases = payload.get("variants", [])
                if not isinstance(aliases, list) or len(aliases) > 5 or any(not valid_name(name) for name in aliases):
                    return self.reply(400, {"error": "额外用户名最多 5 个，且须符合相同的用户名格式。"})
                usernames = list(dict.fromkeys([username, *aliases]))
                if ACTIVE:
                    return self.reply(409, {"error": "已有查询正在进行，请等待完成或先停止。", "id": ACTIVE})
                if len(JOBS) >= 500:
                    return self.reply(409, {"error": "历史记录已达到 500 条，请先备份并清理本地 web-data 目录后重启。"})
                job = {"id": uuid.uuid4().hex, "username": username, "sites": sorted(set(sites)),
                       "created": time.time(), "state": "running", "total": (len(sites) or len(SITES)) * len(usernames),
                       "usernames": usernames, "rows": [], "links": [], "reviews": {}, "message": ""}
                save(job)
                JOBS[job["id"]] = job
                ACTIVE = job["id"]
                threading.Thread(target=run_job, args=(job,), daemon=True).start()
                return self.reply(202, summary(job))
            review_match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/review", self.path)
            if review_match and review_match[1] in JOBS:
                job = JOBS[review_match[1]]
                key, state = payload.get("key"), payload.get("state")
                keys = {account["key"] for account in accounts(job)} | {"link:" + link["id"] for link in job.get("links", [])}
                if not isinstance(key, str) or key not in keys or not isinstance(state, str) or state not in REVIEW_STATES:
                    return self.reply(400, {"error": "核实对象或状态无效。"})
                job.setdefault("reviews", {})[key] = state
                save(job)
                return self.reply(200, detail(job))
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
