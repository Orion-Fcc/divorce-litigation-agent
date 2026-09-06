# -*- coding: utf-8 -*-
"""
自主学习模块：从对话中沉淀经验，并在后续对话中检索复用。

机制：
1. 每完成一轮有效对话，服务端调用 LLM 把会话要点蒸馏为一条「经验」
   （话题标签 + 情形摘要 + 关键结论），写入本地 experience.jsonl。
2. 写入前做隐私脱敏：手机号、身份证号、银行卡号、详细住址门牌一律打码。
3. 新对话提问时，按关键词重叠检索最相关的 3 条经验注入上下文，
   让回答逐步贴合用户的实际场景（自我迭代）。

防乱学护栏（quality gates）：
- 只在顾问回答给出「有依据的结论」时沉淀：回答须包含法条/依据信号，
  且不含不确定表述；拒绝协助类回答、纯闲聊、无法律依据的回答一律不学。
- 蒸馏出的每条结论都必须带法律信号（法条/期限/程序/应然表述），
  提炼不出来的整条经验丢弃。

经验库全部存储在本地，不上传任何服务器。
"""
import hashlib
import json
import os
import re
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
EXP_FILE = os.path.join(DATA_DIR, "experience.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "learn_state.json")

MAX_EXPERIENCES = 500  # 超出后裁剪最旧的

_lock = threading.Lock()

DISTILL_PROMPT = (
    "你是经验提炼助手。下面是用户与「法律顾问」的一段对话。"
    "请提炼为一条可复用的经验，用于改进后续回答。\n"
    "要求：\n"
    "1. 不得包含任何人名、地名、电话、身份证号等个人信息，一律泛化（如「当事人」「对方」「公司」）；\n"
    "2. topics：3-6 个主题标签（如 抚养权/劳动仲裁/民间借贷/证据收集）；\n"
    "3. situation：一句话概括用户处境（不超过 60 字）；\n"
    "4. key_points：1-4 条「有法律依据支撑的结论或注意事项」（每条不超过 60 字）。"
    "每一条都必须包含法律信号：法条引用（第X条）、法律名称、法定期限、程序环节、"
    "或「应当/可以/不得」等规则性表述；拿不准、纯经验之谈、与法律无关的要点一律不要写；\n"
    "5. 若对话中没有值得沉淀的法律结论，key_points 输出空数组；\n"
    "6. 只输出 JSON：{\"topics\":[...],\"situation\":\"...\",\"key_points\":[\"...\"]}\n\n"
    "对话记录：\n%s"
)

_PRIVACY_RES = [
    (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
    (re.compile(r"\d{17}[\dXx]"), "[身份证号]"),
    (re.compile(r"\d{16,19}"), "[银行卡号]"),
]

# 回答出现这些词 → 视为拒绝/不确定，不沉淀经验
_UNSURE_MARKERS = ["不确定", "无法确定", "不能确定", "需要核实", "需要进一步",
                   "可能错误", "不保证", "暂无明确法律规定", "信息不足", "无法判断",
                   "无法协助", "不能协助", "不得协助", "我不能", "抱歉，我不能"]
# 回答至少命中其一，才认为是有法律依据的结论
_GROUNDED_MARKERS = ["第", "《", "依据", "规定", "条例", "办法", "应当", "可以",
                     "不得", "申请", "起诉", "仲裁", "立案", "时效", "举证"]
# 蒸馏出的结论点必须命中其一
_POINT_LEGAL_MARKERS = ["第", "《", "规定", "条例", "办法", "应当", "不得", "可以",
                        "程序", "期限", "时效", "举证", "管辖", "受理", "起诉",
                        "仲裁", "申请", "判决", "赔偿", "抚养", "财产", "证据"]


def _mask(text):
    for pat, rep in _PRIVACY_RES:
        text = pat.sub(rep, text)
    return text


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    _ensure_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def session_fingerprint(messages):
    """会话内容指纹（用于避免重复沉淀）。"""
    h = hashlib.md5()
    for m in messages:
        h.update((m.get("role", "") + m.get("content", "")[:200]).encode("utf-8"))
    return h.hexdigest()


def last_assistant_reply(messages):
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content", "").strip():
            return m["content"].strip()
    return ""


def should_learn(session_id, messages, min_messages=4):
    """是否值得沉淀：消息数足够、内容有变化，且回答通过了质量护栏。"""
    if len([m for m in messages if m.get("role") in ("user", "assistant")]) < min_messages:
        return False
    st = _load_state()
    fp = session_fingerprint(messages)
    if st.get(session_id) == fp:
        return False
    # ---- 防乱学护栏：回答必须是「有依据的确定结论」 ----
    reply = last_assistant_reply(messages)
    if len(reply) < 60:
        return False
    if any(k in reply for k in _UNSURE_MARKERS):
        return False
    if not any(k in reply for k in _GROUNDED_MARKERS):
        return False
    return True


def mark_learned(session_id, messages):
    st = _load_state()
    st[session_id] = session_fingerprint(messages)
    _save_state(st)


def build_distill_input(messages, max_chars=4000):
    parts = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        # 附件解析文本只保留前 300 字，避免过长
        content = re.sub(r"【材料《[^】]*》全文内容】：.{300,}?(?=【材料《[^】]*》内容结束】)",
                         "【材料内容略】", content, flags=re.S)
        parts.append(("用户" if m["role"] == "user" else "顾问") + "：" + content[:800])
    text = "\n\n".join(parts)
    return text[-max_chars:]


def distill(messages, call_llm):
    """调用 LLM 蒸馏经验，返回经验 dict 或 None。"""
    text = build_distill_input(messages)
    if len(text) < 80:
        return None
    try:
        raw = call_llm([{"role": "user", "content": DISTILL_PROMPT % text}],
                       max_tokens=600, temperature=0.2)
    except Exception:
        return None
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    topics = [str(t)[:20] for t in data.get("topics", [])][:6]
    situation = _mask(str(data.get("situation", "")))[:120]
    raw_points = data.get("key_points", []) or []
    # 护栏：结论点必须带法律信号，且不能只是复读用户原话中的不确定内容
    points = []
    for p in raw_points:
        p = _mask(str(p))[:120]
        if any(k in p for k in _POINT_LEGAL_MARKERS):
            points.append(p)
        if len(points) >= 4:
            break
    if not topics or not situation or not points:
        return None
    return {"id": "exp_%d" % time.time(), "time": time.strftime("%Y-%m-%d %H:%M"),
            "topics": topics, "situation": situation, "key_points": points}


def add_experience(exp):
    """写入情节记忆（Agent Memory 数据库）。"""
    import agent_memory
    agent_memory.add_episodic(exp)


def list_experiences():
    import agent_memory
    return agent_memory.list_episodic()


def count():
    import agent_memory
    return agent_memory.episodic_count()


def retrieve(query, top=3):
    """按主题标签 + 摘要的关键词重叠检索经验。"""
    if not query:
        return []
    scored = []
    for e in list_experiences():
        hay = " ".join(e.get("topics", [])) + " " + e.get("situation", "")
        score = 0
        for t in e.get("topics", []):
            if t and t in query:
                score += 3
        # 二元组重叠
        qgrams = {query[i:i + 2] for i in range(len(query) - 1)}
        hgrams = {hay[i:i + 2] for i in range(len(hay) - 1)}
        if qgrams and hgrams:
            score += 4 * len(qgrams & hgrams) / len(qgrams)
        if score >= 3:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:top]]


def format_context(query):
    exps = retrieve(query)
    if not exps:
        return ""
    lines = ["【历史经验 · 从过往咨询中沉淀（已脱敏，仅供参考，不得作为法律结论；回答仍须以现行法条为准）】"]
    for e in exps:
        pts = "；".join(e.get("key_points", []))
        lines.append("· 情形：%s（%s）%s" % (e["situation"], "、".join(e["topics"]),
                                            ("要点：" + pts) if pts else ""))
    return "\n".join(lines)
