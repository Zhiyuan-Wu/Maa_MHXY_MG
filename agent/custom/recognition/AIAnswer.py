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


# 模糊匹配阈值。**已禁用（=1.01，difflib ratio 永远 < 1.01 故下方模糊段不触发）**。
# 原 0.85 的实证假设"异题两两最高 0.80"不全：梦幻西游题库有大量结构相似但含义不同的题，
# 异题相似度实测 0.86~0.96（"封印命中"vs"抵抗封印"0.86、"帮主"vs"副帮主"0.96、
# "物理防御"vs"法术防御"0.86、"花果山技能"vs"普陀山技能"0.88），与同题 1 字 OCR 错位
# （p5≈0.857）重叠——任何阈值都无法既挡异题又留同题容错。08.07 实战 7 条模糊命中 5 条
# 错配点错答案（详见 SKILL 5r-log-analysis §5）。故禁用，miss 全调 AI（deepseek 关思考
# ~1.5s/题、5 开并行每号 +~10s，AI 质量高：31 题仅 1 错）。模糊段代码（best_q 循环）保留
# 备查，要恢复改回 0.85。
_FUZZY_THRESHOLD = 1.01


def _cache_lookup(question, options):
    nq = _norm(question)
    if not nq:
        return None
    with _CACHE_LOCK:
        cache = _cache_load()
    letter = _find_letter((cache.get(nq) or {}).get("answer", ""), options)
    if letter:
        logger.info(f"缓存精确命中：《{question}》→ {letter}")
        return letter
    best_q, best_ratio = None, 0.0
    for q in cache:
        r = difflib.SequenceMatcher(None, nq, q).ratio()
        if r > best_ratio:
            best_ratio, best_q = r, q
    if best_q and best_ratio >= _FUZZY_THRESHOLD:
        matched = cache[best_q]
        logger.info(f"缓存模糊命中({best_ratio:.2f})：《{question}》≈《{matched.get('question','')}》→ {matched.get('answer','')}")
        return _find_letter(matched.get("answer", ""), options)
    return None


def _cache_store(question, answer_text, options=None):
    nq = _norm(question)
    if not nq or not answer_text:
        return
    with _CACHE_LOCK:
        cache = _cache_load()
        cache[nq] = {
            "answer": answer_text,
            "options": options or {},   # 识别到的 A/B/C/D 候选项文本（便于人工核对 / 排查 OCR）
            "question": question,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _cache_save(cache)


def _cached_or_ask(question, options, ask):
    letter = _cache_lookup(question, options)
    if letter:
        return letter
    logger.info(f"缓存未命中，调用AI：《{question}》")
    ai_letter = ask()
    up = (ai_letter or "").strip().upper()
    if up in options and options[up]:
        _cache_store(question, options[up], options)
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


# Few-shot 样本：覆盖梦幻西游科举/元宵灯谜的常见题型（字谜拆字、成语双关/典故、物品谜、
# 数学借瓶法）。每条答案均经灯谜题库核实，用来教模型"先按谜面推理、再只回字母"。
# 这些题多半已在缓存里（命中即返回），样本主要让模型遇到**新题**时套用同一思路。
_FEW_SHOT = [
    {"q": "亚，猜一成语", "opts": "A:死心塌地 B:有口难言 C:自命不凡 D:有声有色",
     "hint": "“亚”字加“口”旁就是“哑”——有口却说不出", "a": "B"},
    {"q": "十月十日，猜一字", "opts": "A:丰 B:廿 C:明 D:朝",
     "hint": "“朝”字拆开正好是 十、月、十、日 四个部件", "a": "D"},
    {"q": "武大郎设宴，猜一成语", "opts": "A:推杯换盏 B:高朋满座 C:觥筹交错 D:宴无好宴",
     "hint": "武大郎个子矮，所以他请的客人都比他“高”", "a": "B"},
    {"q": "悟空思做遮体裙，猜一成语", "opts": "A:与虎谋皮 B:以德服人 C:三人成虎 D:笑逐颜开",
     "hint": "悟空的招牌服饰是虎皮裙，做裙要先谋虎皮", "a": "A"},
    {"q": "有风不动无风动，不动无风动有风（猜一物）", "opts": "A:扇子 B:灯泡 C:窗户 D:风铃",
     "hint": "有天然风时不用动它，没风时要手动扇出风", "a": "A"},
    {"q": "3个空瓶换1瓶啤酒，18个空瓶最多免费喝几瓶", "opts": "A:6 B:9 C:8 D:7",
     "hint": "借瓶法：换回来的酒喝完仍是空瓶，18÷(3-1)=9", "a": "B"},
]


def _ask_with_openai(base_url, api_key, model, question, options):
    valid = {k: v for k, v in options.items() if v}
    if not valid:
        return "错误：没有有效的选项"
    prompt = f"问题：{question}\n请从以下选项中选择一个最正确的答案，并只返回选项的字母（例如：A, B, C, D）。\n"
    for k, v in valid.items():
        prompt += f"{k}: {v}\n"
    messages = [{"role": "system", "content":
        "你是猜谜高手（字谜、成语谜、灯谜、脑筋急转弯、常识题）。"
        "请先按谜面推理（字形拆字 / 双关谐音 / 典故 / 常识 / 借瓶法等），"
        "再只回答一个选项字母（A/B/C/D），不要输出多余文字。"}]
    for ex in _FEW_SHOT:
        messages.append({"role": "user",
            "content": f"问题：{ex['q']}\n{ex['opts']}\n提示：{ex['hint']}\n只返回字母。"})
        messages.append({"role": "assistant", "content": ex["a"]})
    messages.append({"role": "user", "content": prompt})
    client = OpenAI(base_url=base_url, api_key=api_key or "ollama",
                   http_client=httpx.Client(trust_env=False, timeout=120), max_retries=0)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
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
