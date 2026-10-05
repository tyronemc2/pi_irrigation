"""Web dashboard and JSON API."""
from __future__ import annotations

import hmac
from datetime import timedelta
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from .climate import validate_rule
from .scheduler import validate_schedule


def create_app(system) -> Flask:
    app = Flask(__name__)
    cfg = system.cfg
    password = cfg["web"].get("password") or ""

    def auth(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if password:
                a_ = request.authorization
                if not (a_ and a_.username == "admin"
                        and hmac.compare_digest(a_.password or "", password)):
                    return Response("Login required", 401,
                                    {"WWW-Authenticate": 'Basic realm="Greenhouse"'})
            return fn(*a, **kw)
        return wrapper

    def body() -> dict:
        return request.get_json(silent=True) or {}

    @app.get("/sw.js")
    def service_worker():  # must be served from the root to control the whole app
        r = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(app.static_folder, "manifest.webmanifest",
                                   mimetype="application/manifest+json")

    @app.get("/")
    @auth
    def index():
        return render_template("index.html", site=cfg["location"].get("name", "Greenhouse"))

    @app.get("/api/status")
    @auth
    def status():
        return jsonify(system.dashboard_state())

    @app.post("/api/zones/<int:zone_id>/run")
    @auth
    def run_zone(zone_id):
        ok, msg = system.controller.request_run(zone_id, body().get("minutes", 5), "Manual")
        return jsonify(ok=ok, message=msg), (200 if ok else 409)

    @app.post("/api/stop")
    @auth
    def stop():
        system.controller.stop()
        return jsonify(ok=True, message="All water stopped")

    @app.post("/api/refill")
    @auth
    def refill():
        ok, msg = system.controller.refill(str(body().get("action", "")))
        return jsonify(ok=ok, message=msg), (200 if ok else 409)

    @app.post("/api/pause")
    @auth
    def pause():
        try:
            days = int(body().get("days", 1))
        except (TypeError, ValueError):
            return jsonify(ok=False, message="Days must be a whole number"), 400
        if not 1 <= days <= 30:
            return jsonify(ok=False, message="Pause for 1 to 30 days"), 400
        until = system.now() + timedelta(days=days)
        system.store.set("paused_until", until.isoformat(timespec="seconds"))
        return jsonify(ok=True, message=f"Schedules paused for {days} day{'s' * (days > 1)}")

    @app.post("/api/resume")
    @auth
    def resume():
        system.store.set("paused_until", None)
        return jsonify(ok=True, message="Schedules resumed")

    def _clean(data):
        return validate_schedule(data, system.controller.zones.keys(),
                                 cfg["safety"]["max_run_minutes"])

    @app.post("/api/schedules")
    @auth
    def add_schedule():
        try:
            s = system.store.add_schedule(_clean(body()))
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        return jsonify(ok=True, message="Schedule saved", schedule=s)

    @app.put("/api/schedules/<int:sid>")
    @auth
    def update_schedule(sid):
        try:
            s = system.store.update_schedule(sid, _clean(body()))
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        if not s:
            return jsonify(ok=False, message="Schedule not found"), 404
        return jsonify(ok=True, message="Schedule saved", schedule=s)

    @app.delete("/api/schedules/<int:sid>")
    @auth
    def delete_schedule(sid):
        if not system.store.delete_schedule(sid):
            return jsonify(ok=False, message="Schedule not found"), 404
        return jsonify(ok=True, message="Schedule deleted")

    @app.get("/api/temperature")
    @auth
    def temperature():
        now = system.now()
        return jsonify(now=now.isoformat(timespec="seconds"), current=system.climate.air_temp(),
                       average_10min=system.climate.log.smoothed(now),
                       hours=system.climate.log.series(now))

    def _clean_rule(data):
        return validate_rule(data, system.controller.zones.keys(), cfg["safety"]["max_run_minutes"])

    @app.post("/api/temp-rules")
    @auth
    def add_rule():
        try:
            r = system.store.add_rule(_clean_rule(body()))
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        return jsonify(ok=True, message="Temperature rule saved", rule=r)

    @app.put("/api/temp-rules/<int:rid>")
    @auth
    def update_rule(rid):
        try:
            r = system.store.update_rule(rid, _clean_rule(body()))
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        if not r:
            return jsonify(ok=False, message="Rule not found"), 404
        return jsonify(ok=True, message="Temperature rule saved", rule=r)

    @app.delete("/api/temp-rules/<int:rid>")
    @auth
    def delete_rule(rid):
        if not system.store.delete_rule(rid):
            return jsonify(ok=False, message="Rule not found"), 404
        return jsonify(ok=True, message="Temperature rule deleted")

    @app.post("/api/notify-test")
    @auth
    def notify_test():
        if not system.notifier.enabled:
            return jsonify(ok=False, message="Notifications are off. Set an ntfy topic in config.yaml"), 409
        system.notifier.send("Greenhouse test", "Notifications are working.")
        return jsonify(ok=True, message="Test notification sent")

    @app.get("/healthz")
    def health():
        return jsonify(ok=True, controller_online=system.controller.online)

    return app
