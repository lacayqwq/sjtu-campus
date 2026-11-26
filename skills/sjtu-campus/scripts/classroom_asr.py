from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import wave

from common import emit, run, write_json

DEFAULT_HOTWORDS = [
    "Canvas",
    "统计应用",
    "population",
    "sample",
    "sampling",
    "sample size",
    "probability",
    "random variable",
    "p value",
    "parameter",
    "statistic",
    "estimator",
    "estimate",
    "standard error",
    "left cheek",
    "right cheek",
    "Bernoulli",
    "binomial",
    "Poisson",
    "Gaussian",
    "quantile",
    "law of large numbers",
    "central limit theorem",
    "Slutsky",
    "Delta Method",
    "order statistics",
    "empirical distribution function",
]


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("编号必须为正整数")
    return number


def nonnegative(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("时间必须为非负数")
    return number


def positive(value: str) -> float:
    number = float(value)
    if number <= 0 or number > 1200:
        raise argparse.ArgumentTypeError("单块时长须在 0 到 1200 秒之间")
    return number


def check_audio(path: Path, expected_duration: float) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                raise RuntimeError("音频必须是单声道、16 kHz、16 位 PCM WAV")
            duration = audio.getnframes() / audio.getframerate()
    except (wave.Error, EOFError) as error:
        raise RuntimeError("音频文件不完整，请删除该分块 WAV 后重新提取") from error
    # MP4 的音频流可能比画面时长短不到一秒，末尾分块允许这一容器差异。
    if abs(duration - expected_duration) > 0.75:
        raise RuntimeError(f"音频时长为 {duration:.3f} 秒，预期 {expected_duration:.3f} 秒；请重新提取该分块")
    return duration


def extract_audio(url: str, path: Path, start: float, duration: float, *, reextract: bool) -> float:
    if path.exists() and not reextract:
        return check_audio(path, duration)
    import imageio_ffmpeg

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".partial.wav")
    if temporary.exists():
        temporary.unlink()
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        url,
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "-y",
        str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("音频提取超时；请用 --reextract 重试该分块") from error
    if result.returncode != 0:
        # FFmpeg errors can repeat the signed media URL. Never copy them to output.
        raise RuntimeError("音频提取失败；请检查课堂会话或稍后使用 --reextract 重试")
    actual_duration = check_audio(temporary, duration)
    if path.exists():
        path.unlink()
    temporary.replace(path)
    return actual_duration


def load_audio_result(path: Path, video: int, start: float, duration: float) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("分块结果不存在或不是有效 JSON") from error
    if (
        not data.get("ok")
        or data.get("videoId") != video
        or data.get("start") != start
        or data.get("duration") != duration
    ):
        raise RuntimeError("已有分块结果与本次录像或时间范围不一致，不会覆盖")
    result = data.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise RuntimeError("已有分块结果缺少识别内容")
    return data


def transcribe(audio: Path, hotwords: list[str], model_name: str) -> tuple[list[dict], str]:
    from funasr import AutoModel
    import funasr

    model = AutoModel(
        model=model_name,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        device="cpu",
        trust_remote_code=True,
        disable_update=True,
    )
    result = model.generate(
        input=str(audio),
        batch_size=1,
        language="中文",
        itn=True,
        hotwords=hotwords,
        merge_vad=False,
        sentence_timestamp=True,
    )
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise RuntimeError("识别模型返回了不支持的数据格式")
    if not isinstance(result[0].get("text"), str) or not isinstance(result[0].get("timestamps"), list):
        raise RuntimeError("识别结果缺少正文或逐词时间戳")
    return result, getattr(funasr, "__version__", "unknown")


def main() -> None:
    parser = argparse.ArgumentParser(description="本地课堂录音高质量语音识别（单分块）")
    parser.add_argument("--video", type=positive_int, required=True, help="由 classroom.py videos 返回的录像编号")
    parser.add_argument("--view", type=positive_int, default=1, help="课堂画面编号，默认使用画面 1")
    parser.add_argument("--audio", type=Path, help="已提取的 WAV；省略时通过学校官方接口获取音频")
    parser.add_argument("--start", type=nonnegative, required=True, help="相对原录像的起点，单位秒")
    parser.add_argument("--duration", type=positive, required=True, help="本块时长，建议不超过 600 秒")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="FunAudioLLM/Fun-ASR-Nano-2512")
    parser.add_argument("--hotword", action="append", default=[], help="额外课件热词，可重复指定")
    parser.add_argument("--reextract", action="store_true", help="仅当上次音频提取不完整时，重新生成脚本自己的分块 WAV")
    args = parser.parse_args()
    if args.start % 1 or args.duration % 1:
        raise RuntimeError("为便于断点续做，分块起点与时长须使用整秒")
    start = int(args.start)
    duration = int(args.duration)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"chunk-{start:04d}.json"
    if output.exists():
        previous = load_audio_result(output, args.video, start, duration)
        emit({"ok": True, "cached": True, "videoId": args.video, "start": start, "duration": duration,
              "output": str(output), "words": len(previous["result"][0]["timestamps"])})
        return

    source = "local"
    if args.audio:
        audio = args.audio.expanduser().resolve()
        if not audio.is_file():
            raise RuntimeError("指定的音频文件不存在")
        actual_duration = check_audio(audio, duration)
    else:
        from classroom import Classroom, streams

        client = Classroom()
        rows = streams(client, args.video)
        stream = next((row for row in rows if row["view"] == args.view), None)
        if not stream:
            raise RuntimeError(f"录像 {args.video} 没有画面 {args.view}；请先运行 streams 查询")
        if args.start + args.duration > stream["duration"] + 0.15:
            raise RuntimeError(f"时间范围超过画面时长 {stream['duration']:.3f} 秒")
        audio = args.output_dir / f"chunk-{start:04d}.wav"
        actual_duration = extract_audio(stream["url"], audio, args.start, args.duration, reextract=args.reextract)
        source = "classroom-api"
        del stream, rows

    hotwords = list(dict.fromkeys(DEFAULT_HOTWORDS + args.hotword))
    result, version = transcribe(audio, hotwords, args.model)
    data = {
        "ok": True,
        "schemaVersion": 1,
        "videoId": args.video,
        "view": args.view,
        "source": source,
        "start": start,
        "duration": duration,
        "actualDuration": actual_duration,
        "model": args.model,
        "funasrVersion": version,
        "language": "中文（保留英文术语）",
        "hotwords": hotwords,
        "result": result,
    }
    write_json(output, data)
    emit({"ok": True, "cached": False, "videoId": args.video, "start": start, "duration": duration,
          "actualDuration": actual_duration, "output": str(output), "words": len(result[0]["timestamps"])})


if __name__ == "__main__":
    run(main)
