from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
from utils import logger
import difflib
import json
import os
import re
import threading
import time
import httpx
from datetime import datetime
from openai import OpenAI

_CACHE_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "keju_ai_cache.json"))
_CACHE_LOCK = threading.Lock()
_PUNCT_RE = re.compile(r"[\s\t\r\n]|[，,、。！？?!：:；;“”\"'（）()【】\[\]\-·—…]")
_ANSWER_BOXES = {"A": [509,306,269,91], "B": [825,304,270,95], "C": [506,408,268,88], "D": [831,404,265,96]}


def _norm(s):
    return _PUNCT_RE.sub("", (s or "").lower())


def _cache_load():
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _cache_save(cache):
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CACHE_PATH)


def _find_letter(answer_text, options):
    na = _norm(answer_text)
    if not na:
        return None
    for letter, text in options.items():
        if text and _norm(text) == na:
            return letter
    return None


def _cache_lookup(question, options):
    nq = _norm(question)
    if not nq:
        return None
    with _CACHE_LOCK:
        cache = _cache_load()
    letter = _find_letter((cache.get(nq) or {}).get("answer", ""), options)
    if letter:
        return letter
    best_q, best_ratio = None, 0.0
    for q in cache:
        r = difflib.SequenceMatcher(None, nq, q).ratio()
        if r > best_ratio:
            best_ratio, best_q = r, q
    if best_q and best_ratio * 100 >= 80:
        return _find_letter(cache[best_q].get("answer", ""), options)
    return None


def _cache_store(question, answer_text):
    nq = _norm(question)
    if not nq or not answer_text:
        return
    with _CACHE_LOCK:
        cache = _cache_load()
        cache[nq] = {"answer": answer_text, "question": question, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _cache_save(cache)


def _cached_or_ask(question, options, ask):
    letter = _cache_lookup(question, options)
    if letter:
        return letter
    ai_letter = ask()
    up = (ai_letter or "").strip().upper()
    if up in options and options[up]:
        _cache_store(question, options[up])
    return ai_letter


def _sort_ocr_by_position(ocr_results):
    rows = {}
    for result in ocr_results:
        y = result.box[1]
        for row_y in rows:
            if abs(y - row_y) < 20:
                rows[row_y].append(result)
                break
        else:
            rows[y] = [result]
    for row_y in rows:
        rows[row_y].sort(key=lambda r: r.box[0])
    out = []
    for row_y in sorted(rows):
        out.extend(rows[row_y])
    return out


def _ocr_question(context):
    image1 = context.tasker.controller.post_screencap().wait().get()
    reco = context.run_recognition("科举乡试题目", image1,
        pipeline_override={"科举乡试题目": {"roi": [511,186,602,107], "expected": [""], "recognition": "OCR"}})
    if not reco or not reco.hit:
        return image1, None
    return image1, "".join(t.text for t in _sort_ocr_by_position(reco.all_results) if t.text)


def _ocr_option(context, image, node, roi):
    r = context.run_recognition(node, image, pipeline_override={node: {"roi": roi, "expected": [""], "recognition": "OCR"}})
    return r.all_results[-1].text if (r and r.all_results) else ""


def _read_options(context, image):
    return {
        "A": _ocr_option(context, image, "科举乡试答案a", [509,306,269,91]),
        "B": _ocr_option(context, image, "科举乡试答案b", [825,304,270,95]),
        "C": _ocr_option(context, image, "科举乡试答案c", [506,408,268,88]),
        "D": _ocr_option(context, image, "科举乡试答案d", [831,404,265,96]),
    }


def _click_answer(context, list_answer, question, answer):
    up = (list_answer or "").strip().upper()
    if up not in _ANSWER_BOXES:
        logger.info(f"ai返回值有问题：{list_answer}，默认选择第a答案")
        up = "A"
    box = _ANSWER_BOXES[up]
    nc = context.clone()
    nc.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
    logger.info(f"AI返回答案：{list_answer}。识别题目：{question}，识别答案列表：{answer}")
    time.sleep(2)


def _ask_with_openai(base_url, api_key, model, question, options):
    valid = {k: v for k, v in options.items() if v}
    if not valid:
        return "错误：没有有效的选项"
    prompt = f"问题：{question}\n请从以下选项中选择一个最正确的答案，并只返回选项的字母（例如：A, B, C, D）。\n"
    for k, v in valid.items():
        prompt += f"{k}: {v}\n"
    client = OpenAI(base_url=base_url, api_key=api_key or "ollama",
                   http_client=httpx.Client(trust_env=False, timeout=120), max_retries=0)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": "You are a helpful assistant."},
                      {"role": "user", "content": prompt}],
            max_tokens=50, stream=False, extra_body={"thinking": {"type": "disabled"}},
        )
        content = (resp.choices[0].message.content or "").strip().upper()
        for ch in content:
            if ch in valid:
                return ch
        return f"AI回复无效: {content}"
    except Exception as e:
        return f"请求错误: {e}"


@AgentServer.custom_recognition("AIAnswer")
class AIAnswer(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> CustomRecognition.AnalyzeResult:
        image1, question = _ocr_question(context)
        if question is None:
            return CustomRecognition.AnalyzeResult(box=(0,0,0,0), detail="答题结束")
        answer = _read_options(context, image1)
        attach = context.get_node_data("活动-科举乡试-开始答题API")["attach"]
        listAnswer = _cached_or_ask(question, answer,
            lambda: _ask_with_openai(attach["url"], attach["apikey"], attach["model"], question, answer))
        _click_answer(context, listAnswer, question, answer)
        return CustomRecognition.AnalyzeResult(box=(0,0,0,0), detail="ai答题完成")


@AgentServer.custom_recognition("zhipu")
class zhipu(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> CustomRecognition.AnalyzeResult:
        image1, question = _ocr_question(context)
        if question is None:
            return CustomRecognition.AnalyzeResult(box=(0,0,0,0), detail="答题结束")
        answer = _read_options(context, image1)
        uipikey = context.get_node_data("活动-科举乡试-开始答题agent-智谱")["attach"]["apikey"]
        listAnswer = _cached_or_ask(question, answer, lambda: _ask_with_openai(
            "https://open.bigmodel.cn/api/paas/v4", uipikey, "GLM-4-Flash-250414", question, answer))
        _click_answer(context, listAnswer, question, answer)
        return CustomRecognition.AnalyzeResult(box=(0,0,0,0), detail="ai答题完成")
