from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import urllib.parse
import urllib.request

from common import data_dir, dialog, emit, keychain_get, keychain_set, paginated, read_json, request_json, run, write_json

BASE_URL = "https://oc.sjtu.edu.cn"
ACCOUNT = "canvas-token"


def token() -> str:
    value = keychain_get(ACCOUNT)
    if not value:
        raise RuntimeError("尚未配置 Canvas Token，请先执行 configure")
    return value


def course_rows() -> list[dict]:
    rows = paginated(
        f"{BASE_URL}/api/v1/courses?enrollment_state=active&per_page=100&include[]=term",
        token(),
    )
    return [
        {
            "id": row["id"],
            "name": row.get("name"),
            "code": row.get("course_code"),
            "term": (row.get("term") or {}).get("name"),
        }
        for row in rows
    ]


def selected_courses(course_id: int | None) -> list[dict]:
    courses = course_rows()
    if course_id is None:
        return courses
    selected = [course for course in courses if course["id"] == course_id]
    if not selected:
        raise RuntimeError(f"当前有效课程中找不到课程 {course_id}")
    return selected


def configure() -> None:
    value = dialog("请输入 Canvas Access Token。Token 仅保存到 macOS 钥匙串。", secret=True)
    payload, _ = request_json(f"{BASE_URL}/api/v1/users/self", value)
    keychain_set(ACCOUNT, value)
    write_json(data_dir() / "canvas-profile.json", {"id": payload.get("id"), "name": payload.get("name")})
    emit({"ok": True, "user": {"id": payload.get("id"), "name": payload.get("name")}})


def assignment_status(assignment: dict) -> dict:
    submission = assignment.get("submission")
    if not isinstance(submission, dict):
        return {"status": "unknown", "statusLabel": "待同步确认"}
    if submission.get("excused") is True:
        return {"status": "done", "statusLabel": "已豁免"}
    if submission.get("redoRequest") is True:
        return {"status": "open", "statusLabel": "需重新提交"}
    state = submission.get("state")
    if submission.get("submittedAt") or state in ("submitted", "pending_review"):
        return {"status": "done", "statusLabel": "已批改" if state == "graded" else "已提交"}
    submission_types = assignment.get("submissionTypes")
    if (
        isinstance(submission_types, list)
        and submission_types
        and all(kind in ("on_paper", "none") for kind in submission_types)
    ):
        if state == "graded" and submission.get("missing") is not True:
            return {"status": "done", "statusLabel": "已批改"}
        return {"status": "unknown", "statusLabel": "线下 / 无需在线提交"}
    if state in ("unsubmitted", "graded") or submission.get("missing") is True:
        return {"status": "open", "statusLabel": "未完成"}
    return {"status": "unknown", "statusLabel": "待同步确认"}


def assignments(args: argparse.Namespace) -> None:
    def load(course: dict) -> list[dict]:
        rows = paginated(
            f"{BASE_URL}/api/v1/courses/{course['id']}/assignments?per_page=100&include[]=submission",
            token(),
        )
        result = []
        for row in rows:
            raw_submission = row.get("submission")
            submission = raw_submission if isinstance(raw_submission, dict) else {}
            item = {
                "id": row["id"],
                "courseId": course["id"],
                "courseName": course["name"],
                "name": row.get("name"),
                "dueAt": row.get("due_at"),
                "url": row.get("html_url"),
                "published": row.get("published"),
                "submissionTypes": row.get("submission_types"),
                "submission": {
                    "state": submission.get("workflow_state"),
                    "submittedAt": submission.get("submitted_at"),
                    "gradedAt": submission.get("graded_at"),
                    "score": submission.get("score"),
                    "grade": submission.get("grade"),
                    "excused": submission.get("excused"),
                    "missing": submission.get("missing"),
                    "redoRequest": submission.get("redo_request"),
                } if isinstance(raw_submission, dict) else None,
            }
            item.update(assignment_status(item))
            result.append(item)
        return result

    with ThreadPoolExecutor(max_workers=4) as executor:
        groups = list(executor.map(load, selected_courses(args.course)))
    rows = [row for group in groups for row in group]
    if args.query:
        rows = [row for row in rows if args.query.casefold() in (row["name"] or "").casefold()]
    if args.pending:
        rows = [row for row in rows if row["status"] == "open"]
    rows.sort(key=lambda row: (row["dueAt"] is None, row["dueAt"] or ""))
    emit({"ok": True, "count": len(rows), "assignments": rows[: args.limit]})


def files(args: argparse.Namespace) -> None:
    def load(course: dict) -> list[dict]:
        rows = paginated(
            f"{BASE_URL}/api/v1/courses/{course['id']}/files?per_page=100&sort=updated_at&order=desc",
            token(),
        )
        return [
            {
                "id": row["id"],
                "courseId": course["id"],
                "courseName": course["name"],
                "name": row.get("display_name"),
                "contentType": row.get("content-type"),
                "size": row.get("size"),
                "updatedAt": row.get("updated_at"),
            }
            for row in rows
        ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        groups = list(executor.map(load, selected_courses(args.course)))
    rows = [row for group in groups for row in group]
    if args.query:
        rows = [row for row in rows if args.query.casefold() in (row["name"] or "").casefold()]
    rows.sort(key=lambda row: row["updatedAt"] or "", reverse=True)
    emit({"ok": True, "count": len(rows), "files": rows[: args.limit]})


def download(args: argparse.Namespace) -> None:
    payload, _ = request_json(f"{BASE_URL}/api/v1/files/{args.file}", token())
    url = payload.get("url")
    if not url:
        raise RuntimeError("Canvas 没有返回文件下载地址")
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError("目标文件已存在，不会覆盖")
    with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as response, output.open("xb") as stream:
        while chunk := response.read(1024 * 1024):
            stream.write(chunk)
    emit({"ok": True, "path": str(output), "name": payload.get("display_name"), "size": output.stat().st_size})


def announcements(args: argparse.Namespace) -> None:
    rows = paginated(
        f"{BASE_URL}/api/v1/courses/{args.course}/discussion_topics?only_announcements=true&per_page=100",
        token(),
    )
    emit(
        {
            "ok": True,
            "announcements": [
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "postedAt": row.get("posted_at"),
                    "message": row.get("message"),
                    "url": row.get("html_url"),
                }
                for row in rows[: args.limit]
            ],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi Canvas connector")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure")
    sub.add_parser("status")
    sub.add_parser("courses")
    for name in ("assignments", "files"):
        command = sub.add_parser(name)
        command.add_argument("--course", type=int)
        command.add_argument("--query")
        command.add_argument("--limit", type=int, default=50)
        if name == "assignments":
            command.add_argument("--pending", action="store_true")
    command = sub.add_parser("download")
    command.add_argument("--file", type=int, required=True)
    command.add_argument("--output", required=True)
    command = sub.add_parser("announcements")
    command.add_argument("--course", type=int, required=True)
    command.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if args.command == "configure":
        configure()
    elif args.command == "status":
        profile = read_json(data_dir() / "canvas-profile.json", {})
        payload, _ = request_json(f"{BASE_URL}/api/v1/users/self", token())
        emit({"ok": True, "connected": True, "user": {"id": payload.get("id"), "name": payload.get("name")}, "cachedProfile": profile})
    elif args.command == "courses":
        rows = course_rows()
        emit({"ok": True, "count": len(rows), "courses": rows})
    elif args.command == "assignments":
        assignments(args)
    elif args.command == "files":
        files(args)
    elif args.command == "download":
        download(args)
    elif args.command == "announcements":
        announcements(args)


if __name__ == "__main__":
    run(main)
