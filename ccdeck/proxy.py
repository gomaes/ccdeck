"""Front server: token check + raw HTTP/WebSocket relay to ttyd (/tty/*) or to the internal Flask app.

Implemented at the TCP level with the standard library only, so WebSocket upgrades to ttyd
work without extra dependencies. Every relayed request is forced to `Connection: close`
(one request per connection), so authentication is checked for every request."""
from __future__ import annotations

import base64
import hmac
import selectors
import socket
import socketserver
import sys
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

COOKIE_NAME = "ccdeck_token"
MAX_HEAD = 64 * 1024

# Injected into ttyd's index.html: report WebSocket open/close to the parent page, which
# implements exponential-backoff reconnection (ttyd's own reconnect is disabled).
INJECT = (b"<script>(function(){var W=window.WebSocket;if(!W||window.parent===window)return;"
          b"function post(m){try{window.parent.postMessage(m,location.origin)}catch(e){}}"
          b"function P(u,p){var s=p===undefined?new W(u):new W(u,p);"
          b"s.addEventListener('open',function(){post({ccdeck:'open'})});"
          b"s.addEventListener('close',function(e){post({ccdeck:'close',code:e.code})});return s}"
          b"P.prototype=W.prototype;P.CONNECTING=0;P.OPEN=1;P.CLOSING=2;P.CLOSED=3;window.WebSocket=P;"
          b"})();</script>")


def token_ok(expected, headers, query=None):
    """Check Bearer header, cookie or (optionally) ?token=. `headers` is a dict with lowercase keys."""
    if not expected:
        return False
    cands = []
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        cands.append(auth[7:].strip())
    cookie = headers.get("cookie")
    if cookie:
        try:
            c = SimpleCookie()
            c.load(cookie)
            if COOKIE_NAME in c:
                cands.append(c[COOKIE_NAME].value)
        except Exception:
            pass
    if query:
        cands += query.get("token", [])
    return any(hmac.compare_digest(c.encode(), expected.encode()) for c in cands)


def read_head(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(8192)
        if not chunk:
            return None, None
        buf += chunk
        if len(buf) > MAX_HEAD:
            return None, None
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head, rest


def parse_head(head):
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ValueError("bad request line")
    headers = []
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers.append((k.strip(), v.strip()))
    return parts[0], parts[1], parts[2], headers


def build_head(method, target, version, headers):
    lines = ["%s %s %s" % (method, target, version)] + ["%s: %s" % kv for kv in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def simple_response(sock, status, text, extra=()):
    body = text.encode()
    hdr = ["HTTP/1.1 %s" % status, "Content-Type: text/plain; charset=utf-8",
           "Content-Length: %d" % len(body), "Connection: close"] + list(extra)
    try:
        sock.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode() + body)
    except OSError:
        pass


def pipe(a, b):
    """Bidirectional relay until both directions are closed."""
    sel = selectors.DefaultSelector()
    a.setblocking(False)
    b.setblocking(False)
    sel.register(a, selectors.EVENT_READ, b)
    sel.register(b, selectors.EVENT_READ, a)
    open_dirs = 2
    try:
        while open_dirs:
            for key, _ in sel.select(timeout=None):
                src, dst = key.fileobj, key.data
                try:
                    data = src.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    sel.unregister(src)
                    open_dirs -= 1
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                dst.setblocking(True)
                try:
                    dst.sendall(data)
                except OSError:
                    return
                finally:
                    dst.setblocking(False)
    finally:
        sel.close()


def _dechunk(body):
    out, i = b"", 0
    while True:
        j = body.find(b"\r\n", i)
        if j < 0:
            return out
        size = int(body[i:j].split(b";")[0] or b"0", 16)
        if size == 0:
            return out
        out += body[j + 2:j + 2 + size]
        i = j + 2 + size + 2


def inject_script(resp):
    """Insert INJECT into an HTML response (non-compressed). Returns the rewritten response bytes."""
    if b"\r\n\r\n" not in resp:
        return resp
    head, body = resp.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    hdrs = [ln for ln in lines[1:]]
    lower = [ln.lower() for ln in hdrs]
    if not lines[0].split(b" ")[1:2] == [b"200"] or not any(l.startswith(b"content-type: text/html") for l in lower):
        return resp
    if any(l.startswith(b"content-encoding:") and b"identity" not in l for l in lower):
        return resp
    if any(l.startswith(b"transfer-encoding:") and b"chunked" in l for l in lower):
        body = _dechunk(body)
    idx = body.find(b"<head>")
    body = body[:idx + 6] + INJECT + body[idx + 6:] if idx >= 0 else INJECT + body
    keep = [h for h, l in zip(hdrs, lower)
            if not (l.startswith(b"content-length:") or l.startswith(b"transfer-encoding:")
                    or l.startswith(b"connection:"))]
    keep += [b"Content-Length: %d" % len(body), b"Connection: close", b"Cache-Control: no-store"]
    return b"\r\n".join([lines[0]] + keep) + b"\r\n\r\n" + body


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        ctx = self.server.ctx
        sock = self.request
        sock.settimeout(30)
        try:
            head, rest = read_head(sock)
            if head is None:
                return
            method, target, version, headers = parse_head(head)
        except (OSError, ValueError):
            return
        hmap = {k.lower(): v for k, v in headers}
        url = urlsplit(target)
        is_tty = url.path == "/tty" or url.path.startswith("/tty/")
        is_ws = "websocket" in hmap.get("upgrade", "").lower()
        drop = {"connection", "keep-alive", "proxy-connection"}
        if is_tty:
            if not token_ok(ctx["token"](), hmap):
                simple_response(sock, "401 Unauthorized", "unauthorized\n")
                return
            upstream = ("127.0.0.1", ctx["ttyd_port"])
            drop |= {"cookie", "authorization"}
            inject = method == "GET" and url.path in ("/tty", "/tty/") and not is_ws
            if inject:
                drop.add("accept-encoding")
        else:
            upstream = ("127.0.0.1", ctx["app_port"]())
            inject = False
            drop |= {"x-forwarded-for", "x-ccdeck-peer"}
        new = [(k, v) for k, v in headers if k.lower() not in drop]
        if is_ws:
            new += [("Connection", "Upgrade")]
        else:
            new += [("Connection", "close")]
        if is_tty and ctx.get("ttyd_cred"):
            new.append(("Authorization", "Basic " + base64.b64encode(ctx["ttyd_cred"].encode()).decode()))
        if not is_tty:
            new.append(("X-Forwarded-For", self.client_address[0]))
        try:
            up = socket.create_connection(upstream, timeout=10)
        except OSError:
            simple_response(sock, "502 Bad Gateway", "upstream (%s) unavailable\n" % ("ttyd" if is_tty else "app"))
            return
        try:
            up.sendall(build_head(method, target, version, new) + rest)
            if inject:
                up.settimeout(30)
                resp = b""
                while True:
                    chunk = up.recv(65536)
                    if not chunk:
                        break
                    resp += chunk
                sock.sendall(inject_script(resp))
                return
            sock.settimeout(None)
            up.settimeout(None)
            pipe(sock, up)
        except OSError:
            pass
        finally:
            try:
                up.close()
            except OSError:
                pass


class FrontServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bind, port, ctx):
        self.ctx = ctx
        if ":" in bind:
            self.address_family = socket.AF_INET6
        super().__init__((bind, port), _Handler)

    def handle_error(self, request, client_address):  # pragma: no cover
        print("proxy error from %s: %r" % (client_address, sys.exc_info()[1]), file=sys.stderr)
