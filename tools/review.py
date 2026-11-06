"""제안 → 승인 화면용 검사 (server.py와 tools가 같이 씀).

- 교정 규칙 영향: 이 규칙(틀린 표기 → 바른 표기)이 기존 전사본에서 몇 문장을 바꾸는지 + 예시
  (교정 작업의 Whisper 전사 jobs/*/segments.jsonl, 과목 폴더의 교정본 md, 중간고사 NotebookLM 전사본)
- 경고: 2글자 이하, 조사가 붙은 규칙(포토신세시스를 → photosynthesis를), 대표 표기가 아닌 바른 표기,
  표기 규칙 위반(교수님이 한국어로 말한 용어를 영어로 바꾸는 규칙: 최대값 → maximum)
- 한글 발음 ↔ 영어 비교 (포토신세시스 ≈ photosynthesis): 한국어 용어인지 영어를 한글로 적은 것인지 가림
- 결과 화면의 직접 입력(manual.json)에서 바뀐 낱말 → 교정 규칙 제안
"""
import difflib, json, re
from pathlib import Path

try:
    from . import common as C
except ImportError:
    import common as C

JOBS = C.BASE / "jobs"  # 교정 작업 폴더 (work.JOBS와 같음)
HANGUL = re.compile(r"[가-힣]")
LATIN = re.compile(r"[A-Za-z]")
# 조사 (긴 것부터)
PARTICLES = ("이라고", "에서는", "으로는", "에게서", "이라는", "라는", "라고", "에서", "에게", "으로", "까지", "부터", "이랑",
             "이고", "이며", "이나", "처럼", "보다", "하고", "이", "가", "을", "를", "은", "는", "의", "에", "로", "와", "과",
             "도", "만", "랑", "나", "고")

# ---------- 한글 발음 ↔ 영어 ----------

_L = "K K N T T L M P P S S _ J J J K T P H".split()                     # 초성 (ㅇ은 소리 없음)
_V = "a e a e o e o e o a e e o u o e i u _ i i".split()                 # 중성 (ㅡ는 덧붙은 모음이라 뺌)
_T = "_ K K K N N N T L K M L L L P L M P P T T N T T K T P _".split()   # 종성 (ㅇ → N, ㅅ → T: 로봇)


def hangul_key(s):
    out = []
    for ch in s:
        c = ord(ch) - 0xAC00
        if not 0 <= c < 11172:
            continue
        for x in (_L[c // 588], _V[c % 588 // 28], _T[c % 28]):
            if x != "_":
                out.append(x)
    return _squeeze("".join(out))


_EN_RULES = [(r"tion|sion", "Son"), (r"ph", "P"), (r"th", "T"), (r"sh", "S"), (r"ch", "J"), (r"ck", "K"),
             (r"qu", "K"), (r"x", "KS"), (r"c(?=[eiy])", "S"), (r"ng", "N"), (r"e$", ""), (r"(?<=[aeiou])r(?![aeiou])", ""),
             (r"^h", "H"), (r"h", ""), (r"[wy]", ""), (r"[cgk]", "K"), (r"[bpfv]", "P"), (r"[dt]", "T"), (r"[sz]", "S"),
             (r"j", "J"), (r"[lr]", "L"), (r"m", "M"), (r"n", "N")]


def english_key(s):
    parts = []
    for w in re.findall(r"[a-z]+", s.lower()):
        for pat, rep in _EN_RULES:
            w = re.sub(pat, rep, w)
        parts.append(w)
    return _squeeze("".join(parts))


def _squeeze(k):
    return re.sub(r"([A-Z])\1+", r"\1", k)


def sounds_like(hangul, english):
    """한글 표기가 영어를 소리 나는 대로 적은 것과 얼마나 비슷한지 0~1 (포토신세시스/photosynthesis는 높고, 광합성/photosynthesis는 낮음)."""
    return key_similarity(hangul_key(hangul), english_key(english))


def key_similarity(a, b):
    if not a or not b:
        return 0.0
    ca, cb = re.sub(r"[a-z]", "", a), re.sub(r"[a-z]", "", b)
    cons = difflib.SequenceMatcher(None, ca, cb).ratio()
    full = difflib.SequenceMatcher(None, a, b).ratio()
    return round(0.6 * cons + 0.4 * full, 2)


TRANSLIT_MIN = 0.55  # 이 이상이면 영어를 한글로 적은 것으로 봄 


def is_transliteration(hangul, english):
    return sounds_like(hangul, english) >= TRANSLIT_MIN


# ---------- 조사 ----------

def split_particle(word):
    """'cell이' → ('cell', '이'), '최대값을' → ('최대값', '을'). 조사가 없으면 (word, '')."""
    w = word.strip()
    for p in PARTICLES:
        if w.endswith(p) and len(w) > len(p):
            stem = w[:-len(p)]
            if LATIN.search(stem[-1:]) or (HANGUL.search(stem[-1:]) and len(stem) >= 2):
                return stem, p
    return w, ""


# ---------- 교정 규칙 검사 ----------

def _glossary_index(subject):
    glo = C.load_json(C.subject_dir(subject) / "glossary.json", {"terms": []})["terms"]
    rep, alias = {}, {}
    for t in glo:
        rep[C.term_key(t["term"])] = t["term"]
        for a in t.get("aliases") or []:
            alias[C.term_key(a)] = t["term"]
    return rep, alias


def rule_warnings(subject, wrong, right, ctx=None):
    """[{"code", "text"}]. code: short / particle / translation / not_representative / conflict."""
    ctx = ctx or {}
    sub = ctx.get("sub") or C.subject(subject)
    rep, alias = ctx.get("gloss") or _glossary_index(subject)
    fix = ctx.get("fix")
    if fix is None:
        fix = C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]
    out = []
    w, r = wrong.strip(), right.strip()
    if len(w.replace(" ", "")) <= 2:
        out.append({"code": "short", "text": "2글자 이하 규칙 — 다른 낱말 속 글자까지 바뀔 수 있음"})
    ws, wp = split_particle(w)
    rs, rp = split_particle(r)
    if rp and LATIN.search(rs[-1:]):
        out.append({"code": "particle", "text": f"바른 표기에 조사 '{rp}'가 붙어 있음 — 조사를 뺀 규칙이 나음 ({ws if wp == rp else w} → {rs})"})
    elif wp and wp == rp and len(ws.replace(" ", "")) > 2:
        out.append({"code": "particle", "text": f"양쪽에 같은 조사 '{wp}'가 붙어 있음 — '{ws} → {rs}'로 승인하는 게 나음"})
    if sub.get("prefer_english_terms") and HANGUL.search(w) and LATIN.search(r) and not HANGUL.search(rs):
        if alias.get(C.term_key(w)) != r and not is_transliteration(ws, rs):
            out.append({"code": "translation", "text": f"'{ws}'는 '{rs}'의 한글 발음이 아니라 한국어 용어로 보임 (발음 유사도 {sounds_like(ws, rs)})"
                                                      " — 교수님이 한국어로 말한 용어는 영어로 번역하지 않는다. 한국어 맞춤법만 고치거나 거절"})
    k = C.term_key(rs)
    if k in alias and k not in rep and alias[k] != rs:
        out.append({"code": "not_representative", "text": f"바른 표기 '{rs}'는 용어집의 별칭 — 대표 표기는 '{alias[k]}'"})
    for f in fix:
        if f["wrong"] == w and f["right"] != r:
            out.append({"code": "conflict", "text": f"교정 사전에 이미 '{f['wrong']} → {f['right']}' 규칙이 있음"})
    return out


# ---------- 기존 전사본에서의 영향 ----------

def corpus(subject):
    """[(출처 이름, 종류, 문장 목록)] 종류: whisper(교정 작업의 Whisper 전사), final(교정본 md), midterm(NotebookLM)."""
    from_jobs = JOBS
    out = []
    if from_jobs.is_dir():
        for d in sorted(from_jobs.iterdir()):
            j = C.load_json(d / "job.json") if d.is_dir() else None
            if not j or j.get("subject") != subject or j.get("status") == "superseded"                     or not (d / "segments.jsonl").exists():  # 대체된 작업은 같은 녹음을 두 번 세지 않게 뺌
                continue
            with open(d / "segments.jsonl", encoding="utf-8") as f:
                segs = [json.loads(l)["text"] for l in f if l.strip()]
            out.append((d.name, "whisper", segs))
    s = C.settings()
    for kind, folder in (("final", C.finals_transcripts_dir(subject)),
                         ("midterm", C.root() / subject / s["midterm_dir"] / s["transcripts_dir"])):
        if folder.is_dir():
            for p in sorted(folder.glob("*.md")):
                text = p.read_text(encoding="utf-8", errors="replace")
                if kind == "final":
                    text = text.split("\n---\n## 처리 메모")[0]
                    sents = [re.sub(r"^\*\*\[[\d:]+\]\*\*\s*(\([^)]*\)\s*)?", "", l) for l in text.split("\n") if l.startswith("**[")]
                else:
                    sents = [x for x in re.split(r"(?<=[.?!])\s+|\n+", text) if x.strip()]
                out.append((p.name, kind, sents))
    return out


def impact(wrong, right, docs, examples=2):
    """교정 사전처럼(그대로 바꾸기) 적용했을 때 바뀌는 문장 수 {"whisper", "final", "midterm", "examples": [...]}."""
    res = {"whisper": 0, "final": 0, "midterm": 0, "files": 0, "examples": []}
    if not wrong:
        return res
    for name, kind, sents in docs:
        hit = 0
        for s in sents:
            if wrong in s:
                hit += 1
                if len(res["examples"]) < examples and (kind != "midterm" or not res["examples"]):
                    k = s.index(wrong)
                    a, b = max(0, k - 25), min(len(s), k + len(wrong) + 25)
                    piece = ("…" if a else "") + s[a:b] + ("…" if b < len(s) else "")
                    if any(x["before"] == piece for x in res["examples"]):
                        continue
                    res["examples"].append({"where": name, "kind": kind, "before": piece,
                                            "after": piece.replace(wrong, right)})
        if hit:
            res[kind] += hit
            res["files"] += 1
    return res


def analyze_pending(subject, items):
    """승인 대기 제안에 화면용 분석을 붙임: 교정 규칙은 warnings·impact·default(기본 체크), 표기 규칙은 기존 규칙과 겹침."""
    sub = C.subject(subject)
    ctx = {"sub": sub, "gloss": _glossary_index(subject),
           "fix": C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]}
    docs = None
    have_notation = {n["spoken"]: n["written"] for n in sub.get("notation") or []}
    for s in items:
        d = s.get("data") or {}
        if s["type"] == "fix_rule":
            docs = docs if docs is not None else corpus(subject)
            warn = rule_warnings(subject, d.get("wrong", ""), d.get("right", ""), ctx)
            s["check"] = {"warnings": warn, "impact": impact(d.get("wrong", ""), d.get("right", ""), docs),
                          "default": not warn}
        elif s["type"] == "notation":
            old = have_notation.get(d.get("spoken"))
            warn = [{"code": "exists", "text": f"이미 있는 표기 규칙: {d.get('spoken')} → {old}"}] if old else []
            s["check"] = {"warnings": warn, "default": not old}
        elif s["type"] == "progress":
            s["check"] = {"warnings": [], "default": False}
    progress = [s for s in items if s["type"] == "progress"]
    if progress:  # 진도 위치는 가장 최근 제안만 기본 체크
        max(progress, key=lambda s: s.get("created", ""))["check"]["default"] = True
    return items


def fix_warnings(subject):
    """교정 사전(이미 승인된 규칙)마다 경고 [{"wrong", "right", "warnings"}]."""
    ctx = {"sub": C.subject(subject), "gloss": _glossary_index(subject), "fix": []}
    rules = C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]
    return [{"wrong": r["wrong"], "right": r["right"], "warnings": rule_warnings(subject, r["wrong"], r["right"], ctx)}
            for r in rules]


# ---------- 직접 입력 → 교정 규칙 제안 ----------

def manual_pairs(orig, typed, max_words=3):
    """원문과 직접 입력한 문장을 낱말 단위로 비교해 바뀐 짧은 부분 [(원문 낱말, 입력 낱말)]."""
    a, b = orig.split(), typed.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "replace" and i2 - i1 <= max_words and j2 - j1 <= max_words:
            w, r = " ".join(a[i1:i2]), " ".join(b[j1:j2])
            ws, wp = split_particle(w)
            rs, rp = split_particle(r)
            if wp and wp == rp:  # 같은 조사는 떼고 제안
                w, r = ws, rs
            out.append((w.strip(".,?!"), r.strip(".,?!")))
    return [(w, r) for w, r in out if w and r and w != r]


def manual_suggestions(subject, name, segments, manual):
    """직접 입력에서 바뀐 낱말 → 교정 규칙 제안 항목 (2글자 이하·흔한 단어·이미 있는 규칙 제외)."""
    stop = {C.term_key(w) for w in C.subject(subject).get("term_stopwords") or []}
    have = {r["wrong"] for r in C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]}
    orig = {s["idx"]: s for s in segments}
    found = {}
    for k, m in manual.items():
        s = orig.get(int(k))
        if not s or s.get("missing"):
            continue
        for w, r in manual_pairs(s["text"], m["text"]):
            if len(w.replace(" ", "")) <= 2 or C.term_key(w) in stop or w in have:
                continue
            found.setdefault((w, r), []).append(str(s["idx"]))
    return [{"type": "fix_rule", "origin": "manual", "evidence": [{"source": name, "segments": ", ".join(v)}],
             "data": {"wrong": w, "right": r}} for (w, r), v in found.items()]
