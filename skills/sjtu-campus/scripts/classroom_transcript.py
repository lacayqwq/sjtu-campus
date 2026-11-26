from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path
import re

from common import emit, run, write_json

PUNCTUATION = set("，。！？；：、,.!?;:")
TERMINAL = set("。！？!?")
ASCII_WORD = re.compile(r"[A-Za-z0-9]")


def clock(seconds: float, srt: bool = False) -> str:
    milliseconds = max(0, round(seconds * 1000))
    whole, millis = divmod(milliseconds, 1000)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def join_token(parts: list[str], token: str) -> None:
    token = token.strip()
    if not token:
        return
    if not parts:
        parts.append(token)
        return
    previous = parts[-1]
    no_space = (
        token in PUNCTUATION
        or token.startswith(("'", "’", "%", ")", "]", "}"))
        or previous.endswith(("(", "[", "{", "-", "/"))
        or token.startswith(("/", "-"))
    )
    if not no_space and ASCII_WORD.search(previous[-1]) and ASCII_WORD.search(token[0]):
        parts.append(" ")
    parts.append(token)


def token_confidence(tokens: list[dict]) -> dict:
    scores = [float(token["score"]) for token in tokens
              if token.get("token", "").strip() not in PUNCTUATION
              and isinstance(token.get("score"), (int, float))]
    if not scores:
        return {"mean": None, "lowCount": 0, "lowFraction": None}
    low_count = sum(score < 0.35 for score in scores)
    return {
        "mean": round(sum(scores) / len(scores), 4),
        "lowCount": low_count,
        "lowFraction": round(low_count / len(scores), 4),
    }


def segment_words(words: list[dict], max_duration: float, pause: float) -> list[dict]:
    segments = []
    current: list[dict] = []

    def finish() -> None:
        if not current:
            return
        parts: list[str] = []
        for token in current:
            join_token(parts, token["token"])
        confidence = token_confidence(current)
        segments.append({
            "index": len(segments) + 1,
            "start": current[0]["start"],
            "end": max(current[-1]["end"], current[0]["start"] + 0.04),
            "text": "".join(parts).strip(),
            "confidence": confidence,
            "reviewRecommended": confidence["mean"] is not None and (
                confidence["mean"] < 0.5 or (confidence["lowFraction"] or 0) > 0.45
            ),
            "tokens": current.copy(),
        })

    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            exceeds = word["end"] - current[0]["start"] > max_duration
            previous_ends = current[-1]["token"].strip() in TERMINAL
            if gap >= pause or (exceeds and (previous_ends or gap >= 0.3)):
                finish()
                current = []
        current.append(word)
        if word["token"].strip() in TERMINAL:
            finish()
            current = []
    finish()
    return segments


def parse_keep_range(value: str) -> tuple[float, float]:
    match = re.fullmatch(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("授课区间须为 START:END，例如 300:2700")
    return float(match.group(1)), float(match.group(2))


def merge_ranges(ranges: list[tuple[float, float]], media_duration: float) -> list[dict]:
    ordered = sorted(ranges)
    merged: list[list[float]] = []
    for start, end in ordered:
        if not 0 <= start < end <= media_duration:
            raise RuntimeError(f"授课区间 {start}:{end} 无效；须满足 0 <= start < end <= 媒体覆盖终点 {media_duration}")
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [{"start": start, "end": end} for start, end in merged]


def complement_ranges(ranges: list[dict], start: float, end: float) -> list[dict]:
    excluded = []
    cursor = start
    for item in ranges:
        if item["start"] > cursor + 0.001:
            excluded.append({"start": cursor, "end": item["start"]})
        cursor = max(cursor, item["end"])
    if cursor < end - 0.001:
        excluded.append({"start": cursor, "end": end})
    return excluded


def filter_words(words: list[dict], ranges: list[dict]) -> list[dict]:
    if not ranges:
        return words
    kept = []
    starts = [item["start"] for item in ranges]
    for word in words:
        midpoint = (word["start"] + word["end"]) / 2
        index = bisect.bisect_right(starts, midpoint) - 1
        if index >= 0 and midpoint < ranges[index]["end"]:
            kept.append(word)
    return kept


def teaching_gaps(chunks: list[dict], ranges: list[dict]) -> list[dict]:
    gaps = []
    for item in ranges:
        cursor = item["start"]
        for chunk in chunks:
            chunk_start = chunk["start"]
            chunk_end = chunk["start"] + chunk["duration"]
            if chunk_end <= cursor or chunk_start >= item["end"]:
                continue
            if chunk_start > cursor + 0.001:
                gaps.append({"start": cursor, "end": min(chunk_start, item["end"])})
            cursor = max(cursor, min(chunk_end, item["end"]))
            if cursor >= item["end"] - 0.001:
                break
        if cursor < item["end"] - 0.001:
            gaps.append({"start": cursor, "end": item["end"]})
    return gaps


def read_chunks(input_dir: Path, video: int | None) -> tuple[list[dict], list[dict]]:
    paths = sorted(input_dir.glob("chunk-*.json"))
    if not paths:
        raise RuntimeError("目录中没有 chunk-*.json；请先运行 classroom_asr.py")
    chunks = []
    words = []
    expected_video = video
    previous_end = -1.0
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("ok") or data.get("schemaVersion") != 1:
            raise RuntimeError(f"{path.name} 不是有效的课堂识别分块")
        match = re.fullmatch(r"chunk-(\d+)\.json", path.name)
        if not match or data.get("start") != int(match.group(1)):
            raise RuntimeError(f"{path.name} 的文件名与记录起点不一致")
        video_id = data.get("videoId")
        if not isinstance(video_id, int) or video_id <= 0:
            raise RuntimeError(f"{path.name} 缺少录像编号")
        if expected_video is None:
            expected_video = video_id
        if video_id != expected_video:
            raise RuntimeError("输入目录包含不同录像的分块，已停止合并")
        start = data.get("start")
        duration = data.get("duration")
        result = data.get("result")
        if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)) or duration <= 0:
            raise RuntimeError(f"{path.name} 的时间范围无效")
        if start < previous_end - 0.001:
            raise RuntimeError("分块时间范围重叠，已停止合并")
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise RuntimeError(f"{path.name} 缺少识别结果")
        timestamps = result[0].get("timestamps")
        if not isinstance(timestamps, list) or not timestamps:
            if data.get("silent") is True:
                chunks.append({
                    "file": path.name,
                    "videoId": video_id,
                    "start": start,
                    "duration": duration,
                    "wordCount": 0,
                    "model": data.get("model"),
                    "funasrVersion": data.get("funasrVersion"),
                    "hotwords": data.get("hotwords", []),
                })
                previous_end = start + duration
                continue
            raise RuntimeError(f"{path.name} 没有逐词时间戳，不能用此脚本猜测时间轴")
        chunk_words = []
        last_end = 0.0
        for index, token in enumerate(timestamps):
            if not isinstance(token, dict) or not isinstance(token.get("token"), str):
                raise RuntimeError(f"{path.name} 的第 {index + 1} 个词格式无效")
            begin, end = token.get("start_time"), token.get("end_time")
            if not isinstance(begin, (int, float)) or not isinstance(end, (int, float)) or begin < 0 or end < begin:
                raise RuntimeError(f"{path.name} 的逐词时间戳无效")
            if begin < last_end - 0.051:
                raise RuntimeError(f"{path.name} 的时间戳出现回退；请检查模型输出")
            if end > duration + 1.0:
                raise RuntimeError(f"{path.name} 的词时间超出分块范围")
            last_end = max(last_end, end)
            item = {
                "token": token["token"],
                "start": round(start + begin, 3),
                "end": round(start + end, 3),
                "score": token.get("score") if isinstance(token.get("score"), (int, float)) else None,
                "chunkStart": start,
            }
            chunk_words.append(item)
            words.append(item)
        chunks.append({
            "file": path.name,
            "videoId": video_id,
            "start": start,
            "duration": duration,
            "wordCount": len(chunk_words),
            "model": data.get("model"),
            "funasrVersion": data.get("funasrVersion"),
            "hotwords": data.get("hotwords", []),
        })
        previous_end = start + duration
    return chunks, words


def main() -> None:
    parser = argparse.ArgumentParser(description="合并课堂识别分块并生成带时间戳的 TXT、SRT 和 JSON")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--video", type=int, help="可选：核对录像编号")
    parser.add_argument("--title", default="课堂原始识别稿")
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--max-duration", type=float, default=28.0)
    parser.add_argument("--pause", type=float, default=1.8)
    parser.add_argument("--keep-range", action="append", default=[], type=parse_keep_range,
                        metavar="START:END", help="只保留授课区间，按词时间中点过滤；可重复指定")
    args = parser.parse_args()
    if not 5 <= args.max_duration <= 60 or not 0.5 <= args.pause <= 5:
        raise RuntimeError("分组参数无效：最长 5–60 秒，静音间隔 0.5–5 秒")
    if args.video is not None and args.video <= 0:
        raise RuntimeError("录像编号必须为正整数")
    chunks, source_words = read_chunks(args.input_dir, args.video)
    coverage_start = chunks[0]["start"]
    coverage_end = chunks[-1]["start"] + chunks[-1]["duration"]
    ranges = merge_ranges(args.keep_range, coverage_end) if args.keep_range else []
    words = filter_words(source_words, ranges)
    segments = segment_words(words, args.max_duration, args.pause)
    if not segments:
        raise RuntimeError("授课区间内没有可输出的识别文本；未生成空白文稿")
    gaps = teaching_gaps(chunks, ranges) if ranges else []
    if not ranges:
        expected = coverage_start
        for chunk in chunks:
            if chunk["start"] != expected:
                gaps.append({"start": expected, "end": chunk["start"]})
            expected = chunk["start"] + chunk["duration"]
    teaching_seconds = round(sum(item["end"] - item["start"] for item in ranges), 3) if ranges else None
    excluded = complement_ranges(ranges, coverage_start, coverage_end) if ranges else []
    kept_counts = {chunk["start"]: 0 for chunk in chunks}
    for word in words:
        kept_counts[word["chunkStart"]] += 1
    for chunk in chunks:
        chunk["sourceWordCount"] = chunk["wordCount"]
        chunk["wordCount"] = kept_counts[chunk["start"]]
    metadata = {
        "title": args.title,
        "videoId": chunks[0]["videoId"],
        "coverage": {"start": coverage_start, "end": coverage_end, "gaps": gaps},
        "teachingRanges": ranges,
        "excludedRanges": excluded,
        "teachingSeconds": teaching_seconds,
        "filter": "按逐词时间中点保留授课区间；SRT 和 TXT 保留原录像时间轴" if ranges else "未过滤",
        "model": chunks[0]["model"],
        "funasrVersion": chunks[0]["funasrVersion"],
        "chunkCount": len(chunks),
        "wordCount": len(words),
        "sourceWordCount": len(source_words),
        "note": "自动语音识别原始稿，不代表逐字准确；低置信片段需结合录音复核。",
    }
    header = (
        f"{args.title}\n"
        f"录像编号：{metadata['videoId']}\n"
        f"媒体分块覆盖范围：{clock(coverage_start)}–{clock(coverage_end)}\n"
    )
    if ranges:
        header += "有效授课区间：" + "、".join(
            f"{clock(item['start'])}–{clock(item['end'])}" for item in ranges
        ) + f"（共 {teaching_seconds:g} 秒）\n"
        header += "排除区间：" + ("、".join(
            f"{clock(item['start'])}–{clock(item['end'])}" for item in excluded
        ) if excluded else "无") + "\n"
        header += "过滤方式：按逐词时间中点保留；时间戳仍对应原录像。\n"
    header += (
        f"识别模型：{metadata['model']}（FunASR {metadata['funasrVersion']}）\n"
        "说明：这是自动语音识别原始稿，保留中英文及识别口误；时间戳供回听定位，不代表逐字准确。\n"
    )
    if gaps:
        header += f"警告：有效范围内存在 {len(gaps)} 个未转写区间，详见 JSON 的 coverage.gaps。\n"
    header += "\n"
    text = header + "\n".join(
        f"[{clock(segment['start'])} --> {clock(segment['end'])}] {segment['text']}"
        for segment in segments
    ) + "\n"
    srt = "\n\n".join(
        f"{segment['index']}\n{clock(segment['start'], True)} --> {clock(segment['end'], True)}\n{segment['text']}"
        for segment in segments
    ) + "\n"
    prefix = args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    outputs = [prefix.with_suffix(".txt"), prefix.with_suffix(".srt"), prefix.with_suffix(".json")]
    if any(path.exists() for path in outputs):
        raise RuntimeError("输出 TXT、SRT 或 JSON 已存在，不会覆盖；请更换 --output-prefix")
    temporary_txt = prefix.with_suffix(".txt.tmp")
    temporary_srt = prefix.with_suffix(".srt.tmp")
    temporary_txt.write_text(text, encoding="utf-8")
    temporary_srt.write_text(srt, encoding="utf-8")
    temporary_txt.replace(outputs[0])
    temporary_srt.replace(outputs[1])
    write_json(outputs[2], {**metadata, "chunks": chunks, "segments": segments})
    emit({
        "ok": True,
        "videoId": metadata["videoId"],
        "chunks": len(chunks),
        "words": len(words),
        "sourceWords": len(source_words),
        "segments": len(segments),
        "teachingSeconds": teaching_seconds,
        "excludedRanges": excluded,
        "gaps": gaps,
        "reviewRecommended": sum(segment["reviewRecommended"] for segment in segments),
        "outputs": [str(path) for path in outputs],
    })


if __name__ == "__main__":
    run(main)
