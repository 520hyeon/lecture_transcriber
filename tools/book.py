"""원서 md (subjects.json "books") → 절 단위 색인, 교정용 발췌, 용어 후보.

    python tools/book.py excerpt <작업이름>     이 강의 전사본과 관련된 원서 절의 의사코드·정의 발췌 미리보기
    python tools/book.py terms <과목> [--out 파일]   목차·절 제목·강조 용어·색인 (/bootstrap-glossary용)

- 원서 md는 PDF를 변환한 것이라 수식 글꼴 문자가 깨져 있다.
  의사코드와 정의 문장에서 자주 나오는 것만 바로잡는다.
- 절·그림·식 번호는 넣지 않는다. 원서와 수업의 판이 다르면 번호가 달라진다 (설명 문장 속 번호도 지움).
- 절 색인은 data/<과목>/cache/book/<원서 파일명>.json에 캐시 (원서 파일 크기·수정 시각이 같으면 다시 읽지 않음).
"""
import glob, math, re, sys
from pathlib import Path

try:
    from . import common as C
    from . import review as RV
except ImportError:
    import common as C
    import review as RV

# PDF 변환본에서 깨지는 수식 글꼴 문자 (확인한 것만)
SYMBOLS = {"\x00": "−", "\x14": "≤", "\x15": "≥", "Œ": "[", "\x8d": "]", "‚": "Θ", "Š": "!", "\x01": "·",
           "\x02": "×", "\x0f": "•", "\x17": "ν", "\x19": "π", "\x10": "(", "\x11": ")", "\x12": "[", "\x13": "]",
           "\x03": "*", "\x1e": "φ", "\x16": "μ", "\x1b": "σ"}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CALL = re.compile(r"\b([A-Z][A-Z\-]{2,})\.([^/\n]{0,80}?)/")          # QUICKSORT.A; p; r/ → QUICKSORT(A, p, r)
_NUMBERED = re.compile(r"^\d{1,2} \S")                                 # 의사코드 줄 "3 for j = p to r − 1"
_PROC = re.compile(r"^[A-Z][A-Z\-]{2,}\(")                             # QUICKSORT(A, p, r)
# 번호 지우기: "Section 2.3.1" → "Section", "Figure 7.1" → "Figure", 식 번호 "(7.5)" → ""
_NUM_WORDS = (r"Sections?|Chapters?|Figures?|Tables?|Exercises?|Problems?|Theorems?|Lemmas?|Corollary|Corollaries|"
              r"Definitions?|Examples?|EXAMPLE|ALGORITHM|Algorithms?|[Ee]quations?|[Ii]nequalit(?:y|ies)|Appendix|Parts?|pages?")
_NUMS = re.compile(rf"\b({_NUM_WORDS})\s+[A-Z]?\d+(?:\.\d+)*(?:[-–]\d+)?(?:\s*(?:and|or|,|–)\s*[A-Z]?\d+(?:\.\d+)*(?:[-–]\d+)?)*")
_EQ_TAG = re.compile(r"\s*\(\d+\.\d+[a-z]?\)")

STOP = set("""the a an of and or to in on for with by from as is are be this that these those it its we our you your
can will may also each any all one two three more most such than then there their which when where while what how
not no but if into over under between using use used first second last new other same some time number value values
example figure table section chapter exercise problem problems exercises theorem lemma proof definition page""".split())


# ---------- 읽기·정리 ----------

def book_files(subject):
    """subjects.json "books"의 경로(과목 폴더 기준, * 가능) → [(경로, 메모)]."""
    out = []
    for b in C.subject(subject).get("books") or []:
        pat = str(C.root() / subject / b["path"])
        for p in sorted(glob.glob(pat)):
            out.append((Path(p), b.get("note") or ""))
    return out


def fix_symbols(text):
    for a, b in SYMBOLS.items():
        text = text.replace(a, b)
    text = _CTRL.sub("", text)
    text = text.replace(" : : ", "..").replace(": : :", "...")
    return _CALL.sub(lambda m: f"{m.group(1)}({m.group(2).replace(';', ',').strip()})", text)


def strip_numbers(text):
    """절·그림·식·정리 번호를 지움 (3판 번호라 4판과 다름)."""
    text = _NUMS.sub(lambda m: m.group(1), text)
    text = _EQ_TAG.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _plain(line):
    """md 강조 표시 지우기: '**for **j' → 'for j', '***term ***' → 'term'."""
    line = re.sub(r"\*{1,3}([^*\n]+?)\s*\*{1,3}", lambda m: m.group(1) + " ", line)
    return re.sub(r"\s{2,}", " ", line.replace("*", "")).strip()


_PAGE_JUNK = [re.compile(p) for p in (
    r"^\*\d+(\.\d+)* .+ \d+ \*$",               # 쪽 머리말 "*1.2 Section title 171 *"
    r"^\*\d+ Chapter \d+ .+\*$",                # "*172 Chapter 1 Title *"
    r"^\[Original PDF page \d+\]",              # PDF 변환 표시
    r"^\d{1,4} ?$",                             # 쪽 번호만 있는 줄
)]


def parse(path):
    """원서 md → 절 목록 [{"title", "lines": [...], "terms": [...강조 용어]}]. 쪽 머리말·쪽 번호 줄은 버림."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace").split("\n")
    units, cur = [], {"title": "(앞부분)", "lines": [], "terms": []}
    i, n = 0, len(raw)
    while i < n:
        line = raw[i].rstrip()
        s = line.strip()
        title = None
        m = re.match(r"^\*\*\d+(?:\.\d+)+ (.+?)\s*\*\*\s*$", s)            # "**1.2 Section title **"
        if m:
            title = m.group(1)
        elif re.match(r"^#{1,3} \*\*\d+ (.+?)\s*\*\*\s*$", s):           # 장 "### **1 Title **"
            title = re.match(r"^#{1,3} \*\*\d+ (.+?)\s*\*\*\s*$", s).group(1)
        elif re.fullmatch(r"\d+\.\d+(?:\.\d+)? ?", line) and i + 2 < n:  # "2.2 " 다음 줄에 제목
            j = i + 1
            while j < n and not raw[j].strip():
                j += 1
            if j < n and re.match(r"^[A-Z][A-Za-z ,'\-–:()]{2,80}$", raw[j].strip()):
                title, i = raw[j].strip(), j
        if title:
            if cur["lines"]:
                units.append(cur)
            cur = {"title": strip_numbers(fix_symbols(title)), "lines": [], "terms": []}
            i += 1
            continue
        if s and not any(p.match(s) for p in _PAGE_JUNK):
            t = fix_symbols(s)
            cur["lines"].append(t)
            cur["terms"] += [x.strip() for x in re.findall(r"\*\*\*([^*\n]{2,60}?)\s*\*\*\*", t)]
        i += 1
    if cur["lines"]:
        units.append(cur)
    return units


def load_units(subject):
    """과목 원서 전체의 절 목록 (캐시). [{"book", "title", "lines", "terms"}]"""
    out = []
    for path, _ in book_files(subject):
        st = path.stat()
        cp = C.subject_dir(subject) / "cache" / "book" / f"{path.name}.json"
        cached = C.load_json(cp)
        if not cached or (cached.get("size"), cached.get("mtime")) != (st.st_size, int(st.st_mtime)):
            cached = {"size": st.st_size, "mtime": int(st.st_mtime), "units": parse(path)}
            C.save_json(cp, cached)
        out += [{"book": path.name, **u} for u in cached["units"]]
    return out


def book_label(file):
    """'Author_Book_Title.pdf.md' → 'Author Book Title'."""
    return re.sub(r"\.pdf\.md$|\.md$", "", file).replace("_", " ")


# ---------- 전사본 → 관련 절 ----------

def transcript_terms(subject, text):
    """전사본에 나온 용어 {영어 표기: (등장 수, 가중치)}: 용어집(대표 표기·별칭)·교정 사전 바른 표기는 가중치 2,
    전사본에 적힌 영어 낱말은 1 (두 낱말 묶음은 2번 이상 나온 것만, 영어 환각 문장의 우연한 낱말을 줄이려고)."""
    low = text.lower()
    found = {}

    def add(t, c, w):
        t = t.lower()
        oc, ow = found.get(t, (0, 0))
        found[t] = (oc + c, max(ow, w))

    glo = C.load_json(C.subject_dir(subject) / "glossary.json", {"terms": []})["terms"]
    fix = C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]
    for t in glo:
        forms = [t["term"]] + (t.get("aliases") or [])
        cnt = sum(low.count(f.lower()) for f in forms if len(f) >= 2)
        if cnt and re.search(r"[A-Za-z]", t["term"]):
            add(t["term"], cnt, 2)
    for r in fix:
        if re.search(r"[A-Za-z]", r["right"]) and r["wrong"].lower() in low:
            add(r["right"], low.count(r["wrong"].lower()), 2)
    # 한글 발음으로 적힌 영어 용어 (포토신세시스 → photosynthesis): 용어집·교정 사전의 영어 표기와 소리를 비교
    eng = {t["term"] for t in glo if re.search(r"[A-Za-z]", t["term"])} | {r["right"] for r in fix if re.search(r"[A-Za-z]", r["right"])}
    keys = {e: RV.english_key(e) for e in eng}
    toks = [RV.split_particle(w)[0] for w in re.findall(r"[가-힣]+", text)]
    cand = {}
    for k, w in enumerate(toks):
        for x in (w, f"{w} {toks[k + 1]}" if k + 1 < len(toks) else ""):
            if len(x.replace(" ", "")) >= 2:
                cand[x] = cand.get(x, 0) + 1
    for x, c in cand.items():
        hk = RV.hangul_key(x)
        for e, ek in keys.items():  # 길이가 비슷하고 첫소리가 같은(또는 모음으로 시작하는) 것만 비교
            if abs(len(hk) - len(ek)) <= 3 and hk and ek and (hk[0] == ek[0] or hk[0].islower() or ek[0].islower())                     and RV.key_similarity(hk, ek) >= 0.75:
                add(e, c, 2)
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text)]
    grams = {}
    for k, w in enumerate(words):
        if len(w) >= 4 and w not in STOP:
            add(w, 1, 1)
        if k + 1 < len(words) and words[k + 1] not in STOP and w not in STOP:
            bg = f"{w} {words[k + 1]}"
            grams[bg] = grams.get(bg, 0) + 1
    for bg, c in grams.items():
        if c >= 2:
            add(bg, c, 1)
    multi = [t for t, (c, w) in found.items() if " " in t and w > 1]
    return {t: v for t, v in found.items()  # 용어집의 여러 낱말 용어 일부인 낱말(low side의 side)은 뺌
            if " " in t or v[1] > 1 or not any(t in m.split() for m in multi)}


def _term_re(term):
    """'stem cell' ↔ 'stemcell' ↔ 'stem-cell' 모두 맞게."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term) if p]
    return re.compile(r"(?<![A-Za-z])" + r"[\s\-]?".join(parts) + r"(?![A-Za-z])")


def _count(term, text):
    return len(_term_re(term).findall(text))


def select_units(subject, text, units=None, max_units=None):
    """전사본과 관련된 원서 절 [(절, 점수, 일치 용어)]. 점수 = Σ 용어 가중치(전사본 등장 수 · 희귀도) × (본문 등장 + 절 제목 15 / 강조 3)."""
    units = units if units is not None else load_units(subject)
    if not units:
        return []
    tt = transcript_terms(subject, text)
    bodies = [" ".join(u["lines"]).lower() for u in units]
    compact = [re.sub(r"[\s\-]", "", b) for b in bodies]
    titles = [u["title"].lower() for u in units]
    has = {t: [k for k, cb in enumerate(compact) if re.sub(r"[\s\-]", "", t) in cb and _count(t, bodies[k])] for t in tt}
    df = {t: len(v) for t, v in has.items()}
    N = len(units)
    weight = {t: w * math.log(1 + c) * math.log(N / df[t]) for t, (c, w) in tt.items()
              if df[t] and df[t] <= N * (0.3 if w > 1 else 0.15)}
    scored = []
    in_unit = {}
    for t in weight:
        for k in has[t]:
            in_unit.setdefault(k, []).append(t)
    for k, terms in in_unit.items():
        u, b, ti = units[k], bodies[k], titles[k]
        hits, sc, in_title = [], 0.0, False
        emph = {x.lower() for x in u["terms"]}
        for t in terms:
            w = weight[t]
            c = _count(t, b)
            tb = 15 if _count(t, ti) else 0  # 절 제목에 나오는 용어가 가장 중요
            in_title |= bool(tb)
            sc += w * (min(c, 6) + tb + (3 if t in emph else 0))
            hits.append(t)
        sc *= 1 if in_title else 0.6  # 제목에 이 강의 용어가 하나도 없는 절은 덜 관련
        if sc:
            scored.append((u, sc, hits))
    if not scored:
        return []
    scored.sort(key=lambda r: -r[1])
    best = scored[0][1]
    max_units = max_units or C.settings()["book_max_sections"]
    return [r for r in scored if r[1] >= best * 0.35 and len(r[2]) >= 2][:max_units]


# ---------- 발췌 ----------

def _pseudocode(lines):
    """의사코드 덩어리: "NAME(args)" + 번호 줄, 또는 "procedure …" + 이어지는 짧은 줄."""
    blocks, i = [], 0
    while i < len(lines):
        s = lines[i]
        if _PROC.match(s) and len(s) < 70 and i + 1 < len(lines) and _NUMBERED.match(lines[i + 1]):
            blk = [_plain(s)]
            i += 1
            while i < len(lines) and _NUMBERED.match(lines[i]) and len(blk) < 30:
                blk += _split_numbered(_code_line(lines[i]))
                i += 1
            blocks.append(blk)
            continue
        if s.startswith("procedure ") or (s.startswith("ALGORITHM") and i + 1 < len(lines) and lines[i + 1].startswith("procedure ")):
            blk = []
            if s.startswith("ALGORITHM"):
                blk.append(strip_numbers(s))
                i += 1
            while i < len(lines) and len(blk) < 20:
                t = lines[i]
                if blk and (len(t) > 75 and not re.search(r":=|\bthen\b|\breturn\b|\bfor\b|\bwhile\b", t)):
                    break
                if blk and re.match(r"^(ALGORITHM|EXAMPLE|Definition|THEOREM)\b", t):
                    break
                blk.append(t)
                i += 1
            blocks.append(blk)
            continue
        i += 1
    return blocks


def _code_line(s):
    s = _plain(s)
    s = re.sub(r"(?<=\s)D(?=\s)", "=", " " + s + " ")   # 수식 글꼴에서 "="가 D, "+"가 C로 깨짐 (의사코드 줄에서만 바꿈)
    s = re.sub(r"(?<=\s)C(?=\s)", "+", s)
    return s.strip()


def _split_numbered(s):
    """변환 중에 붙어 버린 의사코드 줄 나누기: '5 if i == k // … 6 return A[q]' → 두 줄."""
    m = re.match(r"^(\d{1,2}) ", s)
    out, n = [], int(m.group(1))
    while True:
        k = re.search(rf"\s({n + 1}) (?=\S)", s)
        if not k:
            break
        out.append(s[:k.start()].strip())
        s, n = s[k.start():].strip(), n + 1
    return out + [s]


def _math_prose(s):
    """정의 문장 속 수식: 'A[q C 1..r]' → 'A[q + 1..r]', 'x D A[r]' → 'x = A[r]' (한 글자 변수 사이만)."""
    s = re.sub(r"(?<=\b[a-zA-Z]) C (?=[a-z0-9]\b)", " + ", s)
    s = re.sub(r"(?<=\b[a-zA-Z]) D (?=[A-Za-z0-9])", " = ", s)
    return s


def _definitions(unit, hits, limit=3):
    """정의 문장: ***굵은 기울임*** 용어가 든 문장, 또는 "Definition N" 다음 문장."""
    lines, out = unit["lines"], []
    for k, s in enumerate(lines):
        if re.match(r"^Definition \d+ ?$", s):
            body = []
            for t in lines[k + 1:k + 8]:
                if re.match(r"^(EXAMPLE|Definition|THEOREM|ALGORITHM)\b", t) or sum(map(len, body)) > 420:
                    break
                body.append(t)
            out.append(("Definition", " ".join(body)))
        elif "***" in s:
            terms = [x.strip() for x in re.findall(r"\*\*\*([^*\n]{2,60}?)\s*\*\*\*", s)]
            if any(t.lower() in hits or any(h in t.lower() for h in hits) for t in terms):
                out.append((", ".join(terms), s))
        if len(out) >= limit:
            break
    res = []
    for head, body in out:
        body = re.sub(r"^\d+[-–]\d+\s+", "", strip_numbers(_math_prose(_plain(body))))  # 연습 문제 번호 "7-2 …"
        head = re.sub(r"^\d+[-–]\d+\s+", "", head)
        sents = re.split(r"(?<=[.:])\s+", body)
        short = ""
        for x in sents:  # 정의가 든 앞 문장 2~3개만
            if len(short) + len(x) > 380:
                break
            short += x + " "
        res.append((head, (short or body[:380]).strip()))
    return res


def excerpt(subject, text, max_chars=None, units=None):
    """전사본 text와 관련된 원서 절의 정의·의사코드 발췌 → (md 줄 목록, 메타). 번호는 넣지 않음."""
    s = C.settings()
    max_chars = max_chars or s["book_excerpt_chars"]
    picked = select_units(subject, text, units)
    L, meta, used = [], [], 0
    for u, sc, hits in picked:
        top = sorted(hits, key=lambda t: -len(t))[:6]
        head = [f"### {u['title']} — {book_label(u['book'])} (일치 용어: {', '.join(top)})"]
        body = []
        for name, d in _definitions(u, set(hits)):
            body.append(f"- 정의 ({strip_numbers(name)}): {d}")
        for blk in _pseudocode(u["lines"])[:3]:
            body += ["```", *[strip_numbers(x) if not _NUMBERED.match(x) else x for x in blk], "```"]
        if not body:
            continue
        size = sum(len(x) + 1 for x in head + body)
        if used + size > max_chars:
            if used:
                break
            body = body[:max(3, len(body) // 2)]
        L += [""] + head + body
        used += size
        meta.append({"book": u["book"], "title": u["title"], "terms": top})
    return L, meta


def context_section(subject, text, decks_registered):
    """context.md에 넣을 "원서 참고" 절 (원서가 없거나 관련 절이 없으면 [])."""
    if not book_files(subject):
        return [], []
    s = C.settings()
    limit = s["book_excerpt_chars"] if not decks_registered else s["book_excerpt_chars_with_slides"]
    if not limit:
        return [], []
    L, meta = excerpt(subject, text, limit)
    if not L:
        return [], []
    why = ("강의자료가 등록되지 않아 원서로 대신 참고한다" if not decks_registered
           else "보조 자료: 슬라이드가 우선이고, 원서는 용어 철자·정의 확인용")
    return (["", f"## 원서 참고 ({why})",
             "- 의사코드와 정의만 짧게 뽑았다. 용어 철자·기호(A[p..q−1] 등) 확인에만 쓰고, 원서 내용을 전사본에 덧붙이지 않는다.",
             "- 원서는 교수님 판과 장·절 번호가 다르므로 번호는 넣지 않았다. 교정본·문단 제목에 원서 장·절 번호를 쓰지 않는다.",
             "- paragraphs의 slide에는 원서를 넣지 않는다 (강의자료로 등록된 파일만)."] + L, meta)


# ---------- 용어 후보 (/bootstrap-glossary) ----------

def book_terms(subject):
    """원서 용어 후보 {용어: {"count", "where": [절 제목…], "kind": set}}: 절 제목, 강조 용어(***…***), 색인 항목, 절차 이름."""
    units = load_units(subject)
    out = {}

    def add(t, where, kind):
        t = re.sub(r"\s+", " ", _plain(t)).strip(" ,.;:")
        if not (2 < len(t) <= 60) or not re.search(r"[A-Za-z]", t) or t.lower() in STOP:
            return
        e = out.setdefault(t, {"count": 0, "where": [], "kind": set()})
        e["count"] += 1
        e["kind"].add(kind)
        if where and where not in e["where"] and len(e["where"]) < 3:
            e["where"].append(where)

    in_index = False
    for u in units:
        if u["title"] != "(앞부분)":
            add(u["title"], "", "title")
        for t in u["terms"]:
            add(t, u["title"], "emphasis")
        for s in u["lines"]:
            if re.match(r"^Index( I-\d+)? ?$", s):
                in_index = True
                continue
            if in_index:
                m = re.match(r"^([a-zA-Z][a-zA-Z \-']{2,50}),\s*\d", s)
                if m:
                    add(m.group(1), "색인", "index")
            m = re.match(r"^ALGORITHM \d+ (.+?)\.?\s*$", s)
            if m:
                add(m.group(1), u["title"], "algorithm")
    return out


def main():
    C.utf8_stdout()
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "excerpt":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import work as W
        job = W.load_job(a[1]) or sys.exit(f"작업이 없습니다: {a[1]}")
        text = " ".join(x["text"] for x in W.load_segments(a[1]))
        L, meta = context_section(job["subject"], text, bool((job.get("context") or {}).get("decks_registered")))
        print("\n".join(L) if L else "관련 원서 절이 없습니다.")
    elif len(a) >= 2 and a[0] == "terms":
        terms = book_terms(a[1])
        rows = sorted(terms.items(), key=lambda kv: (-len(kv[1]["kind"]), -kv[1]["count"], kv[0].lower()))
        lines = [f"- {t} · {e['count']}회 · {'/'.join(sorted(e['kind']))}" + (f" · {'; '.join(e['where'])}" if e["where"] else "")
                 for t, e in rows]
        if "--out" in a:
            out = Path(a[a.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"원서 용어 후보 {len(lines)}개 → {out}")
        else:
            print("\n".join(lines[:200]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
