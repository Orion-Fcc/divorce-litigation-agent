# -*- coding: utf-8 -*-
"""
Agent Memory：SQLite 数据库支撑的三层记忆。

- 情节记忆 episodic：从历史对话蒸馏的通用经验（原 experience.jsonl 迁移至此）
- 语义记忆 semantic：关于当事人/案件的事实（身份、时间、金额、财产、诉求、风险），
  跨会话长期保留，对话中实时提取、检索注入
- 会话记忆 session：每个会话的更新时间与摘要

所有记忆本地存储于 python_app/data/agent_memory.db，不上传。

护栏：
- 事实提取只保留稳定、明确的信息，强制脱敏（人名/电话/身份证/地址泛化）
- 经验沉淀沿用 learning.py 的质量护栏（有法条依据的确定结论才学）
- 语义记忆去重（内容指纹），上限 2000 条，超限裁最旧
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "agent_memory.db")
MAX_SEMANTIC = 2000
MAX_EPISODIC = 500

_lock = threading.Lock()
_conn = None

FACT_PROMPT = (
    "你是记忆提取助手。请从下面「用户与法律顾问」的对话中，提取关于用户自身情况的"
    "稳定事实，供以后对话参考。\n"
    "要求：\n"
    "1. 只提取明确、稳定的事实（身份、关系、时间节点、金额、财产、证据、诉求、风险、"
    "重要进展），不提取闲聊、假设、反问、法律条文本身；\n"
    "2. 必须脱敏：不得包含真实人名、电话、身份证号、银行卡号、详细地址，"
    "一律泛化（如「当事人」「对方」「一套房」「一笔借款」）；\n"
    "3. 每条事实一句话（不超过 50 字），type 取：身份/关系/时间/金额/财产/诉求/风险/进展/其他；\n"
    "4. 没有值得记的事实就输出空数组；\n"
    "5. 只输出 JSON 数组：[{\"type\":\"身份\",\"content\":\"当事人是女方，正在与男方协商离婚\"}]\n\n"
    "对话记录：\n%s"
)

_PRIVACY_RES = [
    (re.compile(r"1[3-9]\d{9}"), "某手机号"),
    (re.compile(r"\d{17}[\dXx]"), "某身份证号"),
    (re.compile(r"\d{16,19}"), "某卡号"),
]


def _mask(text):
    for pat, rep in _PRIVACY_RES:
        text = pat.sub(rep, text)
    return text


def _conn_():
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("""CREATE TABLE IF NOT EXISTS episodic(
            id TEXT PRIMARY KEY, time TEXT, topics TEXT, situation TEXT, key_points TEXT)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS semantic(
            id TEXT PRIMARY KEY, sig TEXT UNIQUE, session_id TEXT, time TEXT,
            type TEXT, content TEXT)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS session(
            session_id TEXT PRIMARY KEY, updated TEXT, summary TEXT)""")
        _conn.commit()
        _migrate_legacy()
    return _conn


def _migrate_legacy():
    """把旧版 experience.jsonl 迁移进数据库（一次性）。"""
    legacy = os.path.join(DATA_DIR, "experience.jsonl")
    if not os.path.exists(legacy):
        return
    try:
        with _lock:
            n = 0
            with open(legacy, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    _add_episodic(e, skip_lock=True)
                    n += 1
        os.replace(legacy, legacy + ".migrated")
        print("[记忆] 已迁移旧经验 %d 条到 Agent Memory 数据库。" % n)
    except Exception as e:
        print("[记忆] 旧经验迁移失败：", e)


# ---------------------------------------------------------------- 情节记忆（经验）
def _add_episodic(exp, skip_lock=False):
    if not skip_lock:
        _lock.acquire()
    try:
        c = _conn_()
        c.execute("INSERT OR REPLACE INTO episodic(id,time,topics,situation,key_points) "
                  "VALUES(?,?,?,?,?)",
                  (exp["id"], exp.get("time", ""), json.dumps(exp.get("topics", []),
                                                              ensure_ascii=False),
                   exp.get("situation", ""), json.dumps(exp.get("key_points", []),
                                                        ensure_ascii=False)))
        c.execute("DELETE FROM episodic WHERE id NOT IN "
                  "(SELECT id FROM episodic ORDER BY time DESC LIMIT %d)" % MAX_EPISODIC)
        c.commit()
    finally:
        if not skip_lock:
            _lock.release()


def add_episodic(exp):
    _add_episodic(exp)


def list_episodic():
    rows = _conn_().execute("SELECT * FROM episodic ORDER BY time DESC").fetchall()
    return [{"id": r[0], "time": r[1], "topics": json.loads(r[2]),
             "situation": r[3], "key_points": json.loads(r[4])} for r in rows]


def episodic_count():
    return _conn_().execute("SELECT COUNT(*) FROM episodic").fetchone()[0]


def clear_episodic():
    with _lock:
        _conn_().execute("DELETE FROM episodic")
        _conn_().commit()


# ---------------------------------------------------------------- 语义记忆（当事人事实）
def _sig(content):
    core = re.sub(r"[\s，。、！？：；“”‘’（）()\-—]", "", content)
    return hashlib.md5(core.encode("utf-8")).hexdigest()


def add_semantic(session_id, facts):
    """写入事实（按内容指纹去重），返回新增条数。"""
    added = 0
    with _lock:
        c = _conn_()
        for f in facts:
            content = _mask(str(f.get("content", ""))).strip()[:120]
            ftype = str(f.get("type", "其他"))[:10]
            if not content:
                continue
            sig = _sig(content)
            try:
                c.execute("INSERT INTO semantic(id,sig,session_id,time,type,content) "
                          "VALUES(?,?,?,?,?,?)",
                          ("mem_%d" % time.time_ns(), sig, session_id,
                           time.strftime("%Y-%m-%d %H:%M"), ftype, content))
                added += 1
            except sqlite3.IntegrityError:
                continue
        c.execute("DELETE FROM semantic WHERE id NOT IN "
                  "(SELECT id FROM semantic ORDER BY time DESC LIMIT %d)" % MAX_SEMANTIC)
        c.commit()
    return added


def list_semantic():
    rows = _conn_().execute(
        "SELECT id,type,content,time FROM semantic ORDER BY time DESC").fetchall()
    return [{"id": r[0], "type": r[1], "content": r[2], "time": r[3]} for r in rows]


def semantic_count():
    return _conn_().execute("SELECT COUNT(*) FROM semantic").fetchone()[0]


def clear_semantic():
    with _lock:
        _conn_().execute("DELETE FROM semantic")
        _conn_().commit()


def _grams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def retrieve_semantic(query, top=4):
    """关键词 + 向量双路召回当事人事实。"""
    facts = list_semantic()
    if not facts or not query:
        return []
    q_grams = _grams(re.sub(r"[^\u4e00-\u9fff0-9a-zA-Z]", "", query))
    scored = {}
    for i, f in enumerate(facts):
        hay = f["content"] + f["type"]
        h_grams = _grams(hay)
        s = len(q_grams & h_grams) / max(1, len(q_grams))
        if s > 0:
            scored[i] = s * 5
    # 向量召回（可用时）
    try:
        import vector_rag
        vecs = vector_rag.embed_texts([query] + [f["content"] for f in facts], batch=16)
        if vecs is not None:
            sims = vecs[0].astype("float32").dot(vecs[1:].astype("float32").T)
            for i, s in enumerate(sims):
                if float(s) > 0.35:
                    scored[i] = max(scored.get(i, 0), float(s) * 5)
    except Exception:
        pass
    order = sorted(scored.items(), key=lambda x: -x[1])[:top]
    return [facts[i] for i, _ in order]


def format_memory_context(query):
    facts = retrieve_semantic(query)
    if not facts:
        return ""
    lines = ["【长期记忆 · 已脱敏的当事人情况（从历史对话中记录，回答时结合这些情况，"
             "若有出入以当事人最新表述为准）】"]
    for f in facts:
        lines.append("· %s：%s" % (f["type"], f["content"]))
    return "\n".join(lines)


def extract_facts(messages, call_llm):
    """调用 LLM 从对话中提取事实，返回 [{type, content}]。"""
    parts = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        parts.append(("用户" if m["role"] == "user" else "顾问") + "：" +
                     str(m.get("content", ""))[:600])
    text = "\n\n".join(parts)[-3500:]
    if len(text) < 40:
        return []
    try:
        raw = call_llm([{"role": "user", "content": FACT_PROMPT % text}],
                       max_tokens=500, temperature=0.1)
    except Exception:
        return []
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [{"type": str(x.get("type", "其他"))[:10],
             "content": str(x.get("content", ""))[:120]}
            for x in data if isinstance(x, dict) and x.get("content")]


# ---------------------------------------------------------------- 会话记忆
def touch_session(session_id, messages):
    """记录会话更新时间与摘要（取最近一条用户消息）。"""
    summary = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content", "").strip():
            summary = str(m["content"]).strip()[:80]
            break
    with _lock:
        c = _conn_()
        c.execute("INSERT OR REPLACE INTO session(session_id,updated,summary) "
                  "VALUES(?,?,?)",
                  (str(session_id), time.strftime("%Y-%m-%d %H:%M"), summary))
        c.commit()


def list_sessions():
    rows = _conn_().execute(
        "SELECT session_id,updated,summary FROM session ORDER BY updated DESC").fetchall()
    return [{"session_id": r[0], "updated": r[1], "summary": r[2]} for r in rows]
