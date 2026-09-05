# -*- coding: utf-8 -*-
"""
从官方来源下载婚姻家事相关法律文本，构建本地法律库（JSON）。

用法：
    python tools/build_legal_db.py            # 下载并重建全部
    python tools/build_legal_db.py --check    # 只校验本地库完整性

数据来源均为政府/法院官方网站（见 SOURCES 中每条 source_url）。
法律文本可能修订，建议定期重新运行本脚本更新。
"""
import json
import os
import re
import sys
import time
from html import unescape
from html.parser import HTMLParser

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, "legal_db")

# ---------------------------------------------------------------- 法律清单
# article_range: [起, 止]（中文数字条号转整数后的闭区间），None 表示全部
# start_after: 从该字符串首次出现之后开始扫描条文（跳过新闻导语/目录等）
SOURCES = [
    {
        "id": "minfadian-hunyin",
        "name": "中华人民共和国民法典·婚姻家庭编（第五编）",
        "short": "民法典·婚姻家庭编",
        "source_name": "湖南省人民政府门户网站（转载全国人大通过文本）",
        "source_url": "https://www.hunan.gov.cn/zqt/zcsd/202005/t20200528_13751405.html",
        "publish": "2020-05-28",
        "effective": "2021-01-01",
        "start_after": "第一千零四十条",
        "article_range": [1040, 1118],
    },
    {
        "id": "jieshi-1",
        "name": "最高人民法院关于适用《中华人民共和国民法典》婚姻家庭编的解释（一）",
        "short": "婚姻家庭编司法解释（一）",
        "source_name": "最高人民法院官网（法释〔2020〕22号）",
        "source_url": "https://www.court.gov.cn/zixun/xiangqing/282071.html",
        "publish": "2020-12-29",
        "effective": "2021-01-01",
        "start_after": "制定本解释",
        "article_range": None,
    },
    {
        "id": "jieshi-2",
        "name": "最高人民法院关于适用《中华人民共和国民法典》婚姻家庭编的解释（二）",
        "short": "婚姻家庭编司法解释（二）",
        "source_name": "西藏自治区司法厅官网（转载法释〔2025〕1号）",
        "source_url": "https://sft.xizang.gov.cn/xwzx/xwfbh/202501/t20250116_457574.html",
        "publish": "2025-01-15",
        "effective": "2025-02-01",
        "start_after": "法释〔2025〕1号",
        "article_range": None,
    },
    {
        "id": "caili-guiding",
        "name": "最高人民法院关于审理涉彩礼纠纷案件适用法律若干问题的规定",
        "short": "涉彩礼纠纷规定",
        "source_name": "最高人民法院官网（法释〔2024〕1号）",
        "source_url": "https://www.court.gov.cn/zixun/xiangqing/423442.html",
        "publish": "2024-01-17",
        "effective": "2024-02-01",
        "start_after": "制定本规定",
        "article_range": None,
    },
    {
        "id": "fanjiabaoli-fa",
        "name": "中华人民共和国反家庭暴力法",
        "short": "反家庭暴力法",
        "source_name": "中国人大网",
        "source_url": "http://www.npc.gov.cn/c2/c10134/201905/t20190521_260193.html",
        "publish": "2015-12-27",
        "effective": "2016-03-01",
        "start_after": None,
        "article_range": None,
    },
    {
        "id": "funv-quanyi-fa",
        "name": "中华人民共和国妇女权益保障法（2022年修订）",
        "short": "妇女权益保障法",
        "source_name": "深圳市人力资源和社会保障局官网（转载2022年修订文本）",
        "source_url": "https://hrss.sz.gov.cn/ztfw/xzzfgs/sqgk/yjxx/content/post_11035339.html",
        "publish": "2022-10-30",
        "effective": "2023-01-01",
        "start_after": None,
        "article_range": None,
    },
    {
        "id": "weichengnianren-fa",
        "name": "中华人民共和国未成年人保护法（2020年修订）",
        "short": "未成年人保护法",
        "source_name": "国家互联网信息办公室官网（转载新华社文本）",
        "source_url": "https://www.cac.gov.cn/2020-10/22/c_1604928959588622.htm",
        "publish": "2020-10-17",
        "effective": "2021-06-01",
        "start_after": None,
        "article_range": None,
    },
    {
        "id": "hunyin-dengji-tiaoli",
        "name": "婚姻登记条例（2025年修订）",
        "short": "婚姻登记条例",
        "source_name": "吉安县人民法院官网（转载国务院令第804号）",
        "source_url": "https://jaxfy.jxfy.gov.cn/article/detail/2025/04/id/8792886.shtml",
        "publish": "2025-04-06",
        "effective": "2025-05-10",
        "start_after": "第二次修订",
        "article_range": None,
    },
    {
        "id": "minshi-susong-fa",
        "name": "中华人民共和国民事诉讼法（2023年修正）",
        "short": "民事诉讼法",
        "source_name": "最高人民法院国际商事法庭官网（来源：国家法律法规数据库）",
        "source_url": "https://cicc.court.gov.cn/html/1/218/62/83/443.html",
        "publish": "2023-09-01",
        "effective": "2024-01-01",
        "start_after": None,
        "article_range": None,
    },
    # ================= 新增：更多婚姻家庭相关法律 =================
    {
        "id": "minfadian-jicheng",
        "name": "中华人民共和国民法典·继承编（第六编）",
        "short": "民法典·继承编",
        "source_name": "湖南省人民政府门户网站（转载全国人大通过文本）",
        "source_url": "https://www.hunan.gov.cn/zqt/zcsd/202005/t20200528_13751405.html",
        "publish": "2020-05-28",
        "effective": "2021-01-01",
        "start_after": "第一千一百一十九条",
        "article_range": [1119, 1163],
    },
    {
        "id": "jicheng-jieshi-1",
        "name": "最高人民法院关于适用《中华人民共和国民法典》继承编的解释（一）",
        "short": "继承编司法解释（一）",
        "source_name": "最高人民法院官网（法释〔2020〕23号）",
        "source_url": "https://www.court.gov.cn/zixun/xiangqing/282091.html",
        "publish": "2020-12-29",
        "effective": "2021-01-01",
        "start_after": "制定本解释",
        "article_range": None,
    },
    {
        "id": "jiating-jiaoyu-fa",
        "name": "中华人民共和国家庭教育促进法",
        "short": "家庭教育促进法",
        "source_name": "成都市成华区人民检察院官网（转载主席令第98号）",
        "source_url": "http://www.cdchjcy.gov.cn/wcnrfzyf/236843.jhtml",
        "publish": "2021-10-23",
        "effective": "2022-01-01",
        "start_after": "全文如下",
        "article_range": None,
    },
    {
        "id": "renshen-anquan-baohuling",
        "name": "最高人民法院关于办理人身安全保护令案件适用法律若干问题的规定",
        "short": "人身安全保护令规定",
        "source_name": "最高人民法院官网（法释〔2022〕17号）",
        "source_url": "https://www.court.gov.cn/fabu/xiangqing/366021.html",
        "publish": "2022-07-14",
        "effective": "2022-08-01",
        "start_after": "制定本规定",
        "article_range": None,
    },
    {
        "id": "xingfa-hunyin",
        "name": "中华人民共和国刑法·婚姻家庭相关条文（第257-262条节选）",
        "short": "刑法·婚姻家庭条文",
        "source_name": "司法部普法平台（12348陕西法网，转载刑法全文，节选）",
        "source_url": "http://ya.sn.12348.gov.cn/zixundayi/578.html",
        "publish": "2020-12-26",
        "effective": "2021-03-01",
        "start_after": "第二百五十六条",
        "article_range": [257, 262],
    },
    {
        "id": "susongfei-banfa",
        "name": "诉讼费用交纳办法（国务院令第481号）",
        "short": "诉讼费用交纳办法",
        "source_name": "中国政府网",
        "source_url": "https://www.gov.cn/ziliao/flfg/2006-12/29/content_483682.htm",
        "publish": "2006-12-19",
        "effective": "2007-04-01",
        "start_after": "诉讼费用交纳办法",
        "article_range": None,
    },
    {
        "id": "laonianren-quanyi-fa",
        "name": "中华人民共和国老年人权益保障法（2018年修正）",
        "short": "老年人权益保障法",
        "source_name": "介休市人民政府门户网站（2018年修正本）",
        "source_url": "https://www.jiexiu.gov.cn/Upload/main/InfoPublicity/PublicInformation/File/2021/05/27/202105271149166153.pdf",
        "publish": "2018-12-29",
        "effective": "2018-12-29",
        "start_after": None,
        "article_range": None,
        "inline_articles": True,   # PDF 文本无换行，需放宽「第X条」匹配
    },
    {
        "id": "muying-baojian-fa",
        "name": "中华人民共和国母婴保健法（2017年修正）",
        "short": "母婴保健法",
        "source_name": "中国政府网",
        "source_url": "https://www.gov.cn/guoqing/2021-10/29/content_5647619.htm",
        "publish": "2017-11-04",
        "effective": "2017-11-05",
        "start_after": None,
        "article_range": None,
    },
    {
        "id": "hunyin-dengji-guifan",
        "name": "婚姻登记工作规范（民发〔2015〕230号）",
        "short": "婚姻登记工作规范",
        "source_name": "中国政府网（民政部文件）",
        "source_url": "https://www.gov.cn/zhengce/zhengceku/2015-12/12/content_5554664.htm",
        "publish": "2015-12-08",
        "effective": "2015-12-08",
        "start_after": "婚姻登记工作规范",
        "article_range": None,
    },
]

# 页面正文中出现以下词时，视为正文结束（其后为页脚/相关链接等噪音）
CUT_MARKERS = ["责任编辑", "【纠错】", "我要纠错", "网站纠错", "分享到", "相关新闻",
               "相关阅读", "打印本页", "扫一扫", "友情链接", "ICP备案", "版权所有"]

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}
# 条文起始标记必须出现在行首，避免把正文里引用的「民法典第一千零五十一条」误当新条文
# 支持「第X条之一」类条文（如刑法第二百六十条之一）
ARTICLE_RE = re.compile(
    r"(?m)^(第[零一二两三四五六七八九十百千]+条(?:之[一二三四五六七八九十]+)?)\s*")
# PDF 提取文本无换行时的宽松版（仅对标记 inline_articles 的来源启用）
ARTICLE_RE_INLINE = re.compile(
    r"(第[零一二两三四五六七八九十百千]+条(?:之[一二三四五六七八九十]+)?)\s*")
# 纯页码行（PDF 提取常见噪音）
PAGE_NO_RE = re.compile(r"^\s*[-—]?\s*\d+\s*[-—]?\s*$")


def cn_to_int(s):
    """中文数字（如 一千零四十 / 二百九十一 / 三十八）转整数，支持 1-9999。"""
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


class TextExtractor(HTMLParser):
    """极简 HTML 正文提取：跳过 script/style，块级标签换行。"""

    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "section", "article", "header", "footer", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif not self.skip and tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        elif not self.skip and tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        raw = unescape("".join(self.parts))
        lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in raw.split("\n")]
        return "\n".join(ln for ln in lines if ln)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")


def _decode(data, ctype=""):
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    encodings = ([m.group(1)] if m else []) + ["utf-8", "gb18030"]
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _fetch_curl(url):
    """urllib 失败时的兜底：调用系统 curl（兼容某些政府的非常规 TLS 配置）。"""
    import subprocess
    import tempfile
    jar = os.path.join(tempfile.gettempdir(), "flk_cookies.txt")
    out = subprocess.run(
        ["curl", "-sL", "--max-time", "90", "--max-redirs", "10",
         "-c", jar, "-b", jar, "-A", UA, "-w", "\n%{content_type}", url],
        capture_output=True, timeout=120)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError("curl 下载失败，returncode=%s" % out.returncode)
    body, _, ctype = out.stdout.rpartition(b"\n")
    return _decode(body, ctype.decode("ascii", errors="replace"))


def _pdf_to_text(data):
    import io
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF 来源需要 pypdf：pip install -r requirements.txt")
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def _download_bytes(url):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def _fetch_pdf_curl(url):
    import subprocess
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "flk_doc.pdf")
    out = subprocess.run(["curl", "-sL", "--max-time", "120", "--max-redirs", "10",
                          "-A", UA, "-o", tmp, url], capture_output=True, timeout=150)
    if out.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("curl 下载 PDF 失败，returncode=%s" % out.returncode)
    try:
        with open(tmp, "rb") as f:
            return _pdf_to_text(f.read())
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def fetch(url):
    import urllib.request
    if url.lower().endswith(".pdf"):
        try:
            data, _ = _download_bytes(url)
        except Exception:
            return _fetch_pdf_curl(url)
        return _pdf_to_text(data)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
        return _decode(data, ctype)
    except Exception:
        return _fetch_curl(url)


def _art_no(marker):
    """从「第二百六十条之一」提取条号整数 260。"""
    core = marker[1:] if marker.startswith("第") else marker
    core = re.sub(r"之[一二三四五六七八九十]+$", "", core)
    core = core.replace("条", "")
    return cn_to_int(core)


def parse_articles(text, article_range=None, inline=False):
    """把正文按「第X条」切分为条文列表。"""
    regex = ARTICLE_RE_INLINE if inline else ARTICLE_RE
    pieces = regex.split(text)
    articles = []
    i = 1  # pieces[0] 是第一条之前的内容
    while i + 1 < len(pieces) + 1 and i < len(pieces):
        marker = pieces[i]
        body = pieces[i + 1] if i + 1 < len(pieces) else ""
        no = _art_no(marker)
        if no is None:
            i += 2
            continue
        body = re.sub(r"\s*\n\s*", "\n", body).strip()
        # 剔除粘连在上一条正文末尾的「第X编/章/节」标题行
        body = "\n".join(
            l for l in body.split("\n")
            if not (re.match(r"^第[零一二三四五六七八九十百千]+[编章节][\s　]", l.strip())
                    and len(l.strip()) <= 30 and "。" not in l)
            and not PAGE_NO_RE.match(l)
        ).strip()
        if article_range and not (article_range[0] <= no <= article_range[1]):
            i += 2
            continue
        articles.append({"no": no, "marker": marker, "text": (marker + " " + body).strip()})
        i += 2
    # 去掉条号重复的（页面可能重复收录；按 marker 去重以保留「条之一」）
    seen, uniq = set(), []
    for a in articles:
        if a["marker"] in seen:
            continue
        seen.add(a["marker"])
        uniq.append(a)
    return uniq


def build_one(src):
    last_err = None
    for attempt in range(4):
        try:
            html = fetch(src["source_url"]) if attempt < 2 else _fetch_curl(src["source_url"])
            ex = TextExtractor()
            ex.feed(html)
            text = ex.text()
            if src.get("start_after"):
                idx = text.find(src["start_after"])
                if idx == -1:
                    raise RuntimeError("start_after 未找到: %s" % src["start_after"])
                text = text[idx:]
            # 切掉页脚噪音（只认第一条正文之后出现的标记，避免页头导航误切）
            first_art = ARTICLE_RE.search(text)
            min_pos = first_art.start() if first_art else 0
            cut_positions = [p for p in (text.find(m) for m in CUT_MARKERS) if p > min_pos]
            if cut_positions:
                text = text[:min(cut_positions)]
            articles = parse_articles(text, src.get("article_range"),
                                      inline=bool(src.get("inline_articles")))
            if not articles:
                raise RuntimeError("未解析到任何条文")
            return articles
        except Exception as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise last_err


def check_db():
    path = os.path.join(DB_DIR, "manifest.json")
    if not os.path.exists(path):
        print("法律库不存在，请先运行 python tools/build_legal_db.py")
        return 1
    manifest = json.load(open(path, encoding="utf-8"))
    ok = True
    for law in manifest["laws"]:
        fp = os.path.join(DB_DIR, law["id"] + ".json")
        if not os.path.exists(fp):
            print("[缺失]", law["id"])
            ok = False
            continue
        data = json.load(open(fp, encoding="utf-8"))
        print("[OK] %-22s %3d 条  %s" % (law["id"], len(data["articles"]), law["name"]))
    return 0 if ok else 1


def main():
    if "--check" in sys.argv:
        sys.exit(check_db())
    only = []
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if idx + 1 < len(sys.argv):
            only = [x.strip() for x in sys.argv[idx + 1].split(",") if x.strip()]
    targets = [s for s in SOURCES if (not only or s["id"] in only)]
    os.makedirs(DB_DIR, exist_ok=True)

    # 部分构建时合并已有法律（不丢失其余条目），最终按 SOURCES 顺序输出
    existing = {}
    mpath = os.path.join(DB_DIR, "manifest.json")
    if only and os.path.exists(mpath):
        try:
            existing = {l["id"]: l for l in json.load(open(mpath, encoding="utf-8"))["laws"]}
        except Exception:
            existing = {}

    built = {}
    for src in targets:
        print("下载: %s ..." % src["short"], flush=True)
        try:
            articles = build_one(src)
        except Exception as e:
            print("  [失败] %s: %s" % (src["id"], e))
            continue
        first, last = articles[0], articles[-1]
        print("  %d 条（%s ~ %s）" % (len(articles), first["marker"], last["marker"]))
        doc = dict(src)
        doc.pop("start_after", None)
        doc.pop("inline_articles", None)
        doc["articles"] = articles
        with open(os.path.join(DB_DIR, src["id"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        m = dict(src)
        m.pop("start_after", None)
        m.pop("inline_articles", None)
        m["article_count"] = len(articles)
        built[src["id"]] = m
        time.sleep(1)  # 友好抓取

    all_laws = dict(existing)
    all_laws.update(built)
    manifest = {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "laws": [all_laws[s["id"]] for s in SOURCES if s["id"] in all_laws]}
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print("完成，共 %d 部（本次构建 %d 部）。" % (len(manifest["laws"]), len(built)))


if __name__ == "__main__":
    main()
