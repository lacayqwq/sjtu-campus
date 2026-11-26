from __future__ import annotations

import argparse
from datetime import datetime, timezone
import uuid

from common import data_dir, emit, read_json, run, write_json


def load() -> list[dict]:
    return read_json(data_dir() / "todos.json", [])


def save(rows: list[dict]) -> None:
    write_json(data_dir() / "todos.json", rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi personal todos")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("list")
    command.add_argument("--all", action="store_true")
    command = sub.add_parser("save")
    command.add_argument("--id")
    command.add_argument("--title", required=True)
    command.add_argument("--note")
    command.add_argument("--due")
    command = sub.add_parser("complete")
    command.add_argument("--id", required=True)
    command.add_argument("--undo", action="store_true")
    command = sub.add_parser("remove")
    command.add_argument("--id", required=True)
    command.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    rows = load()

    if args.command == "list":
        selected = rows if args.all else [row for row in rows if not row["completed"]]
        selected.sort(key=lambda row: (row["completed"], row.get("dueAt") is None, row.get("dueAt") or ""))
        emit({"ok": True, "count": len(selected), "todos": selected})
    elif args.command == "save":
        due = datetime.fromisoformat(args.due).isoformat() if args.due else None
        if args.id:
            matches = [row for row in rows if row["id"] == args.id]
            if not matches:
                raise RuntimeError("找不到该待办")
            row = matches[0]
            row.update({"title": args.title.strip(), "note": args.note, "dueAt": due})
        else:
            row = {
                "id": uuid.uuid4().hex,
                "title": args.title.strip(),
                "note": args.note,
                "dueAt": due,
                "completed": False,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            rows.append(row)
        save(rows)
        emit({"ok": True, "todo": row})
    elif args.command == "complete":
        matches = [row for row in rows if row["id"] == args.id]
        if not matches:
            raise RuntimeError("找不到该待办")
        matches[0]["completed"] = not args.undo
        save(rows)
        emit({"ok": True, "todo": matches[0]})
    elif args.command == "remove":
        if not args.confirm:
            raise RuntimeError("删除待办需要用户确认后添加 --confirm")
        remaining = [row for row in rows if row["id"] != args.id]
        if len(remaining) == len(rows):
            raise RuntimeError("找不到该待办")
        save(remaining)
        emit({"ok": True, "deleted": args.id})


if __name__ == "__main__":
    run(main)
