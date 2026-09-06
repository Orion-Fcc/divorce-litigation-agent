# -*- coding: utf-8 -*-
"""
批量收录：从国家法律法规数据库（flk.npc.gov.cn，全国人大常委会办公厅维护）
下载全部「现行有效」的宪法、法律、法律解释、行政法规、监察法规、司法解释，
解析条文后写入本地法律库（legal_db/flk-*.json），并与 build_legal_db.py
的精编库合并生成 manifest.json。

用法：
    python tools/build_all_laws.py --list-only      # 只拉清单元数据，存 flk_catalog.json
    python tools/build_all_laws.py --max 10         # 只处理前 10 部（联调测试）
    python tools/build_all_laws.py                  # 完整收录（断点续传，跳过已完成）
    python tools/build_all_laws.py --refresh        # 重新下载全部并覆盖本地（跟踪修订）
    python tools/build_all_laws.py --merge-only     # 只做合并 manifest（不联网）

合并规则：官方库与精编库按名称去重——名称互为前缀时以官方库为准
（如整本《民法典》取代婚姻家庭编/继承编分册，《刑法》全文取代节选）。
"""
import json
import os
import re
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "legal_db")
sys.path.insert(0, TOOLS_DIR)

import build_legal_db  # noqa: E402  复用 TextExtractor/parse_articles/fetch 等
from build_legal_db import parse_articles  # noqa: E402

STATE_FILE = os.path.join(DB_DIR, "_flk_state.json")
CATALOG_FILE = os.path.join(DB_DIR, "flk_catalog.json")

BASE = "https://flk.npc.gov.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

# 收录范围：全部现行有效（sxx=3）。地方性法规数量过万且地域性强，暂不收录。
CATEGORIES = [
    (100, "宪法"),
    (110, "宪法相关法"),
    (120, "民法商法"),
    (130, "行政法"),
    (140, "经济法"),
    (150, "社会法"),
    (155, "生态环境法"),
    (160, "刑法"),
    (170, "诉讼与非诉讼程序法"),
    (180, "法律解释"),
    (210, "行政法规"),
    (220, "监察法规"),
    (320, "高法司法解释"),
    (330, "高检司法解释"),
    (340, "联合发布司法解释"),
]

# 这些文件虽标「有效」但不是条文型文本（名单、公报、有关决定等），跳过
SKIP_TITLE_PATTERNS = [
    r"公布.*名单$", r"任免名单", r"代表资格", r"会议议程",
    r"^全国人民代表大会(常务委员会)?关于.{0,30}的决定$",
]

_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE + "/",
    "Origin": BASE,
    "Content-Type": "application/json;charset=utf-8",
}


def _requests():
    import requests
    requests.packages.urllib3.disable_warnings()
    return requests


def _get(url, params=None, tries=5):
    """GET with retry（该站 SSL 偶发 EOF，需要重试）。"""
    req = _requests()
    last = None
    for i in range(tries):
        try:
            r = req.get(url, params=params, headers=_HEADERS, timeout=40, verify=False)
            if r.status_code == 200:
                return r
            last = RuntimeError("HTTP %s" % r.status_code)
        except Exception as e:
            last = e
        time.sleep(1.5 + i * 2)
    raise last


def _post(url, payload, tries=5):
    req = _requests()
    last = None
    for i in range(tries):
        try:
            r = req.post(url, json=payload, headers=_HEADERS, timeout=40, verify=False)
            if r.status_code == 200:
                return r
            last = RuntimeError("HTTP %s" % r.status_code)
        except Exception as e:
            last = e
        time.sleep(1.5 + i * 2)
    raise last


def _list_payload(code, page_num, page_size=100):
    return {
        "searchRange": 1, "sxrq": [], "gbrq": [], "searchType": 2,
        "sxx": ["3"], "gbrqYear": [], "flfgCodeId": [str(code)],
        "zdjgCodeId": [], "searchContent": "",
        "orderByParam": {"order": "-1", "sort": ""},
        "scoreDto": {"ppdScore": None, "flfgflScore": None,
                     "zdjgScore": None, "sxxScore": None},
        "pageNum": page_num, "pageSize": page_size,
    }


def fetch_catalog():
    """拉取全部现行有效文件的清单元数据（bbbs/title/日期/分类）。"""
    catalog, seen = [], set()
    for code, cat_name in CATEGORIES:
        page = 1
        while True:
            r = _post(BASE + "/law-search/search/list", _list_payload(code, page))
            d = r.json()
            if d.get("code") != 200:
                raise RuntimeError("列表接口异常: %s" % d.get("msg"))
            rows = d.get("rows") or []
            if not rows:
                break
            for row in rows:
                bbbs = row.get("bbbs")
                if not bbbs or bbbs in seen:
                    continue
                title = (row.get("title") or "").strip()
                if any(re.search(p, title) for p in SKIP_TITLE_PATTERNS):
                    continue
                seen.add(bbbs)
                catalog.append({
                    "bbbs": bbbs, "title": title,
                    "gbrq": row.get("gbrq") or "", "sxrq": row.get("sxrq") or "",
                    "flxz": row.get("flxz") or cat_name,
                    "zdjgName": row.get("zdjgName") or "",
                    "category": cat_name, "sxx": "有效",
                })
            total = d.get("total") or 0
            print("  [%s] 第 %d 页，累计 %d / %d" % (cat_name, page, len(catalog), total))
            if page * 100 >= total:
                break
            page += 1
            time.sleep(0.3)
        time.sleep(0.5)
    return catalog


def fetch_detail(bbbs):
    """详情接口（服务器偶发返回非 JSON，重试最多 3 次）。"""
    last = None
    for attempt in range(3):
        try:
            r = _get(BASE + "/law-search/search/flfgDetails", params={"bbbs": bbbs})
            d = r.json()
            if d.get("code") != 200:
                raise RuntimeError("详情接口异常: %s" % d.get("msg"))
            return d.get("data") or {}
        except (ValueError, RuntimeError) as e:
            last = e
            time.sleep(1 + attempt * 1.5)
    raise last


def resolve_urls(bbbs):
    """获取 docx/pdf 签名下载链接（不依赖详情接口，避开 WAF 挑战）。"""
    urls = {}
    for fmt, key in (("docx", "docx_url"), ("pdf", "pdf_url")):
        try:
            r = _get(BASE + "/law-search/download/pc",
                     params={"format": fmt, "bbbs": bbbs, "fileId": ""})
            d = r.json()
            if d.get("code") == 200 and (d.get("data") or {}).get("url"):
                urls[key] = d["data"]["url"]
        except Exception:
            continue
    return urls


def _download_docx(url):
    import io
    import docx
    req = _requests()
    fr = req.get(url, headers={"User-Agent": UA}, timeout=180, verify=False)
    if fr.status_code != 200 or len(fr.content) <= 1000:
        raise RuntimeError("docx 下载失败 HTTP %s" % fr.status_code)
    doc = docx.Document(io.BytesIO(fr.content))
    text = "\n".join(p.text for p in doc.paragraphs).strip()
    if len(text) <= 200:
        raise RuntimeError("docx 正文过短")
    return text


def _download_pdf(url):
    req = _requests()
    fr = req.get(url, headers={"User-Agent": UA}, timeout=180, verify=False)
    if fr.status_code != 200 or len(fr.content) <= 1000:
        raise RuntimeError("pdf 下载失败 HTTP %s" % fr.status_code)
    text = build_legal_db._pdf_to_text(fr.content)
    if len(text.strip()) <= 200:
        raise RuntimeError("pdf 正文过短")
    return text.strip()


def fetch_full_text(bbbs, urls=None):
    """提取全文：优先已解析好的下载链接（两阶段），兜底现场解析。"""
    if urls is None:
        urls = resolve_urls(bbbs)
    if urls.get("docx_url"):
        try:
            return _download_docx(urls["docx_url"])
        except Exception:
            pass
    if urls.get("pdf_url"):
        try:
            return _download_pdf(urls["pdf_url"])
        except Exception:
            pass
    raise RuntimeError("无法获取全文（docx/pdf 均失败）")


def _norm_title(t):
    return re.sub(r"[\s·（）()\-—]+", "", t or "")


def _short_name(title):
    s = title.replace("中华人民共和国", "")
    s = re.sub(r"（(20\d{2}年)?(修正|修订|修改)[^）]*）", "", s)
    s = re.sub(r"\((20\d{2}年)?(修正|修订|修改)[^)]*\)", "", s)
    return s.strip() or title


def build_one(item, urls=None):
    """下载全文并入库（元数据直接用清单行，不依赖详情接口）。"""
    bbbs = item["bbbs"]
    text = fetch_full_text(bbbs, urls)
    articles = parse_articles(text, inline=False)
    if not articles:
        # 无条文结构的文件（极少数决定类）：整篇作为一条
        articles = [{"no": 1, "marker": "全文",
                     "text": re.sub(r"\s+", " ", text)[:20000]}]
    law_id = "flk-" + bbbs
    doc = {
        "id": law_id,
        "name": item["title"],
        "short": _short_name(item["title"]),
        "category": item["category"],
        "source_name": "国家法律法规数据库（全国人大常委会办公厅）",
        "source_url": BASE + "/detail2.html?" + bbbs,
        "publish": item.get("gbrq") or "",
        "effective": item.get("sxrq") or "",
        "office": item.get("zdjgName") or "",
        "sxx": "有效",
        "articles": articles,
    }
    with open(os.path.join(DB_DIR, law_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return doc


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("done", {})
    st.setdefault("failed", {})
    st.setdefault("urls", {})
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def _guess_category(name):
    if "条例" in name:
        return "行政法规"
    if "解释" in name:
        return "司法解释"
    if ("办法" in name) or ("规定" in name):
        return "行政法规"
    if "规范" in name:
        return "规范性文件"
    if name.endswith("法") or "法典" in name:
        return "法律"
    return "其他"


def merge_manifest():
    """官方库 + 精编库合并生成 manifest.json（被官方库取代的精编文件直接删除）。"""
    laws = []
    for fn in sorted(os.listdir(DB_DIR)):
        if not fn.endswith(".json") or fn in ("manifest.json", "flk_catalog.json"):
            continue
        try:
            with open(os.path.join(DB_DIR, fn), encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("articles"):
                laws.append(doc)
        except Exception:
            continue
    # 名称去重：互为前缀 → 保留官方库（flk-）版本
    flk_titles = [_norm_title(l["name"]) for l in laws if l["id"].startswith("flk-")]
    kept = []
    for l in laws:
        if l["id"].startswith("flk-"):
            kept.append(l)
            continue
        nt = _norm_title(l["name"])
        dup = any(nt.startswith(t) or t.startswith(nt) for t in flk_titles)
        if dup:
            print("[合并] 官方库已含《%s》，删除精编库同名条目" % l["name"])
            try:
                os.remove(os.path.join(DB_DIR, l["id"] + ".json"))
            except OSError:
                pass
        else:
            if not l.get("category"):
                l["category"] = _guess_category(l["name"])
                try:
                    with open(os.path.join(DB_DIR, l["id"] + ".json"), "w",
                              encoding="utf-8") as f:
                        json.dump(l, f, ensure_ascii=False, indent=1)
                except OSError:
                    pass
            kept.append(l)
    order = {c: i for i, (_, c) in enumerate(CATEGORIES)}
    kept.sort(key=lambda l: (order.get(l.get("category", ""), 99), l["name"]))
    manifest = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "laws": [{"id": l["id"], "name": l["name"], "short": l["short"],
                  "category": l.get("category", ""),
                  "source_name": l.get("source_name"),
                  "source_url": l.get("source_url"),
                  "publish": l.get("publish"), "effective": l.get("effective"),
                  "article_count": len(l["articles"])} for l in kept],
    }
    with open(os.path.join(DB_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def main():
    argv = sys.argv[1:]
    only_list = "--list-only" in argv
    merge_only = "--merge-only" in argv
    refresh = "--refresh" in argv
    max_n = None
    if "--max" in argv:
        max_n = int(argv[argv.index("--max") + 1])

    os.makedirs(DB_DIR, exist_ok=True)

    if merge_only:
        m = merge_manifest()
        print("合并完成，共 %d 部。" % len(m["laws"]))
        return

    # 1) 精编库（民法典全本 + 婚姻家事相关；供合并与兜底）
    if not only_list and "--skip-curated" not in argv:
        print("== 第 1 步：构建精编库 ==", flush=True)
        try:
            build_legal_db.main()
        except SystemExit:
            pass

    # 2) 清单元数据
    if "--skip-curated" in argv and os.path.exists(CATALOG_FILE):
        print("== 第 2 步：复用已有清单 ==", flush=True)
        with open(CATALOG_FILE, encoding="utf-8") as f:
            catalog = json.load(f)
    else:
        print("== 第 2 步：拉取官方库清单 ==", flush=True)
        catalog = fetch_catalog()
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=1)
    print("清单元数据：%d 部。" % len(catalog), flush=True)
    if only_list:
        return

    # 3) 两阶段下载：
    #    阶段 A（官方接口，低并发）：详情 + 解析下载链接；带正文的直接入库
    #    阶段 B（CDN 下载，高并发）：按已解析链接并行下载 docx/pdf 并入库
    state = load_state()
    if refresh:
        state["done"] = {}
        state["urls"] = {}
    todo = [c for c in catalog if c["bbbs"] not in state["done"]]
    if max_n is not None:
        todo = todo[:max_n]
    api_workers = 2
    if "--workers" in argv:
        api_workers = max(1, min(6, int(argv[argv.index("--workers") + 1])))
    dl_workers = 6
    if "--dl-workers" in argv:
        dl_workers = max(1, min(12, int(argv[argv.index("--dl-workers") + 1])))
    polite = "--polite" in argv  # 慢速模式：降低触发官网反爬的概率
    print("== 第 3 步：两阶段下载（共 %d 部待处理；官方接口 %d 并发，CDN 下载 %d 并发）=="
          % (len(todo), api_workers, dl_workers), flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ok = fail = 0
    lock = threading.Lock()

    def _finish(item, doc=None, err=None):
        nonlocal ok, fail
        with lock:
            if doc is not None:
                state["done"][item["bbbs"]] = True
                state["failed"].pop(item["bbbs"], None)
                ok += 1
                print("[%d/%d] 《%s》 %d 条" % (ok + fail, len(todo), doc["name"],
                                                len(doc["articles"])), flush=True)
            else:
                state["failed"][item["bbbs"]] = "%s: %s" % (item["title"], err)[:200]
                fail += 1
                print("[%d/%d] 失败《%s》: %s" % (ok + fail, len(todo), item["title"], err),
                      flush=True)
            if (ok + fail) % 10 == 0:
                save_state(state)

    def _pass_a(item):
        """阶段 A：官方接口解析下载链接（绕过详情接口，避开 WAF）。"""
        urls = resolve_urls(item["bbbs"])
        with lock:
            state["urls"][item["bbbs"]] = urls
            if len(state["urls"]) % 20 == 0:
                save_state(state)
        if polite:
            time.sleep(3)
        return None

    # ---- 阶段 A ----
    need_a = [c for c in todo if c["bbbs"] not in state["urls"]]
    if need_a:
        print("[阶段A] 解析详情与下载链接：%d 部…" % len(need_a), flush=True)
        if api_workers > 1 and len(need_a) > 1:
            with ThreadPoolExecutor(max_workers=api_workers) as pool:
                futures = {pool.submit(_pass_a, item): item for item in need_a}
                for fut in as_completed(futures):
                    item = futures[fut]
                    try:
                        doc = fut.result()
                        if doc is not None:
                            _finish(item, doc=doc)
                    except Exception as e:
                        _finish(item, err=e)
        else:
            for item in need_a:
                try:
                    doc = _pass_a(item)
                    if doc is not None:
                        _finish(item, doc=doc)
                except Exception as e:
                    _finish(item, err=e)
                time.sleep(0.2)
        with lock:
            save_state(state)

    # ---- 阶段 B ----
    todo_b = [c for c in todo if c["bbbs"] not in state["done"]]
    if todo_b:
        print("[阶段B] 并行下载全文：%d 部…" % len(todo_b), flush=True)

        def _pass_b(item):
            urls = state["urls"].get(item["bbbs"])
            return build_one(item, urls=urls or None)

        if dl_workers > 1 and len(todo_b) > 1:
            with ThreadPoolExecutor(max_workers=dl_workers) as pool:
                futures = {pool.submit(_pass_b, item): item for item in todo_b}
                for fut in as_completed(futures):
                    item = futures[fut]
                    try:
                        _finish(item, doc=fut.result())
                    except Exception as e:
                        _finish(item, err=e)
        else:
            for item in todo_b:
                try:
                    _finish(item, doc=_pass_b(item))
                except Exception as e:
                    _finish(item, err=e)
                time.sleep(0.1)
    with lock:
        save_state(state)
    print("下载完成：成功 %d，失败 %d。" % (ok, fail), flush=True)

    # 4) 合并 manifest
    m = merge_manifest()
    print("== 完成：法律库共 %d 部，共 %d 条。==" % (
        len(m["laws"]), sum(l["article_count"] for l in m["laws"])), flush=True)


if __name__ == "__main__":
    main()
