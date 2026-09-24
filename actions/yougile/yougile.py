#!/usr/bin/env python3
"""GitHub <-> YouGile bridge (stdlib only).

Commands:
  check     validate that the PR title references an existing, live YouGile task
  pr-event  post the PR / review / merge event into the task chat
  message   post an arbitrary text into the chat of the task given by --task

Environment:
  YOUGILE_API_KEY   key of the technical YouGile user (required for network calls)
  YOUGILE_BASE_URL  default https://ru.yougile.com/api-v2
  TASK_PREFIX       default XIT
  GITHUB_EVENT_NAME, GITHUB_EVENT_PATH  provided by GitHub Actions
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = os.environ.get("YOUGILE_BASE_URL", "https://ru.yougile.com/api-v2").rstrip("/")
PREFIX = os.environ.get("TASK_PREFIX", "XIT")
TITLE_RE = re.compile(rf"^({PREFIX}-\d+): \S.*")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
# YouGile API cannot create real mentions, so the reviewer is tagged as plain text.
REVIEWER = os.environ.get("YOUGILE_REVIEWER", "@Михаил")


class YouGileUnavailable(Exception):
    """YouGile did not answer (timeout / 5xx) after retries."""


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def api(method: str, path: str, body: Any | None = None) -> Any:
    key = os.environ.get("YOUGILE_API_KEY", "")
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    # Reads are retried; a POST is sent once (a retry after a read timeout could duplicate the message).
    attempts = 3 if method == "GET" else 1
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                fail(f"YouGile {method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
            error = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            error = str(e)
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise YouGileUnavailable(f"YouGile {method} {path}: {error}")


def find_task(code: str) -> dict | None:
    offset = 0
    while True:
        page = api("GET", f"/task-list?limit=1000&offset={offset}")
        for task in page.get("content", []):
            if task.get("idTaskProject") == code or task.get("idTaskCommon") == code:
                return task
        paging = page.get("paging", {})
        if not paging.get("next"):
            return None
        offset += paging.get("limit", 1000)


def post(task_id: str, text: str) -> None:
    body = {"text": text, "textHtml": html.escape(text).replace("\n", "<br>"), "label": ""}
    api("POST", f"/chats/{task_id}/messages", body)


def load_event() -> tuple[str, dict]:
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        return os.environ.get("GITHUB_EVENT_NAME", ""), json.load(fh)


def task_code(title: str) -> str | None:
    m = TITLE_RE.match(title or "")
    return m.group(1) if m else None


def cmd_check(_: argparse.Namespace) -> None:
    _, event = load_event()
    title = event["pull_request"]["title"]
    code = task_code(title)
    if not code:
        fail(f"Название PR должно быть вида '{PREFIX}-123: Короткое описание', сейчас: {title!r}")
    if not CYRILLIC_RE.search(title.split(":", 1)[1]):
        fail(f"Название PR пишется на русском: '{PREFIX}-123: Добавить …', сейчас: {title!r}")
    if not os.environ.get("YOUGILE_API_KEY"):
        print(f"::warning::YOUGILE_API_KEY is not set - only the title format of {code} was checked")
        return
    task = find_task(code)
    if not task or task.get("deleted"):
        fail(f"YouGile task {code} not found")
    if task.get("archived"):
        fail(f"YouGile task {code} is archived")
    if task.get("completed"):
        print(f"::warning::YouGile task {code} is already completed")
    print(f"{code}: {task.get('title')}")


def describe(event_name: str, event: dict) -> str | None:
    pr = event["pull_request"]
    repo = event["repository"]["full_name"]
    head = f"PR #{pr['number']} в {repo}: {pr['title']}\n{pr['html_url']}"
    action = event.get("action")
    if event_name == "pull_request_review":
        review = event["review"]
        who = review["user"]["login"]
        state = review.get("state", "").lower()
        if state == "approved":
            return f"👍 Approve от {who}\n{head}"
        if state == "changes_requested":
            note = (review.get("body") or "").strip()
            return f"✏️ {who} запросил изменения\n{head}" + (f"\n\n{note[:1000]}" if note else "")
        return None
    if action == "opened" and pr.get("draft"):
        return f"📝 Открыт draft-PR ({pr['user']['login']})\n{head}"
    if action in ("opened", "reopened", "ready_for_review"):
        verb = {"opened": "Открыт", "reopened": "Переоткрыт", "ready_for_review": "Готов к ревью"}[action]
        return (
            f"🔀 {verb} PR ({pr['user']['login']}, {pr['head']['ref']} → {pr['base']['ref']})\n{head}\n\n"
            f"👀 {REVIEWER}, нужно ревью"
        )
    if action == "closed" and pr.get("merged"):
        by = (pr.get("merged_by") or {}).get("login", "?")
        sha = (pr.get("merge_commit_sha") or "")[:12]
        return f"✅ PR влит в {pr['base']['ref']} ({by}), commit {sha}\n{head}"
    if action == "closed":
        return f"🚫 PR закрыт без merge\n{head}"
    return None


def cmd_pr_event(_: argparse.Namespace) -> None:
    event_name, event = load_event()
    code = task_code(event["pull_request"]["title"])
    if not code:
        print("No task code in PR title - nothing to post")
        return
    text = describe(event_name, event)
    if not text:
        print("Event is not interesting for YouGile")
        return
    if not os.environ.get("YOUGILE_API_KEY"):
        print("::warning::YOUGILE_API_KEY is not set - message skipped")
        return
    task = find_task(code)
    if not task:
        print(f"::warning::YouGile task {code} not found - message skipped")
        return
    post(task["id"], text)
    print(f"Posted to {code}")


def cmd_message(args: argparse.Namespace) -> None:
    task = find_task(args.task)
    if not task:
        print(f"::warning::YouGile task {args.task} not found - message skipped")
        return
    post(task["id"], args.text)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("pr-event").set_defaults(func=cmd_pr_event)
    msg = sub.add_parser("message")
    msg.add_argument("--task", required=True)
    msg.add_argument("--text", required=True)
    msg.set_defaults(func=cmd_message)
    args = parser.parse_args()
    try:
        args.func(args)
    except YouGileUnavailable as e:
        # An outage of YouGile must not block PRs or deploys: the title format is already checked.
        print(f"::warning::YouGile недоступен, шаг пропущен: {e}")


if __name__ == "__main__":
    main()
