"""Offline API tests: run in a disposable container with --network none."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server as app


class LocalAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = app.Server(("127.0.0.1", 18765), app.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.data_patch = patch.object(app, "DATA", Path(self.tmp.name))
        self.data_patch.start()
        self.worker_patch = patch.object(app, "run_job", lambda job: None)
        self.worker_patch.start()
        with app.LOCK:
            app.JOBS.clear()
            app.ACTIVE = app.PROCESS = None

    def tearDown(self):
        self.worker_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", 18765, timeout=3)
        base = {"Host": "127.0.0.1:18765", "Content-Type": "application/json", "X-Local-Token": app.TOKEN}
        base.update(headers or {})
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=base)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def create(self):
        status, _, raw = self.request("POST", "/api/jobs", {"username": "fixture-user", "sites": ["GitHub"]})
        self.assertEqual(status, 202)
        return json.loads(raw)["id"]

    def test_query_cancel_persist_and_restart(self):
        ident = self.create()
        self.assertEqual(self.request("POST", "/api/jobs", {"username": "other"})[0], 409)
        status, _, raw = self.request("POST", f"/api/jobs/{ident}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["state"], "cancelled")
        self.assertTrue((app.DATA / f"{ident}.json").exists())
        app.JOBS[ident]["state"] = "running"
        app.save(app.JOBS[ident])
        app.JOBS.clear()
        app.load()
        self.assertEqual(app.JOBS[ident]["state"], "interrupted")

    def test_origin_host_token_and_mime_boundaries(self):
        for headers in ({"Host": "evil.example:18765"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("GET", "/api/bootstrap", headers=headers)[0], 403)
        for headers in ({"X-Local-Token": "wrong"}, {"Origin": "null"}):
            self.assertEqual(self.request("POST", "/api/jobs", {"username": "fixture"}, headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/jobs", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/jobs", {"username": "fixture"}, {"Origin": "http://127.0.0.1:18765"})[0], 202)

    def test_input_paths_and_arbitrary_sites_rejected(self):
        for username in ("../fixture", "-option", "=formula", "foo/bar", "<script>", "a" * 65, [], None):
            self.assertEqual(self.request("POST", "/api/jobs", {"username": username})[0], 400)
        for sites in (["http://127.0.0.1"], [1], "GitHub"):
            self.assertEqual(self.request("POST", "/api/jobs", {"username": "fixture", "sites": sites})[0], 400)
        self.assertEqual(self.request("GET", "/api/jobs/../../etc/passwd/export.txt")[0], 404)
        self.assertEqual(self.request("GET", "/../server.py")[0], 404)
        self.assertEqual(self.request("POST", "/api/jobs", {"username": "a" * 5000})[0], 400)

    def test_export_escaping_and_safe_links(self):
        ident = self.create()
        app.JOBS[ident]["rows"] = [{"site": "=1+1", "url": "https://example.invalid/user", "status": "Claimed", "detail": "\t=2", "seconds": 1}]
        for fmt in ("csv", "txt", "json"):
            status, headers, body = self.request("GET", f"/api/jobs/{ident}/export.{fmt}")
            self.assertEqual(status, 200)
            self.assertIn(f"{ident}.{fmt}", headers["Content-Disposition"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            if fmt == "csv":
                self.assertIn("'=1+1", body.decode("utf-8-sig"))
                self.assertIn("'\t=2", body.decode("utf-8-sig"))
        self.assertEqual(app.safe_url("javascript:alert(1)"), "")
        self.assertEqual(app.safe_url("https://user:pass@example.invalid"), "")
        self.assertEqual(app.safe_url("https://example.invalid/abc"), "https://example.invalid/abc")
        self.assertEqual(self.request("GET", "/")[1]["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
