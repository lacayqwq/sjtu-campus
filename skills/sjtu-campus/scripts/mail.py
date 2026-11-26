from __future__ import annotations

import argparse
from email import message_from_bytes
from email.message import EmailMessage, Message
from email.header import decode_header, make_header
from email.utils import parseaddr
from html.parser import HTMLParser
import imaplib
from pathlib import Path
import uuid

from common import data_dir, dialog, emit, keychain_get, keychain_set, read_json, run, write_json

ACCOUNT = "mail-password"


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def decoded(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


def config() -> dict:
    payload = read_json(data_dir() / "mail.json")
    if not payload:
        raise RuntimeError("尚未配置校园邮箱，请先执行 configure")
    return payload


def connect() -> imaplib.IMAP4_SSL:
    payload = config()
    password = keychain_get(ACCOUNT)
    if not password:
        raise RuntimeError("钥匙串中没有邮箱密码，请重新配置")
    client = imaplib.IMAP4_SSL(payload["host"], payload["port"], timeout=20)
    client.login(payload["address"], password)
    return client


def configure(args: argparse.Namespace) -> None:
    address = dialog("请输入校园邮箱地址，例如 name@sjtu.edu.cn")
    password = dialog("请输入邮箱密码或专用授权码。仅保存到 macOS 钥匙串。", secret=True)
    client = imaplib.IMAP4_SSL(args.host, args.port, timeout=20)
    try:
        client.login(address, password)
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    keychain_set(ACCOUNT, password)
    write_json(data_dir() / "mail.json", {"address": address, "host": args.host, "port": args.port})
    emit({"ok": True, "address": address, "host": args.host, "port": args.port})


def text_parts(message: Message) -> tuple[str, list[dict]]:
    plain, html = [], []
    attachments = []
    for part in message.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = decoded(part.get_filename()) if part.get_filename() else None
        if disposition == "attachment" or filename:
            attachments.append({"index": len(attachments), "filename": filename or "attachment", "contentType": content_type, "size": len(part.get_payload(decode=True) or b""), "part": part})
            continue
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        (plain if content_type == "text/plain" else html).append(text)
    if plain:
        body = "\n".join(plain)
    else:
        extractor = TextExtractor()
        extractor.feed("\n".join(html))
        body = "\n".join(line.strip() for line in extractor.parts if line.strip())
    metadata = [{key: value for key, value in item.items() if key != "part"} for item in attachments]
    return body, metadata


def summary(payload: bytes, folder: str) -> dict:
    message = message_from_bytes(payload)
    name, address = parseaddr(message.get("From", ""))
    return {
        "uid": None,
        "folder": folder,
        "subject": decoded(message.get("Subject")),
        "from": {"name": decoded(name), "address": address},
        "date": message.get("Date"),
        "messageId": message.get("Message-ID"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi SJTU mail connector")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("configure")
    command.add_argument("--host", default="mail.sjtu.edu.cn")
    command.add_argument("--port", type=int, default=993)
    sub.add_parser("folders")
    command = sub.add_parser("search")
    command.add_argument("--query", default="")
    command.add_argument("--from", dest="sender", default="")
    command.add_argument("--folder", default="INBOX")
    command.add_argument("--limit", type=int, default=30)
    command.add_argument("--scan", type=int, default=200)
    command = sub.add_parser("read")
    command.add_argument("--uid", required=True)
    command.add_argument("--folder", default="INBOX")
    command = sub.add_parser("attachment")
    command.add_argument("--uid", required=True)
    command.add_argument("--folder", default="INBOX")
    command.add_argument("--index", type=int, required=True)
    command.add_argument("--output", required=True)
    command = sub.add_parser("draft")
    command.add_argument("--to", required=True)
    command.add_argument("--subject", required=True)
    command.add_argument("--body")
    command.add_argument("--body-file")
    args = parser.parse_args()

    if args.command == "configure":
        configure(args)
        return
    if args.command == "draft":
        body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body or ""
        message = EmailMessage()
        message["To"] = args.to
        message["From"] = config()["address"]
        message["Subject"] = args.subject
        message.set_content(body)
        path = data_dir() / "drafts" / f"{uuid.uuid4().hex}.eml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(message.as_bytes())
        emit({"ok": True, "draft": str(path), "note": "仅保存草稿，未发送"})
        return

    client = connect()
    try:
        if args.command == "folders":
            status, rows = client.list()
            if status != "OK":
                raise RuntimeError("无法读取邮箱文件夹")
            emit({"ok": True, "folders": [row.decode(errors="replace") for row in rows]})
            return
        status, _ = client.select(args.folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"无法打开邮箱文件夹 {args.folder}")
        status, rows = client.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("无法搜索邮件")
        uids = rows[0].split()
        if args.command == "search":
            selected = uids[-min(max(args.scan, args.limit), 500) :]
            found = []
            for uid in reversed(selected):
                status, parts = client.uid(
                    "fetch",
                    uid,
                    "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])",
                )
                if status != "OK":
                    continue
                for part in parts:
                    if not isinstance(part, tuple):
                        continue
                    row = summary(part[1], args.folder)
                    row["uid"] = uid.decode()
                    haystack = f"{row['subject']} {row['from']['name']} {row['from']['address']}".casefold()
                    if args.query.casefold() in haystack and args.sender.casefold() in haystack:
                        found.append(row)
                        break
                if len(found) >= args.limit:
                    break
            emit({"ok": True, "count": len(found), "messages": found})
            return
        status, parts = client.uid("fetch", args.uid, "(BODY.PEEK[])")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            raise RuntimeError("找不到该邮件或无法读取正文")
        message = message_from_bytes(parts[0][1])
        row = summary(parts[0][1], args.folder)
        row["uid"] = args.uid
        body, attachments = text_parts(message)
        if args.command == "read":
            emit({"ok": True, "message": {**row, "body": body, "attachments": attachments}})
            return
        parts_with_content = []
        for part in message.walk():
            filename = decoded(part.get_filename()) if part.get_filename() else None
            if part.get_content_disposition() == "attachment" or filename:
                parts_with_content.append((filename or "attachment", part))
        if args.index < 0 or args.index >= len(parts_with_content):
            raise RuntimeError("附件序号无效")
        filename, part = parts_with_content[args.index]
        output = Path(args.output).expanduser().resolve()
        if output.exists():
            raise RuntimeError("目标文件已存在，不会覆盖")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(part.get_payload(decode=True) or b"")
        emit({"ok": True, "path": str(output), "filename": filename})
    finally:
        client.logout()


if __name__ == "__main__":
    run(main)
