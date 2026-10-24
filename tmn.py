#!/usr/bin/env python3
"""ZYROX TempMail CLI — Termux tool (TempMail Ninja reverse-engineered API).

Full CLI: interactive menu + direct commands.
Animated AUTO-RECEIVE mode: live spinner watch — new mail pops instantly
with the OTP highlighted.

Usage:
  python tmn.py            -> interactive menu
  python tmn.py new        -> random .lat mail (24h)
  python tmn.py custom <name>
  python tmn.py shadow     -> .org/.com mail (30 days)
  python tmn.py inbox      -> inbox list
  python tmn.py read <n>   -> open mail #n from last inbox list
  python tmn.py mails      -> my addresses
  python tmn.py auto       -> animated auto-receive watch (Ctrl+C to stop)

Data: ~/.zyrox_tmn.json (token + current email) — session auto-restores.
"""
import argparse
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tmn_client import TMNClient, TMNError  # noqa: E402

STORE = os.path.expanduser("~/.zyrox_tmn.json")
SPIN = ["◐", "◓", "◑", "◒"]
OTP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

# ---------------- colors ----------------
G, C, Y, R, M, B, DIM, X = ("\033[92m", "\033[96m", "\033[93m", "\033[91m",
                            "\033[95m", "\033[1m", "\033[2m", "\033[0m")


def cprint(color, *a):
    print(color + " ".join(str(x) for x in a) + X)


# ---------------- store ----------------
def load_store():
    try:
        return json.load(open(STORE))
    except Exception:
        return {}


def save_store(d):
    try:
        json.dump(d, open(STORE, "w"))
    except Exception:
        pass


# ---------------- spinner ----------------
class Spinner:
    def __init__(self, label):
        self.label = label
        self.on = False
        self.t = None
        self.start = 0.0

    def __enter__(self):
        self.on = True
        self.start = time.time()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        return self

    def _run(self):
        i = 0
        while self.on:
            el = int(time.time() - self.start)
            sys.stdout.write(f"\r{C}{SPIN[i % 4]} {self.label}... {DIM}{el}s{X}   ")
            sys.stdout.flush()
            i += 1
            time.sleep(0.12)

    def __exit__(self, *a):
        self.on = False
        if self.t:
            self.t.join(0.4)
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()


# ---------------- client session ----------------
class Session:
    """TMNClient + store persistence (token restore sabse pehle)."""

    def __init__(self):
        self.store = load_store()
        self.c = TMNClient()
        self.c.connect()
        token = self.store.get("token")
        if token:
            r = self.c.guest_data(token)
            if isinstance(r, dict) and r.get("activeEmail"):
                self.c.token = token
                self.c.email = r["activeEmail"].get("emailAddress")
            else:
                self.c.guest_data(None)
        else:
            self.c.guest_data(None)

    def persist(self):
        if self.c.token:
            self.store["token"] = self.c.token
        if self.c.email:
            self.store["email"] = self.c.email
        save_store(self.store)

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass


def with_session(fn, tries=3):
    """Session reset pe fresh session se retry (TMN server flaky hai)."""
    last = None
    for _ in range(tries):
        s = Session()
        try:
            r = fn(s)
            if isinstance(r, dict) and r.get("error") == "timeout":
                last = r
                continue
            return r
        except TMNError as e:
            last = e
            continue
        finally:
            s.persist()
            s.close()
    if isinstance(last, Exception):
        raise last
    return last


def err_text(r):
    e = (r or {}).get("error", "") if isinstance(r, dict) else ""
    if e == "rate_limit_create_email_warning":
        return "Rate limit! Server cooldown — wait 1-2 min and retry."
    if e == "session_expired":
        return "Session expired. Create a fresh mail (option 1)."
    if e == "session_active_email_required":
        return "No active mail. Create one first (option 1/2/3)."
    if e == "timeout":
        return "Server timeout — retry once."
    return f"Server error: {e or r}"


def strip_html(html):
    if not html:
        return ""
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    # links bachao: <a href="URL">text</a> -> text [ URL ] (warna href udd jata hai)
    txt = re.sub(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                 lambda m: (re.sub(r"<[^>]+>", "", m.group(2)).strip() or "LINK") + " [ " + m.group(1) + " ]",
                 txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"[ \t]+", " ", txt).strip()


def extract_otp(text):
    m = OTP_RE.search(text or "")
    return m.group(1) if m else None


LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+")

def extract_links(text):
    out = []
    for u in LINK_RE.findall(text or ""):
        u = u.rstrip(".,;")
        if u not in out:
            out.append(u)
    return out[:5]


def _rl_retry(fn):
    """Create pe rate_limit aaye to khud wait karke retry (3 tries).
    Shared/carrier IPs pe server cooldown aam hai — user ko lagta tha
    'domain nahi chal raha'; ab tool khud ruk ke bana dega."""
    r = None
    for attempt in range(3):
        r = fn()
        if isinstance(r, dict) and \
                r.get("error") == "rate_limit_create_email_warning" and attempt < 2:
            time.sleep(25)
            continue
        break
    return r


def fetch_all_domains():
    def f(s):
        return {"quick": s.c.domains(11), "shadow": s.c.domains(1)}
    return with_session(f)


# ---------------- actions ----------------
def do_new():
    with Spinner("Creating .lat mail"):
        r = with_session(lambda s: _rl_retry(lambda: s.c.generate()))
    if isinstance(r, dict) and r.get("emailData"):
        em = r["emailData"]["emailAddress"]
        cprint(G, "\n✅ MAIL READY 😈🔥")
        print(f"📧 {B}{em}{X}")
        cprint(DIM, "⏳ Expiry: 24 hours (.lat Quick) — OTPs will land here.")
    else:
        cprint(R, "⚠️", err_text(r) if not isinstance(r, Exception) else str(r))


def do_custom(alias, interactive=False):
    alias = re.sub(r"[^a-z0-9._-]", "", (alias or "").lower())[:30]
    if len(alias) < 3:
        cprint(R, "❌ Alias must be at least 3 characters. Example: rahul123")
        return

    domain, etype, exp_label = None, 1, "24 hours"
    if interactive:
        with Spinner("Loading domains..."):
            all_d = fetch_all_domains()
        opts = [(d, 1, "24 hours", ".lat Quick") for d in (all_d or {}).get("quick", [])]
        opts += [(d, 2, "30 days", "Shadow") for d in (all_d or {}).get("shadow", [])]
        if not opts:
            cprint(R, "⚠️ Domain list nahi aayi — server slow hai, retry karo.")
            return
        print(f"\n{C}🌐 Domain choose karo:{X}")
        for i, (d, _t, _e, tag) in enumerate(opts, 1):
            print(f"  {B}{i}{X}. {d}  {DIM}({tag}){X}")
        try:
            pick = input(f"{Y}Domain number (Enter = pehla .lat): {X}").strip()
        except (EOFError, KeyboardInterrupt):
            pick = ""
        if pick.isdigit() and 1 <= int(pick) <= len(opts):
            domain, etype, exp_label, _tag = opts[int(pick) - 1]
        else:
            domain, etype, exp_label = opts[0][0], opts[0][1], opts[0][2]

    def f(s):
        dom = domain or (s.c.domains(11) or [None])[0]
        if not dom:
            return {"error": "no_domain"}
        return _rl_retry(lambda: s.c.create_custom(alias, dom, etype))
    with Spinner(f"Creating {alias}@{domain or '...'}"):
        r = with_session(f)
    if isinstance(r, dict) and r.get("emailData"):
        cprint(G, "\n✅ CUSTOM MAIL READY 😈")
        print(f"📧 {B}{r['emailData']['emailAddress']}{X}")
        cprint(DIM, f"⏳ Expiry: {exp_label}")
    else:
        cprint(R, "⚠️", err_text(r) if not isinstance(r, Exception) else str(r))
        cprint(DIM, "(If the alias is taken, try another name)")


def do_shadow():
    def f(s):
        doms = s.c.domains(1)
        if not doms:
            return {"error": "no_domain"}
        a = s.c.emit("generate_email_alias", timeout=30) or {}
        alias = re.sub(r"[^a-z0-9._-]", "",
                       (a.get("alias") or f"user{int(time.time()) % 100000}").lower())
        import random
        return _rl_retry(lambda: s.c.create_custom(alias, random.choice(doms), 2))
    with Spinner("Creating Shadow mail"):
        r = with_session(f)
    if isinstance(r, dict) and r.get("emailData"):
        cprint(G, "\n👻 SHADOW MAIL READY")
        print(f"📧 {B}{r['emailData']['emailAddress']}{X}")
        cprint(DIM, "⏳ Expiry: 30 days — .org/.com accepted by more big sites ✅")
    else:
        cprint(R, "⚠️", err_text(r) if not isinstance(r, Exception) else str(r))


def do_domains():
    with Spinner("Loading domains..."):
        all_d = fetch_all_domains()
    q = (all_d or {}).get("quick", [])
    sh = (all_d or {}).get("shadow", [])
    print(f"\n{C}🌐 AVAILABLE DOMAINS{X}")
    print(f"{B}⚡ Quick (.lat — 24 hours):{X}")
    for d in q:
        print(f"   • {d}")
    print(f"{B}👻 Shadow (.org/.com — 30 days):{X}")
    for d in sh:
        print(f"   • {d}")
    cprint(DIM, "Custom mail (option 2) me inme se koi bhi domain choose kar sakte ho.")


_last_msgs = []


def fetch_msgs():
    def f(s):
        if not s.c.email and s.store.get("token"):
            s.c.restore(s.store["token"])
        return s.c.messages()
    return with_session(f)


def do_inbox():
    global _last_msgs
    with Spinner("Searching inbox"):
        r = fetch_msgs()
    if isinstance(r, dict) and r.get("error"):
        cprint(R, "⚠️", err_text(r))
        return
    msgs = (r or {}).get("email_messages") or [] if isinstance(r, dict) else []
    _last_msgs = msgs
    email = load_store().get("email") or "-"
    if not msgs:
        cprint(Y, f"\n📥 INBOX EMPTY — {email}")
        cprint(DIM, "No mail yet. AUTO mode (option 6) watches live for you.")
        return
    cprint(G, f"\n📥 INBOX — {email}  ({len(msgs)} mail)")
    print("━" * 46)
    for i, msg in enumerate(msgs[:15], 1):
        sender = msg.get("sender") or {}
        frm = sender.get("text") if isinstance(sender, dict) else str(sender)
        print(f"{B}{i}.{X} {msg.get('subject') or '(no subject)'}")
        print(f"   👤 {frm} • {msg.get('date', '')}")
    cprint(DIM, "\nOpen with: read <number>  (menu option 5)")


def do_read(n):
    global _last_msgs
    if not _last_msgs:
        do_inbox()
    if not _last_msgs or n < 1 or n > len(_last_msgs):
        cprint(R, "❌ Invalid number. Pehle inbox dekho.")
        return
    mid = _last_msgs[n - 1].get("id")
    with Spinner("Opening mail"):
        full = with_session(lambda s: s.c.message(mid))
    data = ((full or {}).get("emailMessage") or {}).get("data") or {}
    frm = data.get("from") or {}
    frm = frm.get("text") if isinstance(frm, dict) else str(frm)
    subject = data.get("subject") or "(no subject)"
    body = strip_html(data.get("content") or "")
    otp = extract_otp(subject + " " + body)
    print("\n" + "━" * 46)
    print(f"📖 {B}{subject}{X}")
    print(f"👤 From: {frm}")
    if otp:
        print(f"\n🔑 {G}{B}OTP: {otp}{X}")
    links = extract_links(body)
    if links:
        print(f"\n🔗 {C}{B}LINKS:{X}")
        for u in links:
            print(f"   {u}")
    print("━" * 46)
    print(body[:3000])


def do_mails():
    with Spinner("Loading your mails"):
        r = with_session(lambda s: s.c.emails_list())
    lst = (r or {}).get("emailList") or [] if isinstance(r, dict) else []
    if not lst:
        cprint(Y, "📧 No mails created yet.")
        return
    cprint(G, "\n📧 MY MAILS")
    print("━" * 46)
    for i, e in enumerate(lst[:20], 1):
        exp = (e.get("expiresAt") or "")[:10]
        print(f"{i}. {B}{e.get('emailAddress')}{X}  ⏳ till {exp}")


def print_mail_card(msg, full=None):
    sender = msg.get("sender") or {}
    frm = sender.get("text") if isinstance(sender, dict) else str(sender)
    subject = msg.get("subject") or "(no subject)"
    body = ""
    if full:
        data = ((full or {}).get("emailMessage") or {}).get("data") or {}
        body = strip_html(data.get("content") or "")
    otp = extract_otp(subject + " " + body)
    print("\a")  # beep
    print(f"\n{G}{'━' * 46}{X}")
    cprint(G, "📩 NEW MAIL ARRIVED! 🔥")
    print(f"👤 From:    {frm}")
    print(f"📌 Subject: {B}{subject}{X}")
    if otp:
        print(f"\n🔑 {G}{B}OTP: {otp}{X}")
    links = extract_links(body)
    if links:
        print(f"\n🔗 {C}{B}LINKS:{X}")
        for u in links:
            print(f"   {u}")
    if body:
        print(f"\n{body[:800]}")
    print(f"{G}{'━' * 46}{X}")


def do_auto(interval=8):
    email = load_store().get("email")
    if not email:
        cprint(R, "❌ No active mail. Pehle new/custom/shadow se banao.")
        return
    cprint(G, f"\n📡 AUTO-RECEIVE MODE — watching {B}{email}{X}")
    cprint(DIM, f"Polling every {interval}s • Ctrl+C to stop • new mail pops here with OTP 🔥")
    seen = set()
    try:
        r = fetch_msgs()
        seen = {m.get("id") for m in ((r or {}).get("email_messages") or [])}
    except Exception:
        pass
    i = 0
    started = time.time()
    try:
        while True:
            i += 1
            el = int(time.time() - started)
            sys.stdout.write(f"\r{C}{SPIN[i % 4]} LIVE {DIM}watching... {el}s | checks: {i} | mails seen: {len(seen)}{X}   ")
            sys.stdout.flush()
            time.sleep(interval)
            try:
                r = fetch_msgs()
            except Exception:
                continue
            if isinstance(r, dict) and r.get("error"):
                continue
            msgs = (r or {}).get("email_messages") or [] if isinstance(r, dict) else []
            for msg in msgs:
                if msg.get("id") in seen:
                    continue
                seen.add(msg.get("id"))
                full = None
                try:
                    full = with_session(lambda s: s.c.message(msg.get("id")))
                except Exception:
                    pass
                sys.stdout.write("\r" + " " * 70 + "\r")
                print_mail_card(msg, full)
    except KeyboardInterrupt:
        print(f"\n{Y}⏹ Auto-receive stopped.{X}")


# ---------------- UI ----------------
def banner():
    print(f"""{M}{B}
███████╗██╗   ██╗██████╗  ██████╗ ██╗  ██╗
╚══███╔╝╚██╗ ██╔╝██╔══██╗██╔═══██╗╚██╗██╔╝
  ███╔╝  ╚████╔╝ ██████╔╝██║   ██║ ╚███╔╝
 ███╔╝    ╚██╔╝  ██╔══██╗██║   ██║ ██╔██╗
███████╗   ██║   ██║  ██║╚██████╔╝██╔╝ ██╗
╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝{X}
{C}     ⚡ ZYROX TEMPMAIL CLI — TERMUX EDITION ⚡{X}
{M}             😈 by ZYROX MODS 😈{X}""")
    email = load_store().get("email")
    if email:
        print(f"📧 Current: {B}{email}{X}")
    print("━" * 46)


def menu():
    banner()
    while True:
        print(f"""
{C}┌─ MENU ─────────────────────────────{X}
│  {B}1{X} ➕ New Mail (.lat, 24h)
│  {B}2{X} ✏️  Custom Name + Domain
│  {B}3{X} 👻 Shadow Mail (.org/.com, 30d)
│  {B}4{X} 📥 Inbox
│  {B}5{X} 📖 Read Mail (number)
│  {B}6{X} 📡 AUTO-RECEIVE (animated, live)
│  {B}7{X} 📧 My Mails
│  {B}8{X} 🌐 All Domains
│  {B}0{X} 🚪 Exit
{C}└────────────────────────────────────{X}""")
        try:
            ch = input(f"{Y}Choose: {X}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if ch == "1":
            do_new()
        elif ch == "2":
            alias = input("✏️ Alias (name only, e.g. rahul123): ").strip()
            do_custom(alias, interactive=True)
        elif ch == "3":
            do_shadow()
        elif ch == "4":
            do_inbox()
        elif ch == "5":
            n = input("Mail number: ").strip()
            do_read(int(n) if n.isdigit() else -1)
        elif ch == "6":
            do_auto()
        elif ch == "7":
            do_mails()
        elif ch == "8":
            do_domains()
        elif ch == "0":
            cprint(M, "👋 Bye!")
            break
        else:
            cprint(R, "❌ Invalid choice")


def main():
    ap = argparse.ArgumentParser(prog="tmn", description="ZYROX TempMail CLI (TempMail Ninja)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("new")
    p = sub.add_parser("custom")
    p.add_argument("name")
    sub.add_parser("shadow")
    sub.add_parser("inbox")
    p = sub.add_parser("read")
    p.add_argument("n", type=int)
    sub.add_parser("mails")
    p = sub.add_parser("auto")
    p.add_argument("--interval", type=int, default=8)
    args = ap.parse_args()
    if not args.cmd:
        menu()
    elif args.cmd == "new":
        do_new()
    elif args.cmd == "custom":
        do_custom(args.name)
    elif args.cmd == "shadow":
        do_shadow()
    elif args.cmd == "inbox":
        do_inbox()
    elif args.cmd == "read":
        do_read(args.n)
    elif args.cmd == "mails":
        do_mails()
    elif args.cmd == "auto":
        do_auto(args.interval)


if __name__ == "__main__":
    main()
