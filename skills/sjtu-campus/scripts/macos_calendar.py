from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from common import data_dir, emit, read_json, run, write_json

LOCAL = ZoneInfo("Asia/Shanghai")
ENSURE_SCRIPT = r'''
on run argv
    set calendarName to item 1 of argv
    tell application "Calendar"
        if not (exists calendar calendarName) then
            make new calendar with properties {name:calendarName}
        end if
        return name of calendar calendarName
    end tell
end run
'''
UPSERT_SCRIPT = r'''
on run argv
    set calendarName to item 1 of argv
    set startText to item 2 of argv
    set endText to item 3 of argv
    set eventTitle to item 4 of argv
    set eventNotes to item 5 of argv
    set eventLocation to item 6 of argv
    set existingUid to item 7 of argv
    set firstAlarm to (item 8 of argv) as integer
    set secondAlarm to (item 9 of argv) as integer
    set allDay to (item 10 of argv) as boolean
    set startDate to my dateFromISO(startText)
    set endDate to my dateFromISO(endText)
    tell application "Calendar"
        if not (exists calendar calendarName) then
            make new calendar with properties {name:calendarName}
        end if
        set targetCalendar to calendar calendarName
        set targetEvent to missing value
        if existingUid is not "" then
            try
                set matchedEvents to every event of targetCalendar whose uid is existingUid
                if (count of matchedEvents) > 0 then set targetEvent to item 1 of matchedEvents
            end try
        end if
        tell targetCalendar
            if targetEvent is missing value then
                set targetEvent to make new event with properties {summary:eventTitle, start date:startDate, end date:endDate, description:eventNotes, location:eventLocation, allday event:allDay}
            else
                set summary of targetEvent to eventTitle
                set allday event of targetEvent to allDay
                set start date of targetEvent to startDate
                set end date of targetEvent to endDate
                set description of targetEvent to eventNotes
                set location of targetEvent to eventLocation
            end if
        end tell
        tell targetEvent
            set existingAlarms to every display alarm
            if firstAlarm > 0 then
                if (count of existingAlarms) ≥ 1 then
                    set trigger interval of item 1 of existingAlarms to (-firstAlarm)
                else
                    make new display alarm at end with properties {trigger interval:(-firstAlarm)}
                end if
            end if
            if secondAlarm > 0 then
                if (count of existingAlarms) ≥ 2 then
                    set trigger interval of item 2 of existingAlarms to (-secondAlarm)
                else
                    make new display alarm at end with properties {trigger interval:(-secondAlarm)}
                end if
            end if
            return uid
        end tell
    end tell
end run

on dateFromISO(isoText)
    set newDate to current date
    set day of newDate to 1
    set year of newDate to (text 1 thru 4 of isoText) as integer
    set month of newDate to (text 6 thru 7 of isoText) as integer
    set day of newDate to (text 9 thru 10 of isoText) as integer
    set hours of newDate to (text 12 thru 13 of isoText) as integer
    set minutes of newDate to (text 15 thru 16 of isoText) as integer
    set seconds of newDate to (text 18 thru 19 of isoText) as integer
    return newDate
end dateFromISO
'''
DELETE_SCRIPT = r'''
on run argv
    set calendarName to item 1 of argv
    set eventUid to item 2 of argv
    tell application "Calendar"
        if not (exists calendar calendarName) then return "missing"
        set matchedEvents to every event of calendar calendarName whose uid is eventUid
        if (count of matchedEvents) is 0 then return "missing"
        delete item 1 of matchedEvents
        return "deleted"
    end tell
end run
'''


def mapping_path() -> Path:
    return data_dir() / "macos-calendar.json"


def local_text(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL)
    return parsed.astimezone(LOCAL).strftime("%Y-%m-%dT%H:%M:%S")


def osascript(script: str, arguments: list[str]) -> str:
    result = subprocess.run(
        ["osascript", "-", *arguments],
        input=script,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        message = result.stderr.strip() or "macOS 日历操作失败"
        raise RuntimeError(message)
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync campus deadlines to macOS Calendar")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ensure", "status"):
        command = sub.add_parser(name)
        command.add_argument("--calendar", default="校园助手")
    command = sub.add_parser("upsert")
    command.add_argument("--calendar", default="校园助手")
    command.add_argument("--source", required=True)
    command.add_argument("--title", required=True)
    command.add_argument("--start", required=True)
    command.add_argument("--end", required=True)
    command.add_argument("--all-day", action="store_true")
    command.add_argument("--notes", default="")
    command.add_argument("--location", default="")
    command.add_argument("--alarm-minutes", type=int, nargs="*")
    command.add_argument("--confirm", action="store_true")
    command = sub.add_parser("delete")
    command.add_argument("--calendar", default="校园助手")
    command.add_argument("--source", required=True)
    command.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    payload = read_json(mapping_path(), {"events": {}})
    events = payload.setdefault("events", {})
    if args.command in ("ensure", "status"):
        name = osascript(ENSURE_SCRIPT, [args.calendar])
        emit({"ok": True, "calendar": name, "trackedEvents": len(events)})
        return
    if not args.confirm:
        raise RuntimeError("写入 macOS 日历需要用户确认后添加 --confirm")
    if args.command == "upsert":
        start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
        if end <= start:
            raise RuntimeError("事件结束时间必须晚于开始时间")
        if not args.title.strip():
            raise RuntimeError("事件标题不能为空")
        if args.all_day and (len(args.start) != 10 or len(args.end) != 10):
            raise RuntimeError("全天事件的开始和结束必须使用 YYYY-MM-DD")
        configured_alarms = args.alarm_minutes
        if configured_alarms is None:
            configured_alarms = [] if args.all_day else [1440, 120]
        alarms = (configured_alarms + [0, 0])[:2]
        if any(value < 0 for value in alarms):
            raise RuntimeError("提醒时间不能为负数")
        previous = events.get(args.source, {})
        uid = osascript(
            UPSERT_SCRIPT,
            [
                args.calendar,
                local_text(args.start),
                local_text(args.end),
                args.title.strip(),
                args.notes,
                args.location,
                previous.get("uid", ""),
                str(alarms[0]),
                str(alarms[1]),
                "true" if args.all_day else "false",
            ],
        )
        if not uid:
            raise RuntimeError("macOS 日历没有返回事件标识")
        events[args.source] = {
            "uid": uid,
            "calendar": args.calendar,
            "title": args.title.strip(),
            "startAt": args.start,
            "endAt": args.end,
        }
        write_json(mapping_path(), payload)
        emit({"ok": True, "source": args.source, "uid": uid, "calendar": args.calendar, "updated": bool(previous)})
        return
    previous = events.get(args.source)
    if not previous:
        raise RuntimeError("找不到该来源对应的日历事件")
    result = osascript(DELETE_SCRIPT, [args.calendar, previous["uid"]])
    if result == "deleted":
        del events[args.source]
        write_json(mapping_path(), payload)
    emit({"ok": True, "source": args.source, "result": result})


if __name__ == "__main__":
    run(main)
