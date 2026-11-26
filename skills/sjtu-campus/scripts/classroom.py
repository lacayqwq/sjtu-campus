from __future__ import annotations

import argparse
from html.parser import HTMLParser
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from common import emit, keychain_get, paginated, request_json, run

CANVAS = "https://oc.sjtu.edu.cn"
VIDEO_HOME = "https://v.sjtu.edu.cn/jy-application-resourcemanage-ui/"
BASE = "https://v.sjtu.edu.cn/jy-application-resourcemanage/v1"
ME = "https://v.sjtu.edu.cn/jy-application-resourcemanage/authority/me"


class AuthError(RuntimeError):
    pass


class LaunchFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self.current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.current = {
                "action": values.get("action") or "",
                "method": (values.get("method") or "get").lower(),
                "fields": [],
            }
        elif tag == "input" and self.current is not None and values.get("name"):
            kind = (values.get("type") or "text").lower()
            if "disabled" not in values and kind not in {"checkbox", "radio", "button", "submit", "image", "reset", "file"}:
                self.current["fields"].append((values["name"], values.get("value") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


class Classroom:
    def __init__(self) -> None:
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.jwt_token = ""
        self.logged_in = False

    def open(self, url: str, data: bytes | None = None) -> tuple[bytes, str, str]:
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "User-Agent": "Kimi-SJTU-Campus/1.0",
            },
        )
        try:
            with self.opener.open(request, timeout=25) as response:
                body = response.read()
                current_url = response.geturl()
                parsed = urllib.parse.urlparse(current_url)
                fragment_query = parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else ""
                values = urllib.parse.parse_qs(parsed.query) | urllib.parse.parse_qs(fragment_query)
                self.jwt_token = values.get("jwt_token", [self.jwt_token])[0]
                return body, response.headers.get("Content-Type", ""), current_url
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403):
                raise AuthError("校园视频会话未建立或已失效") from error
            raise RuntimeError(f"校园登录入口返回 HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"无法连接校园视频：{error.reason}") from error

    def login(self) -> None:
        if self.logged_in:
            return
        token = keychain_get("canvas-token")
        if not token:
            raise AuthError("尚未配置 Canvas Token，无法自动登录课堂视频")
        login_url = ""
        courses = paginated(f"{CANVAS}/api/v1/courses?enrollment_state=active&per_page=100", token)
        for course in courses:
            course_id = course.get("id")
            if not isinstance(course_id, int):
                continue
            tabs = paginated(f"{CANVAS}/api/v1/courses/{course_id}/tabs?per_page=100", token)
            tab = next(
                (
                    row
                    for row in tabs
                    if "课堂视频" in str(row.get("label", "")) and "旧版" not in str(row.get("label", ""))
                ),
                None,
            )
            if not tab:
                continue
            target = urllib.parse.urljoin(CANVAS, str(tab.get("html_url", "")))
            if urllib.parse.urlparse(target).netloc != "oc.sjtu.edu.cn":
                continue
            query = urllib.parse.urlencode({"return_to": target})
            payload, _ = request_json(f"{CANVAS}/api/v1/login/session_token?{query}", token)
            candidate = payload.get("session_url") if isinstance(payload, dict) else None
            if not isinstance(candidate, str) or urllib.parse.urlparse(candidate).netloc != "oc.sjtu.edu.cn":
                raise RuntimeError("Canvas 没有返回有效的课堂视频登录地址")
            login_url = candidate
            break
        if not login_url:
            raise RuntimeError("Canvas 有效课程中没有找到课堂视频入口")

        url = login_url
        data = None
        for _ in range(10):
            body, content_type, current_url = self.open(url, data)
            try:
                self.check_identity(auto_login=False)
                self.logged_in = True
                return
            except AuthError:
                pass
            if "html" not in content_type.lower():
                break
            parser = LaunchFormParser()
            parser.feed(body.decode("utf-8", errors="replace"))
            form = next((item for item in parser.forms if item["action"]), None)
            if not form:
                break
            target = urllib.parse.urljoin(current_url, form["action"])
            parsed = urllib.parse.urlparse(target)
            if parsed.scheme != "https" or not (
                parsed.hostname == "sjtu.edu.cn" or str(parsed.hostname).endswith(".sjtu.edu.cn")
            ):
                raise RuntimeError("课堂视频登录表单指向了非校园地址")
            fields = urllib.parse.urlencode(form["fields"]).encode()
            if form["method"] == "post":
                url, data = target, fields
            else:
                separator = "&" if parsed.query else "?"
                url, data = f"{target}{separator}{fields.decode()}", None
            time.sleep(0.5)
        raise AuthError("通过 Canvas 自动登录课堂视频失败：纯 HTTP 登录流程未完成")

    def request(self, url: str, *, identity: bool = False, auto_login: bool = True) -> object:
        for attempt in range(2):
            headers = {"Accept": "application/json", "Referer": VIDEO_HOME, "User-Agent": "Kimi-SJTU-Campus/1.0"}
            if self.jwt_token:
                headers["jwt-token"] = self.jwt_token
            request = urllib.request.Request(url, headers=headers)
            try:
                with self.opener.open(request, timeout=25) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code in (401, 403) or (identity and error.code == 500):
                    if auto_login and attempt == 0:
                        self.login()
                        continue
                    raise AuthError("校园视频登录已失效") from error
                if error.code in (408, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(0.25)
                    continue
                raise RuntimeError(f"课堂视频接口返回 HTTP {error.code}") from error
            except urllib.error.URLError as error:
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                raise RuntimeError(f"无法连接课堂视频：{error.reason}") from error
            except json.JSONDecodeError as error:
                if auto_login and attempt == 0:
                    self.login()
                    continue
                raise RuntimeError("课堂视频没有返回预期数据") from error
        raise RuntimeError("课堂视频请求失败")

    def check_identity(self, *, auto_login: bool = True) -> None:
        payload = self.request(ME, identity=True, auto_login=auto_login)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise AuthError("校园视频身份验证失败")
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("user"):
            raise AuthError("校园视频身份验证失败")


def unwrap(payload: object) -> object:
    if not isinstance(payload, dict):
        raise RuntimeError("课堂视频响应数据格式已变化")
    if payload.get("status") in (401, 403) or str(payload.get("code")) == "-1":
        raise AuthError("校园视频登录已失效")
    if payload.get("ok") is False or payload.get("dataOk") is False:
        raise RuntimeError("课堂视频接口请求失败")
    status = payload.get("status")
    if isinstance(status, int) and status != 200:
        raise RuntimeError(f"课堂视频接口返回状态 {status}")
    return payload.get("result") if payload.get("result") is not None else payload.get("data")


def all_pages(client: Classroom, path: str, params: dict[str, str]) -> list[dict]:
    records = []
    for index in range(1, 101):
        query = urllib.parse.urlencode({**params, "page.pageIndex": str(index), "page.pageSize": "1000"})
        response = client.request(f"{BASE}/{path}?{query}")
        data = unwrap(response)
        if (
            index == 1
            and isinstance(response, dict)
            and response.get("ok") is True
            and response.get("dataOk") is True
            and isinstance(data, dict)
            and data.get("rowCount") == 0
            and data.get("pageCount") == 0
            and data.get("records") in (None, [])
        ):
            return []
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise RuntimeError("课堂视频列表数据格式已变化")
        rows = data["records"]
        if not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("课堂视频列表数据格式已变化")
        records.extend(rows)
        page_count = data.get("pageCount")
        if isinstance(page_count, int) and index >= page_count:
            return records
        if page_count is None and len(rows) < 1000:
            return records
    raise RuntimeError("课堂视频分页过多，已停止读取")


def positive_id(value: object, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"课堂视频{label}格式已变化") from error
    if number <= 0:
        raise RuntimeError(f"课堂视频{label}格式已变化")
    return number


def list_courses(client: Classroom) -> list[dict]:
    client.check_identity()
    terms = client.request(f"{BASE}/list/termYear")
    if isinstance(terms, dict):
        terms = unwrap(terms)
    if not isinstance(terms, list) or not terms:
        return []
    term = next((row for row in terms if isinstance(row, dict) and row.get("currentTerm") is True), terms[-1])
    if not isinstance(term, dict):
        raise RuntimeError("课堂视频学期数据格式已变化")
    term_id = positive_id(term.get("id"), "学期编号")
    records = all_pages(
        client,
        "myself/curriculum",
        {"acteId": str(term_id), "courBeginTime": "", "courEndTime": ""},
    )
    on_demand = all_pages(
        client,
        "group_subject_vod_list/t-1",
        {"page.orders[0].asc": "false", "page.orders[0].field": "courBeginTime"},
    )
    for row in on_demand:
        if positive_id(row.get("acteId"), "课程学期编号") == term_id:
            records.append(row)
    courses = {}
    for row in records:
        if row.get("teclId") is None:
            continue
        course_id = positive_id(row.get("teclId"), "课程编号")
        name = row.get("subjName")
        if not isinstance(name, str):
            raise RuntimeError("课堂视频课程数据格式已变化")
        courses[course_id] = {"id": course_id, "code": row.get("subjCode") or "", "name": name}
    return list(courses.values())


def list_videos(client: Classroom, course_id: int) -> list[dict]:
    client.check_identity()
    records = all_pages(
        client,
        "subject_vod_list_new",
        {
            "teclIds": str(course_id),
            "page.orders[0].asc": "false",
            "page.orders[0].field": "courBeginTime",
        },
    )
    videos = []
    for row in records:
        if not isinstance(row.get("courBeginTime"), str) or not isinstance(row.get("courEndTime"), str):
            raise RuntimeError("课堂视频录像数据格式已变化")
        videos.append(
            {
                "id": positive_id(row.get("id"), "录像编号"),
                "start": row["courBeginTime"],
                "end": row["courEndTime"],
                "room": row.get("clroName") or "",
                "period": str(row.get("letiNumber") or ""),
            }
        )
    return videos


def streams(client: Classroom, video_id: int) -> list[dict]:
    client.check_identity()
    payload = unwrap(client.request(f"{BASE}/course_vod_urls_new?{urllib.parse.urlencode({'courseId': video_id})}"))
    if not isinstance(payload, dict) or not isinstance(payload.get("courseVodViewList"), list):
        raise RuntimeError("暂未获取到可播放录像，请稍后重试")
    result = []
    for row in payload["courseVodViewList"]:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            raise RuntimeError("课堂视频播放数据格式已变化")
        parsed = urllib.parse.urlparse(row["url"])
        if (
            parsed.scheme != "https"
            or parsed.hostname != "live.sjtu.edu.cn"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or not parsed.path.lower().endswith(".mp4")
        ):
            raise RuntimeError("课堂视频返回了不支持的媒体地址")
        result.append(
            {
                "url": row["url"],
                "view": positive_id(row.get("viewNum"), "画面编号"),
                "duration": float(row.get("vodTime") or 0),
            }
        )
    return result


def live_schedule(client: Classroom) -> list[dict]:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    client.check_identity()
    terms = client.request(f"{BASE}/list/termYear")
    if isinstance(terms, dict):
        terms = unwrap(terms)
    if not isinstance(terms, list):
        raise RuntimeError("课堂视频学期数据格式已变化")
    current = next((row for row in terms if row.get("currentTerm") is True), None)
    if not current:
        raise RuntimeError("没有找到当前学期")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    clock = now.strftime("%Y-%m-%d %H:%M:%S")
    rows = all_pages(client, "myself/curriculum", {
        "acteId": str(positive_id(current.get("id"), "学期编号")),
        "courBeginTime": f"{day} 00:00:00", "courEndTime": f"{day} 23:59:59",
    })
    result = []
    for row in rows:
        start, end = row.get("courBeginTime"), row.get("courEndTime")
        if not isinstance(start, str) or not isinstance(end, str):
            raise RuntimeError("课堂视频节次时间格式已变化")
        result.append({
            "videoId": positive_id(row.get("id"), "节次编号"), "name": row.get("subjName"),
            "start": start, "end": end, "scheduledNow": start <= clock < end,
            "liveEnabled": str(row.get("liveEnable")) == "1",
        })
    return sorted(result, key=lambda row: row["start"])


def live_detail(client: Classroom, lesson: dict, *, include_urls: bool) -> dict:
    result = {"ok": True, "lesson": lesson, "available": False, "streams": []}
    if not lesson["liveEnabled"] or not lesson["scheduledNow"]:
        result["reason"] = "直播未启用" if not lesson["liveEnabled"] else "当前不在该节上课时段"
        return result
    config = unwrap(client.request(f"{BASE}/mss_sdk_play_mode"))
    if isinstance(config, str):
        config = json.loads(config)
    if not isinstance(config, dict):
        raise RuntimeError("直播协议配置格式已变化")
    modes = [str(config.get(key)) for key, show in zip(("first", "second", "last"), config.get("show", [1, 1, 1])) if str(show) != "0"]
    if "3" not in modes:
        raise RuntimeError("此课程的官方播放器未提供 HLS 模式")
    query = urllib.parse.urlencode({"courseId": lesson["videoId"], "playType": 3})
    payload = client.request(f"{BASE}/course_vod_videoinfos?{query}")
    if isinstance(payload, dict) and payload.get("status") == 501:
        result["reason"] = "平台报告直播未开始"
        return result
    data = unwrap(payload)
    if not isinstance(data, dict) or data.get("errorMsg"):
        raise RuntimeError("平台未提供该节的直播播放信息")
    if str(data.get("lvcrLiveStatus")) == "0":
        result["reason"] = "平台报告尚未开播"
        return result
    views = data.get("courseDeviceViewDtoList")
    if data.get("lvcrLiveStatus") is None or not isinstance(views, list):
        raise RuntimeError("直播详情格式已变化")
    for row in views:
        if not isinstance(row, dict):
            raise RuntimeError("直播机位格式已变化")
        address = row.get("chanNameMainPlayUrl")
        if not address:
            continue
        parsed = urllib.parse.urlsplit(address)
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".sjtu.edu.cn")
                or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None
                or not parsed.path.lower().endswith(".m3u8")):
            raise RuntimeError("直播机位没有返回受支持的校园 HTTPS HLS 地址")
        ticket = row.get("mainTokenStr")
        if not isinstance(ticket, str) or not ticket:
            raise RuntimeError("平台没有返回该直播机位的播放凭证")
        item = {"view": positive_id(row.get("deviViewNum"), "机位编号"), "protocol": "HLS"}
        if include_urls:
            token_param = urllib.parse.urlencode({"account_token": ticket})
            item["url"] = parsed._replace(query="&".join(filter(None, [parsed.query, token_param])), fragment="").geturl()
        result["streams"].append(item)
    result["available"] = bool(result["streams"])
    if not result["available"]:
        result["reason"] = "平台未返回可播放的机位"
    return result


def timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def transcript(client: Classroom, video_id: int, start: float, end: float) -> dict:
    client.check_identity()
    payload = client.request(f"{BASE}/course/ai/translate/{video_id}?useOriginal=true")
    empty = (
        isinstance(payload, dict)
        and payload.get("code") is None
        and payload.get("status") == 200
        and payload.get("ok") is True
        and payload.get("data") is None
    )
    data = None if empty else unwrap(payload)
    if data is None:
        return {"available": False, "segments": [], "text": ""}
    if not isinstance(data, dict) or not isinstance(data.get("afterAssemblyList"), list):
        raise RuntimeError("课堂视频文稿数据格式已变化")
    available = False
    segments = []
    for row in data["afterAssemblyList"]:
        if not isinstance(row, dict) or not isinstance(row.get("res"), str):
            raise RuntimeError("课堂视频文稿数据格式已变化")
        begin, finish = row.get("bg"), row.get("ed")
        if not isinstance(begin, (int, float)) or not isinstance(finish, (int, float)) or begin < 0 or finish < begin:
            raise RuntimeError("课堂视频文稿数据格式已变化")
        text = row["res"].strip()
        if text:
            available = True
            if finish > start * 1000 and begin < end * 1000:
                segments.append({"startMs": begin, "endMs": finish, "text": text})
    segments.sort(key=lambda row: row["startMs"])
    text = "\n\n".join(
        f"[{timestamp(int(row['startMs']))}–{timestamp(int(row['endMs']))}] {row['text']}" for row in segments
    )
    if text:
        text += "\n"
    return {"available": available, "segments": segments, "text": text}


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi 交大课堂视频连接器")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("courses")
    command.add_argument("--query")
    command = sub.add_parser("videos")
    command.add_argument("--course", type=positive_int, required=True)
    command = sub.add_parser("live", help="查看本人今天的课堂直播安排和当前节次的 HLS 机位")
    command.add_argument("--video", type=positive_int, help="本人今日课表中的节次 ID")
    command.add_argument("--include-urls", action="store_true", help="仅供本机播放器通过管道接收临时签名地址")
    command = sub.add_parser("streams")
    command.add_argument("--video", type=positive_int, required=True)
    command = sub.add_parser("transcript")
    command.add_argument("--video", type=positive_int, required=True)
    command.add_argument("--start", type=float, default=0)
    command.add_argument("--end", type=float, default=600)
    args = parser.parse_args()
    client = Classroom()

    if args.command == "courses":
        rows = list_courses(client)
        if args.query:
            rows = [row for row in rows if args.query.casefold() in f"{row['name']} {row['code']}".casefold()]
        emit({"ok": True, "count": len(rows), "courses": rows})
    elif args.command == "videos":
        rows = list_videos(client, args.course)
        emit({"ok": True, "courseId": args.course, "count": len(rows), "videos": rows})
    elif args.command == "live":
        import sys

        if args.include_urls and (args.video is None or sys.stdout.isatty()):
            raise RuntimeError("播放地址只可结合 --video 交给本机程序通过管道接收")
        schedule = live_schedule(client)
        if args.video is None:
            emit({"ok": True, "timezone": "Asia/Shanghai", "lessons": schedule})
        else:
            lesson = next((row for row in schedule if row["videoId"] == args.video), None)
            if lesson is None:
                raise RuntimeError("该节次不在本人今日课堂视频课表中")
            emit(live_detail(client, lesson, include_urls=args.include_urls))
    elif args.command == "streams":
        rows = streams(client, args.video)
        emit({"ok": True, "videoId": args.video, "count": len(rows), "streams": rows})
    elif args.command == "transcript":
        if args.start < 0 or args.end <= args.start:
            raise RuntimeError("文稿时间范围无效")
        result = transcript(client, args.video, args.start, args.end)
        emit({"ok": True, "videoId": args.video, "startSeconds": args.start, "endSeconds": args.end, **result})


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("编号必须为正整数")
    return number


if __name__ == "__main__":
    run(main)
