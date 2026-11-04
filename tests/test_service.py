from datetime import date

import httpx
import pytest

from app.core import Cache, HTTPException, Settings, collect_metrics, generate_insight
from app.main import create_app
from app.prompts import ISSUE_INSIGHT_SYSTEM_PROMPT


async def test_metrics_excludes_prs_and_uses_closer():
    def handler(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(
                200,
                json=[
                    {"number": 1, "closed_at": "2026-01-02T01:00:00Z"},
                    {"number": 2, "closed_at": "2026-01-02T01:00:00Z", "pull_request": {}},
                    {"number": 3, "closed_at": "2026-01-03T00:00:00Z"},
                ],
            )
        return httpx.Response(
            200,
            json={
                "number": 1,
                "closed_at": "2026-01-02T01:00:00Z",
                "user": {"login": "author"},
                "closed_by": {"login": "closer"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect_metrics(client, "a/b", date(2026, 1, 1), date(2026, 1, 3))
    assert result["facts"] == {"total_closed": 1, "unique_closers": 1, "top_closer_share": 1}
    assert result["closers"] == [{"login": "closer", "closed": 1}]


async def test_scan_limit_is_not_silently_partial():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[{"pull_request": {}}] * 100)
        )
    ) as client:
        with pytest.raises(HTTPException) as error:
            await collect_metrics(client, "a/b", date(2026, 1, 1), date(2026, 1, 3))
    assert error.value.status_code == 422


async def test_rate_limit():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403))
    ) as client:
        with pytest.raises(HTTPException) as error:
            await collect_metrics(client, "a/b", date(2026, 1, 1), date(2026, 1, 3))
    assert error.value.status_code == 503
    assert error.value.headers["Retry-After"] == "60"


@pytest.mark.parametrize("value,expected", [(2, 2), (999, 502)])
async def test_insight_evidence_validation(value, expected):
    import json

    output = {
        "narrative": "Two issues closed.",
        "hypothesis": "Possible triage activity.",
        "confidence": 0.4,
        "evidence": [{"metric": "total_closed", "value": value}],
    }

    def handler(request):
        payload = json.loads(request.content)
        assert payload["messages"][0] == {"role": "system", "content": ISSUE_INSIGHT_SYSTEM_PROMPT}
        assert json.loads(payload["messages"][1]["content"]) == {"total_closed": 2}
        return httpx.Response(200, json={"message": {"content": json.dumps(output)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if expected == 502:
            with pytest.raises(HTTPException) as error:
                await generate_insight(client, Settings(), {"facts": {"total_closed": 2}})
            assert error.value.status_code == 502
        else:
            result = await generate_insight(client, Settings(), {"facts": {"total_closed": 2}})
            assert result["evidence"][0]["value"] == 2


def test_cache_persists_and_expires(tmp_path):
    path = str(tmp_path / "cache.db")
    Cache(path).put("a", {"x": 1}, 60)
    assert Cache(path).get("a") == {"x": 1}
    Cache(path).put("old", {}, -1)
    assert Cache(path).get("old") is None


def test_http_validation_and_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "http.db"))
    with create_app().test_client() as client:
        assert client.get("/health").status_code == 200
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert b"Tempo" in dashboard.data
        assert b"{{" not in dashboard.data
        assert client.get("/docs").status_code == 200
        assert client.get("/api/metrics").status_code == 422
        assert client.get("/missing").get_json()["detail"]
        assert client.get("/health").headers["X-Content-Type-Options"] == "nosniff"
        for repo, start, end in [
            ("../evil", "2026-01-01", "2026-01-02"),
            ("a/b", "2026-01-02", "2026-01-01"),
            ("a/b", "2026-01-01", "2026-05-01"),
            ("a/b", "2026-01-01", "2099-01-01"),
        ]:
            assert (
                client.get(
                    "/api/metrics", query_string={"repo": repo, "start": start, "end": end}
                ).status_code
                == 422
            )


def test_http_cache_reuses_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "http.db"))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    with create_app().test_client() as client:
        original_client = httpx.AsyncClient
        monkeypatch.setattr(
            "app.main.httpx.AsyncClient",
            lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
        )
        params = {"repo": "a/b", "start": "2026-01-01", "end": "2026-01-02"}
        first = client.get("/api/metrics", query_string=params)
        second = client.get("/api/metrics", query_string=params)
        assert first.status_code == 200
        assert first.get_json() == second.get_json()
        assert len(calls) == 1


def test_flask_insights_and_error_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "insights.db"))
    calls = []

    async def collect(*args):
        return {"facts": {"total_closed": 0}}

    async def generate(*args):
        calls.append(1)
        return {"narrative": "No data", "evidence": [{"metric": "total_closed", "value": 0}]}

    monkeypatch.setattr("app.main.collect_metrics", collect)
    monkeypatch.setattr("app.main.generate_insight", generate)
    client = create_app().test_client()
    query = {"repo": "a/b", "start": "2026-01-01", "end": "2026-01-02"}
    first = client.post("/api/insights", query_string=query)
    assert first.status_code == 200
    assert client.post("/api/insights", query_string=query).get_json() == first.get_json()
    assert len(calls) == 1
    assert client.get("/api/insights", query_string=query).status_code == 405

    async def limited(*args):
        raise HTTPException(503, "Rate limited", {"Retry-After": "60"})

    monkeypatch.setattr("app.main.collect_metrics", limited)
    query["repo"] = "a/c"
    response = client.get("/api/metrics", query_string=query)
    assert response.status_code == 503
    assert response.get_json() == {"detail": "Rate limited"}
    assert response.headers["Retry-After"] == "60"
