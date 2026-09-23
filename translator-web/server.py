#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""APP本地化翻译工作台 · 本地服务

复用 app-localization-translator 技能的提示词与 API 调用，把命令行流水线变成可交互网页：
支持上传表格或手动输入、多目标语言、A/B 模型可选，进度通过 SSE 实时回传，完成后下载 xlsx。
"""

import os
import sys
import json
import time
import uuid
import hashlib
import subprocess
import requests
import pandas as pd
from flask import Flask, request, Response, send_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)


def _load_env():
    """从项目根目录的 .env 读配置。必须先于 import translate 执行，
    否则技能脚本在导入时读不到 COMMANDCODE_API_KEY。"""
    f = os.path.join(PROJECT_ROOT, ".env")
    if os.path.isfile(f):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

SKILL_SCRIPTS = os.path.join(PROJECT_ROOT, "app-localization-translator", "scripts")
sys.path.insert(0, SKILL_SCRIPTS)
import translate as engine  # noqa: E402  提示词、模型、API 调用、表格解析均复用技能脚本

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
TTS_DIR = os.path.join(OUTPUT_DIR, "tts")
HISTORY_DIR = os.path.join(OUTPUT_DIR, "history")
for d in (UPLOAD_DIR, OUTPUT_DIR, TTS_DIR, HISTORY_DIR):
    os.makedirs(d, exist_ok=True)

# 配音主通道 = TTSMaker 官方开发者 API（api.ttsmaker.cn/v1），可直接指定 voice_id（1480 = Alayna 美式英语女声）
# token 优先读环境变量；默认用官方文档公布的测试 token（每周 5 万字符），正式使用建议申请专属 token
TTSMAKER_TOKEN = os.environ.get("TTSMAKER_TOKEN", "")  # 留空则配音不可用，翻译流程不受影响
TTSMAKER_API = "https://api.ttsmaker.cn/v1"

# 备选通道 = 微软 Edge 神经语音（免费无密钥），用于 TTSMaker 不支持的语种（阿拉伯语、印地语等）
EDGE_TTS = os.path.join(os.path.dirname(sys.executable), "edge-tts") or "edge-tts"

# 精选音色（工作台只用这两个，皆已验证可走官方 API 合成）
FEATURED_VOICES = [
    {"value": "ttsmaker:1480", "label": "1480 - Alayna（美式英语 Female）", "lang": "英语"},
    {"value": "ttsmaker:30001", "label": "30001 - Katarzyna（波兰语 Female）", "lang": "波兰语"},
]
# 各语言默认音色
DEFAULT_VOICE = {"英语": "ttsmaker:1480", "波兰语": "ttsmaker:30001"}

# 目标语言 → 默认音色
VOICE_MAP = {
    "英语": "en-US-AvaNeural", "日语": "ja-JP-NanamiNeural", "韩语": "ko-KR-SunHiNeural",
    "繁体中文": "zh-TW-HsiaoChenNeural", "中文": "zh-CN-XiaoxiaoNeural",
    "德语": "de-DE-KatjaNeural", "法语": "fr-FR-DeniseNeural", "西班牙语": "es-ES-ElviraNeural",
    "葡萄牙语": "pt-BR-FranciscaNeural", "俄语": "ru-RU-SvetlanaNeural",
    "阿拉伯语": "ar-SA-ZariyahNeural", "印地语": "hi-IN-SwaraNeural",
    "印尼语": "id-ID-GadisNeural", "马来语": "ms-MY-YasminNeural", "泰语": "th-TH-PremwadeeNeural",
    "越南语": "vi-VN-HoaiMyNeural",
}
# 英语可选女声（用于试听对比与替换默认）
EN_VOICES = [
    {"id": "en-US-AvaNeural", "label": "Ava 女声"},
    {"id": "en-US-EmmaNeural", "label": "Emma 女声"},
    {"id": "en-US-AriaNeural", "label": "Aria 女声"},
    {"id": "en-US-JennyNeural", "label": "Jenny 女声"},
    {"id": "en-US-MichelleNeural", "label": "Michelle 女声"},
    {"id": "en-US-AnaNeural", "label": "Ana 女声（童声）"},
    {"id": "en-US-AvaMultilingualNeural", "label": "Ava 多语言女声"},
    {"id": "en-US-EmmaMultilingualNeural", "label": "Emma 多语言女声"},
]

# 全站免费档实测只有两个 id：ling-3.0-flash-sante:free 与 laguna-s-2.1-free；
# LongCat 2.0 的 :free 档已退役（403），laguna 上游频繁 429（实测 0/5）。
DEFAULT_A = engine.MODEL_A
DEFAULT_B = "meituan/LongCat-2.0"

MODELS = [
    {"id": DEFAULT_A, "label": "Ling 3.0 Flash Sante", "tier": "免费", "note": "翻译稳"},
    {"id": "poolside/laguna-s-2.1-free", "label": "Laguna S 2.1", "tier": "免费", "note": "上游常限流"},
    {"id": DEFAULT_B, "label": "LongCat 2.0", "tier": "付费", "note": "质检首选"},
    {"id": "Qwen/Qwen3.7-Flash", "label": "Qwen3.7 Flash", "tier": "极低", "note": "$0.03/M"},
]

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def parse_manual(text):
    """每行一条，支持 `key|中文`，只写中文时 key 自动生成"""
    items = []
    for idx, line in enumerate(text.strip().split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            key, zh = line.split("|", 1)
            items.append({"key": key.strip() or f"key_{idx}", "zh": zh.strip()})
        else:
            items.append({"key": f"key_{idx}", "zh": line})
    return items


def read_items(path):
    df = pd.read_excel(path) if path.endswith(".xlsx") else pd.read_csv(path, encoding="utf-8-sig")
    key_col = "key" if "key" in df.columns else df.columns[0]
    zh_col = "zh" if "zh" in df.columns else df.columns[1]
    return [{"key": str(r[key_col]), "zh": str(r[zh_col])} for _, r in df.iterrows()]


def brief(exc):
    """把接口异常压成一句人话"""
    m = str(exc)
    if "429" in m:
        return "429 上游限流/不可用"
    if "403" in m:
        return "403 无权限（该模型档位可能已下线）"
    return m[:80]


def timed_call(model, system, user, tries=3):
    """计时调用；免费档偶发 429（上游临时不可用），退避后重试"""
    t0 = time.time()
    for i in range(tries):
        try:
            return engine.call_api(model, system, user), round(time.time() - t0, 1)
        except Exception as exc:
            msg = str(exc)
            retryable = "429" in msg or "temporarily unavailable" in msg
            if i == tries - 1 or not retryable:
                raise
            time.sleep(3)


def clean_map(mapping, items):
    """丢弃模型输出里的分隔行、表头等无效条目，只保留与输入 key 对得上的译文"""
    valid = {it["key"] for it in items}
    return {k: v for k, v in mapping.items() if k in valid and v}


def build_table(items, translated=None, header="译文"):
    line_head = "Key|中文内容" if translated is None else f"Key|中文内容|{header}"
    lines = [line_head]
    for it in items:
        if translated is None:
            lines.append(f"{it['key']}|{it['zh']}")
        else:
            lines.append(f"{it['key']}|{it['zh']}|{translated.get(it['key'], '')}")
    return "\n".join(lines)


def run_language(items, lang, model_a, model_b, max_iter):
    """单个目标语言的流水线：A 翻译 → B 质检循环 → 定稿"""
    yield ("node", {"id": 2, "state": "running", "msg": f"发送 {len(items)} 条原文", "model": model_a})
    try:
        result, sec = timed_call(model_a, engine.TRANSLATION_PROMPT, f"目标语言：{lang}\n\n{build_table(items)}")
    except Exception as exc:
        yield ("node", {"id": 2, "state": "fail", "model": model_a, "msg": f"调用失败：{brief(exc)}"})
        yield ("node", {"id": 3, "state": "fail", "msg": "未执行（无初译结果）"})
        yield ("node", {"id": 4, "state": "fail", "msg": "初译失败，跳过该语言"})
        yield ("final", {"map": {}})
        return
    draft = clean_map(engine.parse_table(result), items)
    yield ("node", {"id": 2, "state": "done" if draft else "fail", "model": model_a, "elapsed": sec,
                    "count": len(draft), "msg": f"返回 {len(draft)} 条译文" if draft else "未解析到译文"})
    yield ("draft", {"map": draft, "count": len(draft), "elapsed": sec})
    if not draft:
        yield ("node", {"id": 4, "state": "fail", "msg": "初译结果为空，跳过该语言"})
        yield ("final", {"map": {}})
        return

    current, passed = draft, False
    for i in range(1, max_iter + 1):
        yield ("node", {"id": 3, "state": "running", "round": i, "model": model_b,
                        "msg": f"复核 {len(current)} 条译文"})
        try:
            result, sec = timed_call(
                model_b, engine.QA_PROMPT, f"请对以下翻译结果进行质检：\n\n{build_table(items, current, f'{lang}译文')}"
            )
        except Exception as exc:
            # 质检模型不可用时保留当前译文继续，不让单个语言拖垮整批任务
            yield ("node", {"id": 3, "state": "warn", "round": i, "model": model_b,
                            "msg": f"质检失败：{brief(exc)}"})
            yield ("node", {"id": 4, "state": "warn", "passed": False, "msg": "质检中断，保留现有译文"})
            yield ("final", {"map": current})
            return
        qa_map = clean_map(engine.parse_table(result), items)
        if not qa_map:
            yield ("node", {"id": 3, "state": "warn", "round": i, "model": model_b, "elapsed": sec,
                            "msg": "正文为空，保留上一版"})
            break
        passed = any(k in result for k in ("全文翻译合规", "全部通过", "无需修改"))
        current = qa_map
        yield ("qa", {"round": i, "map": qa_map, "passed": passed, "elapsed": sec})
        yield ("node", {"id": 3, "state": "done", "round": i, "passed": passed, "model": model_b,
                        "elapsed": sec, "count": len(qa_map),
                        "msg": "判定全部合规" if passed else "有修正，继续下一轮"})
        if passed:
            break
    yield ("node", {"id": 4, "state": "done", "passed": passed,
                    "msg": "质检通过" if passed else f"达最大轮次 {max_iter}，采用最后一版"})
    yield ("final", {"map": current})


@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/models")
def models():
    return {"models": MODELS, "default_a": DEFAULT_A, "default_b": DEFAULT_B}


@app.route("/api/preview", methods=["POST"])
def preview():
    manual = request.form.get("manual", "").strip()
    if manual:
        return {"items": parse_manual(manual), "source": "手动输入"}
    file = request.files.get("file")
    if not file:
        return {"error": "未选择文件，也没有手动输入内容"}, 400
    path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{file.filename}")
    file.save(path)
    try:
        return {"items": read_items(path), "source": file.filename}
    except Exception as exc:
        return {"error": f"文件解析失败：{exc}"}, 400


@app.route("/api/download/<name>")
def download(name):
    return send_file(os.path.join(OUTPUT_DIR, name), as_attachment=True)


def save_history(record, keep=100):
    """落盘一条翻译记录，并只保留最近 keep 条"""
    with open(os.path.join(HISTORY_DIR, f"{record['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    files = sorted(f for f in os.listdir(HISTORY_DIR) if f.endswith(".json"))
    for old in files[:-keep]:
        os.remove(os.path.join(HISTORY_DIR, old))


@app.route("/api/history")
def history_list():
    out = []
    files = sorted((f for f in os.listdir(HISTORY_DIR) if f.endswith(".json")), reverse=True)
    for fn in files[:60]:
        try:
            with open(os.path.join(HISTORY_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            out.append({"id": d["id"], "time": d["time"], "langs": d.get("langs", []),
                        "count": len(d.get("items", [])), "source": d.get("source", ""),
                        "file": d.get("file", ""), "models": d.get("models", "")})
        except Exception:
            continue
    return {"history": out}


@app.route("/api/history/<hid>")
def history_detail(hid):
    path = os.path.join(HISTORY_DIR, f"{os.path.basename(hid)}.json")
    if not os.path.exists(path):
        return {"error": "记录不存在"}, 404
    return send_file(path, mimetype="application/json")


@app.route("/api/voices")
def voices():
    """返回精选音色（两个），按语言给出默认值"""
    lang = request.args.get("language") or "英语"
    return {"voices": FEATURED_VOICES, "default": DEFAULT_VOICE.get(lang, FEATURED_VOICES[0]["value"])}


@app.route("/api/tts-limit")
def tts_limit():
    """TTSMaker token 剩余额度，便于前端提示"""
    try:
        r = requests.get(f"{TTSMAKER_API}/get-token-status", params={"token": TTSMAKER_TOKEN}, timeout=20)
        return r.json()
    except Exception as exc:
        return {"error": brief(exc)}


def synth_ttsmaker(text, voice_id, path):
    """官方 API 下单 → 拿临时 URL → 落盘（URL 仅 2 小时有效，必须转存）"""
    r = requests.post(f"{TTSMAKER_API}/create-tts-order", timeout=120, json={
        "token": TTSMAKER_TOKEN, "text": text, "voice_id": int(voice_id),
        "audio_format": "mp3", "audio_speed": 1.0, "audio_volume": 0,
        "text_paragraph_pause_time": 0})
    d = r.json()
    if d.get("status") != "success" or not d.get("audio_file_url"):
        raise RuntimeError(f"{d.get('error_code')} {d.get('error_details')}")
    audio = requests.get(d["audio_file_url"], timeout=120)
    with open(path, "wb") as f:
        f.write(audio.content)


@app.route("/api/tts", methods=["POST"])
def tts():
    """按需合成单条译文的配音；文件名按 来源+音色+文本 哈希缓存"""
    p = request.get_json(force=True)
    text = (p.get("text") or "").strip()
    spec = p.get("voice") or "ttsmaker:1480"
    if not text:
        return {"error": "文本为空"}, 400
    provider, _, voice = spec.partition(":")
    name = "tts_" + hashlib.md5(f"{spec}|{text}".encode()).hexdigest()[:14] + ".mp3"
    path = os.path.join(TTS_DIR, name)
    if not os.path.exists(path):
        try:
            if provider == "ttsmaker":
                if not TTSMAKER_TOKEN:
                    return {"error": "未配置 TTSMAKER_TOKEN，请在项目根目录 .env 中填写后再试"}, 400
                synth_ttsmaker(text, voice, path)
            else:
                subprocess.run([EDGE_TTS, "--voice", voice or "en-US-AvaNeural", "--text", text,
                                "--write-media", path], check=True, timeout=90, capture_output=True)
        except Exception as exc:
            return {"error": f"配音失败：{brief(exc)}"}, 500
    return send_file(path, mimetype="audio/mpeg")


@app.route("/api/run", methods=["POST"])
def run():
    p = request.get_json(force=True)
    items, langs = p["items"], p["langs"]
    model_a = p.get("model_a") or DEFAULT_A
    model_b = p.get("model_b") or DEFAULT_B
    max_iter = int(p.get("max_iterations", 3))

    def stream():
        try:
            yield sse("node", {"id": 1, "state": "done", "msg": f"{len(items)} 条 · {len(langs)} 个语言"})
            finals, drafts = {}, {}
            for lang in langs:
                yield sse("lang", {"lang": lang, "state": "running"})
                final = {}
                for event, data in run_language(items, lang, model_a, model_b, max_iter):
                    if event == "final":
                        final = data["map"]
                        continue
                    if event == "draft":
                        drafts[lang] = data["map"]
                    yield sse(event, data)
                finals[lang] = final
                yield sse("lang", {"lang": lang, "state": "done"})
            yield sse("result", {"finals": finals})

            rows = []
            for it in items:
                row = {"key": it["key"], "zh": it["zh"]}
                for lang in langs:
                    row[lang] = finals.get(lang, {}).get(it["key"], "")
                rows.append(row)
            name = f"翻译定稿_{time.strftime('%m%d_%H%M%S')}.xlsx"
            pd.DataFrame(rows).to_excel(os.path.join(OUTPUT_DIR, name), index=False, engine="openpyxl")
            yield sse("node", {"id": 5, "state": "done", "msg": name})

            save_history({
                "id": time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4],
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": p.get("source", ""), "items": items, "langs": langs,
                "finals": finals, "drafts": drafts, "file": name,
                "models": f"{model_a} → {model_b}",
            })
            yield sse("done", {"file": name, "count": len(rows)})
        except Exception as exc:
            yield sse("error", {"msg": str(exc)})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8770, threaded=True, debug=False)
