import json
import re
from datetime import UTC, date, datetime, timedelta

import httpx

from app.core import HTTPException, request_json
from app.github_tools import TOOL_DEFINITIONS, TOOL_NAMES
from app.prompts import AGENT_SYSTEM_PROMPT

REPO_PATTERN = re.compile(
    r"(?<![\w./-])(?:https?://github\.com/)?"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100})(?![\w./-])",
    re.IGNORECASE,
)


def parse_selection(message, history):
    # Only user messages supply repository context; assistant output is not trusted as input.
    if re.search(r"https?://(?!github\.com/)[^\s]+", message, re.IGNORECASE):
        raise HTTPException(422, "Use a public github.com repository URL or owner/repo")
    texts = [item["content"] for item in history if item["role"] == "user"] + [message]
    repo = None
    for text in reversed(texts):
        matches = REPO_PATTERN.findall(text)
        if matches:
            repo = matches[-1].lower()
            break
    if repo and repo.split("/")[1] in (".", ".."):
        repo = None
    today = datetime.now(UTC).date()
    start, end = today - timedelta(days=30), today
    # A current request can override a prior period, otherwise retain the last explicit period.
    for text in reversed(texts):
        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if dates:
            if len(dates) != 2:
                raise HTTPException(422, "Provide both start and end dates in YYYY-MM-DD format")
            try:
                start, end = (date.fromisoformat(value) for value in dates)
            except ValueError:
                raise HTTPException(422, "Invalid date; use YYYY-MM-DD") from None
            break
        days = re.search(r"\b(?:last|past)\s+(\d{1,5})\s+days?\b", text, re.IGNORECASE)
        if days:
            count = int(days.group(1))
            if not 1 <= count <= 90:
                raise HTTPException(422, "Period must span 1-90 days")
            end = today
            start = end - timedelta(days=count)
            break
        week = re.search(r"\b(?:last|past)\s+(?:a\s+|one\s+)?week\b", text, re.IGNORECASE)
        if week:
            end, start = today, today - timedelta(days=7)
            break
    if not 0 < (end - start).days <= 90 or end > today:
        raise HTTPException(422, "Period must span 1-90 days with an end no later than today (UTC)")
    return repo, start, end


def compact(value):
    if isinstance(value, list):
        return [compact(v) for v in value[:10]] + (
            [f"{len(value) - 10} additional entries omitted"] if len(value) > 10 else []
        )
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items()}
    if isinstance(value, str):
        return value[:300]
    return value


async def chat(message, history, settings, run_selected_tool):
    repo, start, end = parse_selection(message, history)
    if repo is None:
        return {
            "reply": "Please provide a public GitHub repository as owner/repo, then ask what you want to know.",
            "repo": None,
            "start": None,
            "end": None,
            "tools": [],
        }
    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    # History provides selection context only; prior assistant claims are not evidence.
    messages.append(
        {
            "role": "user",
            "content": f"{message}\nRepository: {repo}. Period: {start} through {end} (end exclusive, UTC).",
        }
    )
    results = []
    seen = set()
    async with httpx.AsyncClient(timeout=180) as client:
        for _ in range(5):
            response = await request_json(
                client,
                "POST",
                f"{settings.ollama_base_url.rstrip('/')}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "stream": False,
                    "think": False,
                    "tools": TOOL_DEFINITIONS if len(results) < 4 else [],
                    "messages": messages,
                    "options": {"temperature": 0, "num_predict": 900, "num_ctx": 8192},
                },
            )
            try:
                assistant = response["message"]
                calls = assistant.get("tool_calls") or []
                content = assistant.get("content") or ""
                if not isinstance(content, str) or not isinstance(calls, list):
                    raise TypeError
            except (KeyError, TypeError, ValueError):
                raise HTTPException(502, "Model returned an invalid response") from None
            if not calls:
                if not results:
                    # An ungrounded model response is not presented as an insight.
                    return {
                        "reply": "Please specify which insight you want, such as pull requests, reviews, issues, or releases.",
                        "repo": repo,
                        "start": str(start),
                        "end": str(end),
                        "tools": [],
                    }
                if not content.strip():
                    raise HTTPException(502, "Model did not produce an answer after using tools")
                return {
                    "reply": content.strip(),
                    "repo": repo,
                    "start": str(start),
                    "end": str(end),
                    "tools": results,
                }
            if len(calls) + len(results) > 4:
                calls = calls[: 4 - len(results)]
                assistant = {**assistant, "tool_calls": calls}
            messages.append(assistant)
            for call in calls:
                try:
                    function = call["function"]
                    name = function["name"]
                    args = function.get("arguments", {})
                    if name not in TOOL_NAMES or args not in ({}, None) or name in seen:
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    raise HTTPException(
                        502, "Model requested an invalid or repeated tool"
                    ) from None
                seen.add(name)
                data = await run_selected_tool(name, repo, start, end)
                results.append({"name": name, "data": data})
                summary = json.dumps(compact(data), ensure_ascii=True)
                if len(summary) > 24000:
                    raise HTTPException(502, "Tool result exceeds agent context budget")
                messages.append({"role": "tool", "tool_name": name, "content": summary})
            if len(results) == 4:
                messages.append(
                    {
                        "role": "user",
                        "content": "The tool budget is exhausted. Answer now using only the tool results; note that the analysis may be incomplete.",
                    }
                )
    raise HTTPException(502, "Model exceeded the agent iteration budget")
