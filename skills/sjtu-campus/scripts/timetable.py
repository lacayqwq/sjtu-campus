from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from common import data_dir, emit, iso_date, read_json, run, write_json

SHANGHAI = ZoneInfo("Asia/Shanghai")
PERIODS = [
    ((8, 0), (8, 45)),
    ((8, 55), (9, 40)),
    ((10, 0), (10, 45)),
    ((10, 55), (11, 40)),
    ((12, 0), (12, 45)),
    ((12, 55), (13, 40)),
    ((14, 0), (14, 45)),
    ((14, 55), (15, 40)),
    ((16, 0), (16, 45)),
    ((16, 55), (17, 40)),
    ((18, 0), (18, 45)),
    ((18, 55), (19, 40)),
    ((19, 50), (20, 20)),
]


def current_semester() -> str:
    now = datetime.now(SHANGHAI)
    if now.month >= 9:
        return f"{now.year}-{now.year + 1}-1"
    if now.month <= 2:
        return f"{now.year - 1}-{now.year}-1"
    if now.month <= 7:
        return f"{now.year - 1}-{now.year}-2"
    return f"{now.year - 1}-{now.year}-3"


def timetable_path(semester: str) -> Path:
    return data_dir() / "timetable" / f"{semester}.json"


def weeks(mask: str) -> list[int]:
    if not mask or set(mask) - {"0", "1"}:
        raise RuntimeError("研究生课表包含无效周次位图")
    return [index + 1 for index, value in enumerate(mask[:30]) if value == "1"]


def bounded_integer(value: object, label: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(label)
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(label) from error
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(label)
    if isinstance(value, str) and str(number) != value.strip():
        raise ValueError(label)
    if not 1 <= number <= maximum:
        raise ValueError(label)
    return number


def valid_schedule_row(row: object) -> dict:
    if not isinstance(row, dict):
        raise ValueError("整行不是对象")
    for key in ("BJDM", "KCMC"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"{key} 缺失")
    if not isinstance(row.get("ZCBH"), str) or not row["ZCBH"] or set(row["ZCBH"]) - {"0", "1"}:
        raise ValueError("ZCBH 无效")
    return {
        **row,
        "XQ": bounded_integer(row.get("XQ"), "XQ 无效", 7),
        "KSJCDM": bounded_integer(row.get("KSJCDM"), "KSJCDM 无效", 20),
        "JSJCDM": bounded_integer(row.get("JSJCDM"), "JSJCDM 无效", 20),
    }


def valid_course_row(row: object) -> dict:
    if not isinstance(row, dict) or not isinstance(row.get("BJDM"), str) or not row["BJDM"].strip():
        raise ValueError("BJDM 缺失")
    first_date = row.get("SCSKRQ")
    if first_date is not None and (
        not isinstance(first_date, str) or not isinstance(first_date[:10], str)
    ):
        raise ValueError("SCSKRQ 无效")
    if first_date:
        datetime.strptime(first_date[:10], "%Y-%m-%d")
    return row


def valid_rows(rows: list[dict], validator) -> tuple[list[dict], str | None]:
    valid = []
    first_error = None
    for row in rows:
        try:
            valid.append(validator(row))
        except (ValueError, TypeError) as error:
            first_error = first_error or str(error)
    return valid, first_error


def derive_start(schedule_rows: list[dict], course_rows: list[dict]) -> str | None:
    starts = set()
    for course in course_rows:
        if not course.get("SCSKRQ"):
            continue
        offsets = []
        for row in schedule_rows:
            if row["BJDM"] != course["BJDM"]:
                continue
            numbers = weeks(row["ZCBH"])
            if numbers:
                offsets.append((numbers[0] - 1) * 7 + int(row["XQ"]) - 1)
        if not offsets:
            continue
        first = datetime.strptime(course["SCSKRQ"][:10], "%Y-%m-%d").date()
        starts.add((first - timedelta(days=min(offsets))).isoformat())
    return starts.pop() if len(starts) == 1 else None


def map_graduate(semester: str, schedule_rows: list[dict], course_rows: list[dict]) -> dict:
    schedule_rows, schedule_issue = valid_rows(schedule_rows, valid_schedule_row)
    if not schedule_rows and schedule_issue:
        raise RuntimeError(f"研究生课表返回了无法识别的数据（{schedule_issue}）")
    course_rows, _ = valid_rows(course_rows, valid_course_row)
    courses = {row["BJDM"]: row for row in course_rows}
    groups: dict[str, list[dict]] = {}
    for row in schedule_rows:
        key = json.dumps(
            [row["BJDM"], row["XQ"], row["ZCBH"], row.get("JASMC"), row.get("JSXM")],
            ensure_ascii=False,
        )
        groups.setdefault(key, []).append(row)
    lessons = []
    for rows in groups.values():
        rows.sort(key=lambda row: row["KSJCDM"])
        run_rows = []

        def append_run() -> None:
            if not run_rows:
                return
            first, last = run_rows[0], run_rows[-1]
            course = courses.get(first["BJDM"], {})
            start, end = int(first["KSJCDM"]), int(last["JSJCDM"])
            lessons.append(
                {
                    "sourceId": first.get("WID") or f"{first['BJDM']}-{first['XQ']}-{first['KSJCDM']}",
                    "courseCode": (course.get("KCDM") or first.get("KCDM") or "").strip(),
                    "courseName": (course.get("KCMC") or first["KCMC"]).strip(),
                    "className": course.get("BJMC") or first.get("BJMC"),
                    "teacher": course.get("RKJS") or first.get("JSXM"),
                    "campus": course.get("XQDM_DISPLAY"),
                    "classroom": first.get("JASMC"),
                    "weekday": int(first["XQ"]),
                    "startPeriod": start,
                    "endPeriod": end,
                    "periodsText": f"{start}节" if start == end else f"{start}-{end}节",
                    "weeksText": first.get("ZCMC") or "周次未提供",
                    "weekNumbers": weeks(first["ZCBH"]),
                    "note": course.get("XKBZ") or first.get("KBBZ"),
                }
            )

        for row in rows:
            if run_rows and int(row["KSJCDM"]) != int(run_rows[-1]["JSJCDM"]) + 1:
                append_run()
                run_rows = []
            run_rows.append(row)
        append_run()
    if not lessons:
        raise RuntimeError("研究生课表为空，已保留原有课表；请确认学期和登录状态")
    for index, lesson in enumerate(lessons, 1):
        lesson["id"] = index
    return {
        "semester": semester,
        "semesterStart": derive_start(schedule_rows, course_rows),
        "syncedAt": datetime.now(timezone.utc).isoformat(),
        "lessons": lessons,
    }


def import_graduate(args: argparse.Namespace) -> None:
    payload = read_json(Path(args.input).expanduser().resolve())
    result = map_graduate(payload["semester"], payload["scheduleRows"], payload["courseRows"])
    write_json(timetable_path(payload["semester"]), result)
    emit(
        {
            "ok": True,
            "semester": result["semester"],
            "semesterStart": result["semesterStart"],
            "courses": len({row["courseCode"] for row in result["lessons"]}),
            "lessons": len(result["lessons"]),
            "occurrences": sum(len(row["weekNumbers"]) for row in result["lessons"]),
        }
    )


def import_dela(args: argparse.Namespace) -> None:
    source = Path(args.database).expanduser().resolve()
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM timetable_lessons WHERE semester = ?", (args.semester,)
        ).fetchall()
        sync = connection.execute(
            "SELECT semester_start, last_synced_at FROM timetable_sync WHERE semester = ?",
            (args.semester,),
        ).fetchone()
    finally:
        connection.close()
    if not rows:
        raise RuntimeError("Dela 数据库中该学期没有课程，未修改现有课表")
    lessons = []
    for row in rows:
        lessons.append(
            {
                "id": row["id"],
                "sourceId": row["source_id"],
                "courseCode": row["course_code"],
                "courseName": row["course_name"],
                "className": row["class_name"],
                "teacher": row["teacher"],
                "campus": row["campus"],
                "classroom": row["classroom"],
                "weekday": row["weekday"],
                "startPeriod": row["start_period"],
                "endPeriod": row["end_period"],
                "periodsText": row["periods_text"],
                "weeksText": row["weeks_text"],
                "weekNumbers": json.loads(row["week_numbers"]),
                "note": row["note"],
            }
        )
    result = {
        "semester": args.semester,
        "semesterStart": sync["semester_start"] if sync else None,
        "syncedAt": sync["last_synced_at"] if sync else None,
        "lessons": lessons,
    }
    write_json(timetable_path(args.semester), result)
    emit({"ok": True, "semester": args.semester, "lessons": len(lessons), "courses": len({row["courseCode"] for row in lessons})})


def load_events() -> list[dict]:
    return read_json(data_dir() / "events.json", [])


def local_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo:
        parsed = parsed.astimezone(SHANGHAI).replace(tzinfo=None)
    return parsed


def event_overlaps(event: dict, start_date: date, end_date: date) -> bool:
    start = local_time(event["startAt"])
    end = local_time(event["endAt"])
    query_start = datetime.combine(start_date, time.min)
    query_end = datetime.combine(end_date, time.min)
    return start < query_end and end > query_start


def query(args: argparse.Namespace) -> None:
    start_date, end_date = date.fromisoformat(iso_date(args.start)), date.fromisoformat(iso_date(args.end))
    if end_date <= start_date:
        raise RuntimeError("结束日期必须晚于开始日期")
    payload = read_json(timetable_path(args.semester))
    if not payload:
        raise RuntimeError("该学期尚未同步课表")
    monday = date.fromisoformat(payload["semesterStart"])
    monday -= timedelta(days=monday.weekday())
    items = []
    warnings = []
    for lesson in payload["lessons"]:
        try:
            start_period = PERIODS[lesson["startPeriod"] - 1][0]
            end_period = PERIODS[lesson["endPeriod"] - 1][1]
        except (IndexError, TypeError):
            warnings.append(f"{lesson['courseName']}：节次无效")
            continue
        for week in lesson["weekNumbers"]:
            day = monday + timedelta(weeks=week - 1, days=lesson["weekday"] - 1)
            if not start_date <= day < end_date:
                continue
            start = datetime.combine(day, time(*start_period), SHANGHAI)
            end = datetime.combine(day, time(*end_period), SHANGHAI)
            items.append(
                {
                    "source": "course",
                    "lessonId": lesson["id"],
                    "title": lesson["courseName"],
                    "teacher": lesson.get("teacher"),
                    "startAt": start.isoformat(),
                    "endAt": end.isoformat(),
                    "location": lesson.get("classroom"),
                }
            )
    for event in load_events():
        try:
            if event_overlaps(event, start_date, end_date):
                items.append({**event, "source": "event"})
        except (KeyError, TypeError, ValueError):
            warnings.append(f"{event.get('title') or '个人日程'}：时间无效")
    items.sort(key=lambda item: item["startAt"])
    emit({"ok": True, "semester": args.semester, "items": items, "warnings": warnings})


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi timetable and calendar")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("import-graduate")
    command.add_argument("--input", required=True)
    command = sub.add_parser("import-dela")
    command.add_argument("--database", required=True)
    command.add_argument("--semester", required=True)
    command = sub.add_parser("query")
    command.add_argument("--semester", default=current_semester())
    command.add_argument("--start", required=True)
    command.add_argument("--end", required=True)
    sub.add_parser("status")
    command = sub.add_parser("create-event")
    command.add_argument("--title", required=True)
    command.add_argument("--start", required=True)
    command.add_argument("--end", required=True)
    command.add_argument("--location")
    command.add_argument("--confirm", action="store_true")
    command = sub.add_parser("delete-event")
    command.add_argument("--id", required=True)
    command.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.command == "import-graduate":
        import_graduate(args)
    elif args.command == "import-dela":
        import_dela(args)
    elif args.command == "query":
        query(args)
    elif args.command == "status":
        root = data_dir() / "timetable"
        emit({"ok": True, "semesters": sorted(path.stem for path in root.glob("*.json"))})
    elif args.command == "create-event":
        if not args.confirm:
            raise RuntimeError("创建日历事件需要用户确认后添加 --confirm")
        start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
        if end <= start:
            raise RuntimeError("事件结束时间必须晚于开始时间")
        events = load_events()
        event = {
            "id": uuid.uuid4().hex,
            "title": args.title.strip(),
            "startAt": start.isoformat(),
            "endAt": end.isoformat(),
            "location": args.location,
        }
        events.append(event)
        write_json(data_dir() / "events.json", events)
        emit({"ok": True, "event": event})
    elif args.command == "delete-event":
        if not args.confirm:
            raise RuntimeError("删除日历事件需要用户确认后添加 --confirm")
        events = load_events()
        remaining = [event for event in events if event["id"] != args.id]
        if len(remaining) == len(events):
            raise RuntimeError("找不到该日历事件")
        write_json(data_dir() / "events.json", remaining)
        emit({"ok": True, "deleted": args.id})


if __name__ == "__main__":
    run(main)
