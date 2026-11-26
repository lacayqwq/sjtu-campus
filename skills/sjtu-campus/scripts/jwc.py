from __future__ import annotations

import argparse
import calendar
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from common import emit, run
from notice_common import acknowledge, load_store, normalize_text, parse_html, pending_items, save_store, source_state


NAME = "jwc"
ORIGIN = "https://jwc.sjtu.edu.cn"
LIST_URL = f"{ORIGIN}/index/mxxsdtz.htm"
DEFAULT_STORE = Path("campus-assistant/notices.json")


class ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.current: dict | None = None
        self.section = ""
        self.capture = ""
        self.next_pending = False
        self.next_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "li" and (values.get("id") or "").startswith("line_u"):
            self.current = {"day": "", "month": "", "title": "", "summary": "", "href": ""}
        if self.current is not None:
            if tag == "div" and "sj" in classes:
                self.section = "date"
            elif tag == "div" and "wz" in classes:
                self.section = "content"
            elif tag == "a" and self.section == "content" and not self.current["href"]:
                self.current["href"] = values.get("href") or ""
            elif tag == "h2" and self.section in ("date", "content"):
                self.capture = "day" if self.section == "date" else "title"
            elif tag == "p" and self.section in ("date", "content"):
                self.capture = "month" if self.section == "date" else "summary"
        if "p_next" in classes:
            self.next_pending = True
        if self.next_pending and tag == "a" and values.get("href"):
            self.next_url = values["href"]

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h2", "p"):
            self.capture = ""
        if tag == "div" and self.current is not None:
            self.section = ""
        if tag == "a" and self.next_pending:
            self.next_pending = False
        if tag == "li" and self.current is not None:
            for key in self.current:
                self.current[key] = normalize_text(self.current[key])
            self.rows.append(self.current)
            self.current = None
            self.section = ""
            self.capture = ""

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture] += data


def parse_list(html: str, page_url: str = LIST_URL) -> dict:
    parser = ListParser()
    parser.feed(html)
    parser.close()
    article_ids = re.findall(r"dynclicks_u\d+_(\d+)", html)
    items = []
    for index, row in enumerate(parser.rows):
        url = urllib.parse.urljoin(page_url, row["href"])
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        path_id = re.search(r"/(\d+)\.htm$", parsed.path)
        article_id = (
            article_ids[index] if index < len(article_ids) else None
        ) or (query.get("wbnewsid") or [None])[0] or (path_id.group(1) if path_id else None)
        if parsed.scheme not in ("http", "https") or not article_id or not row["title"]:
            raise RuntimeError("教务处通知列表内容不完整")
        try:
            published = datetime.strptime(f"{row['month']}.{row['day']}", "%Y.%m.%d").date()
        except ValueError as error:
            raise RuntimeError("教务处通知日期无效") from error
        items.append({
            "id": f"jwc:{article_id}",
            "source": NAME,
            "title": row["title"],
            "summary": row["summary"],
            "publishedAt": f"{published.isoformat()}T00:00:00+08:00",
            "url": url,
        })
    if not items:
        raise RuntimeError("未能读取教务处通知列表")
    next_url = urllib.parse.urljoin(page_url, parser.next_url) if parser.next_url else None
    return {"items": items, "next": next_url}


def fetch(url: str, headers: dict | None = None) -> tuple[str | None, object]:
    request_headers = {"Accept": "text/html,*/*;q=0.8", "User-Agent": "Kimi-SJTU-Campus/1.0", **(headers or {})}
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace"), response.headers
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, error.headers
        error.close()
        raise RuntimeError(f"教务处请求失败（HTTP {error.code}）") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接教务处：{error.reason}") from error


def header(headers: object, name: str) -> str | None:
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is not None:
            return value
        for key, value in headers.items():
            if key.casefold() == name.casefold():
                return value
    return None


def current_time(value: str | None) -> datetime:
    now = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def months_before(now: datetime, count: int) -> datetime:
    month_index = now.year * 12 + now.month - 1 - count
    year, month = divmod(month_index, 12)
    month += 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def sync(args: argparse.Namespace) -> None:
    store = args.store.resolve()
    data = load_store(store)
    state = source_state(data, NAME)
    initial = not state.get("initialized")
    now = current_time(args.now)
    request_headers = {}
    if not initial:
        if state.get("etag"):
            request_headers["If-None-Match"] = state["etag"]
        if state.get("modified"):
            request_headers["If-Modified-Since"] = state["modified"]
    html, response_headers = fetch(LIST_URL, request_headers)
    added = []
    if html is None:
        if initial:
            raise RuntimeError("教务处首次同步尚未建立基线")
    else:
        page = parse_list(html)
        items = page["items"]
        if initial:
            cutoff = months_before(now, 2)
            pages = 1
            while (
                page["next"]
                and pages < 20
                and all(datetime.fromisoformat(item["publishedAt"]) >= cutoff for item in page["items"])
            ):
                target = urllib.parse.urlparse(page["next"])
                if target.scheme != "https" or target.hostname != "jwc.sjtu.edu.cn":
                    raise RuntimeError("教务处分页地址无效")
                older_html, _ = fetch(page["next"])
                if older_html is None:
                    break
                page = parse_list(older_html, page["next"])
                items.extend(page["items"])
                pages += 1
            items = [item for item in items if datetime.fromisoformat(item["publishedAt"]) >= cutoff]
        for item in items:
            item_id = item["id"]
            is_new = item_id not in state["items"]
            state["items"][item_id] = item
            if is_new:
                added.append(item_id)
                if not initial:
                    state["pending"].append(item_id)
        state["etag"] = header(response_headers, "ETag")
        state["modified"] = header(response_headers, "Last-Modified")
    state["initialized"] = True
    state["checkedAt"] = now.isoformat()
    save_store(store, data)
    emit({
        "ok": True,
        "initialSync": initial,
        "imported": len(added) if initial else 0,
        "added": added,
        "candidates": pending_items(state),
        "pendingCount": len(state["pending"]),
    })


def read_notice(args: argparse.Namespace) -> None:
    data = load_store(args.store.resolve())
    state = source_state(data, NAME)
    item = state["items"].get(args.id)
    if not item:
        raise RuntimeError("通知 ID 不存在，请先同步教务处通知")
    parsed = urllib.parse.urlparse(item["url"])
    if parsed.scheme != "https" or parsed.hostname != "jwc.sjtu.edu.cn":
        raise RuntimeError("此通知发布在外部网站，请查看原文")
    html, _ = fetch(item["url"])
    if html is None:
        raise RuntimeError("教务处正文读取失败")
    plain, _ = parse_html(html)
    if "该栏目内容已设置访问权限" in plain and "校内IP" in plain:
        raise RuntimeError("该通知仅允许校内 IP 访问，请连接校园网或 VPN 后重试")
    text, links = parse_html(html, "v_news_content")
    if not text:
        raise RuntimeError("未能读取通知正文，请查看原文")
    attachments = []
    seen = set()
    for link in links:
        target = urllib.parse.urljoin(item["url"], link["href"])
        parsed_target = urllib.parse.urlparse(target)
        file_id = urllib.parse.parse_qs(parsed_target.query).get("wbfileid", [None])[0]
        if (
            parsed_target.scheme == "https"
            and parsed_target.hostname == "jwc.sjtu.edu.cn"
            and parsed_target.path == "/system/_content/download.jsp"
            and file_id
            and file_id not in seen
        ):
            seen.add(file_id)
            attachments.append({
                "id": file_id,
                "name": link["text"] or "附件",
                "url": target,
                "requiresCaptcha": True,
            })
    emit({
        "ok": True,
        **item,
        "text": text[:20000],
        "truncated": len(text) > 20000,
        "attachments": attachments,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi 教务处通知增量同步")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("sync")
    command.add_argument("--now", help="测试用 ISO 时间")
    command = sub.add_parser("read")
    command.add_argument("--id", required=True)
    command = sub.add_parser("ack")
    command.add_argument("--id", action="append", required=True)
    args = parser.parse_args()
    if args.command == "sync":
        sync(args)
    elif args.command == "read":
        read_notice(args)
    else:
        emit(acknowledge(args.store.resolve(), NAME, args.id))


if __name__ == "__main__":
    run(main)
