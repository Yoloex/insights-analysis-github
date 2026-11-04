import asyncio
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean, median

import httpx

from app.core import HTTPException, request_json

TOOL_NAMES = [
    "issue_closures",
    "pull_request_activity",
    "review_activity",
    "review_turnaround",
    "merge_cycle_time",
    "contributor_activity",
    "activity_trends",
    "labels_and_themes",
    "work_distribution",
    "release_activity",
    "repository_context",
]

_DESCRIPTIONS = {
    "issue_closures": "Current issues closed in the requested period and their closing actors.",
    "pull_request_activity": "Pull requests created, closed, or merged in the requested period and the current open pull request count.",
    "review_activity": "Submitted reviews during the period on pull requests created in the period.",
    "review_turnaround": "Time to first submitted review for pull requests created in the period.",
    "merge_cycle_time": "Creation-to-merge durations for pull requests merged in the period.",
    "contributor_activity": "Issue and pull request authors for items created in the period.",
    "activity_trends": "Compare created and closed issues and pull requests with the prior equal period.",
    "labels_and_themes": "Current labels on issues closed in the period; labels are not inferred themes.",
    "work_distribution": "Distribution of currently closed issues by closing actor.",
    "release_activity": "Releases published in the requested period.",
    "repository_context": "Current repository metadata, not a historical snapshot.",
}
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
    for name in TOOL_NAMES
]

_MAX_PAGES = 10
_MAX_ITEMS = 100
_PER_PAGE = 100


def _error(message):
    raise HTTPException(502, f"Unexpected GitHub response: {message}")


def _timestamp(value):
    if not isinstance(value, str):
        _error("missing timestamp")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except ValueError:
        _error("invalid timestamp")


def _in_period(value, start, end):
    if value is None:
        return False
    return (
        datetime.combine(start, time.min, UTC)
        <= _timestamp(value)
        < datetime.combine(end, time.min, UTC)
    )


def _number(item):
    value = item.get("number")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _error("invalid item number")
    return value


def _login(user):
    if user is None:
        return "unknown"
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        _error("invalid user")
    return user["login"][:100]


def _bounded(items):
    if len(items) > _MAX_ITEMS:
        raise HTTPException(422, "More than 100 matching items; choose a smaller period")
    return items


def _item_url(repo, kind, number):
    return f"https://github.com/{repo}/{kind}/{number}"


async def _get(client, path, **params):
    return await request_json(
        client, "GET", f"https://api.github.com{path}", params=params, follow_redirects=False
    )


async def _pages(client, path, params, stop_before=None, stop_field="created_at"):
    results = []
    for page in range(1, _MAX_PAGES + 1):
        batch = await _get(client, path, **params, per_page=_PER_PAGE, page=page)
        if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
            _error("expected a list of objects")
        results.extend(batch)
        # A descending sort permits stopping once all subsequent items are older.
        if len(batch) < _PER_PAGE or (
            stop_before
            and batch
            and _timestamp(batch[-1].get(stop_field)) < datetime.combine(stop_before, time.min, UTC)
        ):
            return results
    raise HTTPException(422, "Scan exceeded 10 pages; choose a smaller period")


async def _issues(client, repo, start, end):
    items = await _pages(
        client,
        f"/repos/{repo}/issues",
        {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "since": (datetime.combine(start, time.min, UTC) - timedelta(seconds=1)).isoformat(),
        },
    )
    issues = [item for item in items if "pull_request" not in item]
    for issue in issues:
        _timestamp(issue.get("created_at"))
    return issues


async def _pulls(client, repo, start):
    # Updated order captures old PRs merged or closed in the period as well as new PRs.
    items = await _pages(
        client,
        f"/repos/{repo}/pulls",
        {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
        },
        stop_before=start,
        stop_field="updated_at",
    )
    for item in items:
        _timestamp(item.get("created_at"))
    return items


def _issue_record(repo, issue):
    number = _number(issue)
    return {
        "number": number,
        "url": _item_url(repo, "issues", number),
        "created_at": issue.get("created_at"),
        "closed_at": issue.get("closed_at"),
        "author": _login(issue.get("user")),
    }


def _pull_record(repo, pull):
    number = _number(pull)
    return {
        "number": number,
        "url": _item_url(repo, "pull", number),
        "created_at": pull.get("created_at"),
        "closed_at": pull.get("closed_at"),
        "merged_at": pull.get("merged_at"),
        "author": _login(pull.get("user")),
    }


async def _closed_details(client, repo, issues, start, end):
    selected = _bounded([i for i in issues if _in_period(i.get("closed_at"), start, end)])
    semaphore = asyncio.Semaphore(5)

    async def detail(issue):
        number = _number(issue)
        async with semaphore:
            data = await _get(client, f"/repos/{repo}/issues/{number}")
        if not isinstance(data, dict) or _number(data) != number:
            _error("invalid issue detail")
        return data

    details = await asyncio.gather(*(detail(issue) for issue in selected))
    return [
        i for i in details if "pull_request" not in i and _in_period(i.get("closed_at"), start, end)
    ]


def _counts(counter):
    return [
        {"login": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _durations(values):
    return {
        "count": len(values),
        "mean_hours": round(mean(values), 2) if values else None,
        "median_hours": round(median(values), 2) if values else None,
    }


async def _reviews(client, repo, pulls):
    semaphore = asyncio.Semaphore(5)

    async def one(pull):
        number = _number(pull)
        async with semaphore:
            reviews = await _pages(client, f"/repos/{repo}/pulls/{number}/reviews", {})
        # Bound returned evidence and work per PR, even if the endpoint has more pages.
        return _bounded(reviews)

    return await asyncio.gather(*(one(pull) for pull in pulls))


async def run_tool(name: str, client: httpx.AsyncClient, repo: str, start: date, end: date) -> dict:
    if name not in TOOL_NAMES:
        raise HTTPException(404, "Unknown tool")
    if start >= end:
        raise HTTPException(422, "Start must precede end")
    if name == "activity_trends" and start.toordinal() <= (end - start).days:
        raise HTTPException(422, "Period is too early for a prior-period comparison")
    result = {
        "tool": name,
        "repo": repo,
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "fetched_at": datetime.now(UTC).isoformat(),
        "limitations": [
            "Dates use UTC and the half-open interval [start, end).",
            "GitHub data may change during pagination; this is not a snapshot.",
            (
                "Scans are limited to 10 pages of 100 and 100 matching items; "
                "exceeding a bound raises an error."
            ),
        ],
    }
    if name == "repository_context":
        data = await _get(client, f"/repos/{repo}")
        if not isinstance(data, dict):
            _error("expected repository object")
        result["facts"] = {
            "url": f"https://github.com/{repo}",
            "description": (data.get("description") or "")[:500],
            "default_branch": (data.get("default_branch") or "")[:200],
            "language": data.get("language"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues_and_prs": data.get("open_issues_count"),
            "archived": data.get("archived"),
        }
        result["limitations"].append(
            "Metadata is current and not filtered by the requested period."
        )
        return result
    if name == "release_activity":
        releases = await _pages(client, f"/repos/{repo}/releases", {})
        releases = _bounded(
            [
                r
                for r in releases
                if not r.get("draft") and _in_period(r.get("published_at"), start, end)
            ]
        )
        records = []
        for r in releases:
            release_id = r.get("id")
            if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
                _error("invalid release id")
            records.append(
                {
                    "id": release_id,
                    "url": f"https://api.github.com/repos/{repo}/releases/{release_id}",
                    "tag": str(r.get("tag_name") or "")[:200],
                    "published_at": r["published_at"],
                    "prerelease": bool(r.get("prerelease")),
                    "author": _login(r.get("author")),
                }
            )
        result.update(facts={"published": len(records)}, releases=records)
        result["limitations"].append(
            "Only currently visible published releases are counted; drafts and deleted releases are excluded."
        )
        return result

    if name in ("review_activity", "review_turnaround"):
        pulls = await _pages(
            client,
            f"/repos/{repo}/pulls",
            {"state": "all", "sort": "created", "direction": "desc"},
            stop_before=start,
        )
        pulls = _bounded([p for p in pulls if _in_period(p.get("created_at"), start, end)])
        review_lists = await _reviews(client, repo, pulls)
        records, hours = [], []
        for pull, reviews in zip(pulls, review_lists):
            number = _number(pull)
            submitted = []
            for review in reviews:
                if review.get("submitted_at") is None:
                    continue
                timestamp = _timestamp(review["submitted_at"])
                review_id = review.get("id")
                if isinstance(review_id, bool) or not isinstance(review_id, int) or review_id <= 0:
                    _error("invalid review id")
                submitted.append((timestamp, review))
            submitted.sort(key=lambda pair: (pair[0], pair[1]["id"]))
            if name == "review_activity":
                for timestamp, review in submitted:
                    if _in_period(review["submitted_at"], start, end):
                        records.append(
                            {
                                "pull_number": number,
                                "review_id": review["id"],
                                "url": f"https://api.github.com/repos/{repo}/pulls/{number}/reviews/{review['id']}",
                                "submitted_at": review["submitted_at"],
                                "reviewer": _login(review.get("user")),
                                "state": review.get("state"),
                            }
                        )
            elif submitted:
                timestamp, review = submitted[0]
                elapsed = (timestamp - _timestamp(pull.get("created_at"))).total_seconds() / 3600
                if elapsed < 0:
                    _error("review predates pull request")
                hours.append(elapsed)
                records.append(
                    {
                        "pull_number": number,
                        "pull_url": _item_url(repo, "pull", number),
                        "review_url": f"https://api.github.com/repos/{repo}/pulls/{number}/reviews/{review['id']}",
                        "created_at": pull["created_at"],
                        "first_review_at": review["submitted_at"],
                        "hours": round(elapsed, 2),
                    }
                )
        _bounded(records)
        result["facts"] = (
            {"sampled_pull_requests": len(pulls), "submitted_reviews": len(records)}
            if name == "review_activity"
            else {
                "sampled_pull_requests": len(pulls),
                "without_submitted_review": len(pulls) - len(records),
                **_durations(hours),
            }
        )
        result["reviews"] = records
        result["limitations"].extend(
            [
                "Sample: current pull requests created in [start, end); at most 100 PRs and 100 reviews per PR.",
                (
                    "Submitted reviews include all reviewers and states; pending reviews are excluded. "
                    "Review activity counts submissions in the period, not first reviews."
                ),
                (
                    "Turnaround uses the first submitted review ever, including submissions after end, "
                    "as currently visible; unreviewed PRs are excluded from durations."
                ),
            ]
        )
        return result

    if name in ("issue_closures", "labels_and_themes", "work_distribution"):
        issues = await _issues(client, repo, start, end)
        if name != "labels_and_themes":
            issues = await _closed_details(client, repo, issues, start, end)
            records = []
            for issue in issues:
                record = _issue_record(repo, issue)
                record["closed_by"] = _login(issue.get("closed_by"))
                records.append(record)
            counts = Counter(r["closed_by"] for r in records)
            result.update(
                facts={
                    "total_closed": len(records),
                    "unique_closers": len(counts),
                    "top_closer_share": round(max(counts.values()) / len(records), 4)
                    if records
                    else 0,
                },
                closers=_counts(counts),
                issues=records,
            )
            result["limitations"].append(
                "Current closed issues, not closure events; closer is not necessarily the person who did the work."
            )
        else:
            issues = _bounded([i for i in issues if _in_period(i.get("closed_at"), start, end)])
            counts = Counter()
            records = []
            for issue in issues:
                labels = issue.get("labels", [])
                if not isinstance(labels, list) or len(labels) > 100:
                    raise HTTPException(422, "More than 100 labels on an issue or invalid labels")
                names = []
                for label in labels:
                    if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                        _error("invalid label")
                    names.append(label["name"][:100])
                counts.update(set(names))
                records.append({**_issue_record(repo, issue), "labels": names})
            result.update(
                facts={"total_closed": len(records)},
                labels=[{"name": label, "count": count} for label, count in sorted(counts.items())],
                issues=records,
            )
            result["limitations"].append(
                "Current labels on currently closed issues; labels are not inferred themes or historical labels."
            )
        return result

    if name == "merge_cycle_time":
        pulls = await _pulls(client, repo, start)
        pulls = _bounded([p for p in pulls if _in_period(p.get("merged_at"), start, end)])
        records, hours = [], []
        for pull in pulls:
            elapsed = (
                _timestamp(pull["merged_at"]) - _timestamp(pull.get("created_at"))
            ).total_seconds() / 3600
            if elapsed < 0:
                _error("merge predates pull request")
            hours.append(elapsed)
            records.append({**_pull_record(repo, pull), "hours": round(elapsed, 2)})
        result.update(facts=_durations(hours), pull_requests=records)
        result["limitations"].append(
            "Elapsed wall-clock time from creation to merge; not active work time."
        )
        return result

    if name == "pull_request_activity":
        pulls = await _pulls(client, repo, start)
        pulls = _bounded(
            [
                p
                for p in pulls
                if any(
                    _in_period(p.get(field), start, end)
                    for field in ("created_at", "closed_at", "merged_at")
                )
            ]
        )
        open_pulls = _bounded(
            await _pages(
                client,
                f"/repos/{repo}/pulls",
                {
                    "state": "open",
                    "sort": "created",
                    "direction": "desc",
                },
            )
        )
        facts = {
            field.removesuffix("_at"): sum(_in_period(p.get(field), start, end) for p in pulls)
            for field in ("created_at", "closed_at", "merged_at")
        }
        facts["open_now"] = len(open_pulls)
        result.update(
            facts=facts,
            pull_requests=[_pull_record(repo, p) for p in pulls],
            open_pull_requests=[_pull_record(repo, p) for p in open_pulls],
        )
        result["limitations"].append(
            "Current PR timestamps, not historical reopen or close events; categories may overlap. "
            "open_now is the current total, independent of the selected period."
        )
        return result

    # Contributor and trend comparisons use created and current closed timestamps, not commits.
    period_start = start - (end - start) if name == "activity_trends" else start
    issues, pulls = await asyncio.gather(
        _issues(client, repo, period_start, end), _pulls(client, repo, period_start)
    )
    issues = _bounded(
        [
            i
            for i in issues
            if any(_in_period(i.get(f), period_start, end) for f in ("created_at", "closed_at"))
        ]
    )
    pulls = _bounded(
        [
            p
            for p in pulls
            if any(
                _in_period(p.get(f), period_start, end)
                for f in ("created_at", "closed_at", "merged_at")
            )
        ]
    )
    if name == "contributor_activity":
        issue_records = [
            _issue_record(repo, i) for i in issues if _in_period(i.get("created_at"), start, end)
        ]
        pull_records = [
            _pull_record(repo, p) for p in pulls if _in_period(p.get("created_at"), start, end)
        ]
        issue_counts = Counter(i["author"] for i in issue_records)
        pull_counts = Counter(p["author"] for p in pull_records)
        result.update(
            facts={
                "issues_created": len(issue_records),
                "pull_requests_created": len(pull_records),
            },
            contributors=[
                {
                    "login": login,
                    "issues_authored": issue_counts[login],
                    "pull_requests_authored": pull_counts[login],
                }
                for login in sorted(issue_counts.keys() | pull_counts.keys())
            ],
            issues=issue_records,
            pull_requests=pull_records,
        )
        result["limitations"].append(
            "Counts issue authors and PR authors at creation, not closers, reviewers, commits, or total contributions."
        )
    else:

        def period_facts(a, b):
            return {
                "issues_created": sum(_in_period(i.get("created_at"), a, b) for i in issues),
                "issues_closed": sum(_in_period(i.get("closed_at"), a, b) for i in issues),
                "pull_requests_created": sum(_in_period(p.get("created_at"), a, b) for p in pulls),
                "pull_requests_closed": sum(_in_period(p.get("closed_at"), a, b) for p in pulls),
                "pull_requests_merged": sum(_in_period(p.get("merged_at"), a, b) for p in pulls),
            }

        result.update(
            facts={"current": period_facts(start, end), "prior": period_facts(period_start, start)},
            prior_start=period_start.isoformat(),
            prior_end_exclusive=start.isoformat(),
            issues=[_issue_record(repo, i) for i in issues],
            pull_requests=[_pull_record(repo, p) for p in pulls],
        )
        result["limitations"].append(
            "Current timestamps only; reopened, deleted, or reclosed items may not reflect historical events."
        )
    return result
