"""Who may use the app: a signed list of approved e-mail addresses, kept by the admin.

The admin (the app's owner) holds a private signing key on their own PC. The list
of approved users - e-mail, the PCs each may use, plan, expiry date - is signed with
it and published to one place every copy of the app reads: a folder (a shared office
drive) or a web address. Each copy carries only the public key, so it can check the
list but nobody can write one: editing the file breaks its signature.

A user signs in with their e-mail. They are let in when the list approves that
address on this PC (by its PC code) and their date has not passed. An address not
yet approved here leaves a request in the list's folder (and is shown its PC code to
send to the admin); the admin approves it with tools\\access_admin.py.

The list is read at each sign-in. When its place cannot be reached (the office drive
offline) the last good copy is used for OFFLINE_DAYS, then sign-in stops until it can
be read again - so a removed user loses access within that time.

No library beyond Python is needed: Ed25519 signatures (RFC 8032) are done here.
Without access_config.json next to the app, no sign-in is asked (as before).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.environ.get("C2R_ACCESS_CONFIG") or os.path.join(ROOT, "access_config.json")
CACHE_DIR = os.environ.get("C2R_ACCESS_CACHE") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "CAD to Revit")
LIST_NAME = "access.json"
OFFLINE_DAYS = 7
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- Ed25519 (RFC 8032, section 6)
_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _p - 2, _p)


_d = -121665 * _inv(121666) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _p, (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C, D = 2 * P[3] * Q[3] * _d % _p, 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _expand(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _hq(b):
    return int.from_bytes(hashlib.sha512(b).digest(), "little") % _q


def public_key(secret: bytes) -> bytes:
    return _compress(_mul(_expand(secret)[0], _G))


def sign(secret: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = _hq(prefix + msg)
    Rs = _compress(_mul(r, _G))
    s = (r + _hq(Rs + A + msg) * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    A, R = _decompress(public), _decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _q:
        return False
    h = _hq(signature[:32] + public + msg)
    return _equal(_mul(s, _G), _add(R, _mul(h, A)))


# ---------------------------------------------------------------- the signed list
def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def signed(payload: dict, secret: bytes) -> dict:
    return {"payload": payload, "signature": sign(secret, canonical(payload)).hex()}


def check_signed(doc: dict, public_hex: str) -> dict | None:
    """The list's payload when its signature is the admin's, else None."""
    try:
        payload, sig = doc["payload"], bytes.fromhex(doc["signature"])
        return payload if verify(bytes.fromhex(public_hex), canonical(payload), sig) else None
    except Exception:
        return None


# ---------------------------------------------------------------- this PC
def machine_code() -> str:
    """A short code for this PC (from Windows' own machine id), e.g. 3F9A-07C2."""
    raw = ""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                           0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0))
        raw = winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        import uuid
        raw = "%012x" % uuid.getnode()
    h = hashlib.sha256(("c2r:" + raw).encode()).hexdigest().upper()
    return h[:4] + "-" + h[4:8]


# ---------------------------------------------------------------- reading the list
def config() -> dict | None:
    """access_config.json next to the app: {"public_key", "source"}; None = no sign-in."""
    if not os.path.exists(CONFIG):
        return None
    with io.open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def enabled() -> bool:
    return config() is not None


def _source_file(source: str) -> str:
    return source if source.lower().endswith(".json") else os.path.join(source, LIST_NAME)


def _fetch(source: str) -> dict:
    if source.lower().startswith(("http://", "https://")):
        url = source if source.lower().endswith(".json") else source.rstrip("/") + "/" + LIST_NAME
        with urllib.request.urlopen(url, timeout=6) as r:
            return json.loads(r.read().decode("utf-8"))
    with io.open(_source_file(source), encoding="utf-8") as f:
        return json.load(f)


def _cache_path() -> str:
    return os.path.join(CACHE_DIR, "access_cache.json")


def load_list(cfg: dict, today: _dt.date | None = None) -> tuple[dict | None, str]:
    """(the approved list, "" or why it could not be used). The source first; when it
    cannot be read, the last good copy for OFFLINE_DAYS."""
    today = today or _dt.date.today()
    pub = cfg.get("public_key", "")
    try:
        doc = _fetch(cfg.get("source", ""))
        payload = check_signed(doc, pub)
        if payload is None:
            return None, ("The list of approved users is not signed by the admin (it was changed by hand, "
                          "or is from another app). Ask the admin to publish it again.")
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with io.open(_cache_path(), "w", encoding="utf-8") as f:
                json.dump({"fetched": today.isoformat(), "doc": doc}, f)
        except OSError:
            pass
        return payload, ""
    except Exception:
        pass
    try:
        with io.open(_cache_path(), encoding="utf-8") as f:
            c = json.load(f)
        payload = check_signed(c["doc"], pub)
        age = (today - _dt.date.fromisoformat(c["fetched"])).days
        if payload is not None and age <= int(cfg.get("offline_days", OFFLINE_DAYS)):
            return payload, ""
        if payload is not None:
            return None, (f"The list of approved users could not be read for {age} days "
                          f"({cfg.get('source', '')}). Connect to the office network and try again.")
    except Exception:
        pass
    return None, (f"The list of approved users could not be read ({cfg.get('source', '')}). "
                  "Connect to the office network, or ask the admin.")


def check(email: str, cfg: dict | None = None, today: _dt.date | None = None, pc: str | None = None,
          any_pc: bool = False) -> dict:
    """May this e-mail use the app on this PC? {"ok", "message", "email", "pc", "plan", "expires", "name"}.

    any_pc: the office mode, where colleagues use the app on the admin's PC through their
    browser - the e-mail alone is approved, whatever PC it signs in from ("approved" is
    True for an approved address, "requested" when a request was left for the admin)."""
    cfg = cfg if cfg is not None else config()
    pc = pc or machine_code()
    email = (email or "").strip().lower()
    out = {"ok": False, "email": email, "pc": pc, "plan": "", "expires": "", "name": "", "message": "", "role": "",
           "approved": False, "requested": False}
    if cfg is None:
        out.update(ok=True, message="no sign-in on this copy")
        return out
    if not EMAIL.match(email):
        out["message"] = "Type your e-mail address."
        return out
    today = today or _dt.date.today()
    lst, why = load_list(cfg, today)
    if lst is None:
        out["message"] = why
        return out
    u = next((u for u in lst.get("users", []) if u.get("email", "").lower() == email), None)
    if u is None or u.get("status", "active") != "active":
        if any_pc:
            out["requested"] = request(email, pc, cfg)
            out["message"] = ("This e-mail is not approved to use CAD to Revit yet. Your request is with the "
                              "admin; you will get an e-mail when it is approved.")
            return out
        out["message"] = ("This e-mail is not approved to use CAD to Revit. A request was sent to the admin "
                          f"with this PC's code {pc}." if request(email, pc, cfg)
                          else f"This e-mail is not approved to use CAD to Revit. Send the admin your e-mail "
                               f"and this PC's code: {pc}.")
        return out
    # role "admin": may approve, renew and remove users from inside the app (on a PC holding the
    # signing key). It comes from the signed list, so nobody can give it to themselves.
    out.update(plan=u.get("plan", ""), expires=u.get("expires", "") or "", name=u.get("name", ""),
               role=u.get("role", "user"))
    if u.get("expires") and _dt.date.fromisoformat(u["expires"]) < today:
        out["message"] = f"Your access ended on {u['expires']}. Ask the admin to renew it."
        return out
    out["approved"] = True
    pcs = [m.upper() for m in u.get("machines", [])]
    if not any_pc and "ANY" not in pcs and pc.upper() not in pcs:
        out["message"] = (f"This e-mail is approved, but not on this PC. A request was sent to the admin with "
                          f"this PC's code {pc}." if request(email, pc, cfg)
                          else f"This e-mail is approved, but not on this PC. Send the admin this PC's code: {pc}.")
        return out
    out.update(ok=True, message="")
    return out


def still_allowed(email: str, any_pc: bool = False, cfg: dict | None = None, today: _dt.date | None = None,
                  pc: str | None = None) -> bool:
    """Is a signed-in user still approved (not removed, not expired)? Asked now and then while they
    stay signed in; unlike check() it never leaves a request for the admin."""
    cfg = cfg if cfg is not None else config()
    if cfg is None:
        return True
    today = today or _dt.date.today()
    lst, _why = load_list(cfg, today)
    if lst is None:
        return False
    email = (email or "").strip().lower()
    u = next((u for u in lst.get("users", []) if u.get("email", "").lower() == email), None)
    if u is None or u.get("status", "active") != "active":
        return False
    if u.get("expires") and _dt.date.fromisoformat(u["expires"]) < today:
        return False
    pcs = [m.upper() for m in u.get("machines", [])]
    return any_pc or "ANY" in pcs or (pc or machine_code()).upper() in pcs


def request(email: str, pc: str, cfg: dict) -> bool:
    """Leave an access request in the list's folder (a shared folder). False for a web address."""
    src = cfg.get("source", "")
    if src.lower().startswith(("http://", "https://")):
        return False
    folder = os.path.join(os.path.dirname(_source_file(src)), "requests")
    try:
        os.makedirs(folder, exist_ok=True)
        name = re.sub(r"[^\w.@-]", "_", f"{email}__{pc}") + ".json"
        with io.open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            json.dump({"email": email, "pc": pc, "asked": _dt.datetime.now().isoformat(timespec="seconds"),
                       "user": os.environ.get("USERNAME", ""), "computer": os.environ.get("COMPUTERNAME", "")}, f)
        return True
    except OSError:
        return False


def last_email() -> str:
    try:
        with io.open(os.path.join(CACHE_DIR, "last_user.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def remember(email: str) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with io.open(os.path.join(CACHE_DIR, "last_user.txt"), "w", encoding="utf-8") as f:
            f.write(email)
    except OSError:
        pass
