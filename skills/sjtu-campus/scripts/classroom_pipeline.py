from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import subprocess
import sys

from common import emit, run

SCRIPT_DIR = Path(__file__).resolve().parent


def number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeError(f"{label} 必须是有限数值")
    return float(value)


def whole_seconds(value: float, label: str) -> int:
    if abs(value - round(value)) > 0.001:
        raise RuntimeError(f"{label} 换算后必须是整秒，当前为 {value:.3f} 秒")
    return int(round(value))


def parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} 必须是带时区的 ISO 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{label} 不是有效的 ISO 时间") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{label} 必须包含时区，例如 +08:00")
    return parsed


def offset(value: object, video_start: datetime | None, label: str) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return whole_seconds(number(value, label), label)
    if not isinstance(value, str):
        raise RuntimeError(f"{label} 须为秒数、HH:MM 或带时区的 ISO 时间")
    if video_start is None:
        raise RuntimeError(f"{label} 使用时钟时间时，必须提供 videoStart")
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if match:
        hour, minute = map(int, match.groups())
        if hour > 23 or minute > 59:
            raise RuntimeError(f"{label} 不是有效的 HH:MM 时间")
        target = video_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        target = parse_datetime(value, label)
    seconds = (target - video_start).total_seconds()
    return whole_seconds(seconds, label)


def merge_ranges(ranges: list[dict], duration: int) -> list[dict]:
    merged: list[dict] = []
    for item in sorted(ranges, key=lambda row: row["start"]):
        start, end = item["start"], item["end"]
        if not 0 <= start < end <= duration:
            raise RuntimeError(f"授课区间 {start}:{end} 无效；须在 0 到 mediaDuration {duration} 内")
        if merged and start <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append({"start": start, "end": end})
    if not merged:
        raise RuntimeError("每节至少要配置一个有效授课区间")
    return merged


def complement(ranges: list[dict], duration: int) -> list[dict]:
    excluded = []
    cursor = 0
    for item in ranges:
        if item["start"] > cursor:
            excluded.append({"start": cursor, "end": item["start"]})
        cursor = item["end"]
    if cursor < duration:
        excluded.append({"start": cursor, "end": duration})
    return excluded


def resolve_path(value: object, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 必须是非空路径")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def chunk_ready(path: Path, video_id: int, start: int, duration: int) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    result = data.get("result")
    return bool(
        data.get("ok") and data.get("schemaVersion") == 1
        and data.get("videoId") == video_id and data.get("start") == start
        and data.get("duration") == duration
        and isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict)
        and (data.get("silent") is True
             or (isinstance(result[0].get("timestamps"), list) and result[0]["timestamps"]))
    )


def load_plan(config_path: Path) -> dict:
    config_path = config_path.expanduser().resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError("整课配置不存在") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("整课配置不是有效 JSON") from error
    if not isinstance(config, dict) or config.get("version") != 1:
        raise RuntimeError("配置须为 JSON 对象，且 version 必须为 1")
    course = config.get("course")
    raw_sessions = config.get("sessions")
    if not isinstance(course, str) or not course.strip() or not isinstance(raw_sessions, list) or not raw_sessions:
        raise RuntimeError("配置必须包含 course 和非空 sessions")
    chunk_seconds = config.get("chunkSeconds", 300)
    if isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, int) or not 60 <= chunk_seconds <= 600:
        raise RuntimeError("chunkSeconds 须为 60–600 的整数")
    base = config_path.parent
    sessions = []
    seen_videos = set()
    for index, raw in enumerate(raw_sessions):
        label = f"sessions[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"{label} 必须是对象")
        video_id = raw.get("videoId")
        if isinstance(video_id, bool) or not isinstance(video_id, int) or video_id <= 0:
            raise RuntimeError(f"{label}.videoId 必须为正整数")
        if video_id in seen_videos:
            raise RuntimeError(f"录像 {video_id} 在配置中重复")
        seen_videos.add(video_id)
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise RuntimeError(f"{label}.title 不能为空")
        duration = whole_seconds(number(raw.get("mediaDuration"), f"{label}.mediaDuration"), f"{label}.mediaDuration")
        if duration <= 0:
            raise RuntimeError(f"{label}.mediaDuration 必须大于 0")
        video_start = parse_datetime(raw["videoStart"], f"{label}.videoStart") if raw.get("videoStart") is not None else None
        teaching = raw.get("teaching")
        if isinstance(teaching, dict):
            teaching = [teaching]
        if not isinstance(teaching, list):
            raise RuntimeError(f"{label}.teaching 须为区间对象或区间数组")
        ranges = []
        for range_index, item in enumerate(teaching):
            if not isinstance(item, dict) or "start" not in item or "end" not in item:
                raise RuntimeError(f"{label}.teaching[{range_index}] 须含 start 和 end")
            ranges.append({
                "start": offset(item["start"], video_start, f"{label}.teaching[{range_index}].start"),
                "end": offset(item["end"], video_start, f"{label}.teaching[{range_index}].end"),
            })
        ranges = merge_ranges(ranges, duration)
        chunks = []
        for item in ranges:
            start = item["start"]
            while start < item["end"]:
                end = min(start + chunk_seconds, item["end"])
                output_dir = resolve_path(raw.get("outputDir"), base, f"{label}.outputDir")
                path = output_dir / f"chunk-{start:04d}.json"
                chunks.append({
                    "start": start, "duration": end - start,
                    "ready": chunk_ready(path, video_id, start, end - start),
                    "output": str(path),
                })
                start = end
        view = raw.get("view", config.get("view", 1))
        if isinstance(view, bool) or not isinstance(view, int) or view <= 0:
            raise RuntimeError(f"{label}.view 必须为正整数")
        model = raw.get("model", config.get("model", "FunAudioLLM/Fun-ASR-Nano-2512"))
        if not isinstance(model, str) or not model.strip():
            raise RuntimeError(f"{label}.model 不能为空")
        hotwords = raw.get("hotwords", config.get("hotwords", []))
        if not isinstance(hotwords, list) or any(not isinstance(word, str) or not word.strip() for word in hotwords):
            raise RuntimeError(f"{label}.hotwords 须为非空字符串数组")
        sessions.append({
            "videoId": video_id,
            "title": title,
            "videoStart": video_start.isoformat() if video_start else None,
            "mediaDuration": duration,
            "teachingRanges": ranges,
            "excludedRanges": complement(ranges, duration),
            "teachingSeconds": sum(item["end"] - item["start"] for item in ranges),
            "outputDir": str(output_dir),
            "outputPrefix": str(resolve_path(raw.get("outputPrefix"), base, f"{label}.outputPrefix")),
            "view": view,
            "model": model,
            "hotwords": hotwords,
            "chunks": chunks,
        })
    return {"course": course, "chunkSeconds": chunk_seconds, "sessions": sessions}


def summary(plan: dict) -> dict:
    sessions = []
    for session in plan["sessions"]:
        sessions.append({key: value for key, value in session.items() if key not in {"model", "hotwords"}} | {
            "completedChunks": sum(chunk["ready"] for chunk in session["chunks"]),
            "expectedChunks": len(session["chunks"]),
        })
    return {
        "ok": True,
        "course": plan["course"],
        "chunkSeconds": plan["chunkSeconds"],
        "teachingSeconds": sum(session["teachingSeconds"] for session in plan["sessions"]),
        "excludedSeconds": sum(session["mediaDuration"] - session["teachingSeconds"] for session in plan["sessions"]),
        "sessions": sessions,
    }


def child_error(result: subprocess.CompletedProcess[str]) -> str:
    for line in reversed(result.stdout.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            return data["error"]
    return "识别子进程失败；请检查该分块日志与输入配置"


def run_next(plan: dict, reextract: bool) -> dict:
    for session in plan["sessions"]:
        for chunk in session["chunks"]:
            if chunk["ready"]:
                continue
            command = [
                sys.executable, str(SCRIPT_DIR / "classroom_asr.py"),
                "--video", str(session["videoId"]), "--view", str(session["view"]),
                "--start", str(chunk["start"]), "--duration", str(chunk["duration"]),
                "--output-dir", session["outputDir"], "--model", session["model"],
            ]
            for hotword in session["hotwords"]:
                command.extend(["--hotword", hotword])
            if reextract:
                command.append("--reextract")
            log_path = Path(chunk["output"]).with_suffix(".pipeline.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired as error:
                log_path.write_text("识别超过 600 秒；已终止本次子进程，下次 run 从同一分块继续。\n", encoding="utf-8")
                if chunk_ready(Path(chunk["output"]), session["videoId"], chunk["start"], chunk["duration"]):
                    return {"ok": True, "videoId": session["videoId"], "chunk": chunk, "completedAfterTimeout": True}
                raise RuntimeError(f"录像 {session['videoId']} 的 {chunk['start']} 秒分块超过 600 秒且未产出有效 JSON；下次 run 会继续") from error
            log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode != 0:
                raise RuntimeError(child_error(result))
            if not chunk_ready(Path(chunk["output"]), session["videoId"], chunk["start"], chunk["duration"]):
                raise RuntimeError("识别子进程已结束，但没有生成与配置一致的有效分块")
            return {
                "ok": True, "done": False, "videoId": session["videoId"],
                "start": chunk["start"], "duration": chunk["duration"],
                "output": chunk["output"], "log": str(log_path),
            }
    return {"ok": True, "done": True, "message": "全部有效授课分块已完成；可以执行 finalize"}


def finalize(plan: dict) -> dict:
    outputs = []
    for session in plan["sessions"]:
        missing = [chunk["start"] for chunk in session["chunks"] if not chunk["ready"]]
        if missing:
            raise RuntimeError(f"录像 {session['videoId']} 尚有 {len(missing)} 个授课分块未完成，起点为 {missing}")
        command = [
            sys.executable, str(SCRIPT_DIR / "classroom_transcript.py"),
            "--input-dir", session["outputDir"], "--video", str(session["videoId"]),
            "--title", session["title"], "--output-prefix", session["outputPrefix"],
        ]
        for item in session["teachingRanges"]:
            command.extend(["--keep-range", f"{item['start']}:{item['end']}"])
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(child_error(result))
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("格式化脚本未返回有效 JSON") from error
        outputs.extend(payload.get("outputs", []))
    return {"ok": True, "outputs": outputs}


def main() -> None:
    parser = argparse.ArgumentParser(description="按配置过滤非授课时段，逐块续跑并生成整课文稿")
    parser.add_argument("action", choices=["plan", "run", "finalize"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--reextract", action="store_true", help="仅对本次待处理分块重新提取 WAV")
    args = parser.parse_args()
    plan = load_plan(args.config)
    if args.action == "plan":
        emit(summary(plan))
    elif args.action == "run":
        emit(run_next(plan, args.reextract))
    else:
        if args.reextract:
            raise RuntimeError("--reextract 只适用于 run")
        emit(finalize(plan))


if __name__ == "__main__":
    run(main)
