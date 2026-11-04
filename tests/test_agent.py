from datetime import UTC, date, datetime, timedelta

import pytest

from app.agent import chat, parse_selection
from app.core import HTTPException, Settings
from app.main import create_app


def test_selection_context_and_period():
    today = datetime.now(UTC).date()
    repo, start, end = parse_selection(
        "And the last 7 days?",
        [
            {"role": "user", "content": "What about https://github.com/pallets/flask?"},
            {"role": "assistant", "content": "Try someone/else"},
        ],
    )
    assert repo == "pallets/flask"
    assert (start, end) == (today - timedelta(days=7), today)
    assert parse_selection("pallets/flask last week", [])[1:] == (today - timedelta(days=7), today)
    with pytest.raises(HTTPException):
        parse_selection("https://evil.example/a/b", [])
    with pytest.raises(HTTPException):
        parse_selection("pallets/flask last 99999 days", [])
    assert parse_selection(
        "compare 2026-01-01 to 2026-01-15", [{"role": "user", "content": "pallets/flask"}]
    )[1:] == (date(2026, 1, 1), date(2026, 1, 15))
    with pytest.raises(HTTPException):
        parse_selection("pallets/flask last 365 days", [])


async def test_agent_calls_tool_and_returns_answer(monkeypatch):
    replies = iter(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "repository_context", "arguments": {}}}],
                }
            },
            {"message": {"role": "assistant", "content": "The default branch is main."}},
        ]
    )

    async def model(client, method, url, **kwargs):
        return next(replies)

    called = []

    async def tool(name, repo, start, end):
        called.append((name, repo))
        return {"default_branch": "main"}

    monkeypatch.setattr("app.agent.request_json", model)
    result = await chat("What is pallets/flask's default branch?", [], Settings(), tool)
    assert called == [("repository_context", "pallets/flask")]
    assert result["tools"][0]["data"]["default_branch"] == "main"
    assert result["reply"] == "The default branch is main."


async def test_agent_rejects_model_supplied_tool_arguments(monkeypatch):
    async def model(*args, **kwargs):
        return {
            "message": {
                "tool_calls": [
                    {
                        "function": {
                            "name": "repository_context",
                            "arguments": {"repo": "other/repo"},
                        }
                    }
                ]
            }
        }

    async def tool(*args):
        raise AssertionError("Untrusted tool arguments must not execute")

    monkeypatch.setattr("app.agent.request_json", model)
    with pytest.raises(HTTPException) as exc:
        await chat("pallets/flask metadata", [], Settings(), tool)
    assert exc.value.status_code == 502


def test_chat_and_tool_http_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cache.db"))
    client = create_app().test_client()
    assert client.post("/api/chat", json={"message": "hello"}).get_json()["repo"] is None
    assert (
        client.post(
            "/api/chat", json={"message": "x", "history": [{"role": "tool", "content": "x"}]}
        ).status_code
        == 422
    )
    assert (
        client.get("/api/tools/not_a_tool?repo=a/b&start=2026-01-01&end=2026-01-02").status_code
        == 404
    )
