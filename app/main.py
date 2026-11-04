import asyncio
import hashlib
import json
import re
import threading
from datetime import UTC, date, datetime

import httpx
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException as WerkzeugHTTPException

from app.core import Cache, HTTPException, Settings, collect_metrics, generate_insight


def create_app(settings=None):
    app = Flask(__name__)
    settings = settings or Settings()
    cache = Cache(settings.database_path)
    metrics_lock = threading.Lock()
    insight_lock = threading.Lock()

    def selection():
        repo = request.args.get("repo", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repo):
            raise HTTPException(422, "repo must be owner/name")
        if repo.split("/")[1] in (".", ".."):
            raise HTTPException(422, "Invalid repository name")
        try:
            start = date.fromisoformat(request.args.get("start", ""))
            end = date.fromisoformat(request.args.get("end", ""))
        except ValueError:
            raise HTTPException(422, "start and end must be ISO dates (YYYY-MM-DD)") from None
        if not 0 < (end - start).days <= 90:
            raise HTTPException(422, "Period must span 1?90 days; end is exclusive")
        if end > datetime.now(UTC).date():
            raise HTTPException(422, "end must be today or earlier (UTC)")
        return repo.lower(), start, end

    async def fetch_metrics(repo, start, end):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tempo-assessment",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            return await collect_metrics(client, repo, start, end)

    def cached_metrics(period):
        repo, start, end = period
        key = f"metrics:v1:{repo}:{start}:{end}"
        with metrics_lock:
            result = cache.get(key)
            if result is None:
                result = asyncio.run(fetch_metrics(repo, start, end))
                cache.put(key, result, settings.cache_ttl_seconds)
        return result

    async def fetch_insight(numbers):
        async with httpx.AsyncClient(timeout=180) as client:
            return await generate_insight(client, settings, numbers)

    @app.get("/api/metrics")
    def metrics():
        return jsonify(cached_metrics(selection()))

    @app.post("/api/insights")
    def insights():
        numbers = cached_metrics(selection())
        digest = hashlib.sha256(json.dumps(numbers, sort_keys=True).encode()).hexdigest()
        key = f"insight:v2:{settings.ollama_model}:{digest}"
        with insight_lock:
            result = cache.get(key)
            if result is None:
                result = asyncio.run(fetch_insight(numbers))
                cache.put(key, result, settings.cache_ttl_seconds)
        return jsonify(result)

    @app.get("/health")
    def health():
        cache.get("health")
        return jsonify(status="ok", dependencies="SQLite checked; upstreams checked on use")

    @app.errorhandler(HTTPException)
    def service_error(error):
        return jsonify(detail=error.detail), error.status_code, error.headers

    @app.errorhandler(WerkzeugHTTPException)
    def http_error(error):
        return jsonify(detail=error.description), error.code

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", title="Tempo ? GitHub Insights")

    @app.get("/docs")
    def docs():
        return render_template("docs.html")

    return app
