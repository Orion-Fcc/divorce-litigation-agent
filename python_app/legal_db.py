# -*- coding: utf-8 -*-
"""
本地法律库：加载 + 检索 + 上下文注入。

检索策略（RAG-lite，无外部依赖）：
1. 显式引用：用户提到具体条号（如"1079条""第一千零七十九条"），命中对应条文；
   若同时提到法律别名（民法典/民诉法等），限定该法律。
2. 关键词匹配：领域词表 + 词频打分，每部法律最多取 3 条，控制注入总量。
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "legal_db")

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}

LAW_ALIASES = {
    "minfadian-hunyin": ["民法典", "婚姻家庭编"],
    "jieshi-1": ["解释一", "司法解释一", "婚姻家庭编解释一"],
    "jieshi-2": ["解释二", "司法解释二", "婚姻家庭编解释二"],
    "caili-guiding": ["彩礼规定", "涉彩礼"],
    "fanjiabaoli-fa": ["反家庭暴力法", "反家暴法"],
    "funv-quanyi-fa": ["妇女权益保障法", "妇女法"],
    "weichengnianren-fa": ["未成年人保护法", "未保法"],
    "hunyin-dengji-tiaoli": ["婚姻登记条例", "登记条例"],
    "minshi-susong-fa": ["民事诉讼法", "民诉法"],
    "minfadian-jicheng": ["继承编", "民法典继承编"],
    "jicheng-jieshi-1": ["继承解释", "继承编解释", "继承司法解释"],
    "jiating-jiaoyu-fa": ["家庭教育促进法", "家庭教育法"],
    "renshen-anquan-baohuling": ["保护令规定", "人身安全保护令规定"],
    "xingfa-hunyin": ["刑法", "刑事"],
    "susongfei-banfa": ["诉讼费用交纳办法", "诉讼费办法", "诉讼收费"],
    "laonianren-quanyi-fa": ["老年人权益保障法", "老年人法"],
    "muying-baojian-fa": ["母婴保健法"],
    "hunyin-dengji-guifan": ["婚姻登记工作规范", "登记工作规范", "登记规范"],
}

# 领域关键词（命中即加分；词越长权重越高）
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
    "收养", "继子女", "继父母", "非婚生子女", "私生子",
    "赠与", "转移财产", "隐匿", "隐藏", "挥霍", "伪造债务", "少分", "不分",
    "无过错方", "照顾", "女方", "子女", "未成年", "两周岁", "八周岁", "哺乳",
    "生活困难", "军人", "怀孕", "分娩", "终止妊娠", "流产",
    "证据", "举证", "诉讼时效", "简易程序", "普通程序", "保全", "先予执行",
    "答辩", "起诉状", "代理", "律师", "公证", "鉴定",
    "继承", "遗产", "遗嘱", "遗赠", "继承权", "法定继承", "代位继承",
    "遗产管理人", "赡养", "扶养义务", "老年人", "虐待被监护",
    "诉讼费", "受理费", "案件受理费", "缓交", "减交", "免交",
    "家庭教育", "家庭教育指导", "婚检", "婚前医学检查", "婚前保健",
    "军婚", "现役军人", "重婚罪", "遗弃罪", "虐待罪", "拐骗",
]

_ART_NO_RE = re.compile(r"第?([零一二两三四五六七八九十百千0-9]{1,6})\s*条")
_DIGIT_CN = str.maketrans("0123456789", "零一二三四五六七八九")

_laws = []          # [{id, name, short, source_url, effective, articles:[...]}]
_loaded = False


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


def load():
    global _laws, _loaded
    _laws = []
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
    _loaded = True


def reload():
    load()


def is_loaded():
    return _loaded and bool(_laws)


def manifest():
    load_if_needed()
    return [{"id": l["id"], "name": l["name"], "short": l["short"],
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
    ids = []
    for law_id, aliases in LAW_ALIASES.items():
        if any(a in query for a in aliases):
            ids.append(law_id)
    return ids


def retrieve(query, max_items=8, max_chars=2600, per_law_cap=3):
    """检索相关条文，返回 [{law_id, law_short, marker, text, score}]。"""
    load_if_needed()
    if not _laws or not query:
        return []
    explicit_nos = set(_explicit_article_nos(query))
    mentioned_laws = set(_mentioned_law_ids(query))
    terms = [t for t in DOMAIN_TERMS if t in query]

    scored = []
    for law in _laws:
        law_hits = []
        for art in law["articles"]:
            score = 0
            if art["no"] in explicit_nos:
                if not mentioned_laws or law["id"] in mentioned_laws:
                    score += 100  # 显式引用，最高优先
            body = art["text"]
            for t in terms:
                if t in body:
                    score += 2 if len(t) >= 3 else 1
            if score:
                law_hits.append((score, law, art))
        law_hits.sort(key=lambda x: (-x[0], x[2]["no"]))
        cap = per_law_cap if not mentioned_laws or law["id"] not in mentioned_laws else per_law_cap + 2
        for score, law, art in law_hits[:cap]:
            scored.append({"law_id": law["id"], "law_short": law["short"],
                           "marker": art["marker"], "text": art["text"], "score": score})
    scored.sort(key=lambda x: -x["score"])
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
    lines = ["【法律依据 · 来自本地法律库（官方文本，回答时以此为准并注明出处）】"]
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
