# -*- coding: utf-8 -*-
"""
本地法律库：加载 + 检索 + 上下文注入。

检索策略（RAG，无外部向量库依赖）：
1. 显式引用：用户提到具体条号（如「1079条」「第一千零七十九条」），命中对应条文；
   若同时提到法律名（民法典/民诉法等），限定该法律。
2. 法律名匹配：法律全称及简称（去掉「中华人民共和国」前缀）命中查询时，优先检索该法律。
3. 通用检索：查询与法条的二元组（bigram）重叠度打分 + 领域词加分，
   先在法律名层面粗筛 Top 法律，再在条文层面精排，控制注入总量。
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "legal_db")

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}

# 常见简称（法律名里不会自然生成的缩写）
EXTRA_ALIASES = {
    "民事诉讼法": "民诉法",
    "刑事诉讼法": "刑诉法",
    "行政诉讼法": "行诉法",
    "反家庭暴力法": "反家暴法",
    "未成年人保护法": "未保法",
    "妇女权益保障法": "妇女法",
    "消费者权益保护法": "消保法",
    "劳动合同法": "劳动合同法",
}

# 领域关键词（婚姻家事为强项，命中即加分；词越长权重越高）
DOMAIN_TERMS = [
    "协议离婚", "诉讼离婚", "离婚登记", "结婚登记", "冷静期", "离婚", "起诉", "诉讼",
    "调解", "管辖", "立案", "受理", "判决", "上诉", "再审", "执行", "缺席", "送达",
    "抚养费", "抚养权", "抚养", "探望", "探视", "监护", "亲子鉴定", "亲子关系",
    "共同财产", "个人财产", "婚前财产", "财产分割", "财产", "房产", "房屋", "存款",
    "股权", "股票", "基金", "保险", "公积金", "养老金", "车辆", "彩礼", "嫁妆",
    "共同债务", "债务", "借款", "贷款", "房贷",
    "重婚", "同居", "出轨", "婚外情", "忠实", "家暴", "家庭暴力", "虐待", "遗弃",
    "人身安全保护令", "保护令", "告诫书", "报警",
    "损害赔偿", "经济补偿", "经济帮助", "精神损害赔偿", "赔偿",
    "分居", "婚姻无效", "无效婚姻", "撤销婚姻", "可撤销", "胁迫",
    "收养", "继子女", "继父母", "非婚生子女",
    "赠与", "转移财产", "隐匿", "隐藏", "挥霍", "伪造债务", "少分", "不分",
    "无过错方", "照顾", "女方", "子女", "未成年", "两周岁", "八周岁", "哺乳",
    "生活困难", "军人", "怀孕", "分娩", "终止妊娠", "流产",
    "证据", "举证", "诉讼时效", "简易程序", "普通程序", "保全", "先予执行",
    "答辩", "起诉状", "代理", "律师", "公证", "鉴定",
    "继承", "遗产", "遗嘱", "遗赠", "继承权", "法定继承", "代位继承",
    "遗产管理人", "赡养", "扶养义务", "老年人", "虐待被监护",
    "诉讼费", "受理费", "案件受理费", "缓交", "减交", "免交",
    "家庭教育", "家庭教育指导", "婚检", "婚前医学检查", "婚前保健",
    "军婚", "现役军人", "重婚罪", "遗弃罪", "虐待罪",
]

# 口语说法 -> 法条原文常见表述（检索时把同义词也计入命中）
DOMAIN_SYNONYMS = {
    "冷静期": ["三十日", "撤回"],
    "净身出户": ["全部财产", "不分"],
    "婚内财产协议": ["财产协议", "约定"],
    "出轨": ["与他人同居", "重大过错"],
    "私生子": ["非婚生子女"],
    "家暴": ["家庭暴力", "殴打", "虐待"],
    "假离婚": ["离婚协议"],
    "两周岁": ["2周岁"],
    "八周岁": ["8周岁"],
    "婚前财产": ["婚前"],
    "转移财产": ["隐匿", "挥霍"],
    "抚养费": ["抚养"],
    "探视权": ["探望"],
    # 常见口语（覆盖全领域）
    "借钱": ["借款", "借贷", "欠款"],
    "欠钱": ["欠款", "借款"],
    "还钱": ["偿还", "返还", "清偿"],
    "不还": ["逾期", "不返还", "清偿"],
    "辞退": ["解除劳动合同", "经济补偿", "赔偿金"],
    "开除": ["解除劳动合同", "严重违反"],
    "拖欠工资": ["拖欠劳动报酬", "工资"],
    "加班费": ["加班", "延长工作时间"],
    "工伤": ["工伤保险", "劳动能力"],
    "漏水": ["渗水", "相邻", "妨害", "修缮"],
    "楼上": ["相邻"],
    "噪音": ["噪声"],
    "网购": ["网络购物", "电子商务"],
    "退款": ["退货", "退还"],
    "诈骗": ["骗取", "诈骗"],
}

# 查询词中的高频虚词二元组（不做检索信号）
STOP_GRAMS = {
    "请问", "你好", "您好", "谢谢", "怎么", "如何", "什么", "怎样", "是否",
    "可以", "需要", "应该", "应当", "如果", "要是", "现在", "已经", "我们",
    "你们", "他们", "就是", "不是", "还有", "因为", "所以", "但是", "不过",
    "一下", "帮我", "想要", "咨询", "问题", "情况", "相关", "规定", "大概",
    "多少", "多久", "时候", "之前", "之后", "这个", "那个", "这样", "那样",
    "请问", "一下", "一个", "一些", "有点", "遇到", "发生", "进行", "有关",
}

_ART_NO_RE = re.compile(r"第?([零一二两三四五六七八九十百千0-9]{1,6})\s*条")

_laws = []          # [{id, name, short, source_url, effective, articles:[...]}]
_loaded = False
# 检索索引
_law_index = []     # [{law, aliases:[...], alias_grams:set, first_no, last_no}]
_alias_map = {}     # 规范化法律名/简称 -> [law_id]
_gram_index = {}    # 二元组 -> 包含该词元的法律下标数组（条文级倒排索引）


def cn_to_int(s):
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section, number = 0, 0, 0
    for ch in s:
        if ch in CN_NUM:
            number = CN_NUM[ch]
        elif ch == "十":
            section += (number if number else 1) * 10
            number = 0
        elif ch == "百":
            section += number * 100
            number = 0
        elif ch == "千":
            total += (section + number) * 1000
            section, number = 0, 0
        else:
            return None
    return total + section + number


def _norm(t):
    return re.sub(r"[\s·（）()\-—]+", "", t or "").lower()


def _clean_query(q):
    return re.sub(r"[^\u4e00-\u9fff0-9a-zA-Z]", "", q or "").lower()


def _grams(s):
    if len(s) < 2:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _aliases_for(law):
    """由法律全称生成别名：全称、去「中华人民共和国」、去修订括注、常见简称。"""
    name = law["name"]
    out = [name]
    short = _norm(name).replace("中华人民共和国", "")
    if short:
        out.append(short)
    s2 = re.sub(r"（?(20\d{2}年)?(修正|修订|修改)[^）)]*）?", "", name)
    if s2 != name:
        out.append(s2)
        out.append(_norm(s2).replace("中华人民共和国", ""))
    extra = EXTRA_ALIASES.get(short)
    if extra:
        out.append(extra)
    return [a for a in dict.fromkeys(out) if a and len(a) >= 2]


def load():
    global _laws, _loaded, _law_index, _alias_map, _gram_index
    import array
    _laws, _law_index, _alias_map, _gram_index = [], [], {}, {}
    if not os.path.isdir(DB_DIR):
        _loaded = True
        return
    for fn in sorted(os.listdir(DB_DIR)):
        if not fn.endswith(".json") or fn == "manifest.json":
            continue
        try:
            with open(os.path.join(DB_DIR, fn), encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("articles"):
                _laws.append(doc)
        except Exception:
            continue
    # 防御性去重：若精编库与官方库名称互为前缀，以官方库（flk-）为准
    flk_titles = [_norm(l["name"]) for l in _laws if l["id"].startswith("flk-")]
    _laws = [l for l in _laws
             if l["id"].startswith("flk-")
             or not any(t.startswith(_norm(l["name"])) or _norm(l["name"]).startswith(t)
                        for t in flk_titles)]
    for li, law in enumerate(_laws):
        aliases = _aliases_for(law)
        nos = [a["no"] for a in law["articles"] if isinstance(a.get("no"), int)]
        entry = {
            "law": law,
            "aliases": aliases,
            "alias_grams": set().union(*[_grams(_norm(a)) for a in aliases]),
            "first_no": min(nos) if nos else 0,
            "last_no": max(nos) if nos else 0,
        }
        _law_index.append(entry)
        for a in aliases:
            _alias_map.setdefault(_norm(a), []).append(law["id"])
        # 条文级倒排：法律全文的二元组 -> 法律下标
        blob_grams = set()
        for art in law["articles"]:
            blob_grams |= _grams(_clean_query(art["text"]))
        for g in blob_grams:
            if g in STOP_GRAMS or len(g) < 2:
                continue
            arr = _gram_index.get(g)
            if arr is None:
                arr = array.array("H")
                _gram_index[g] = arr
            arr.append(li)
    _loaded = True


def reload():
    load()


def is_loaded():
    return _loaded and bool(_laws)


def manifest():
    load_if_needed()
    return [{"id": l["id"], "name": l["name"], "short": l["short"],
             "category": l.get("category", ""),
             "source_name": l.get("source_name"), "source_url": l.get("source_url"),
             "publish": l.get("publish"), "effective": l.get("effective"),
             "article_count": len(l["articles"])} for l in _laws]


def load_if_needed():
    if not _loaded:
        load()


def get_law(law_id):
    load_if_needed()
    for l in _laws:
        if l["id"] == law_id:
            return l
    return None


def _explicit_article_nos(query):
    """提取查询中显式提到的条号。"""
    nos = []
    for m in _ART_NO_RE.finditer(query):
        no = cn_to_int(m.group(1))
        if no:
            nos.append(no)
    return nos


def _mentioned_law_ids(query):
    """查询中提到的法律（全称或简称）。"""
    q = query
    ids = []
    hits = []
    for alias in _alias_map:  # 规范化后直接子串匹配
        if alias in q:
            hits.append((len(alias), alias))
    hits.sort(reverse=True)
    seen = set()
    for _, alias in hits:
        for law_id in _alias_map[alias]:
            if law_id not in seen:
                seen.add(law_id)
                ids.append(law_id)
        if len(ids) >= 5:
            break
    return ids


def retrieve(query, max_items=8, max_chars=2600, per_law_cap=3):
    """检索相关条文，返回 [{law_id, law_short, marker, text, score}]。"""
    load_if_needed()
    if not _laws or not query:
        return []
    explicit_nos = set(_explicit_article_nos(query))
    mentioned = _mentioned_law_ids(query)
    mentioned_set = set(mentioned)
    q_clean = _clean_query(query)
    q_grams = _grams(q_clean) - STOP_GRAMS
    domain_hits = [t for t in DOMAIN_TERMS if t in query]
    # 口语同义词扩充查询词（如「冷静期」→「三十日」「撤回」；「借钱」→「借款」）
    for syn_key, syns in DOMAIN_SYNONYMS.items():
        if syn_key in query:
            for syn in syns:
                q_grams |= _grams(_clean_query(syn))

    # ---- 第 1 步：法律名层面粗筛（名称匹配 + 条文倒排索引） ----
    law_scores = {}
    for g in q_grams:
        for li in _gram_index.get(g, ()):
            law_scores[li] = law_scores.get(li, 0) + 1
    ranked = []
    for li, score in law_scores.items():
        entry = _law_index[li]
        law = entry["law"]
        if law["id"] in mentioned_set:
            score += 1000
        score += 3 * len(q_grams & entry["alias_grams"])
        if explicit_nos:
            # 条号必须落在该法律条号区间内
            if not (any(entry["first_no"] <= n <= entry["last_no"] for n in explicit_nos)):
                if law["id"] not in mentioned_set:
                    continue
        if score > 0:
            ranked.append((score, entry))
    ranked.sort(key=lambda x: -x[0])
    if not ranked:
        return []
    top = [e for _, e in ranked[:20]] if not mentioned else [e for _, e in ranked[:8]]
    # 提到具体法律时，确保其全部入选
    if mentioned_set:
        for entry in _law_index:
            if entry["law"]["id"] in mentioned_set and entry not in top:
                top.append(entry)

    # ---- 第 2 步：条文层面精排 ----
    scored = []
    for entry in top:
        law = entry["law"]
        law_hits = []
        for art in law["articles"]:
            score = 0
            if art["no"] in explicit_nos:
                if not mentioned_set or law["id"] in mentioned_set:
                    score += 100
            body = art["text"]
            if q_grams:
                score += 8 * len(q_grams & _grams(_clean_query(body))) / max(1, len(q_grams))
            for t in domain_hits:
                if t in body:
                    score += 2 if len(t) >= 3 else 1
            if score >= 2:
                law_hits.append((score, art))
        law_hits.sort(key=lambda x: (-x[0], x[1]["no"]))
        cap = per_law_cap + (2 if law["id"] in mentioned_set else 0)
        for score, art in law_hits[:cap]:
            scored.append({"law_id": law["id"], "law_short": law["short"],
                           "marker": art["marker"], "text": art["text"], "score": score})
    scored.sort(key=lambda x: -x["score"])
    # ---- 混合检索：叠加向量语义召回（可用时），按 (law_id, marker) 去重取高分 ----
    try:
        import vector_rag
        vhits = vector_rag.query(query, top_k=10)
        if vhits:
            by_key = {(i["law_id"], i["marker"]): i for i in scored}
            for v in vhits:
                key = (v["law_id"], v["marker"])
                if key in by_key:
                    by_key[key]["score"] = max(by_key[key]["score"], v["score"] * 8)
                else:
                    law = None
                    for l in _laws:
                        if l["id"] == v["law_id"]:
                            law = l
                            break
                    if law:
                        art = next((a for a in law["articles"]
                                    if a.get("marker") == v["marker"]), None)
                        if art:
                            by_key[key] = {"law_id": law["id"], "law_short": law["short"],
                                           "marker": art["marker"], "text": art["text"],
                                           "score": v["score"] * 8}
            scored = sorted(by_key.values(), key=lambda x: -x["score"])
    except Exception:
        pass
    out, total = [], 0
    for item in scored:
        if len(out) >= max_items or total + len(item["text"]) > max_chars:
            if any(i["score"] >= 100 for i in scored[len(out):]):
                continue  # 显式引用尽量保留
            break
        out.append(item)
        total += len(item["text"])
    return out


def format_context(query):
    """生成注入系统提示词的法律依据文本块。"""
    hits = retrieve(query)
    if not hits:
        return ""
    lines = ["【法律依据 · 来自本地法律库（官方现行有效文本，回答时以此为准并注明出处）】"]
    for h in hits:
        lines.append("《%s》%s：\n%s" % (h["law_short"], h["marker"], h["text"]))
    lines.append("（以上条文摘自国家法律法规数据库及政府官网发布的现行有效文本）")
    return "\n\n".join(lines)


def search(query, law_id=None, limit=30):
    """法条浏览面板的搜索：返回命中条文（含法律名）。"""
    load_if_needed()
    results = []
    nos = set(_explicit_article_nos(query))
    for law in _laws:
        if law_id and law["id"] != law_id:
            continue
        for art in law["articles"]:
            if (query and query in art["text"]) or (art["no"] in nos):
                results.append({"law_id": law["id"], "law_short": law["short"],
                                "marker": art["marker"], "text": art["text"]})
                if len(results) >= limit:
                    return results
    return results
