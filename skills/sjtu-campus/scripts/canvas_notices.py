from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import canvas
from common import emit, paginated, run
from notice_common import acknowledge, html_text, load_store, pending_items, save_store, source_state


NAME = "canvas"
DEFAULT_STORE = Path("campus-assistant/notices.json")


def announcement(course: dict, row: dict, synced_at: str) -> dict:
    try:
        topic_id = int(row["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Canvas 公告缺少有效 ID") from error
    text = html_text(row.get("message"))
    return {
        "id": f"canvas-notice:{course['id']}:{topic_id}",
        "source": NAME,
        "courseId": course["id"],
        "course": course["name"],
        "title": row.get("title") or "未命名公告",
        "postedAt": row.get("posted_at") or row.get("last_reply_at") or synced_at,
        "url": row.get("html_url"),
        "read": row.get("read_state") == "read",
        "text": text[:12000],
        "truncated": len(text) > 12000,
    }


def sync(args: argparse.Namespace) -> None:
    store = args.store.resolve()
    data = load_store(store)
    state = source_state(data, NAME)
    courses = canvas.course_rows()
    access_token = canvas.token()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    synced_at = now.isoformat()
    course_state = state.setdefault("courses", {})
    added = []
    initialized = []
    errors = []
    succeeded = 0
    for course in courses:
        key = str(course["id"])
        first_sync = not course_state.get(key, {}).get("initialized")
        try:
            rows = paginated(
                f"{canvas.BASE_URL}/api/v1/courses/{course['id']}/discussion_topics?only_announcements=true&per_page=100",
                access_token,
            )
            for row in rows:
                item = announcement(course, row, synced_at)
                item_id = item["id"]
                is_new = item_id not in state["items"]
                state["items"][item_id] = item
                if is_new:
                    added.append(item_id)
                    if not first_sync and not item["read"]:
                        state["pending"].append(item_id)
                if item["read"] and item_id in state["pending"]:
                    state["pending"].remove(item_id)
            if first_sync:
                initialized.append(course["id"])
            course_state[key] = {"initialized": True, "syncedAt": synced_at, "error": None}
            succeeded += 1
        except Exception as error:
            message = f"{course['name']}：{error}"
            errors.append(message)
            previous = course_state.get(key, {})
            course_state[key] = {**previous, "syncedAt": previous.get("syncedAt"), "error": str(error)}
    if courses and not succeeded:
        raise RuntimeError("；".join(errors))
    state["checkedAt"] = synced_at
    state["errors"] = errors
    save_store(store, data)
    emit({
        "ok": True,
        "partial": bool(errors),
        "courseCount": len(courses),
        "initializedCourses": initialized,
        "added": added,
        "candidates": pending_items(state),
        "pendingCount": len(state["pending"]),
        "errors": errors,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi Canvas 公告增量同步")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("sync")
    command.add_argument("--now", help="测试用 ISO 时间")
    command = sub.add_parser("ack")
    command.add_argument("--id", action="append", required=True)
    args = parser.parse_args()
    if args.command == "sync":
        sync(args)
    else:
        emit(acknowledge(args.store.resolve(), NAME, args.id))


if __name__ == "__main__":
    run(main)
