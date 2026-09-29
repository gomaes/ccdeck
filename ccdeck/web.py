"""Flask application: REST API + the single-page UI."""
from __future__ import annotations

import pkgutil

from flask import Flask, Response, jsonify, redirect, request

from . import CCDeckError, __version__, validate_name
from .proxy import COOKIE_NAME, token_ok

PUBLIC = {"/api/health", "/api/login", "/", "/favicon.ico"}
COOKIE_MAX_AGE = 365 * 24 * 3600


def _index_html():
    return pkgutil.get_data("ccdeck", "static/index.html")


def create_app(manager, token_getter, cookie_secure=False):
    app = Flask(__name__, static_folder=None)
    index = _index_html()

    def authed():
        hmap = {k.lower(): v for k, v in request.headers.items()}
        return token_ok(token_getter(), hmap)

    def set_cookie(resp):
        resp.set_cookie(COOKIE_NAME, token_getter(), max_age=COOKIE_MAX_AGE, httponly=True,
                        samesite="Strict", secure=bool(cookie_secure), path="/")
        return resp

    @app.before_request
    def _auth():
        if request.path in PUBLIC:
            return None
        if not authed():
            return jsonify(error="unauthorized"), 401
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # CSRF: cookie-authenticated writes must carry a custom header (forces CORS preflight)
            bearer = request.headers.get("Authorization", "").lower().startswith("bearer ")
            if not bearer and request.headers.get("X-CCDeck") != "1":
                return jsonify(error="missing X-CCDeck header"), 403
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    @app.errorhandler(CCDeckError)
    def _err(e):
        return jsonify(error=str(e)), e.status

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    # -- UI / auth -----------------------------------------------------------------
    @app.route("/")
    def index_page():
        tok = request.args.get("token")
        if tok is not None:
            resp = redirect("/")
            if token_ok(token_getter(), {}, {"token": [tok]}):
                set_cookie(resp)
            return resp
        return Response(index, mimetype="text/html")

    @app.route("/favicon.ico")
    def favicon():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" '
               'rx="3" fill="#1f6feb"/><path d="M3 5l3 3-3 3M8 11h5" stroke="#fff" stroke-width="1.6" '
               'fill="none"/></svg>')
        return Response(svg, mimetype="image/svg+xml")

    @app.route("/api/health")
    def health():
        return jsonify(ok=True, version=__version__)

    @app.route("/api/login", methods=["POST"])
    def login():
        tok = str(body().get("token", ""))
        if not token_ok(token_getter(), {}, {"token": [tok]}):
            return jsonify(error="invalid token"), 401
        return set_cookie(jsonify(ok=True))

    @app.route("/api/logout", methods=["POST"])
    def logout():
        resp = jsonify(ok=True)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.route("/api/config")
    def config():
        d = manager.cfg["defaults"]
        w = manager.cfg["watchdog"]
        return jsonify(version=__version__, defaults={
            "cmd": d["cmd"], "dir": d["dir"], "auto_restore": d["auto_restore"],
            "auto_continue_rate_limit": d["auto_continue_rate_limit"],
            "auto_continue_stall": d["auto_continue_stall"], "continue_text": d["continue_text"]},
            watchdog={"idle_seconds": w["idle_seconds"], "stall_seconds": w["stall_seconds"]})

    # -- sessions ------------------------------------------------------------------
    @app.route("/api/sessions")
    def sessions():
        return jsonify(sessions=manager.list_status())

    @app.route("/api/sessions", methods=["POST"])
    def create():
        b = body()
        rec = manager.create(str(b.get("name", "")), cwd=b.get("dir") or None, cmd=b.get("cmd") or None,
                             auto_restore=b.get("auto_restore"), auto_continue=b.get("auto_continue"),
                             title=b.get("title") or None)
        return jsonify(session=rec), 201

    def one(name):
        validate_name(name)
        for s in manager.list_status():
            if s["name"] == name:
                return s
        raise CCDeckError("no such session: %s" % name, 404)

    @app.route("/api/sessions/<name>")
    def get_one(name):
        return jsonify(session=one(name))

    @app.route("/api/sessions/<name>", methods=["PATCH"])
    def patch(name):
        validate_name(name)
        b = body()
        manager.set_options(name, auto_restore=b.get("auto_restore"), auto_continue=b.get("auto_continue"))
        return jsonify(session=one(name))

    @app.route("/api/sessions/<name>", methods=["DELETE"])
    def delete(name):
        manager.delete(validate_name(name))
        return jsonify(ok=True)

    @app.route("/api/sessions/<name>/stop", methods=["POST"])
    def stop(name):
        manager.stop(validate_name(name))
        return jsonify(session=one(name))

    @app.route("/api/sessions/<name>/restart", methods=["POST"])
    def restart(name):
        manager.restart(validate_name(name), fresh=bool(body().get("fresh")))
        return jsonify(session=one(name))

    @app.route("/api/sessions/<name>/resume", methods=["POST"])
    def resume(name):
        manager.resume(validate_name(name), force=bool(body().get("force")))
        return jsonify(session=one(name))

    @app.route("/api/sessions/<name>/rename", methods=["POST"])
    def rename(name):
        new = str(body().get("new_name", ""))
        rec = manager.rename(validate_name(name), new)
        return jsonify(session=one(rec["name"]))

    @app.route("/api/sessions/<name>/keys", methods=["POST"])
    def keys(name):
        b = body()
        manager.send(validate_name(name), key=b.get("key") or None, text=b.get("text") or None,
                     enter=bool(b.get("enter")))
        return jsonify(ok=True)

    @app.route("/api/sessions/<name>/log")
    def log(name):
        lines = request.args.get("lines", "2000")
        if lines != "all":
            try:
                lines = max(1, min(int(lines), 1000000))
            except ValueError:
                raise CCDeckError("bad lines")
        text = manager.log(validate_name(name), lines=lines)
        if request.args.get("format") == "text":
            return Response(text, mimetype="text/plain")
        return jsonify(name=name, text=text)

    @app.route("/api/restore-all", methods=["POST"])
    def restore_all():
        return jsonify(restored=manager.resume_all())

    return app
