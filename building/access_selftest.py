r"""Tests for sign-in: the signed list of approved users, and the app's sign-in gate.

    python -m building.access_selftest
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from building import access

TODAY = dt.date(2026, 10, 14)


class Signatures(unittest.TestCase):
    def test_rfc8032_test_vectors(self):
        for sk, pk, msg, sig in (
                ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
                 "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
                 "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e3970"
                 "1cf9b46bd25bf5f0595bbe24655141438e7a100b"),
                ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
                 "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
                 "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613"
                 "d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")):
            sk, msg = bytes.fromhex(sk), bytes.fromhex(msg)
            self.assertEqual(access.public_key(sk).hex(), pk)
            self.assertEqual(access.sign(sk, msg).hex(), sig)
            self.assertTrue(access.verify(bytes.fromhex(pk), msg, bytes.fromhex(sig)))
            self.assertFalse(access.verify(bytes.fromhex(pk), msg + b"!", bytes.fromhex(sig)))


class TheList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.secret = bytes(range(32))
        self.cfg = {"public_key": access.public_key(self.secret).hex(), "source": os.path.join(self.tmp, "share")}
        os.makedirs(self.cfg["source"])
        self._cache = access.CACHE_DIR
        access.CACHE_DIR = os.path.join(self.tmp, "cache")
        self.publish([{"email": "a@office.com", "machines": ["AAAA-1111"], "expires": "2027-01-01", "status": "active",
                       "plan": "office (free)"},
                      {"email": "b@office.com", "machines": ["ANY"], "expires": "2026-10-01", "status": "active"},
                      {"email": "c@office.com", "machines": ["ANY"], "expires": "", "status": "removed"}])

    def tearDown(self):
        access.CACHE_DIR = self._cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, users, secret=None):
        with open(os.path.join(self.cfg["source"], "access.json"), "w") as f:
            json.dump(access.signed({"users": users}, secret or self.secret), f)

    def check(self, email, pc="AAAA-1111", today=TODAY):
        return access.check(email, self.cfg, today, pc)

    def test_an_approved_email_on_its_pc_gets_in(self):
        r = self.check("A@Office.com ")
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["plan"], r["expires"]), ("office (free)", "2027-01-01"))

    def test_not_on_the_list_leaves_a_request_for_the_admin(self):
        r = self.check("x@office.com", pc="BBBB-2222")
        self.assertFalse(r["ok"])
        self.assertIn("BBBB-2222", r["message"])
        req = os.listdir(os.path.join(self.cfg["source"], "requests"))
        self.assertEqual(len(req), 1)

    def test_another_pc_expired_or_removed_is_refused(self):
        self.assertIn("not on this PC", self.check("a@office.com", pc="CCCC-3333")["message"])
        self.assertIn("ended on 2026-10-01", self.check("b@office.com")["message"])
        self.assertFalse(self.check("c@office.com")["ok"])
        self.assertTrue(self.check("b@office.com", today=dt.date(2026, 9, 30))["ok"])     # 'any' PC

    def test_a_list_edited_by_hand_or_signed_by_another_key_is_refused(self):
        path = os.path.join(self.cfg["source"], "access.json")
        with open(path) as f:
            doc = json.load(f)
        doc["payload"]["users"].append({"email": "me@x.com", "machines": ["ANY"], "status": "active"})
        with open(path, "w") as f:
            json.dump(doc, f)
        r = self.check("me@x.com")
        self.assertFalse(r["ok"])
        self.assertIn("not signed by the admin", r["message"])
        self.publish([{"email": "me@x.com", "machines": ["ANY"], "status": "active"}], secret=bytes(32))
        self.assertFalse(self.check("me@x.com")["ok"])

    def test_offline_the_last_good_list_serves_for_a_week_then_stops(self):
        self.assertTrue(self.check("a@office.com")["ok"])          # read once: cached
        os.remove(os.path.join(self.cfg["source"], "access.json"))
        self.assertTrue(self.check("a@office.com", today=TODAY + dt.timedelta(days=7))["ok"])
        r = self.check("a@office.com", today=TODAY + dt.timedelta(days=8))
        self.assertFalse(r["ok"])
        self.assertIn("could not be read for 8 days", r["message"])

    def test_without_a_config_no_sign_in_is_asked(self):
        old = access.CONFIG
        access.CONFIG = os.path.join(self.tmp, "no_such_config.json")
        try:
            self.assertFalse(access.enabled())
            self.assertTrue(access.check("", None, TODAY, "X")["ok"])
        finally:
            access.CONFIG = old


class TheGate(unittest.TestCase):
    """The app with sign-in on: the sign-in page first, nothing else until signed in."""

    def setUp(self):
        import importlib
        from http.server import ThreadingHTTPServer
        import webapp
        self.webapp = importlib.reload(webapp)
        self.tmp = tempfile.mkdtemp()
        secret = bytes(range(32))
        share = os.path.join(self.tmp, "share")
        os.makedirs(share)
        pc = access.machine_code()
        with open(os.path.join(share, "access.json"), "w") as f:
            json.dump(access.signed({"users": [{"email": "a@office.com", "machines": [pc], "status": "active",
                                                 "expires": "2099-01-01"}]}, secret), f)
        cfgp = os.path.join(self.tmp, "access_config.json")
        with open(cfgp, "w") as f:
            json.dump({"public_key": access.public_key(secret).hex(), "source": share}, f)
        self._cfg, self._cache = access.CONFIG, access.CACHE_DIR
        access.CONFIG, access.CACHE_DIR = cfgp, os.path.join(self.tmp, "cache")
        self.webapp.H.require_login = True
        port = self.webapp.free_port(8970)
        self.srv = ThreadingHTTPServer(("127.0.0.1", port), self.webapp.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.webapp.H.require_login = False
        access.CONFIG, access.CACHE_DIR = self._cfg, self._cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, path, body=None, cookie=""):
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   headers={"Cookie": cookie, "Content-Type": "application/json"},
                                   method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(r) as x:
                return x.status, x.read().decode(), x.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), ""

    def test_sign_in_first_then_the_app(self):
        code, body, _ = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("Your e-mail", body)
        self.assertEqual(self.req("/api/state")[0], 401)
        self.assertEqual(self.req("/api/new", {"path": self.tmp})[0], 401)
        code, body, _ = self.req("/api/login", {"email": "z@office.com"})
        self.assertEqual(code, 403)
        code, body, cookie = self.req("/api/login", {"email": "a@office.com"})
        self.assertEqual(code, 200, body)
        tok = cookie.split(";")[0]
        self.assertEqual(self.req("/api/state", cookie=tok)[0], 200)
        self.assertIn("Structural drawings to Revit", self.req("/", cookie=tok)[1])
        self.assertTrue(json.loads(self.req("/api/access", cookie=tok)[1])["signed_in"])
        self.req("/api/logout", {}, cookie=tok)
        self.assertEqual(self.req("/api/state", cookie=tok)[0], 401)


class Admins(TheGate):
    """An admin manages users inside the app; a user cannot; nothing without the signing key."""

    def setUp(self):
        super().setUp()
        import sys
        sys.path.insert(0, os.path.join(access.ROOT, "tools"))
        import access_admin
        self.tool = access_admin
        self._dirs = (access_admin.ADMIN_DIR, access_admin.KEY, access_admin.USERS)
        adm = os.path.join(self.tmp, "admin")
        access_admin.ADMIN_DIR = adm
        access_admin.KEY, access_admin.USERS = os.path.join(adm, "signing_key.json"), os.path.join(adm, "users.json")
        secret = bytes(range(32))
        share = os.path.join(self.tmp, "share")
        pc = access.machine_code()
        users = [{"email": "a@office.com", "machines": [pc], "status": "active", "expires": "2099-01-01", "role": "user"},
                 {"email": "boss@office.com", "machines": ["ANY"], "status": "active", "expires": "2099-01-01",
                  "role": "admin"}]
        access_admin._write(access_admin.KEY, {"secret": secret.hex(), "public": access.public_key(secret).hex()})
        access_admin._write(access_admin.USERS, {"users": users, "source": share})
        access_admin.publish.__globals__["print"] = lambda *a, **k: None
        with open(os.path.join(share, "access.json"), "w") as f:
            json.dump(access.signed({"users": users}, secret), f)

    def tearDown(self):
        self.tool.ADMIN_DIR, self.tool.KEY, self.tool.USERS = self._dirs
        self.tool.publish.__globals__.pop("print", None)
        super().tearDown()

    def login(self, email):
        return self.req("/api/login", {"email": email})[2].split(";")[0]

    def test_an_admin_adds_and_removes_users_a_user_cannot(self):
        user, boss = self.login("a@office.com"), self.login("boss@office.com")
        self.assertEqual(self.req("/api/admin", cookie=user)[0], 403)
        self.assertEqual(self.req("/api/admin", {"action": "add", "email": "x@y.com"}, cookie=user)[0], 403)
        self.assertFalse(json.loads(self.req("/api/access", cookie=user)[1])["admin"])
        self.assertTrue(json.loads(self.req("/api/access", cookie=boss)[1])["admin"])
        code, body, _ = self.req("/api/admin", {"action": "add", "email": "new@office.com", "pc": "any"}, cookie=boss)
        self.assertEqual(code, 200, body)
        self.assertIn("new@office.com", [u["email"] for u in json.loads(body)["users"]])
        self.assertTrue(access.check("new@office.com", access.config(), pc="ZZZZ-0000")["ok"])   # signed, published
        self.assertEqual(self.req("/api/admin", {"action": "remove", "email": "boss@office.com"}, cookie=boss)[0], 400)
        self.assertEqual(self.req("/api/admin", {"action": "remove", "email": "new@office.com"}, cookie=boss)[0], 200)
        self.assertFalse(access.check("new@office.com", access.config(), pc="ZZZZ-0000")["ok"])
        os.remove(self.tool.KEY)                                    # a PC without the admin's key:
        self.assertEqual(self.req("/api/admin", cookie=boss)[0], 409)
        self.assertFalse(json.loads(self.req("/api/access", cookie=boss)[1])["admin"])   # no Users button


if __name__ == "__main__":
    unittest.main(verbosity=1)
