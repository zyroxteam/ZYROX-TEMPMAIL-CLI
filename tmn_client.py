"""TempMail Ninja backend client — single-I/O-thread design (needs `requests`).

Reverse-engineered 2026-10-23 from the public Nuxt frontend:
- Backend: Socket.IO at https://tm-app.solucioneswc.com:2083 (polling transport)
- Server rejects namespace connect without header  Origin: https://tempmail.ninja
- Guest flow: get_guest_user_data {token} -> generate_temp_mail / create_email
- Session token (JWT) restores the same visitor + active email on a new socket.

Saare HTTP calls ek hi I/O thread se serial hote hain (ek time pe ek request) —
ye pattern live test me proven hai; parallel poll+POST pe server/proxy atakta hai.
"""
import json
import queue
import threading
import time

import requests

BASE = "https://tm-app.solucioneswc.com:2083/socket.io/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Origin": "https://tempmail.ninja",
    "Referer": "https://tempmail.ninja/",
    "Content-Type": "text/plain;charset=UTF-8",
}


class TMNError(Exception):
    pass


class TMNClient:
    def __init__(self, on_new_message=None):
        self.on_new_message = on_new_message  # callable(dict)
        self.token = None          # guest JWT for the active email
        self.email = None          # active email address
        self.alive = False
        self._params = {}
        self._sess = None
        self._ack = 0
        self._jobs = queue.Queue()
        self._io = None

    # ---------- connection ----------
    def connect(self):
        if self.alive:
            return self
        self.alive = True
        self._io = threading.Thread(target=self._io_loop, daemon=True)
        self._io.start()
        # handshake runs inside the io thread via a special job
        self.emit("__handshake__", timeout=30)
        return self

    def close(self):
        self.alive = False

    # ---------- raw HTTP (io thread only) ----------
    def _req(self, method, data=None, timeout=35):
        if self._sess is None:
            self._sess = requests.Session()
        if method == "GET":
            r = self._sess.get(BASE, params=self._params, headers=HEADERS, timeout=timeout)
        else:
            r = self._sess.post(BASE, params=self._params, data=data,
                                headers=HEADERS, timeout=timeout)
        return r.text

    def _handshake(self):
        last = None
        for _ in range(3):
            try:
                self._sess = requests.Session()
                self._params = {"EIO": "4", "transport": "polling", "locale": "en"}
                txt = self._req("GET", timeout=15)
                self._params["sid"] = json.loads(txt[1:])["sid"]
                self._req("POST", b"40", timeout=15)
                return
            except Exception as e:                # noqa: BLE001
                last = e
                time.sleep(2)
        raise TMNError(f"handshake failed: {last}")

    def _poll_once(self, timeout=35):
        try:
            return self._req("GET", timeout=timeout)
        except Exception:
            return ""

    def _process(self, txt, want_ack=None):
        """Packets process karo. want_ack id ka payload mile to return karo."""
        found = _MISSING
        for pkt in txt.split("\x1e"):
            if not pkt:
                continue
            if pkt == "2":                       # ping -> pong
                try:
                    self._req("POST", b"3", timeout=10)
                except Exception:
                    pass
            elif pkt.startswith("42"):           # push event
                try:
                    arr = json.loads(pkt[2:])
                    if arr and arr[0] == "new_message_notify" and self.on_new_message:
                        self.on_new_message(arr[1] if len(arr) > 1 else {})
                except Exception:
                    pass
            elif pkt.startswith("43"):           # ack
                i = 2
                while i < len(pkt) and pkt[i].isdigit():
                    i += 1
                try:
                    if want_ack is not None and int(pkt[2:i]) == want_ack:
                        payload = json.loads(pkt[i:])
                        found = payload[0] if payload else None
                except Exception:
                    pass
        return found

    def _emit_io(self, event, args, timeout=30, _retried=False):
        self._ack += 1
        ack_id = self._ack
        pkt = "42" + str(ack_id) + "[" + json.dumps(event) + (
            "" if not args else "," + ",".join(json.dumps(a) for a in args)) + "]"
        self._req("POST", pkt.encode(), timeout=15)
        deadline = time.time() + timeout
        while time.time() < deadline:
            txt = self._poll_once(timeout=35)
            if "Session ID unknown" in txt:
                raise TMNError("session reset")
            r = self._process(txt, want_ack=ack_id)
            if r is not _MISSING:
                return r
        raise TMNError(f"timeout: {event}")

    def _io_loop(self):
        need_handshake = True
        while self.alive:
            try:
                job = self._jobs.get(timeout=1)
            except queue.Empty:
                job = None
            if job is not None:
                event, args, timeout, done, holder = job
                try:
                    if event == "__handshake__":
                        self._handshake()
                        need_handshake = False
                        holder.append(True)
                    else:
                        if need_handshake:
                            self._handshake()
                            need_handshake = False
                        holder.append(self._emit_io(event, args, timeout))
                except Exception as e:            # noqa: BLE001
                    holder.append(e)
                finally:
                    done.set()
            else:
                # idle: ek poll cycle — naye mail ki push yahi aayegi
                try:
                    if need_handshake:
                        continue
                    txt = self._poll_once(timeout=35)
                    if "Session ID unknown" in txt:
                        self._handshake()
                        if self.token:
                            try:
                                self._emit_io("get_guest_user_data",
                                              ({"token": self.token},), 30)
                            except Exception:
                                pass
                    else:
                        self._process(txt)
                except Exception:
                    time.sleep(2)

    # ---------- public API (any thread) ----------
    def emit(self, event, *args, timeout=30):
        if not self.alive:
            self.connect()
            if event == "__handshake__":
                return True
        done = threading.Event()
        holder = []
        self._jobs.put((event, args, timeout, done, holder))
        if not done.wait(timeout + 40):
            raise TMNError(f"no response: {event}")
        result = holder[0] if holder else None
        if isinstance(result, Exception):
            raise result
        return result

    def guest_data(self, token=None):
        return self.emit("get_guest_user_data", {"token": token}, timeout=20)

    def restore(self, token):
        r = self.guest_data(token)
        if isinstance(r, dict) and r.get("activeEmail"):
            self.token = token
            self.email = r["activeEmail"].get("emailAddress")
        return r

    def _recover(self):
        """Session dobara establish karo — fresh handshake + guest/token init."""
        try:
            self._handshake()
        except Exception:
            pass
        try:
            if self.token:
                r = self.emit("get_guest_user_data", {"token": self.token}, timeout=20)
                if isinstance(r, dict) and r.get("visitor_data"):
                    return True
            r = self.emit("get_guest_user_data", {"token": None}, timeout=20)
            if isinstance(r, dict) and r.get("visitor_data"):
                self.token = None
                self.email = None
                return True
        except Exception:
            pass
        return False

    def _call(self, event, *args, timeout=30):
        """emit + session_expired/timeout/reset aaye to recover karke ek retry."""
        try:
            r = self.emit(event, *args, timeout=timeout)
        except TMNError:
            r = {"error": "session_expired"}
        if isinstance(r, dict) and r.get("error") == "session_expired":
            if self._recover():
                try:
                    r = self.emit(event, *args, timeout=timeout)
                except TMNError:
                    r = {"error": "timeout"}
        return r

    def generate(self):
        r = self._call("generate_temp_mail", timeout=30)
        if isinstance(r, dict) and r.get("emailData"):
            self.token = r.get("newToken") or self.token
            self.email = r["emailData"].get("emailAddress")
        return r

    def domains(self, domain_type=11):
        r = self._call("get_domains", {"domainType": domain_type}, timeout=30)
        if isinstance(r, dict):
            return [d["name"] for d in r.get("domains", []) if isinstance(d, dict)]
        return []

    def create_custom(self, alias, domain, email_type=1):
        r = self._call("create_email", {
            "alias": alias, "domain": domain, "emailType": email_type,
            "password": "", "captchaToken": "",
        }, timeout=30)
        if isinstance(r, dict) and r.get("emailData"):
            self.token = r.get("token") or self.token
            self.email = r["emailData"].get("emailAddress")
        return r

    def messages(self):
        return self._call("get_email_messages", {"messageType": "inbound"}, timeout=25)

    def message(self, message_id):
        return self._call("get_email_message", {"messageId": message_id}, timeout=25)

    def emails_list(self):
        return self._call("get_emails_list", timeout=30)

    def change_active(self, email_address):
        # Official client: payload {"newEmailAddress": ...} bhejta hai aur
        # response me NAYA token deta hai (rotation) — dono handle karo.
        r = self._call("change_active_email", {"newEmailAddress": email_address}, timeout=10)
        if isinstance(r, dict) and not r.get("error"):
            if r.get("token"):
                self.token = r["token"]
            self.email = email_address
        return r


class _Missing:
    pass


_MISSING = _Missing()
