from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from common import read_json, write_json


BLOCK_TAGS = {"br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "tr"}


class TextAndLinksParser(HTMLParser):
    def __init__(self, target_class: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.target_class = target_class
        self.target_depth = 0
        self.skip_depth = 0
        self.parts: list[str] = []
        self.links: list[dict] = []
        self.current_link: dict | None = None

    def active(self) -> bool:
        return self.target_class is None or self.target_depth > 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if self.target_class and self.target_depth == 0 and self.target_class in classes:
            self.target_depth = 1
        elif self.target_depth and tag == "div":
            self.target_depth += 1
        if tag in ("script", "style"):
            self.skip_depth += 1
        if tag == "a" and values.get("href"):
            self.current_link = {"href": values["href"], "text_parts": []}
        if self.active() and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_link is not None:
            self.links.append({
                "href": self.current_link["href"],
                "text": normalize_text("".join(self.current_link["text_parts"])),
            })
            self.current_link = None
        if tag in ("script", "style") and self.skip_depth:
            self.skip_depth -= 1
        if self.active() and tag in BLOCK_TAGS:
            self.parts.append("\n")
        if self.target_depth and tag == "div":
            self.target_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.current_link is not None:
            self.current_link["text_parts"].append(data)
        if self.active() and not self.skip_depth:
            self.parts.append(data)


def normalize_text(value: str) -> str:
    return "\n".join(
        line for line in (" ".join(part.split()) for part in value.splitlines()) if line
    )


def parse_html(value: str, target_class: str | None = None) -> tuple[str, list[dict]]:
    parser = TextAndLinksParser(target_class)
    parser.feed(value)
    parser.close()
    return normalize_text("".join(parser.parts)), parser.links


def html_text(value: object) -> str:
    return parse_html(value if isinstance(value, str) else "")[0]


def load_store(path: Path) -> dict:
    data = read_json(path, {"version": 1, "sources": {}})
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise RuntimeError("通知状态文件结构无效")
    return data


def source_state(data: dict, name: str) -> dict:
    sources = data["sources"]
    state = sources.setdefault(name, {})
    state.setdefault("items", {})
    state.setdefault("pending", [])
    if not isinstance(state["items"], dict) or not isinstance(state["pending"], list):
        raise RuntimeError(f"{name} 通知状态结构无效")
    return state


def save_store(path: Path, data: dict) -> None:
    write_json(path, data)


def pending_items(state: dict) -> list[dict]:
    return [state["items"][item_id] for item_id in state["pending"] if item_id in state["items"]]


def acknowledge(path: Path, name: str, ids: list[str]) -> dict:
    data = load_store(path)
    state = source_state(data, name)
    missing = [item_id for item_id in ids if item_id not in state["items"]]
    if missing:
        raise RuntimeError(f"通知 ID 不存在：{', '.join(missing)}")
    selected = set(ids)
    before = len(state["pending"])
    state["pending"] = [item_id for item_id in state["pending"] if item_id not in selected]
    save_store(path, data)
    return {
        "ok": True,
        "acknowledged": before - len(state["pending"]),
        "pendingCount": len(state["pending"]),
    }
