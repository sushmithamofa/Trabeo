r"""Office mode: colleagues use the app on the admin's PC through their own browser.

    run_office.bat   (python webapp.py --office)

Nothing of the app is copied to anybody: it runs on this PC only, and the office
network reaches it at http://<this PC's name>:8765. Each colleague:

  * signs in with their approved e-mail and a password. The first time, or when the
    password is forgotten, "Set or reset my password" e-mails them a link (valid
    RESET_MINUTES) to choose one - which also proves the address is theirs;
  * an address not approved yet leaves a request; the admin gets an e-mail, approves it
    on the Users page (on this PC only), and the colleague gets an e-mail back;
  * signs in with e-mail + user name + password. Several people can share one approved
    e-mail: each user name has its own password (a reset changes only that one) and its
    own projects in a folder named after it (OFFICE_HOME\<user name>). A new user name is
    made with "Set or reset my password" (the link goes to the e-mail). They upload drawings
    and download the results; nothing opens on this PC's screen.

The passwords (salted PBKDF2 hashes, never the passwords) and the e-mail account the
app sends from are kept in the admin's own folder (tools\access_admin.ADMIN_DIR),
never in the app folder. The e-mail account is set with admin_access.bat, choice 9.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

ADMIN_DIR = os.environ.get("C2R_ADMIN_DIR") or os.path.join(os.path.expanduser("~"), ".c2r_admin")
OFFICE_HOME = os.environ.get("C2R_OFFICE_HOME") or os.path.join(os.path.expanduser("~"), "Documents",
                                                                 "CAD to Revit office")
RESET_MINUTES = 30
MIN_PASSWORD = 8
MAX_TRIES = 5                  # wrong passwords in a row, then a pause of LOCK_MINUTES
LOCK_MINUTES = 15
_ITER = 200_000
_lock = threading.Lock()


def _paths():
    return os.path.join(ADMIN_DIR, "passwords.json"), os.path.join(ADMIN_DIR, "mail.json")


def _read(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- passwords
def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITER).hex()


# One approved e-mail can be shared by several people: each has their own user name, with
# their own password and their own folder, so one person's reset never changes another's.
#   passwords.json: {e-mail: {"users": {user key: {"name", "salt", "hash", "set", "folder"}}}}
# A password from before user names ({e-mail: {"salt", "hash", "name"}}) becomes that name's
# user on first read; one without a name is kept but no longer signs in.

MAX_NAME = 60
_NAME_OK = re.compile(r"^[^\W_](?:[\w .'-]*[^\W_])?$")       # letters/digits, inner spaces . ' - _
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def clean_name(name: str) -> str:
    """A user name as typed, tidied; ValueError when it cannot name a folder."""
    name = re.sub(r"\s+", " ", (name or "")).strip(" .'-_")      # a folder name cannot end in a dot
    if not name:
        raise ValueError("Type your user name.")
    if len(name) > MAX_NAME:
        raise ValueError(f"Keep your user name to {MAX_NAME} characters.")
    if not _NAME_OK.match(name) or not re.search(r"[^\W\d_]", name):
        raise ValueError("Use letters, numbers and spaces in your user name (no / \\ : * ? \" < > |).")
    return name


def user_key(name: str) -> str:
    """The same user whatever the capitals or spaces typed ("priya  sharma" = "Priya Sharma")."""
    return clean_name(name).lower()


def _load() -> dict:
    """passwords.json, with any password from before user names moved under its name. Call with _lock."""
    d = _read(_paths()[0], {})
    changed = False
    for email, rec in d.items():
        if rec.get("hash") and rec.get("name") and "users" not in rec:
            try:
                k = user_key(rec["name"])
            except ValueError:
                continue
            rec["users"] = {k: {f: rec.pop(f) for f in ("name", "salt", "hash", "set", "folder") if f in rec}}
            changed = True
    if changed:
        _write(_paths()[0], d)
    return d


def _user(email: str, user: str) -> dict:
    with _lock:
        rec = _load().get(email.strip().lower()) or {}
    try:
        return (rec.get("users") or {}).get(user_key(user)) or {}
    except ValueError:
        return {}


def has_password(email: str, user: str) -> bool:
    return bool(_user(email, user))


def user_name(email: str, user: str) -> str:
    """The user name as its owner first typed it ("" when there is no such user)."""
    return _user(email, user).get("name", "")


def names_of(email: str) -> list[str]:
    """Everybody signing in with this e-mail."""
    with _lock:
        rec = _load().get(email.strip().lower()) or {}
    return sorted((u["name"] for u in (rec.get("users") or {}).values()), key=str.lower)


def name_of(email: str) -> str:
    return ", ".join(names_of(email))


def set_password(email: str, password: str, user: str) -> None:
    """Save the password of one user name of this e-mail (a new user name is a new user)."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Choose a password of at least {MIN_PASSWORD} characters.")
    name = clean_name(user)
    salt = secrets.token_bytes(16)
    email = email.strip().lower()
    with _lock:
        d = _load()
        rec = d.setdefault(email, {})
        users = rec.setdefault("users", {})
        u = users.setdefault(name.lower(), {"name": name})      # the folder stays when the password changes
        u.update({"salt": salt.hex(), "hash": _hash(password, salt),
                  "set": _dt.datetime.now().isoformat(timespec="seconds")})
        _write(_paths()[0], d)
    _FAILS.pop(_fail_key(email, name), None)


_FAILS: dict[str, list] = {}      # e-mail|user -> [wrong tries in a row, locked until]


def _fail_key(email: str, user: str) -> str:
    try:
        return email.strip().lower() + "|" + user_key(user)
    except ValueError:
        return email.strip().lower() + "|"


def check_password(email: str, password: str, user: str) -> str:
    """"" when right, else why not (wrong, none set yet, or paused after too many tries)."""
    try:
        clean_name(user)
    except ValueError as exc:
        return str(exc)
    key = _fail_key(email, user)
    f = _FAILS.get(key, [0, 0.0])
    if f[1] > time.time():
        return (f"Too many wrong passwords. Try again in {int((f[1] - time.time()) // 60) + 1} minutes, "
                "or reset your password.")
    rec = _user(email, user)
    if not rec:
        return ("This user name has no password yet. Press \"Set or reset my password\" below: "
                "a link comes to your e-mail.")
    if hmac.compare_digest(_hash(password, bytes.fromhex(rec["salt"])), rec["hash"]):
        _FAILS.pop(key, None)
        return ""
    f[0] += 1
    if f[0] >= MAX_TRIES:
        f = [0, time.time() + LOCK_MINUTES * 60]
    _FAILS[key] = f
    return "The e-mail, user name or password is not right."


# ---------------------------------------------------------------- reset links
_TOKENS: dict[str, dict] = {}     # sha256(token) -> {"email", "user", "until"}
_LAST_SENT: dict[str, float] = {}


def new_reset(email: str, user: str) -> str:
    """A one-time token for one user name's link; that user's earlier links stop working."""
    email, name = email.strip().lower(), clean_name(user)
    tok = secrets.token_urlsafe(32)
    with _lock:
        for k in [k for k, v in _TOKENS.items()
                  if (v["email"] == email and v["user"].lower() == name.lower()) or v["until"] < time.time()]:
            _TOKENS.pop(k, None)
        _TOKENS[hashlib.sha256(tok.encode()).hexdigest()] = {"email": email, "user": name,
                                                             "until": time.time() + RESET_MINUTES * 60}
    return tok


def reset_for(token: str, use: bool = False) -> tuple[str, str]:
    """(e-mail, user name) a link is for, ("", "") when unknown or out of date; use=True spends it."""
    k = hashlib.sha256((token or "").encode()).hexdigest()
    with _lock:
        v = _TOKENS.get(k)
        if not v or v["until"] < time.time():
            _TOKENS.pop(k, None)
            return "", ""
        if use:
            _TOKENS.pop(k, None)
        return v["email"], v["user"]


def reset_email(token: str, use: bool = False) -> str:
    """The e-mail a reset token is for ("" when unknown or out of date); use=True spends it."""
    return reset_for(token, use)[0]


def may_send(email: str, seconds: int = 60) -> bool:
    """At most one reset e-mail a minute to an address."""
    email = email.strip().lower()
    with _lock:
        if _LAST_SENT.get(email, 0) + seconds > time.time():
            return False
        _LAST_SENT[email] = time.time()
        return True


# ---------------------------------------------------------------- e-mail
def mail_config() -> dict | None:
    m = _read(_paths()[1], None)
    return m if m and m.get("user") and m.get("password") else None


def save_mail_config(user: str, password: str, admin_to: str = "", host: str = "smtp.gmail.com",
                     port: int = 465) -> None:
    _write(_paths()[1], {"host": host, "port": int(port), "user": user.strip(),
                         "password": password.replace(" ", "").strip() if "gmail" in host else password,
                         "admin_to": (admin_to or user).strip()})


def send(to: str, subject: str, text: str, cfg: dict | None = None) -> None:
    """Send one e-mail from the account set with admin_access.bat (raises when it cannot)."""
    cfg = cfg or mail_config()
    if not cfg:
        raise RuntimeError("No e-mail account is set for the app (admin_access.bat, choice 9).")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"CAD to Revit <{cfg['user']}>", to, subject
    msg.set_content(text)
    port = int(cfg.get("port", 465))
    if port == 465:
        with smtplib.SMTP_SSL(cfg["host"], port, context=ssl.create_default_context(), timeout=20) as s:
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)


def send_later(to: str, subject: str, text: str, log=print) -> None:
    """Send in the background: a slow mail server never holds up the page."""
    def go():
        try:
            send(to, subject, text)
        except Exception as exc:              # noqa: BLE001 - reported, never fatal
            log(f"E-mail to {to} not sent: {exc}")
    threading.Thread(target=go, daemon=True).start()


# ---------------------------------------------------------------- each colleague's own folder
def _email_folder(email: str) -> str:
    return re.sub(r"[^\w.@-]+", "_", email.strip().lower())


def home_of(email: str, user: str) -> str:
    """OFFICE_HOME\\<user name>: each user's own folder, named once and never moved (its
    projects hold their own paths). Two users with one name get "Name (2)"."""
    email, k = email.strip().lower(), user_key(user)
    with _lock:
        d = _load()
        u = ((d.get(email) or {}).get("users") or {}).get(k)
        if u is None:
            raise ValueError(f"{user} has no password for {email} yet.")
        if u.get("folder"):
            return os.path.join(OFFICE_HOME, u["folder"])
        taken = {(x.get("folder") or "").lower() for r in d.values() for x in (r.get("users") or {}).values()}
        taken |= {_email_folder(e) for e in d}                     # nor anybody's earlier e-mail folder
        base = u["name"] + ("_" if u["name"].upper() in _RESERVED else "")
        folder, n = base, 1
        while folder.lower() in taken or os.path.exists(os.path.join(OFFICE_HOME, folder)):
            n += 1
            folder = f"{base} ({n})"
        u["folder"] = folder
        _write(_paths()[0], d)
        return os.path.join(OFFICE_HOME, folder)


def shared_home(email: str) -> str:
    """The folder made under the e-mail before user names, when there is one ("" if not).
    It is never moved: every user of that e-mail can still open its projects."""
    old = os.path.join(OFFICE_HOME, _email_folder(email))
    return old if os.path.isdir(old) else ""


def inside(path: str, folder: str) -> bool:
    try:
        a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False
