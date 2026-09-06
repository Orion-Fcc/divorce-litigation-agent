# -*- coding: utf-8 -*-
"""
向量检索模块（本地嵌入，不依赖任何外部服务）。

- 模型：bge-small-zh-v1.5（量化 ONNX，约 24MB），首次运行自动从 hf-mirror 下载
- 推理：ONNX Runtime CPU，CLS 池化 + L2 归一化
- 向量库：vectors.npy（float16）+ refs.json + 法律库指纹（法律库变更自动重建）
- 检索：余弦相似度（numpy 矩阵乘，毫秒级），与倒排索引混合使用
- 降级：模型/运行库缺失时自动停用，主流程退回纯倒排索引，不影响可用性
"""
import hashlib
import json
import os
import re
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "models", "bge-small-zh")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_DIR = os.path.join(BASE_DIR, "legal_db")
VEC_FILE = os.path.join(DATA_DIR, "law_vectors.npy")
REF_FILE = os.path.join(DATA_DIR, "law_vec_refs.json")
FINGER_FILE = os.path.join(DATA_DIR, "law_vec_finger.txt")

MODEL_URL = ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/"
             "onnx/model_quantized.onnx")
VOCAB_URL = ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/vocab.txt")

_sess = None
_vocab = None
_ready = False
_vectors = None
_refs = []
_build_lock = threading.Lock()
_build_done = False


# ---------------------------------------------------------------- 模型准备
def _download(url, path):
    import requests
    requests.packages.urllib3.disable_warnings()
    r = requests.get(url, stream=True, timeout=300, verify=False,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
    os.replace(tmp, path)


def ensure_model():
    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, "model_quantized.onnx")
    vocab_path = os.path.join(MODEL_DIR, "vocab.txt")
    try:
        if not os.path.exists(model_path) or os.path.getsize(model_path) < 1000:
            print("[向量检索] 首次使用，正在下载本地嵌入模型（约 24MB）…")
            _download(MODEL_URL, model_path)
        if not os.path.exists(vocab_path) or os.path.getsize(vocab_path) < 1000:
            _download(VOCAB_URL, vocab_path)
        return True
    except Exception as e:
        print("[向量检索] 模型下载失败（已降级为关键词检索）:", e)
        return False


def _load_model():
    global _sess, _vocab, _ready
    if _ready:
        return True
    try:
        import onnxruntime
    except ImportError:
        return False
    if not ensure_model():
        return False
    try:
        model_path = os.path.join(MODEL_DIR, "model_quantized.onnx")
        import onnxruntime
        so = onnxruntime.SessionOptions()
        so.intra_op_num_threads = 2  # 限制线程，避免建索引时占满 CPU 影响对话
        so.inter_op_num_threads = 1
        _sess = onnxruntime.InferenceSession(model_path, so,
                                             providers=["CPUExecutionProvider"])
        _vocab = {}
        with open(os.path.join(MODEL_DIR, "vocab.txt"), encoding="utf-8") as f:
            for i, line in enumerate(f):
                _vocab[line.strip()] = i
        _ready = True
        return True
    except Exception as e:
        print("[向量检索] 模型加载失败（已降级为关键词检索）:", e)
        return False


# ---------------------------------------------------------------- 编码
def _tokenize(text, max_len=512):
    """中文按字符查词表（BERT 词表含常用汉字），未知字符用 [UNK]。"""
    ids = [_vocab.get("[CLS]", 101)]
    for ch in text[: max_len - 2]:
        if ch in _vocab:
            ids.append(_vocab[ch])
        else:
            ids.append(_vocab.get("[UNK]", 100))
    ids.append(_vocab.get("[SEP]", 102))
    return ids


def embed_texts(texts, batch=32):
    """批量编码，返回 float16 归一化向量 [n, dim]。"""
    import numpy as np
    if not _load_model():
        return None
    out = []
    for i in range(0, len(texts), batch):
        chunk = [_tokenize(t) for t in texts[i:i + batch]]
        max_len = max(len(c) for c in chunk)
        ids = np.zeros((len(chunk), max_len), dtype=np.int64)
        mask = np.zeros((len(chunk), max_len), dtype=np.int64)
        for j, c in enumerate(chunk):
            ids[j, :len(c)] = c
            mask[j, :len(c)] = 1
        token_type = np.zeros_like(ids)
        res = _sess.run(None, {"input_ids": ids, "attention_mask": mask,
                               "token_type_ids": token_type})
        hidden = res[0][:, 0, :]  # CLS 池化
        norm = np.linalg.norm(hidden, axis=1, keepdims=True) + 1e-9
        out.append((hidden / norm).astype(np.float16))
    return np.vstack(out)


# ---------------------------------------------------------------- 向量库
def _fingerprint():
    h = hashlib.md5()
    for fn in sorted(os.listdir(DB_DIR)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(DB_DIR, fn)
        st = os.stat(fp)
        h.update(("%s|%d|%d;" % (fn, st.st_mtime_ns, st.st_size)).encode("utf-8"))
    return h.hexdigest()


def _load_cache():
    global _vectors, _refs
    try:
        with open(FINGER_FILE, encoding="utf-8") as f:
            if f.read().strip() != _fingerprint():
                return False
        import numpy as np
        _vectors = np.load(VEC_FILE)
        with open(REF_FILE, encoding="utf-8") as f:
            _refs = json.load(f)
        return len(_vectors) == len(_refs) > 0
    except Exception:
        return False


def build_index(laws, force=False, progress_cb=None):
    """为全部法条建立向量索引（增量缓存，法律库不变则跳过）。"""
    global _vectors, _refs, _build_done
    if not _build_lock.acquire(blocking=False):
        return False
    try:
        if not force and _load_cache():
            _build_done = True
            return True
        import legal_db  # noqa: F401  确保法律库已加载
        articles, refs = [], []
        for law in laws or legal_db._laws:
            for art in law.get("articles", []):
                if not art.get("text"):
                    continue
                articles.append(art["text"])
                refs.append({"law_id": law["id"], "marker": art.get("marker", "")})
        if not articles:
            return False
        fp_before = _fingerprint()
        print("[向量检索] 开始为 %d 条法条建立向量索引（首次约需几分钟，之后走缓存）…"
              % len(articles))
        vecs = embed_texts(articles)
        if vecs is None:
            return False
        # 构建期间法律库若发生变化（如全量更新中），本次结果作废，下次启动重建
        if _fingerprint() != fp_before:
            print("[向量检索] 法律库在构建期间发生变化，本次索引作废（下次启动自动重建）。")
            return False
        os.makedirs(DATA_DIR, exist_ok=True)
        import numpy as np
        np.save(VEC_FILE, vecs)
        with open(REF_FILE, "w", encoding="utf-8") as f:
            json.dump(refs, f, ensure_ascii=False)
        with open(FINGER_FILE, "w", encoding="utf-8") as f:
            f.write(fp_before)
        _vectors, _refs = vecs, refs
        _build_done = True
        print("[向量检索] 向量索引构建完成。")
        return True
    except Exception as e:
        print("[向量检索] 索引构建失败（已降级为关键词检索）:", e)
        return False
    finally:
        _build_lock.release()


def start_build(laws=None, delay=90):
    """后台建立向量索引（延迟启动，避免与应用启动抢 CPU 影响首条对话）。"""
    def _run():
        time.sleep(delay)
        build_index(laws)
    threading.Thread(target=_run, daemon=True).start()


def is_ready():
    return _build_done and _vectors is not None


def query(text, top_k=10):
    """语义检索：返回 [{law_id, marker, score}]（score 0~1）。"""
    if not is_ready():
        if not _load_cache():
            return []
    import numpy as np
    q = embed_texts([text], batch=1)
    if q is None:
        return []
    sims = (_vectors.astype(np.float32) @ q[0].astype(np.float32).T)[:, 0]
    idx = np.argsort(-sims)[:top_k]
    out = []
    for i in idx:
        s = float(sims[i])
        if s < 0.25:
            continue
        out.append({"law_id": _refs[i]["law_id"], "marker": _refs[i]["marker"],
                    "score": s})
    return out
