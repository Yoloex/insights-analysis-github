import asyncio
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.prompts import ISSUE_INSIGHT_SYSTEM_PROMPT


class HTTPException(Exception):
    def __init__(self, status_code, detail, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    github_token: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:0.8b"
    database_path: str = "data/cache.sqlite3"
    cache_ttl_seconds: int = Field(default=900, ge=1)


class Cache:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires REAL)"
            )

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value, expires FROM cache WHERE key=?", (key,)).fetchone()
        if row and row[1] > datetime.now(UTC).timestamp():
            return json.loads(row[0])
        return None

    def put(self, key, value, ttl):
        with self.connect() as db:
            # Expired rows are removed on writes to bound old period entries.
            db.execute("DELETE FROM cache WHERE expires <= ?", (datetime.now(UTC).timestamp(),))
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (key, json.dumps(value), datetime.now(UTC).timestamp() + ttl),
            )


async def request_json(client, method, url, **kwargs):
    for attempt in range(3):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.RequestError:
            if attempt < 2:
                await asyncio.sleep(0.2 * (2**attempt))
                continue
            raise HTTPException(503, "Upstream service is unreachable") from None
        if response.status_code in (429, 403):
            raise HTTPException(
                503, "Upstream rate limit or access restriction", headers={"Retry-After": "60"}
            )
        if response.status_code == 404:
            raise HTTPException(404, "Repository or upstream resource not found")
        if response.status_code >= 500 and attempt < 2:
            await asyncio.sleep(0.2 * (2**attempt))
            continue
        if response.is_error:
            raise HTTPException(502, "Upstream service rejected the request")
        try:
            return response.json()
        except ValueError:
            raise HTTPException(502, "Upstream returned invalid JSON") from None


async def collect_metrics(client: httpx.AsyncClient, repo: str, start: date, end: date):
    base = f"https://api.github.com/repos/{repo}"
    candidates = []
    # GitHub's issues endpoint includes PRs. Updated ordering permits a safe time cutoff.
    for page in range(1, 11):
        batch = await request_json(
            client,
            "GET",
            f"{base}/issues",
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "since": f"{start.isoformat()}T00:00:00Z",
                "per_page": 100,
                "page": page,
            },
        )
        if not isinstance(batch, list):
            raise HTTPException(502, "Unexpected GitHub issues response")
        for issue in batch:
            if "pull_request" not in issue and issue.get("closed_at"):
                closed = date.fromisoformat(issue["closed_at"][:10])
                if start <= closed < end:
                    candidates.append(issue)
        if len(batch) < 100:
            break
    else:
        raise HTTPException(422, "Scan exceeded 1,000 updated items; choose a smaller period")
    if len(candidates) > 200:
        raise HTTPException(422, "Period contains over 200 closed issues; choose a smaller period")

    semaphore = asyncio.Semaphore(5)

    async def detail(issue):
        async with semaphore:
            return await request_json(client, "GET", f"{base}/issues/{int(issue['number'])}")

    details = await asyncio.gather(*(detail(issue) for issue in candidates))
    counts = {}
    evidence = []
    for issue in details:
        # Recheck after detail fetch: issues can be reopened during collection.
        closed_at = issue.get("closed_at")
        if not closed_at or not start <= date.fromisoformat(closed_at[:10]) < end:
            continue
        actor = (issue.get("closed_by") or {}).get("login", "unknown")
        counts[actor] = counts.get(actor, 0) + 1
        evidence.append(
            {
                "number": issue["number"],
                "closed_at": closed_at,
                "closed_by": actor,
                "url": f"https://github.com/{repo}/issues/{issue['number']}",
            }
        )
    total = len(evidence)
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    facts = {
        "total_closed": total,
        "unique_closers": len(counts),
        "top_closer_share": round(ranked[0][1] / total, 4) if total else 0,
    }
    return {
        "repo": repo,
        "start": str(start),
        "end_exclusive": str(end),
        "fetched_at": datetime.now(UTC).isoformat(),
        "facts": facts,
        "closers": [{"login": login, "closed": count} for login, count in ranked],
        "issues": sorted(evidence, key=lambda x: x["number"]),
        "limitations": [
            "Current closed issues, not historical closure events or productivity.",
            "Closer is the closing actor, not necessarily the person who did the work.",
            "Concurrent GitHub updates may affect pagination; this is not a snapshot.",
        ],
    }


class Evidence(BaseModel):
    model_config = {"extra": "forbid"}
    metric: str
    value: float


class Narrative(BaseModel):
    model_config = {"extra": "forbid"}
    narrative: str = Field(min_length=1, max_length=1600)
    hypothesis: str = Field(min_length=1, max_length=800)
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1, max_length=3)


async def generate_insight(client, settings, metrics):
    facts = metrics["facts"]
    result = await request_json(
        client,
        "POST",
        f"{settings.ollama_base_url.rstrip('/')}/api/chat",
        json={
            "model": settings.ollama_model,
            "stream": False,
            "think": False,
            "format": Narrative.model_json_schema(),
            "options": {"temperature": 0, "num_predict": 1000, "num_ctx": 4096},
            "messages": [
                {
                    "role": "system",
                    "content": ISSUE_INSIGHT_SYSTEM_PROMPT,
                },
                {"role": "user", "content": json.dumps(facts)},
            ],
        },
    )
    try:
        output = Narrative.model_validate_json(result["message"]["content"])
        for item in output.evidence:
            if item.metric not in facts or item.value != facts[item.metric]:
                raise ValueError("Ungrounded evidence")
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            502, "Model output failed schema or evidence validation; retry"
        ) from None
    return {
        "model": settings.ollama_model,
        **output.model_dump(),
        "confidence_note": "Model-reported subjective confidence; not a calibrated probability.",
        "metrics": metrics,
    }
