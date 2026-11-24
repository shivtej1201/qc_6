"""A tiny SMTP server for tests: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT. Keeps every message it receives."""
import base64
import socket
import socketserver
import threading
from email import message_from_bytes, policy


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        sink = self.server.sink
        w = lambda s: self.wfile.write(s.encode() + b"\r\n")      # noqa: E731
        w("220 sink ready")
        mail = {"from": None, "to": [], "auth": None}
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode(errors="replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                w("250-sink")
                w("250-AUTH PLAIN")
                w("250 OK")
            elif up.startswith("AUTH PLAIN"):
                raw = base64.b64decode(cmd.split()[2]).split(b"\0")
                user, pw = raw[1].decode(), raw[2].decode()
                if sink.password is not None and (user, pw) != (sink.user, sink.password):
                    w("535 bad credentials")
                    continue
                mail["auth"] = (user, pw)
                w("235 ok")
            elif up.startswith("MAIL FROM"):
                if sink.require_auth and not mail["auth"]:
                    w("530 auth first")
                    continue
                mail["from"] = cmd.split(":", 1)[1].strip(" <>")
                w("250 OK")
            elif up.startswith("RCPT TO"):
                mail["to"].append(cmd.split(":", 1)[1].strip(" <>"))
                w("250 OK")
            elif up == "DATA":
                w("354 go")
                buf = b""
                while True:
                    l2 = self.rfile.readline()
                    if l2 in (b".\r\n", b""):
                        break
                    buf += l2[1:] if l2.startswith(b"..") else l2
                sink.messages.append({**mail, "msg": message_from_bytes(buf, policy=policy.default)})
                mail = {"from": None, "to": [], "auth": mail["auth"]}
                w("250 queued")
            elif up == "QUIT":
                w("221 bye")
                return
            else:
                w("250 OK")


class SmtpSink:
    def __init__(self, user="qc-bot", password="s3cret!", require_auth=True):
        self.user, self.password, self.require_auth, self.messages = user, password, require_auth, []
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.srv.daemon_threads = True
        self.srv.sink = self
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
