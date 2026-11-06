"""환각·누락 감지와 재전사 결과 고르기 (GPU 없이 동작. 재전사 자체는 pipeline.py가 함).

감지 (detect):
  환각 의심 세그먼트
    repeat       같은 단어·구절이 연속 3번 이상 반복되고 문장의 절반 이상을 차지 ("term term term")
    dup          바로 앞·뒤 세그먼트와 같은 문장 (같은 영어 문장 10번 반복 등)
    prompt       Whisper 프롬프트·핫워드 문구가 그대로 나옴 (연속 3단어 이상이 문장의 60% 이상)
    english      language=ko인데 한글 없이 영어 단어만 4개 이상 (한국어 설명을 번역한 출력 의심)
    compression  compression_ratio 과다
    phrase       "시청해주셔서 감사합니다" 같은 알려진 환각 문구
    numbers      숫자·쉼표가 대부분이고 단어가 거의 없음 ("Q1, 2, 3, … 100, 100" 30초. 숫자가 계속 바뀌어 repeat에 안 걸림)
    counting     1씩 늘어나는 숫자 세기가 길게 이어짐 ("1, 2, 3, …, 38")
    same_token   길이에 비해 같은 형태의 토큰(숫자는 #)이 지나치게 많음 ("term 1, term 2, …")
  누락 의심 구간 (소리가 있을 때만)
    gap          세그먼트 사이 간격이 4초 이상
    sparse       8초 이상인 세그먼트에 글자가 너무 적음 (초당 1.2자 미만, "term" 32초 등)
가까운 의심 구간은 하나로 합친다 → region.

복원 (choose): region마다 재전사 결과 여러 개(프롬프트 없이 / 이번 강의 프롬프트로)를 같은 환각 검사로 거른 뒤
  통과한 것 중 한국어 > 영어, 평균 logprob 높은 순으로 고른다.
  영어로만 나온 것만 의심인 구간은 재전사로도 한국어가 안 나오면 실제 영어 발화(슬라이드 낭독)로 보고 원래 문장을 유지한다.
  결과: restored(복원) · kept(원래 문장 유지) · dropped(소리 없는 구간의 환각이라 지움) · missing(청취 불가)
"""
import re

import numpy as np

SR = 16000
FRAME = 0.05  # 소리 크기 계산 단위 (초)

_HANGUL = re.compile(r"[가-힣]")
_TOKEN = re.compile(r"[0-9A-Za-z가-힣#]+")
_LATIN_WORD = re.compile(r"^(?=.*[a-z].*[a-z])[a-z#]+$")  # 영어 단어 (알파벳 2자 이상, "x"·"n" 같은 기호는 제외)
REASON_TEXT = {"repeat": "같은 말 반복", "dup": "앞뒤 문장과 같음", "prompt": "프롬프트·핫워드 누출",
               "english": "영어로만 출력(번역 의심)", "compression": "압축률 과다", "phrase": "알려진 환각 문구",
               "numbers": "숫자 나열", "counting": "숫자 세기", "same_token": "같은 형태 토큰 과다",
               "gap": "간격(소리 있음)", "sparse": "길이에 비해 글자가 적음"}
HALLUCINATION = ("repeat", "dup", "prompt", "english", "compression", "phrase", "numbers", "counting", "same_token")


# ---------- 텍스트 검사 ----------

def tokens(text):
    """비교용 단어 목록: 소문자, 숫자는 #, 문장부호 제거."""
    return _TOKEN.findall(re.sub(r"\d+", "#", (text or "").lower()))


def _norm(text):
    return " ".join(tokens(text))


def repeated_run(toks, min_times=3, max_n=8):
    """같은 n-gram(n ≤ max_n)이 연속 min_times번 이상 반복되는 가장 긴 구간의 단어 수. 숫자만 있는 단위("2, 8, 7, 1")는 제외."""
    best, total = 0, len(toks)
    for n in range(1, max_n + 1):
        i = 0
        while i + n * min_times <= total:
            unit = toks[i:i + n]
            if all(t == "#" for t in unit):
                i += 1
                continue
            k = 1
            while toks[i + k * n:i + (k + 1) * n] == unit:
                k += 1
            if k >= min_times:
                best = max(best, k * n)
                i += k * n
            else:
                i += 1
    return best


def counting_run(text):
    """1씩 늘어나는 숫자가 연속으로 나오는 가장 긴 길이 ("Q1, 2, 3, 4" → 4). 숫자 사이에 낱말이 끼면 끊긴다."""
    best = run = 0
    prev = None
    for t in re.findall(r"\d+|[A-Za-z가-힣]{2,}", text or ""):
        if not t.isdigit():
            prev, run = None, 0
            continue
        n = int(t)
        run = run + 1 if prev is not None and n == prev + 1 else 1
        prev = n
        best = max(best, run)
    return best


def number_flags(text, s):
    """숫자 나열 환각 검사: numbers / counting / same_token 중 해당하는 것."""
    raw = _TOKEN.findall(text or "")
    out = []
    if not raw:
        return out
    digits = sum(1 for t in re.findall(r"[0-9A-Za-z가-힣]+", text) if re.fullmatch(r"[A-Za-z]?\d+", t))
    if digits >= s["numbers_min_count"] and digits >= len(raw) * s["numbers_min_ratio"]:
        out.append("numbers")
    if counting_run(text) >= s["counting_min_run"]:
        out.append("counting")
    toks = tokens(text)
    if len(toks) >= s["same_token_min_count"]:
        top = max(toks.count(t) for t in set(toks))
        if top >= len(toks) * s["same_token_ratio"]:
            out.append("same_token")
    return out


def _longest_common_run(a, b):
    """두 단어 목록에 공통으로 연속해서 나오는 가장 긴 구간의 길이."""
    if not a or not b:
        return 0
    prev, best = [0] * (len(b) + 1), 0
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def is_english_only(text, min_words):
    toks = tokens(text)
    return not _HANGUL.search(text or "") and sum(1 for t in toks if _LATIN_WORD.match(t)) >= min_words


def text_reasons(seg, s, language="ko", prompt_texts=()):
    """세그먼트 하나만 보고 알 수 있는 환각 의심 이유 목록 (dup은 detect에서 따로)."""
    text = seg.get("text") or ""
    toks = tokens(text)
    out = []
    if toks and repeated_run(toks, s["repeat_min_times"]) >= max(4, len(toks) * 0.5):
        out.append("repeat")
    words = [t for t in toks if t != "#"]  # 숫자 목록("2, 8, 7")이 "Lecture 1, 2, 3"과 겹친다고 보지 않도록
    for p in prompt_texts:
        run = _longest_common_run(words, [t for t in tokens(p) if t != "#"])
        if run >= 3 and run >= len(words) * 0.6:
            out.append("prompt")
            break
    if language == "ko" and is_english_only(text, s["english_min_words"]):
        out.append("english")
    cr = seg.get("compression_ratio")
    if cr is not None and cr > s["hallucination_compression"]:
        out.append("compression")
    flat = re.sub(r"\s+", "", text)
    if any(re.sub(r"\s+", "", ph) in flat for ph in s["hallucination_phrases"] if ph.strip()):
        out.append("phrase")
    out += number_flags(text, s)
    return out


def dup_flags(segs):
    """앞뒤 세그먼트와 같은 문장(3단어 이상 또는 8자 이상)인 세그먼트 번호(목록 위치)."""
    norms = [_norm(x.get("text")) for x in segs]
    out = set()
    for k in range(1, len(segs)):
        a = norms[k]
        if a and a == norms[k - 1] and (len(a.split()) >= 3 or len(a.replace(" ", "")) >= 8):
            out |= {k - 1, k}
    return out


# ---------- 소리 크기 ----------

class AudioLevels:
    """50ms 단위 소리 크기(dB). 녹음 전체의 바닥 소음(하위 10%)보다 voice_margin_db 이상 크면 '소리 있음'."""

    def __init__(self, audio, margin_db=12.0):
        n = int(FRAME * SR)
        k = len(audio) // n
        x = np.asarray(audio[:k * n], dtype=np.float32).reshape(k, n) if k else np.zeros((1, n), np.float32)
        rms = np.sqrt(np.mean(np.square(x), axis=1)) + 1e-9
        self.db = 20 * np.log10(rms)
        self.floor = float(np.percentile(self.db, 10))
        self.voiced = self.db > self.floor + margin_db
        self.duration = len(audio) / SR

    def voiced_ratio(self, a, b):
        i, j = max(0, int(a / FRAME)), min(len(self.voiced), int(b / FRAME))
        return float(self.voiced[i:j].mean()) if j > i else 0.0


# ---------- 감지 ----------

def detect(segments, levels, s, language="ko", prompt_texts=()):
    """환각·누락 의심 구간 목록. region = {id, start, end, reasons, segs(원래 세그먼트 idx), halluc, sparse, voiced}."""
    prompt_texts = [p for p in prompt_texts if p and p.strip()]
    items = []  # (start, end, reason, idx, is_hallucination)
    dups = dup_flags(segments)
    for k, seg in enumerate(segments):
        rs = text_reasons(seg, s, language, prompt_texts) + (["dup"] if k in dups else [])
        for r in rs:
            items.append((seg["start"], seg["end"], r, seg["idx"], True))
        dur = seg["end"] - seg["start"]
        chars = len(re.sub(r"\s+", "", seg.get("text") or ""))
        if (not rs and dur >= s["sparse_min_seconds"] and chars / dur < s["sparse_max_cps"]
                and levels.voiced_ratio(seg["start"], seg["end"]) >= s["voiced_min_ratio"]):
            items.append((seg["start"], seg["end"], "sparse", seg["idx"], False))
    bounds = [0.0] + [x for seg in segments for x in (seg["start"], seg["end"])] + [levels.duration]
    for a, b in zip(bounds[0::2], bounds[1::2]):  # 처음~첫 세그먼트, 세그먼트 사이, 마지막~끝
        if b - a >= s["gap_min_seconds"] and levels.voiced_ratio(a, b) >= s["voiced_min_ratio"]:
            items.append((a, b, "gap", None, False))

    items.sort(key=lambda x: (x[0], x[1]))
    regions = []
    for a, b, r, idx, hal in items:
        cur = regions[-1] if regions else None
        if cur and a <= cur["end"] + s["region_join_seconds"]:
            cur["end"] = max(cur["end"], b)
        else:
            cur = {"start": a, "end": b, "reasons": [], "segs": [], "halluc": [], "sparse": []}
            regions.append(cur)
        if r not in cur["reasons"]:
            cur["reasons"].append(r)
        if idx is not None:
            if idx not in cur["segs"]:
                cur["segs"].append(idx)
            if hal and idx not in cur["halluc"]:
                cur["halluc"].append(idx)
            if r == "sparse":
                cur["sparse"].append(idx)
    by_idx = {seg["idx"]: seg for seg in segments}
    for n, rg in enumerate(regions):
        rg["id"] = n
        rg["start"], rg["end"] = round(rg["start"], 2), round(rg["end"], 2)
        rg["voiced"] = round(levels.voiced_ratio(rg["start"], rg["end"]), 2)
        chars = {i: len(re.sub(r"\s+", "", by_idx[i].get("text") or "")) for i in rg["segs"]}
        rg["kept_chars"] = sum(c for i, c in chars.items() if i not in rg["halluc"])
        rg["orig_chars"] = sum(chars.values())
    return regions


# ---------- 재전사 결과 평가·선택 ----------

def evaluate(attempt, segs, s, language="ko", prompt_texts=(), neighbors=()):
    """재전사 결과 하나 검사. {"attempt", "ok", "english", "empty", "score", "chars", "why", "segs"}"""
    prompt_texts = [p for p in prompt_texts if p and p.strip()]
    segs = [x for x in segs if (x.get("text") or "").strip()]
    why = []
    near = {_norm(t) for t in neighbors if _norm(t)}
    dups = dup_flags(segs)
    for k, x in enumerate(segs):
        rs = [r for r in text_reasons(x, s, language, prompt_texts) if r != "english"]
        if k in dups:
            rs.append("dup")
        if _norm(x["text"]) in near:
            rs.append("dup")
        why += [r for r in rs if r not in why]
    text = " ".join(x["text"] for x in segs)
    lps = [x["avg_logprob"] for x in segs if x.get("avg_logprob") is not None]
    return {"attempt": attempt, "ok": not why, "english": bool(segs) and is_english_only(text, 1),
            "empty": not segs, "score": round(sum(lps) / len(lps), 3) if lps else None,
            "chars": len(re.sub(r"\s+", "", text)), "why": why, "segs": segs,
            "text": text[:120]}


def choose(rg, tries, s):
    """region의 결과를 정해 rg에 기록: outcome, attempt, segs(복원 세그먼트), tries(요약)."""
    rg["tries"] = [{k: t[k] for k in ("attempt", "ok", "english", "empty", "score", "chars", "why", "text")} for t in tries]
    ok = [t for t in tries if t["ok"] and not t["empty"]]
    ok.sort(key=lambda t: (t["english"], -(t["score"] if t["score"] is not None else -9)))
    best = ok[0] if ok else None
    rg["segs_new"], rg["attempt"] = [], None
    # 영어로만 나온 것만 의심인데 재전사로도 한국어 문장이 안 나오면 실제 영어 발화(슬라이드 낭독)로 보고 원래 문장 유지
    if set(rg["reasons"]) == {"english"} and (best is None or (best["english"] and best["chars"] <= rg.get("orig_chars", 0))):
        rg["outcome"] = "kept"
        return rg
    if best is None:
        # 소리가 거의 없는 구간의 환각이면 지우기만, 소리가 있으면 청취 불가
        rg["outcome"] = "dropped" if rg["halluc"] and rg["voiced"] < s["silent_max_ratio"] else "missing"
        return rg
    if not rg["halluc"] and best["chars"] <= rg.get("kept_chars", 0):  # 글자가 적은 문장을 다시 들어 봐도 더 나오지 않음
        rg["outcome"] = "kept"
        return rg
    rg["outcome"], rg["attempt"], rg["segs_new"] = "restored", best["attempt"], best["segs"]
    return rg


def assemble(segments, regions):
    """원래 세그먼트 + 복원 결과 → 최종 세그먼트 (시각순, idx 다시 매김).
    restored: 원래 의심 세그먼트 대신 복원 세그먼트 (restored=True)
    missing:  환각 세그먼트는 빼고 [청취 불가] 자리 세그먼트(missing=True) 추가, 환각이 아닌 원래 문장은 유지"""
    drop, add = set(), []
    for rg in regions:
        info = {"region": rg["id"], "reasons": rg["reasons"]}
        if rg["outcome"] == "restored":
            drop |= set(rg["segs"])
            for x in rg["segs_new"]:
                add.append({**{k: x[k] for k in ("start", "end", "text") if k in x},
                            **{k: x[k] for k in ("avg_logprob", "no_speech_prob", "compression_ratio") if k in x},
                            "restored": True, "restore": {**info, "attempt": rg["attempt"]}})
        elif rg["outcome"] in ("dropped", "missing"):
            drop |= set(rg["halluc"])
            if rg["outcome"] == "missing":
                add.append({"start": rg["start"], "end": rg["end"], "text": "", "missing": True, "restore": info})
    out = [dict(x) for x in segments if x["idx"] not in drop] + add
    out.sort(key=lambda x: (x["start"], bool(x.get("missing"))))
    for n, x in enumerate(out):
        x["idx"] = n
    return out


def summary(regions, raw_count):
    st = {"regions": len(regions), "raw_segments": raw_count,
          "hallucinated": sum(len(r["halluc"]) for r in regions),
          "gaps": sum(1 for r in regions if "gap" in r["reasons"]),
          "sparse": sum(len(r["sparse"]) for r in regions)}
    for k in ("restored", "kept", "dropped", "missing"):
        st[k] = sum(1 for r in regions if r.get("outcome") == k)
    st["missing_seconds"] = round(sum(r["end"] - r["start"] for r in regions if r.get("outcome") == "missing"), 1)
    st["restored_seconds"] = round(sum(r["end"] - r["start"] for r in regions if r.get("outcome") == "restored"), 1)
    keep = ("id", "start", "end", "reasons", "segs", "halluc", "sparse", "voiced", "outcome", "attempt", "tries")
    return {"stats": st, "regions": [{k: r.get(k) for k in keep} for r in regions]}
