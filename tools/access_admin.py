r"""The admin's tool: who may use CAD to Revit. Only the admin runs this, on their own PC.

    python tools\access_admin.py                 a menu (or double-click admin_access.bat)
    python tools\access_admin.py setup [--source FOLDER_OR_URL]
    python tools\access_admin.py add EMAIL [--pc CODE|any] [--until YYYY-MM-DD] [--name NAME] [--plan PLAN]
    python tools\access_admin.py renew EMAIL --until YYYY-MM-DD
    python tools\access_admin.py remove EMAIL
    python tools\access_admin.py list
    python tools\access_admin.py requests
    python tools\access_admin.py approve EMAIL   (every PC that EMAIL asked from)
    python tools\access_admin.py publish

The signing key and the master list stay in the admin's own folder (ADMIN_DIR below),
never in the app folder that is copied to other PCs. Keep a copy of that folder safe:
without the key no new list can be signed (setup would make a new key, and every PC
would need the new access_config.json).

Each change is signed and published at once to the source every PC reads (a folder,
e.g. a shared office drive, or for a web address the file to upload is written).
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from building import access  # noqa: E402

ADMIN_DIR = os.environ.get("C2R_ADMIN_DIR") or os.path.join(os.path.expanduser("~"), ".c2r_admin")
KEY = os.path.join(ADMIN_DIR, "signing_key.json")
USERS = os.path.join(ADMIN_DIR, "users.json")
DEFAULT_DAYS = 180
DEFAULT_PLAN = "office (free)"


def _read(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def _secret() -> bytes:
    k = _read(KEY, None)
    if not k:
        raise SystemExit("No signing key on this PC. Run:  python tools\\access_admin.py setup")
    return bytes.fromhex(k["secret"])


def _master() -> dict:
    return _read(USERS, {"users": [], "source": ""})


def setup(source: str = "") -> None:
    """The signing key (once), access_config.json in the app folder, the first publish."""
    k = _read(KEY, None)
    if not k:
        sk = secrets.token_bytes(32)
        k = {"secret": sk.hex(), "public": access.public_key(sk).hex(),
             "made": dt.datetime.now().isoformat(timespec="seconds")}
        _write(KEY, k)
        print(f"A new signing key was made in {ADMIN_DIR}. Keep a copy of that folder safe.")
    m = _master()
    source = source or m.get("source") or os.path.join(ROOT, "access")
    m["source"] = source
    _write(USERS, m)
    cfg = {"app": "CAD to Revit", "public_key": k["public"], "source": source, "offline_days": access.OFFLINE_DAYS}
    _write(access.CONFIG, cfg)
    print(f"Sign-in is on for this copy of the app ({access.CONFIG}).")
    print(f"Approved users are read from: {source}")
    publish()


def publish() -> str:
    m = _master()
    src = m.get("source") or ""
    if not src:
        raise SystemExit("Run setup first.")
    payload = {"app": "CAD to Revit", "issued": dt.date.today().isoformat(),
               "users": sorted(m["users"], key=lambda u: u["email"])}
    doc = access.signed(payload, _secret())
    if src.lower().startswith(("http://", "https://")):
        out = os.path.join(ADMIN_DIR, access.LIST_NAME)
        _write(out, doc)
        print(f"Signed list written to {out} - upload it to {src}")
        return out
    out = src if src.lower().endswith(".json") else os.path.join(src, access.LIST_NAME)
    _write(out, doc)
    print(f"Published: {len(payload['users'])} users -> {out}")
    return out


def _user(m, email):
    email = email.strip().lower()
    return next((u for u in m["users"] if u["email"] == email), None)


def add(email: str, pc: str = "", until: str = "", name: str = "", plan: str = "", role: str = "") -> None:
    email = email.strip().lower()
    if not access.EMAIL.match(email):
        raise SystemExit(f"Not an e-mail address: {email}")
    if role and role not in ("user", "admin"):
        raise SystemExit("A role is 'user' or 'admin'.")
    m = _master()
    u = _user(m, email)
    if u is None:
        u = {"email": email, "name": name, "plan": plan or DEFAULT_PLAN, "machines": [], "status": "active",
             "expires": until or (dt.date.today() + dt.timedelta(days=DEFAULT_DAYS)).isoformat(),
             "added": dt.date.today().isoformat(), "role": role or "user"}
        m["users"].append(u)
    u["status"] = "active"
    if name:
        u["name"] = name
    if plan:
        u["plan"] = plan
    if role:
        u["role"] = role
    if until:
        dt.date.fromisoformat(until)
        u["expires"] = until
    if pc:
        pc = "ANY" if pc.lower() == "any" else pc.upper()
        if pc not in u["machines"]:
            u["machines"].append(pc)
    _write(USERS, m)
    print(f"{email}: {u['plan']}, {u.get('role', 'user')}, until {u['expires']}, "
          f"PCs {', '.join(u['machines']) or '(none yet)'}")
    publish()


def can_sign() -> bool:
    """Is the signing key on this PC (approvals can be made here)?"""
    return bool(_read(KEY, None))


def users_data() -> list[dict]:
    return sorted(_master()["users"], key=lambda u: u["email"])


def requests_data() -> list[dict]:
    return [r for _, r in _requests()]


def renew(email: str, until: str) -> None:
    m = _master()
    u = _user(m, email)
    if u is None:
        raise SystemExit(f"{email} is not in the list.")
    dt.date.fromisoformat(until)
    u["expires"], u["status"] = until, "active"
    _write(USERS, m)
    print(f"{u['email']} renewed until {until}")
    publish()


def remove(email: str) -> None:
    m = _master()
    u = _user(m, email)
    if u is None:
        raise SystemExit(f"{email} is not in the list.")
    u["status"] = "removed"
    _write(USERS, m)
    print(f"{u['email']} removed (their sign-in stops at once; on a PC that cannot reach the list, "
          f"within {access.OFFLINE_DAYS} days)")
    publish()


def show() -> None:
    m = _master()
    print(f"Approved users (source: {m.get('source')}):")
    for u in sorted(m["users"], key=lambda u: u["email"]):
        print(f"  {u['email']:<34} {u['status']:<8} {u.get('plan', ''):<16} until {u.get('expires') or '-':<11} "
              f"PCs: {', '.join(u['machines']) or '-'}   {u.get('name', '')}")
    if not m["users"]:
        print("  (none)")


def _requests():
    src = _master().get("source") or ""
    folder = os.path.join(os.path.dirname(src) if src.lower().endswith(".json") else src, "requests")
    out = []
    if os.path.isdir(folder):
        for n in sorted(os.listdir(folder)):
            r = _read(os.path.join(folder, n), None)
            if r:
                out.append((os.path.join(folder, n), r))
    return out


def requests() -> None:
    rs = _requests()
    print("Access requests:" if rs else "No access requests.")
    for _, r in rs:
        print(f"  {r['email']:<34} PC {r['pc']}  asked {r.get('asked', '')}  ({r.get('user', '')} on {r.get('computer', '')})")


def approve(email: str, until: str = "") -> None:
    email = email.strip().lower()
    rs = [(p, r) for p, r in _requests() if r.get("email", "").lower() == email]
    if not rs:
        raise SystemExit(f"No request from {email}. Add them directly:  add {email} --pc CODE")
    m = _master()
    u = _user(m, email)
    for p, r in rs:
        add(email, pc=r["pc"], until=until if u is None else "")
        os.remove(p)


def _clipboard() -> str:
    """The text on Windows' clipboard ("" when there is none)."""
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        try:
            return r.clipboard_get()
        finally:
            r.destroy()
    except Exception:  # noqa: BLE001 - no clipboard text
        return ""


def mail_setup() -> None:
    """The e-mail account the office mode sends from (password links, approvals, requests).
    For Gmail: 2-Step Verification on, then Google Account > Security > App passwords."""
    import getpass
    from building import office
    print("The e-mail account CAD to Revit sends from (password links, approvals, requests to you).")
    print("For Gmail use an App password (Google Account > Security > App passwords), not your normal one.")
    user = input("Send from (e-mail): ").strip()
    if not access.EMAIL.match(user):
        raise SystemExit("Not an e-mail address.")
    host = "smtp.gmail.com"
    if not user.lower().endswith(("@gmail.com", "@googlemail.com")):
        host = input("Mail server (from your IT, e.g. smtp.office365.com): ").strip() or host
    port = 465 if host == "smtp.gmail.com" else int(input("Port (465 or 587): ").strip() or "587")
    if host == "smtp.gmail.com":
        # a Gmail app password is 16 letters: taken from what the admin copied (Ctrl+C), since a
        # password typed or pasted here does not show and is easily pasted twice
        input("Copy the 16-letter app password from Google's page (select it, Ctrl+C), then press Enter here: ")
        pw = "".join(_clipboard().split())
        if not (len(pw) == 16 and pw.isalpha()):
            print(f"What was copied is not a 16-letter app password ({len(pw)} characters).")
            pw = "".join(getpass.getpass("Type the 16 letters instead (not shown as you type), then Enter: ").split())
            if not (len(pw) == 16 and pw.isalpha()):
                raise SystemExit(f"That was {len(pw)} characters, not the 16 letters of a Gmail app password. "
                                 "Nothing was saved - choose 9 again.")
        print("Got the 16-letter app password.")
    else:
        pw = getpass.getpass("Password (not shown as you type): ")
    to = input(f"Send access requests to (empty = {user}): ").strip() or user
    office.save_mail_config(user, pw, to, host, port)
    try:
        office.send(to, "CAD to Revit: e-mail works", "The app can send e-mails from this account.")
        print(f"Saved, and a test e-mail was sent to {to}.")
    except Exception as exc:  # noqa: BLE001
        print(f"Saved, but the test e-mail was not sent: {exc}\nCheck the address and the app password, "
              "then choose 9 again.")


def menu() -> None:
    while True:
        print("\nCAD to Revit - who may use the app")
        print("  1 List users      2 Requests      3 Approve a request   4 Add a user")
        print("  5 Renew a user    6 Remove a user 7 Publish again       8 Setup / change list place")
        print("  9 E-mail account for the office mode (run_office.bat)                         0 Quit")
        c = input("Choose: ").strip()
        try:
            if c == "1":
                show()
            elif c == "2":
                requests()
            elif c == "3":
                requests()
                approve(input("E-mail to approve: "), input("Until (YYYY-MM-DD, empty = 180 days): ").strip())
            elif c == "4":
                add(input("E-mail: "), input("PC code (or 'any' for every PC): ").strip(),
                    input("Until (YYYY-MM-DD, empty = 180 days): ").strip(), input("Name (optional): ").strip())
            elif c == "5":
                renew(input("E-mail: "), input("Until (YYYY-MM-DD): ").strip())
            elif c == "6":
                remove(input("E-mail: "))
            elif c == "7":
                publish()
            elif c == "8":
                setup(input("Folder or web address of the list (empty = keep): ").strip())
            elif c == "9":
                mail_setup()
            elif c == "0":
                return
        except (SystemExit, ValueError) as e:
            print(e)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Who may use CAD to Revit (admin only).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("setup")
    s.add_argument("--source", default="")
    a = sub.add_parser("add")
    a.add_argument("email")
    a.add_argument("--pc", default="")
    a.add_argument("--until", default="")
    a.add_argument("--name", default="")
    a.add_argument("--plan", default="")
    a.add_argument("--role", default="", help="user or admin (admin may approve users inside the app)")
    r = sub.add_parser("renew")
    r.add_argument("email")
    r.add_argument("--until", required=True)
    sub.add_parser("remove").add_argument("email")
    sub.add_parser("list")
    sub.add_parser("requests")
    p = sub.add_parser("approve")
    p.add_argument("email")
    p.add_argument("--until", default="")
    sub.add_parser("publish")
    sub.add_parser("pc")
    sub.add_parser("mail")
    o = ap.parse_args(argv)
    if o.cmd == "setup":
        setup(o.source)
    elif o.cmd == "add":
        add(o.email, o.pc, o.until, o.name, o.plan, o.role)
    elif o.cmd == "renew":
        renew(o.email, o.until)
    elif o.cmd == "remove":
        remove(o.email)
    elif o.cmd == "list":
        show()
    elif o.cmd == "requests":
        requests()
    elif o.cmd == "approve":
        approve(o.email, o.until)
    elif o.cmd == "publish":
        publish()
    elif o.cmd == "pc":
        print(access.machine_code())
    elif o.cmd == "mail":
        mail_setup()
    else:
        menu()


if __name__ == "__main__":
    main()
