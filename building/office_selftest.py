r"""Tests for the office mode: colleagues use the app on the admin's PC through their browser.

    python -m building.office_selftest
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from building import access, office


class Office(unittest.TestCase):
    """A server in the office mode; every browser counts as a colleague's unless LOCAL says so."""
    local = ()

    def setUp(self):
        import webapp
        self.webapp = importlib.reload(webapp)
        self.tmp = tempfile.mkdtemp()
        secret = bytes(range(32))
        self.share = os.path.join(self.tmp, "share")
        os.makedirs(self.share)
        self.secret = secret
        self.publish([{"email": "a@office.com", "machines": [access.machine_code()], "status": "active",
                       "expires": "2099-01-01", "role": "user"},
                      {"email": "boss@office.com", "machines": ["ANY"], "status": "active", "expires": "2099-01-01",
                       "role": "admin"}])
        cfgp = os.path.join(self.tmp, "access_config.json")
        with open(cfgp, "w") as f:
            json.dump({"public_key": access.public_key(secret).hex(), "source": self.share}, f)
        self._saved = (access.CONFIG, access.CACHE_DIR, office.ADMIN_DIR, office.OFFICE_HOME, office.send_later,
                       office.mail_config)
        access.CONFIG, access.CACHE_DIR = cfgp, os.path.join(self.tmp, "cache")
        office.ADMIN_DIR, office.OFFICE_HOME = os.path.join(self.tmp, "admin"), os.path.join(self.tmp, "office")
        self.mails = []
        office.send_later = lambda to, subject, text, log=print: self.mails.append((to, subject, text))
        office.mail_config = lambda: {"user": "app@office.com", "password": "x", "admin_to": "boss@office.com"}
        office._FAILS.clear()
        office._LAST_SENT.clear()
        H = self.webapp.H
        H.require_login, H.office, H.LOCAL = True, True, self.local
        port = self.webapp.free_port(9010)
        H.office_url = f"http://127.0.0.1:{port}/"
        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"
        self.home_project = self.webapp.HOME.project

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        H = self.webapp.H
        H.require_login, H.office, H.LOCAL, H.office_url = False, False, ("127.0.0.1", "::1"), ""
        (access.CONFIG, access.CACHE_DIR, office.ADMIN_DIR, office.OFFICE_HOME, office.send_later,
         office.mail_config) = self._saved
        self.webapp.SESSIONS.clear()
        self.webapp.OFFICE_STATES.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, users):
        with open(os.path.join(self.share, "access.json"), "w") as f:
            json.dump(access.signed({"users": users}, self.secret), f)

    def req(self, path, body=None, cookie="", raw=None, origin=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        h = {"Cookie": cookie, "Content-Type": "application/octet-stream" if raw is not None else "application/json"}
        if origin:
            h["Origin"] = origin
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(r) as x:
                return x.status, x.read().decode("utf-8", "replace"), x.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), ""

    @staticmethod
    def who(email, user=None):
        return user if user is not None else email.split("@")[0].title()

    def link(self, email, user=None):
        """'Set or reset my password' for a user name: the token from the link in the e-mail."""
        self.mails.clear()
        office._LAST_SENT.clear()
        code, body, _ = self.req("/api/password/forgot", {"email": email, "user": self.who(email, user)})
        self.assertEqual(code, 200, body)
        to, _subject, text = self.mails[-1]
        self.assertEqual(to, email)
        return re.search(r"/reset\?t=([\w-]+)", text).group(1)

    def password(self, email, pw="good password 1", user=None):
        """The link from the e-mail, then the new password."""
        tok = self.link(email, user)
        code, body, _ = self.req("/api/password/set", {"token": tok, "password": pw})
        self.assertEqual(code, 200, body)
        return tok, json.loads(body)

    def login(self, email, pw="good password 1", user=None):
        code, body, cookie = self.req("/api/login", {"email": email, "user": self.who(email, user), "password": pw})
        return code, body, cookie.split(";")[0]


class SignIn(Office):
    def test_the_password_page_first_and_nothing_else(self):
        code, body, _ = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("Set or reset my password", body)
        self.assertEqual(self.req("/api/state")[0], 401)
        self.assertEqual(self.req("/api/upload?name=a.dxf", raw=b"0")[0], 401)
        self.assertEqual(json.loads(self.req("/api/access")[1])["pc"], "")      # no PC code over the network

    def test_a_password_set_by_the_link_then_sign_in(self):
        self.assertEqual(self.login("a@office.com")[0], 403)                     # none set yet
        tok, j = self.password("a@office.com")
        self.assertTrue(j["approved"])
        self.assertEqual(self.req("/api/password/set", {"token": tok, "password": "another one 2"})[0], 400)  # once
        self.assertEqual(self.login("a@office.com", "wrong password")[0], 403)
        code, body, tok = self.login("a@office.com")
        self.assertEqual(code, 200, body)
        self.assertEqual(self.req("/api/state", cookie=tok)[0], 200)
        a = json.loads(self.req("/api/access", cookie=tok)[1])
        self.assertTrue(a["remote"] and a["signed_in"])

    def test_short_passwords_and_old_links_are_refused(self):
        tok = self.link("a@office.com")
        self.assertEqual(self.req("/api/password/set", {"token": tok, "password": "short"})[0], 400)
        self.link("a@office.com")                                                   # a newer link
        self.assertEqual(self.req("/api/password/set", {"token": tok, "password": "long enough 1"})[0], 400)

    def test_five_wrong_passwords_pause_the_sign_in(self):
        self.password("a@office.com")
        for _ in range(office.MAX_TRIES):
            self.login("a@office.com", "wrong password")
        code, body, _ = self.login("a@office.com")
        self.assertEqual(code, 403)
        self.assertIn("Too many", body)

    def test_not_approved_asks_the_admin_once_by_e_mail(self):
        _tok, j = self.password("new@office.com")
        self.assertFalse(j["approved"])
        self.assertTrue(os.listdir(os.path.join(self.share, "requests")))
        to_admin = [m for m in self.mails if m[0] == "boss@office.com"]
        self.assertEqual(len(to_admin), 1)
        self.assertIn("new@office.com", to_admin[0][1])
        code, body, _ = self.login("new@office.com")
        self.assertEqual(code, 403)
        self.assertIn("not approved", body)
        self.assertEqual(len([m for m in self.mails if m[0] == "boss@office.com"]), 1)

    def restart(self):
        """The app closed and started again: sign-ins come back from the file."""
        self.webapp.SESSIONS.clear()
        self.webapp._sessions_loaded[0] = False

    def test_signed_in_until_sign_out_even_after_a_restart(self):
        self.password("a@office.com")
        code, _body, cookie = self.req("/api/login", {"email": "a@office.com", "user": "A",
                                                      "password": "good password 1"})
        self.assertIn("Max-Age=", cookie)                                 # kept when the browser closes
        tok = cookie.split(";")[0]
        self.restart()
        self.assertEqual(self.req("/api/state", cookie=tok)[0], 200)
        with open(self.webapp._sessions_file()) as f:
            self.assertNotIn(tok.split("=", 1)[1], f.read())             # only a hash of it on disk
        self.req("/api/logout", {}, cookie=tok)
        self.restart()
        self.assertEqual(self.req("/api/state", cookie=tok)[0], 401)

    def test_a_removed_user_is_signed_out(self):
        self.password("a@office.com")
        tok = self.login("a@office.com")[2]
        self.assertEqual(self.req("/api/state", cookie=tok)[0], 200)
        self.publish([{"email": "boss@office.com", "machines": ["ANY"], "status": "active", "role": "admin"}])
        old, self.webapp.RECHECK_SECONDS = self.webapp.RECHECK_SECONDS, -1
        try:
            self.assertEqual(self.req("/api/state", cookie=tok)[0], 401)
        finally:
            self.webapp.RECHECK_SECONDS = old
        self.assertFalse(os.path.isdir(os.path.join(self.share, "requests")))   # no request left by the check

    def test_users_page_never_over_the_network(self):
        self.password("boss@office.com")
        tok = self.login("boss@office.com")[2]
        self.assertFalse(json.loads(self.req("/api/access", cookie=tok)[1])["admin"])
        self.assertEqual(self.req("/api/admin", cookie=tok)[0], 403)
        self.assertEqual(self.req("/api/admin", {"action": "add", "email": "x@y.com"}, cookie=tok)[0], 403)

    def test_another_website_cannot_make_changes(self):
        self.password("a@office.com")
        tok = self.login("a@office.com")[2]
        host = self.base[len("http://"):]
        self.assertEqual(self.req("/api/pick", {"kind": "new", "name": "J"}, cookie=tok,
                                  origin=f"http://{host}")[0], 200)
        self.assertEqual(self.req("/api/pick", {"kind": "new", "name": "J"}, cookie=tok,
                                  origin="http://evil.example")[0], 403)


class Projects(Office):
    def signed(self, email):
        self.password(email)
        return self.login(email)[2]

    def test_each_colleague_has_their_own_projects_and_this_pc_keeps_its_own(self):
        a, b = self.signed("a@office.com"), self.signed("boss@office.com")
        ja = json.loads(self.req("/api/pick", {"kind": "new", "name": "Job A"}, cookie=a)[1])
        jb = json.loads(self.req("/api/pick", {"kind": "new", "name": "Job B"}, cookie=b)[1])
        self.assertTrue(office.inside(ja["project"], office.home_of("a@office.com", "A")))
        self.assertTrue(office.inside(jb["project"], office.home_of("boss@office.com", "Boss")))
        self.assertEqual(json.loads(self.req("/api/state", cookie=a)[1])["name"], "Job A")
        self.assertEqual(self.req("/api/recent", {"path": jb["project"]}, cookie=a)[0], 403)   # not theirs
        self.assertEqual(self.webapp.HOME.project, self.home_project)          # this PC's own project untouched

    def test_drawings_are_uploaded_and_nothing_opens_on_this_pc(self):
        a = self.signed("a@office.com")
        self.assertEqual(self.req("/api/upload?name=plan.dxf", raw=b"0\nEOF\n", cookie=a)[0], 400)   # no project
        self.req("/api/pick", {"kind": "new", "name": "Job"}, cookie=a)
        code, body, _ = self.req("/api/upload?name=..%5C..%5Cplan.dxf", raw=b"0\nEOF\n", cookie=a)
        self.assertEqual(code, 200, body)
        f = json.loads(body)["files"]
        self.assertEqual([x["name"] for x in f], ["plan.dxf"])
        self.assertTrue(office.inside(f[0]["path"], office.home_of("a@office.com", "A")))
        self.assertEqual(self.req("/api/upload?name=notes.txt", raw=b"hello", cookie=a)[0], 400)
        for kind in ("files", "folder", "open", "export"):
            self.assertEqual(self.req("/api/pick", {"kind": kind}, cookie=a)[0], 400)
        self.assertEqual(self.req("/api/open", {"what": "project"}, cookie=a)[0], 400)


class SharedEmail(Office):
    """Several people on one approved e-mail: each user name has its own password and folder."""

    def test_a_user_name_is_needed_to_sign_in_and_to_set_a_password(self):
        self.password("a@office.com", user="Priya Sharma")
        code, body, _ = self.req("/api/login", {"email": "a@office.com", "password": "good password 1"})
        self.assertEqual(code, 403)
        self.assertIn("user name", json.loads(body)["error"])
        self.assertEqual(self.req("/api/password/forgot", {"email": "a@office.com"})[0], 400)
        self.assertEqual(self.req("/api/password/forgot", {"email": "a@office.com", "user": "../../x"})[0], 400)
        self.assertEqual(self.login("a@office.com", user="  priya   SHARMA ")[0], 200)   # capitals, spaces

    def test_a_reset_changes_only_that_users_password(self):
        self.password("a@office.com", "priya password", user="Priya")
        self.password("a@office.com", "ravi password", user="Ravi")
        self.password("a@office.com", "priya new password", user="Priya")          # Priya resets hers
        self.assertEqual(self.login("a@office.com", "ravi password", user="Ravi")[0], 200)
        self.assertEqual(self.login("a@office.com", "priya new password", user="Priya")[0], 200)
        self.assertEqual(self.login("a@office.com", "priya password", user="Priya")[0], 403)
        self.assertEqual(self.login("a@office.com", "ravi password", user="Priya")[0], 403)

    def test_each_user_name_has_its_own_folder_and_projects(self):
        self.password("a@office.com", user="Priya Sharma")
        self.password("a@office.com", user="Ravi")
        p, r = self.login("a@office.com", user="Priya Sharma")[2], self.login("a@office.com", user="Ravi")[2]
        jp = json.loads(self.req("/api/pick", {"kind": "new", "name": "Job P"}, cookie=p)[1])
        jr = json.loads(self.req("/api/pick", {"kind": "new", "name": "Job R"}, cookie=r)[1])
        self.assertEqual(jp["project"], os.path.join(office.OFFICE_HOME, "Priya Sharma", "Job P"))
        self.assertEqual(jr["project"], os.path.join(office.OFFICE_HOME, "Ravi", "Job R"))
        self.assertEqual(self.req("/api/recent", {"path": jr["project"]}, cookie=p)[0], 403)   # not hers
        self.assertEqual(json.loads(self.req("/api/access", cookie=p)[1])["user"], "Priya Sharma")
        self.assertFalse(os.path.exists(os.path.join(office.OFFICE_HOME, "a@office.com")))

    def test_the_e_mail_says_whose_password_and_the_page_shows_it(self):
        tok = self.link("a@office.com", "Priya")
        self.assertIn("Priya", self.mails[-1][1])
        j = json.loads(self.req("/api/password/name", {"token": tok})[1])
        self.assertEqual((j["user"], j["email"], j["new"]), ("Priya", "a@office.com", True))
        self.assertEqual(self.req("/api/password/set", {"token": tok, "password": "good password 1"})[0], 200)
        tok = self.link("a@office.com", "priya")
        self.assertEqual(json.loads(self.req("/api/password/name", {"token": tok})[1])["new"], False)

    def test_one_users_link_does_not_spoil_anothers(self):
        tok_p = self.link("a@office.com", "Priya")
        self.link("a@office.com", "Ravi")                            # sent straight after, same mailbox
        self.assertEqual(self.req("/api/password/set", {"token": tok_p, "password": "good password 1"})[0], 200)

    def test_two_users_with_one_name_never_share_a_folder(self):
        self.password("a@office.com", user="Ravi")
        self.password("boss@office.com", user="ravi")
        self.assertEqual(os.path.basename(office.home_of("a@office.com", "Ravi")), "Ravi")
        self.assertEqual(os.path.basename(office.home_of("boss@office.com", "ravi")), "ravi (2)")

    def test_each_users_list_holds_only_their_own_projects(self):
        old = os.path.join(office.OFFICE_HOME, "a@office.com")         # made on the shared login before
        for job in ("Job 1", "Job 2"):
            os.makedirs(os.path.join(old, job))
            with open(os.path.join(old, job, "c2r_project.json"), "w") as f:
                json.dump({}, f)
        self.password("a@office.com", user="Priya")
        self.password("a@office.com", user="Ravi")
        p, r = self.login("a@office.com", user="Priya")[2], self.login("a@office.com", user="Ravi")[2]
        self.req("/api/pick", {"kind": "new", "name": "Priya job"}, cookie=p)
        recent = lambda c: [x["name"] for x in json.loads(self.req("/api/state", cookie=c)[1])["recent"]]
        self.assertEqual(recent(p), ["Priya job"])
        self.assertEqual(recent(r), [])                                # nothing of Priya's, nothing shared
        self.assertTrue(os.path.isdir(os.path.join(old, "Job 1")))     # the earlier projects are not moved

    def test_an_earlier_shared_project_handed_to_one_user_opens_for_them(self):
        old = os.path.join(office.OFFICE_HOME, "a@office.com", "Job 1")
        os.makedirs(old)
        with open(os.path.join(old, "c2r_project.json"), "w") as f:
            json.dump({}, f)
        self.password("a@office.com", user="Priya")
        os.makedirs(office.home_of("a@office.com", "Priya"))
        with open(os.path.join(office.home_of("a@office.com", "Priya"), "recent_projects.json"), "w") as f:
            json.dump({"project": "", "recent": [old]}, f)            # the admin hands Job 1 to Priya
        p = self.login("a@office.com", user="Priya")[2]
        self.assertEqual([x["path"] for x in json.loads(self.req("/api/state", cookie=p)[1])["recent"]], [old])
        self.assertEqual(self.req("/api/recent", {"path": old}, cookie=p)[0], 200)

    def test_a_sign_in_from_before_user_names_signs_in_again(self):
        self.password("a@office.com")
        tok = self.login("a@office.com")[2]
        for s in self.webapp.SESSIONS.values():
            s.pop("user", None)                                   # as made by the app before user names
        self.assertEqual(self.req("/api/state", cookie=tok)[0], 401)

    def test_the_admin_sees_the_user_name(self):
        self.password("new@office.com", user="Anita Rao")
        to_admin = [m for m in self.mails if m[0] == "boss@office.com"]
        self.assertIn("Anita Rao (new@office.com)", to_admin[0][1])


class ThisPC(Office):
    """In the office mode the admin's own browser is served exactly as by run_app.bat."""
    local = ("127.0.0.1", "::1")

    def test_this_pc_signs_in_as_before_and_sees_users(self):
        self.assertIn("Your e-mail", self.req("/")[1])
        self.assertNotIn("Set or reset", self.req("/")[1])
        code, body, cookie = self.req("/api/login", {"email": "a@office.com"})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.req("/api/password/forgot", {"email": "a@office.com"})[0], 404)
        self.assertEqual(self.req("/api/upload?name=a.dxf", raw=b"0")[0], 404)
        self.assertFalse(json.loads(self.req("/api/access", cookie=cookie.split(";")[0])[1])["remote"])


class Passwords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._dir = office.ADMIN_DIR
        office.ADMIN_DIR = self.tmp

    def tearDown(self):
        office.ADMIN_DIR = self._dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_a_salted_hash_is_kept(self):
        office.set_password("A@x.com", "my secret pass", "Priya")
        with open(os.path.join(self.tmp, "passwords.json")) as f:
            text = f.read()
        self.assertNotIn("my secret pass", text)
        self.assertEqual(office.check_password("a@x.com", "my secret pass", "priya"), "")
        self.assertNotEqual(office.check_password("a@x.com", "My secret pass", "Priya"), "")

    def test_names(self):
        self.assertEqual(office.clean_name("  Priya   S. "), "Priya S")
        self.assertEqual(office.clean_name("José O'Neil-Rao"), "José O'Neil-Rao")
        for bad in ("", "   ", "a/b", "x\\y", "C:", "what?", "<b>", "12345", "x" * 61):
            self.assertRaises(ValueError, office.clean_name, bad)
        office.set_password("a@x.com", "my secret pass", "Priya")
        office.set_password("a@x.com", "another pass 2", "PRIYA")    # the same user: only the password changes
        self.assertEqual(office.names_of("A@x.com"), ["Priya"])

    def test_passwords_from_before_user_names_carry_over(self):
        salt = bytes(16)
        old = {"x@y.com": {"salt": salt.hex(), "hash": office._hash("vaibhav pass 1", salt), "set": "2026-10-21",
                           "name": "Vaibhav D"},
               "z@y.com": {"salt": salt.hex(), "hash": office._hash("no name pass", salt), "set": "2026-10-14"}}
        with open(os.path.join(self.tmp, "passwords.json"), "w") as f:
            json.dump(old, f)
        self.assertEqual(office.check_password("x@y.com", "vaibhav pass 1", "Vaibhav D"), "")
        self.assertEqual(office.names_of("x@y.com"), ["Vaibhav D"])
        self.assertNotEqual(office.check_password("z@y.com", "no name pass", "Z"), "")   # no name: a new link
        with open(os.path.join(self.tmp, "passwords.json")) as f:
            self.assertEqual(json.load(f)["z@y.com"]["hash"], old["z@y.com"]["hash"])    # kept, not lost


if __name__ == "__main__":
    unittest.main(verbosity=1)
