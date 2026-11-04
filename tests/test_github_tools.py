from datetime import date

import httpx
import pytest

from app.core import HTTPException
from app.github_tools import TOOL_DEFINITIONS, TOOL_NAMES, run_tool

START = date(2026, 1, 2)
END = date(2026, 1, 4)


def issue(number, created, closed=None, author="issue-author", labels=None):
    return {
        "number": number,
        "created_at": created,
        "updated_at": "2026-01-04T01:00:00Z",
        "closed_at": closed,
        "user": {"login": author},
        "closed_by": {"login": "closer"},
        "labels": labels or [],
    }


def pull(number, created, closed=None, merged=None, author="pr-author"):
    return {
        "number": number,
        "created_at": created,
        "updated_at": "2026-01-04T01:00:00Z",
        "closed_at": closed,
        "merged_at": merged,
        "user": {"login": author},
    }


ISSUES = [
    issue(1, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", labels=[{"name": "bug"}]),
    issue(2, "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z"),
    issue(3, "2026-01-02T00:00:00Z", author="alice"),
    {**issue(4, "2026-01-02T00:00:00Z"), "pull_request": {}},
]
PULLS = [
    pull(10, "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z"),
    pull(11, "2026-01-03T00:00:00Z", author="alice"),
    pull(12, "2025-12-31T00:00:00Z", "2026-01-02T12:00:00Z", "2026-01-02T12:00:00Z"),
    pull(13, "2026-01-04T00:00:00Z"),
]
REVIEWS = {
    10: [
        {
            "id": 101,
            "submitted_at": "2026-01-03T00:00:00Z",
            "user": {"login": "reviewer"},
            "state": "APPROVED",
        },
        {
            "id": 102,
            "submitted_at": "2026-01-04T00:00:00Z",
            "user": {"login": "other"},
            "state": "COMMENTED",
        },
        {"id": 103, "submitted_at": None, "state": "PENDING"},
    ],
    11: [
        {
            "id": 104,
            "submitted_at": "2026-01-05T00:00:00Z",
            "user": {"login": "reviewer"},
            "state": "COMMENTED",
        }
    ],
}


def handler(request):
    assert request.url.host == "api.github.com"
    path = request.url.path
    if path == "/repos/a/b/issues":
        return httpx.Response(200, json=ISSUES)
    if path == "/repos/a/b/pulls":
        if request.url.params.get("state") == "open":
            return httpx.Response(200, json=[PULLS[0]])
        return httpx.Response(200, json=PULLS)
    if path.startswith("/repos/a/b/issues/"):
        return httpx.Response(
            200, json=next(i for i in ISSUES if str(i["number"]) == path.split("/")[-1])
        )
    if path.endswith("/reviews"):
        return httpx.Response(200, json=REVIEWS.get(int(path.split("/")[-2]), []))
    if path == "/repos/a/b/releases":
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "published_at": "2026-01-02T00:00:00Z",
                    "tag_name": "v1",
                    "author": {"login": "alice"},
                    "html_url": "https://evil.invalid/",
                },
                {"id": 8, "published_at": "2026-01-04T00:00:00Z"},
                {"id": 9, "published_at": None},
            ],
        )
    if path == "/repos/a/b":
        return httpx.Response(
            200,
            json={
                "description": "test",
                "html_url": "https://evil.invalid/",
                "open_issues_count": 2,
            },
        )
    raise AssertionError(path)


async def run(name, transport=handler, start=START, end=END):
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        return await run_tool(name, client, "a/b", start, end)


def test_definitions_are_exact_and_argument_free():
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == 11
    assert [d["function"]["name"] for d in TOOL_DEFINITIONS] == TOOL_NAMES
    for definition in TOOL_DEFINITIONS:
        assert definition["type"] == "function"
        assert definition["function"]["description"]
        assert definition["function"]["parameters"] == {
            "type": "object",
            "properties": {},
            "required": [],
        }


@pytest.mark.parametrize("name", TOOL_NAMES)
async def test_all_tools_have_period_and_limitations(name):
    result = await run(name)
    assert result["tool"] == name
    assert result["repo"] == "a/b"
    assert result["start"] == "2026-01-02"
    assert result["end_exclusive"] == "2026-01-04"
    assert result["facts"] is not None
    assert any("half-open" in x for x in result["limitations"])


async def test_issues_and_labels_use_current_closures():
    closures = await run("issue_closures")
    assert closures["facts"]["total_closed"] == 1
    assert closures["closers"] == [{"login": "closer", "count": 1}]
    assert closures["issues"][0]["url"] == "https://github.com/a/b/issues/1"
    labels = await run("labels_and_themes")
    assert labels["labels"] == [{"name": "bug", "count": 1}]
    distribution = await run("work_distribution")
    assert distribution["facts"]["top_closer_share"] == 1


async def test_prs_cycle_contributors_and_prior_period():
    activity = await run("pull_request_activity")
    assert activity["facts"] == {"created": 2, "closed": 2, "merged": 2, "open_now": 1}
    assert activity["open_pull_requests"][0]["url"].startswith("https://github.com/a/b/pull/")
    cycles = await run("merge_cycle_time")
    assert cycles["facts"]["count"] == 2
    assert cycles["facts"]["mean_hours"] == 42
    contributors = await run("contributor_activity")
    assert contributors["facts"] == {"issues_created": 2, "pull_requests_created": 2}
    assert {x["login"] for x in contributors["contributors"]} == {
        "alice",
        "issue-author",
        "pr-author",
    }
    trends = await run("activity_trends")
    assert trends["prior_start"] == "2025-12-31"
    assert trends["prior_end_exclusive"] == "2026-01-02"
    assert trends["facts"]["prior"]["issues_created"] == 1
    assert trends["facts"]["current"]["pull_requests_merged"] == 2


async def test_review_submission_is_distinct_from_first_review():
    activity = await run("review_activity")
    assert activity["facts"] == {"sampled_pull_requests": 2, "submitted_reviews": 1}
    turnaround = await run("review_turnaround")
    assert turnaround["facts"]["count"] == 2
    assert turnaround["facts"]["mean_hours"] == 36
    assert turnaround["reviews"][1]["first_review_at"] == "2026-01-05T00:00:00Z"


async def test_review_pages_are_scanned_and_overflow_errors():
    calls = []

    def pages(request):
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[PULLS[0]])
        if request.url.path.endswith("/reviews"):
            calls.append(int(request.url.params["page"]))
            if request.url.params["page"] == "1":
                return httpx.Response(
                    200, json=[{"id": i + 1, "submitted_at": None} for i in range(100)]
                )
            return httpx.Response(200, json=[{"id": 101, "submitted_at": "2026-01-03T00:00:00Z"}])
        raise AssertionError(request.url)

    with pytest.raises(HTTPException) as error:
        await run("review_activity", pages)
    assert error.value.status_code == 422
    assert calls == [1, 2]


async def test_ten_full_pages_fail_not_partial():
    calls = []

    def pages(request):
        calls.append(request)
        return httpx.Response(200, json=[{"pull_request": {}}] * 100)

    with pytest.raises(HTTPException) as error:
        await run("issue_closures", pages)
    assert error.value.status_code == 422
    assert len(calls) == 10


async def test_more_than_100_matching_pull_requests_fails():
    def pages(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                pull(i + (page - 1) * 100 + 1, "2026-01-02T00:00:00Z")
                for i in range(100 if page == 1 else 1)
            ],
        )

    with pytest.raises(HTTPException) as error:
        await run("pull_request_activity", pages)
    assert error.value.status_code == 422


async def test_fixed_targets_errors_and_boundary_query():
    seen = []

    def inspect(request):
        seen.append(request)
        if request.url.path.endswith("/issues"):
            assert request.url.params["since"] == "2026-01-01T23:59:59+00:00"
        return handler(request)

    releases = await run("release_activity", inspect)
    assert releases["facts"]["published"] == 1
    assert releases["releases"][0]["url"] == "https://api.github.com/repos/a/b/releases/7"
    await run("contributor_activity", inspect)
    context = await run("repository_context", inspect)
    assert context["facts"]["url"] == "https://github.com/a/b"
    assert all(r.url.host == "api.github.com" for r in seen)
    with pytest.raises(HTTPException) as error:
        await run("bad")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        await run("issue_closures", lambda r: httpx.Response(403))
    assert error.value.status_code == 503
    with pytest.raises(HTTPException) as error:
        await run("issue_closures", lambda r: httpx.Response(200, json={}))
    assert error.value.status_code == 502


async def test_review_pagination_and_first_review_order():
    calls = []

    def pages(request):
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[PULLS[0]])
        calls.append(int(request.url.params["page"]))
        if request.url.params["page"] == "1":
            # Pending reviews have no submission timestamp and must not be counted.
            return httpx.Response(
                200, json=[{"id": i + 1, "submitted_at": None} for i in range(100)]
            )
        return httpx.Response(200, json=[])

    result = await run("review_turnaround", pages)
    assert calls == [1, 2]
    assert result["facts"]["without_submitted_review"] == 1
    assert result["facts"]["mean_hours"] is None


async def test_pull_cutoff_and_detail_recheck():
    def cutoff(request):
        if request.url.path.endswith("/pulls"):
            if request.url.params.get("state") == "open":
                return httpx.Response(200, json=[])
            batch = [pull(i + 1, "2025-12-01T00:00:00Z") for i in range(100)]
            for item in batch:
                item["updated_at"] = "2025-12-31T00:00:00Z"
            return httpx.Response(200, json=batch)
        raise AssertionError(request.url)

    result = await run("pull_request_activity", cutoff)
    assert result["facts"] == {"created": 0, "closed": 0, "merged": 0, "open_now": 0}

    def reopened(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[ISSUES[0]])
        return httpx.Response(200, json={**ISSUES[0], "closed_at": None})

    result = await run("issue_closures", reopened)
    assert result["facts"]["total_closed"] == 0
