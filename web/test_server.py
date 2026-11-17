"""Offline API tests: run in a disposable container with --network none."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server as app
from evidence import accounts, canonical_url, public_links, variants


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
                self.assertIn("待核实", body.decode("utf-8-sig"))
        self.assertEqual(app.safe_url("javascript:alert(1)"), "")
        self.assertEqual(app.safe_url("https://user:pass@example.invalid"), "")
        self.assertEqual(app.safe_url("https://example.invalid/abc"), "https://example.invalid/abc")
        self.assertEqual(self.request("GET", "/")[1]["X-Frame-Options"], "DENY")

    def test_variant_preview_and_bounded_query(self):
        status, _, raw = self.request("POST", "/api/variants", {"username": "Example_User"})
        self.assertEqual(status, 200)
        names = json.loads(raw)["usernames"]
        self.assertEqual(names[0], "Example_User")
        self.assertIn("example_user", names)
        self.assertIn("Example-User", names)
        self.assertLessEqual(len(names), 6)
        status, _, raw = self.request("POST", "/api/jobs", {"username": "Example_User", "variants": ["example-user", "example-user"], "sites": ["GitHub"]})
        self.assertEqual(status, 202)
        job = json.loads(raw)
        self.assertEqual(job["usernames"], ["Example_User", "example-user"])
        self.assertEqual(job["total"], 2)
        for names in (["bad/name"], ["fixture"] * 6, "fixture", [1]):
            self.assertEqual(self.request("POST", "/api/jobs", {"username": "fixture", "variants": names})[0], 400)

    def test_dedup_review_export_and_reload(self):
        ident = self.create()
        job = app.JOBS[ident]
        job["rows"] = [
            {"site": "GitHub", "url": "https://www.github.com/Fixture/", "status": "Claimed", "username": "Fixture", "checked_at": "2026-01-01"},
            {"site": "GitHub", "url": "https://github.com/fixture?utm_source=test#bio", "status": "Claimed", "username": "fixture", "checked_at": "2026-01-02"},
            {"site": "Other", "url": "https://example.invalid/Fixture", "status": "Available"}]
        self.assertEqual(app.summary(job)["checked"], 3)
        self.assertEqual(app.summary(job)["found"], 1)
        merged = accounts(job)
        self.assertEqual(len(merged[0]["sources"]), 2)
        key = merged[0]["key"]
        self.assertEqual(merged[0]["review"], "pending")
        status, _, _ = self.request("POST", f"/api/jobs/{ident}/review", {"key": key, "state": "confirmed"})
        self.assertEqual(status, 200)
        self.assertEqual(accounts(job)[0]["review"], "confirmed")
        self.assertIn("已人工核实", app.export(job, "csv")[0].decode("utf-8-sig"))
        self.assertIn("Fixture, fixture", app.export(job, "txt")[0].decode())
        app.JOBS.clear()
        app.load()
        self.assertEqual(accounts(app.JOBS[ident])[0]["review"], "confirmed")
        for key, state in (("../../secret", "confirmed"), (merged[0]["key"], "invented"), ({}, "pending")):
            self.assertEqual(self.request("POST", f"/api/jobs/{ident}/review", {"key": key, "state": state})[0], 400)

    def test_public_link_evidence_is_bounded_and_never_identity_proof(self):
        definitions = {"GitHub": {"url": "https://github.com/{}"}}
        html = '''<a rel="me" href="https://example.invalid/about">personal</a>
          <a href="https://github.com/other-user">profile</a>
          <a href="https://github.com/other-user#bio">duplicate</a>
          <a href="https://github.com/login">navigation</a>
          <a rel="me" href="javascript:alert(1)">bad</a>
          <a rel="me" href="https://user:pass@example.invalid">credentials</a>
          <a href="https://unrelated.invalid/about">ordinary unrelated link</a>
          <nav><a href="https://github.com/nav-user">navigation profile</a></nav>
          <script>fetch('https://example.invalid/')</script>'''
        links = public_links("https://github.com/fixture-user", html, definitions, "fixture-user")
        self.assertEqual(len(links), 2)
        self.assertTrue(all(link["review"] == "pending" for link in links))
        self.assertTrue(all(link["from_url"] == "https://github.com/fixture-user" for link in links))
        self.assertIn("rel=me", links[0]["evidence"])
        self.assertIn("他人账号", links[1]["evidence"])
        more = ''.join(f'<a rel="me" href="https://example.invalid/{i}">x</a>' for i in range(1000))
        self.assertEqual(len(public_links("https://github.com/fixture", more, definitions, "fixture")), 20)
        self.assertNotEqual(canonical_url("https://example.invalid/Case"), canonical_url("https://example.invalid/case"))
        declared = public_links("https://github.com/fixture", '<a itemprop="url" href="https://example.invalid">website</a>', definitions, "fixture")
        self.assertEqual(len(declared), 1)
        self.assertIn("itemprop=url", declared[0]["evidence"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
