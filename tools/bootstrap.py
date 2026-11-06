"""/bootstrap-glossary 보조: 중간고사 자료·원서·NotebookLM 전사본에서 용어집·교정 사전 후보를 뽑아 작은 파일로 정리.

    python tools/bootstrap.py <과목> --list                 출처 목록 (쓰는 것 / 빼는 것과 이유, 확인이 필요한 것)
    python tools/bootstrap.py <과목> --extract [--exclude 패턴 …]   후보 정리 → data/<과목>/cache/bootstrap/*.md
    python tools/bootstrap.py <과목> --apply                proposal.json 검사 → suggestions.json (origin "bootstrap")

출처는 교수님 자료(슬라이드, 과제, 퀴즈, 실습 노트북), 원서, 중간고사 NotebookLM 전사본만.
사용자가 만든 정리 노트(*_필기.pdf, 개념정리노트, *_학습진행요약.md 등, settings "user_note_patterns")는 뺀다.
Claude Code는 원문 전체가 아니라 아래 정리 파일만 읽는다 (사용량 절약). 필요하면 text/<파일>.txt를 Grep으로 찾아본다.
    materials.md   자료별 페이지 제목 + 용어 후보 (영어 낱말 묶음, "한국어(English)" 짝, 코드 식별자)
    spoken.md      용어 후보마다 NotebookLM 전사본에서 영어로 / 한글 발음으로 / 한국어 용어로 적힌 횟수 (교수님이 어떻게 말하는지)
    book.md        원서 용어 중 중간고사 자료·전사본에도 나오는 것 (철자 확인용, 절 번호 없음)
    whisper.md     중간고사 샘플 Whisper 전사 ↔ 같은 날짜 NotebookLM 전사본에서 자주 다르게 적힌 낱말 (교정 사전 후보)
"""
import difflib, fnmatch, re, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import book as B
import review as RV
import auto as A

BOOTSTRAP_SOURCE = "중간고사 자료"
AMBIGUOUS = ["*_written*", "*노트*", "*답안*", "*풀이*", "*요약*"]  # 교수님 자료인지 사용자 자료인지 확인이 필요한 이름
_PAIR_KO_EN = re.compile(r"([가-힣][가-힣 ]{0,15}[가-힣])\s*\(\s*([A-Za-z][A-Za-z \-']{1,40}?)\s*\)")
_PAIR_EN_KO = re.compile(r"([A-Za-z][A-Za-z \-']{1,40}?)\s*\(\s*([가-힣][가-힣 ]{0,15}[가-힣])\s*\)")
_CODE_ID = re.compile(r"\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)(?:\(\))?")


def out_dir(subject):
    return C.subject_dir(subject) / "cache" / "bootstrap"


def _kind(name):
    n = name.lower()
    if "quiz" in n or "퀴즈" in n:
        return "퀴즈"
    if "assignment" in n or "과제" in n or n.startswith("hw") or "_hw" in n:
        return "과제"
    if n.endswith(".ipynb") or "lab" in n or "실습" in n:
        return "실습"
    if n.endswith((".docx",)):
        return "참고 자료"
    return "슬라이드"


def sources(subject, exclude=()):
    """[{path, name, kind, use, why}] — 중간고사 강의자료, 중간고사 NotebookLM 전사본, 원서."""
    s, sub = C.settings(), C.subject(subject)
    notes = list(s["user_note_patterns"]) + list(s["midterm_exclude_patterns"]) + list(sub.get("ignore_files") or [])
    ignore = set(sub.get("ignore_dirs") or [])
    exts = set(s["slide_exts"]) | set(s["reference_exts"])
    out = []
    mid = C.root() / subject / s["midterm_dir"]
    mat = mid / s["materials_dir"]
    if mat.is_dir():
        for p in sorted(mat.rglob("*")):
            if not p.is_file() or p.name.startswith("~$"):
                continue
            rel = p.relative_to(mat)
            row = {"path": str(p), "name": str(rel), "kind": _kind(p.name), "use": True, "why": ""}
            if p.suffix.lower() not in exts:
                row.update(use=False, why=f"형식 제외 ({p.suffix})")
            elif any(part in ignore for part in rel.parts[:-1]) or any(fnmatch.fnmatch(p.name, x) for x in notes):
                row.update(use=False, why="사용자 정리 노트·필기 (출처에서 제외)")
            elif any(fnmatch.fnmatch(p.name, x) or fnmatch.fnmatch(str(rel), x) for x in exclude):
                row.update(use=False, why="--exclude로 뺌")
            elif any(fnmatch.fnmatch(p.name, x) for x in AMBIGUOUS):
                row["why"] = "확인 필요: 사용자가 직접 쓴 자료일 수 있음"
            out.append(row)
    tr = mid / s["transcripts_dir"]
    if tr.is_dir():
        for p in sorted(tr.glob("*.md")):
            row = {"path": str(p), "name": p.name, "kind": "NotebookLM 전사본", "use": True, "why": ""}
            if any(fnmatch.fnmatch(p.name, x) for x in notes) or any(fnmatch.fnmatch(p.name, x) for x in exclude):
                row.update(use=False, why="제외")
            out.append(row)
    for p, note in B.book_files(subject):
        out.append({"path": str(p), "name": p.name, "kind": "원서", "use": True, "why": note.split(".")[0]})
    return out


# ---------- 자료 → 용어 후보 ----------

def material_text(path, cache):
    """자료 텍스트 (PDF는 텍스트 레이어만, OCR 안 함). text/<파일>.txt에 캐시."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pdf_pages as PP
    p = Path(path)
    cp = cache / "text" / f"{p.name}.txt"
    if cp.exists() and cp.stat().st_mtime >= p.stat().st_mtime:
        return cp.read_text(encoding="utf-8")
    text = PP.reference_text(p, cache / "tmp")
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(text, encoding="utf-8")
    return text


def _pages(text):
    parts = re.split(r"^### \[(?:p\.|슬라이드 |셀 )[^\]]*\]\s*$", text, flags=re.M)
    return [x.strip() for x in parts if x.strip()]


def term_candidates(texts, sub, domain=frozenset()):
    """자료 텍스트들 → {후보: {"count", "files", "pair", "title"}}.
    - "한국어(English)" / "English(한국어)" 짝, 코드 식별자(코드 표기 유지 과목)는 모두 후보
    - 영어 2~3낱말 묶음은 2번 이상 나오고 (여러 자료 · 페이지 제목 · 원서 용어) 중 하나면 후보
    - 영어 한 낱말은 원서 강조 용어·용어집(domain)에 있거나 페이지 제목에 3번 이상 나올 때만 (일반 영어 낱말을 거름)
    - 거의 모든 자료에 비슷한 횟수로 나오는 머리말·꼬리말(교수님 이름, 학교 이름)은 뺌"""
    stop = {C.term_key(w) for w in sub.get("term_stopwords") or []} | B.STOP
    cand = defaultdict(lambda: {"count": 0, "files": set(), "pair": set(), "title": 0, "code": False})
    for fname, text in texts:
        for page in _pages(text):
            first = page.split("\n", 1)[0].strip().lower()
            for rx, ko_first in ((_PAIR_KO_EN, True), (_PAIR_EN_KO, False)):
                for m in rx.finditer(page):
                    ko, en = (m.group(1), m.group(2)) if ko_first else (m.group(2), m.group(1))
                    en = en.strip()
                    if len(en.split()) > 4 or C.term_key(en) in stop:
                        continue
                    c = cand[en.lower()]
                    c["count"] += 1
                    c["files"].add(fname)
                    c["pair"].add(ko.strip())
            if sub.get("keep_code_identifiers"):
                for m in _CODE_ID.finditer(page):
                    c = cand[m.group(0)]
                    c["count"] += 1
                    c["files"].add(fname)
                    c["code"] = True
            low = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-']+", page)]
            for n in (1, 2, 3):
                for k in range(len(low) - n + 1):
                    g = low[k:k + n]
                    if g[0] in stop or g[-1] in stop or any(len(w) < 3 for w in g) or (n == 1 and len(g[0]) < 4):
                        continue
                    t = " ".join(g)
                    c = cand[t]
                    c["count"] += 1
                    c["files"].add(fname)
                    if t in first:
                        c["title"] += 1
    nfiles = max(1, len(texts))
    out = {}
    for t, c in cand.items():
        nf = len(c["files"])
        if C.term_key(t) in stop or (nfiles >= 4 and nf >= nfiles * 0.5 and c["count"] <= nf * 2
                                     and not c["pair"] and t not in domain):
            continue  # 머리말·꼬리말 (교수님 이름, 학교 이름처럼 자료마다 한두 번씩)
        multi = " " in t
        if c["pair"] or c["code"]:
            out[t] = c
        elif multi and c["count"] >= 2 and (nf >= 2 or c["title"] or t in domain):
            out[t] = c
        elif not multi and (t in domain or c["title"] >= 3):
            out[t] = c
    # 더 긴 묶음에 거의 늘 포함되는 짧은 묶음은 뺌 ("cell" ⊂ "stem cell", "cell wall" ⊂ "plant cell wall")
    for t in sorted(out, key=len, reverse=True):
        if t not in out:
            continue
        words = t.split()
        for n in range(1, len(words)):
            for k in range(len(words) - n + 1):
                sub_t = " ".join(words[k:k + n])
                if sub_t in out and not out[sub_t]["pair"] and sub_t not in domain and out[sub_t]["count"] <= out[t]["count"] * 1.2:
                    out.pop(sub_t, None)
    return out


def spoken_forms(terms, notebook_text, pairs):
    """용어마다 NotebookLM 전사본에서 영어 그대로 / 한글 발음 / 한국어 용어로 적힌 횟수와 표기."""
    low = notebook_text.lower()
    words = [RV.split_particle(w)[0] for w in re.findall(r"[가-힣]+", notebook_text)]
    toks = Counter(words)
    toks.update(f"{a} {b}" for a, b in zip(words, words[1:]))
    common = {t for t, _ in toks.most_common(200)}  # 아주 흔한 낱말(근데, 여러분들)은 거의 같은 소리일 때만
    buckets = defaultdict(list)
    for tok, c in toks.items():
        k = RV.hangul_key(tok)
        if len(k) >= 2:
            buckets[k[0].upper() if k[0].isupper() else "_"].append((tok, k, c))
    out = {}
    for t in terms:
        ek = RV.english_key(t)
        if len(ek) < 4:  # 짧은 낱말은 한국어 낱말과 우연히 소리가 비슷해서 비교하지 않음
            forms = []
        else:
            heads = [ek[0], "_"] if ek[0].isupper() else ["_"]
            forms = [(tok, c) for h in heads for tok, k, c in buckets.get(h, [])
                     if len(k) >= 4 and abs(len(k) - len(ek)) <= 2
                     and RV.key_similarity(k, ek) >= (0.92 if tok in common else 0.8)]
        en = len(B._term_re(t).findall(low))
        forms.sort(key=lambda x: -x[1])
        ko = [(p, low.count(p.lower())) for p in sorted(pairs.get(t, ()))]
        out[t] = {"english": en, "hangul": forms[:5], "korean": [x for x in ko if x[1]]}
    return out


# ---------- Whisper 샘플 ↔ NotebookLM ----------

def samples(subject):
    return sorted((C.subject_dir(subject) / "cache" / "samples").glob("*.json"))


def whisper_pairs(subject):
    """샘플 Whisper 전사와 같은 녹음의 NotebookLM 전사본을 나란히 맞춰 다르게 적힌 낱말 [(Whisper, NotebookLM, 횟수, 예시)]."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import crosscheck as X
    s = C.settings()
    tr = C.root() / subject / s["midterm_dir"] / s["transcripts_dir"]
    cnt, ex, used = Counter(), {}, []
    for sp in samples(subject):
        d = C.load_json(sp) or {}
        nb = tr / f"{d.get('source_file', '')}.md"
        if not nb.exists() or not d.get("segments"):
            continue
        used.append(d["source_file"])
        sents = X.sentences(nb.read_text(encoding="utf-8", errors="replace"))
        segs, dur = d["segments"], d.get("duration") or d["segments"][-1]["end"]
        for k in range(0, len(segs), 6):  # 6문장씩 NotebookLM의 같은 부분을 찾아 낱말 단위로 비교
            chunk = segs[k:k + 6]
            text = " ".join(x["text"] for x in chunk)
            a, b, sc = X.locate(sents, text, chunk[0]["start"] / dur, chunk[-1]["end"] / dur)
            if sc < 0.3:
                continue
            ref = " ".join(sents[a:b + 1])
            wa, wb = text.split(), ref.split()
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
                if op != "replace" or i2 - i1 > 2 or j2 - j1 > 2:
                    continue
                w, r = " ".join(wa[i1:i2]).strip(".,?!"), " ".join(wb[j1:j2]).strip(".,?!")
                ws, wp = RV.split_particle(w)
                rs, rp = RV.split_particle(r)
                if wp and wp == rp:
                    w, r = ws, rs
                if len(w.replace(" ", "")) <= 2 or w == r or difflib.SequenceMatcher(None, w, r).ratio() < 0.3:
                    continue
                cnt[(w, r)] += 1
                ex.setdefault((w, r), f"{d['source_file']} {chunk[0]['start']:.0f}초: …{text[:60]}…")
    rows = [(w, r, c, ex[(w, r)]) for (w, r), c in cnt.most_common() if c >= 2]
    return rows, used


# ---------- 실행 ----------

def extract(subject, exclude=()):
    sub = C.subject(subject)
    od = out_dir(subject)
    od.mkdir(parents=True, exist_ok=True)
    src = sources(subject, exclude)
    mats = [x for x in src if x["use"] and x["kind"] not in ("NotebookLM 전사본", "원서")]
    texts, titles = [], {}
    for m in mats:
        try:
            t = material_text(m["path"], od)
        except Exception as e:
            print(f"경고: {m['name']} 텍스트를 읽지 못함 ({e})")
            continue
        texts.append((m["name"], t))
        titles[m["name"]] = [p.split("\n", 1)[0].strip()[:60] for p in _pages(t)]
    glo = C.load_json(C.subject_dir(subject) / "glossary.json", {"terms": []})
    known = C.glossary_keys(glo)
    bt = B.book_terms(subject) if B.book_files(subject) else {}
    domain = frozenset({t.lower() for t, e in bt.items() if e["kind"] & {"emphasis", "index", "algorithm"} or " " in t}
                       | {t["term"].lower() for t in glo["terms"]})
    cand = term_candidates(texts, sub, domain)
    ranked = sorted(cand.items(), key=lambda kv: (-(bool(kv[1]["pair"]) * 5 + kv[1]["title"] * 3 + len(kv[1]["files"]) * 2
                                                    + min(kv[1]["count"], 20)), kv[0]))
    L = [f"# {subject} 중간고사 자료 용어 후보", "", f"- 자료 {len(texts)}개 (교수님 자료만). 용어집에 이미 있는 것은 [용어집] 표시.",
         "- 형식: 후보 · 나온 횟수 · 자료 수 · 제목 여부 · 한국어 짝", ""]
    for t, c in ranked[:400]:
        L.append(f"- {t} · {c['count']}회 · {len(c['files'])}개 자료" + (" · 제목" if c["title"] else "")
                 + (f" · 짝: {', '.join(sorted(c['pair']))}" if c["pair"] else "") + (" [용어집]" if C.term_key(t) in known else ""))
    L += ["", "## 자료별 페이지 제목"]
    for name, ts in titles.items():
        L.append(f"- {name}: " + " / ".join(dict.fromkeys(x for x in ts if x))[:700])
    (od / "materials.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    nb_text = ""
    for x in src:
        if x["use"] and x["kind"] == "NotebookLM 전사본":
            nb_text += Path(x["path"]).read_text(encoding="utf-8", errors="replace") + "\n"
    top = [t for t, _ in ranked[:250]] + [t["term"] for t in glo["terms"] if re.search(r"[A-Za-z]", t["term"])]
    top = list(dict.fromkeys(x.lower() if not sub.get("keep_code_identifiers") else x for x in top))
    pairs = {t: c["pair"] for t, c in cand.items() if c["pair"]}
    sp = spoken_forms(top, nb_text, pairs) if nb_text else {}
    L = [f"# {subject}: 교수님이 용어를 어떻게 말하는지 (중간고사 NotebookLM 전사본 기준, 인식 오류 있음)", "",
         "- 형식: 용어 · 영어로 적힌 수 · 한글 발음으로 적힌 표기(수) · 한국어 용어로 적힌 표기(수)",
         "- 한글 발음이 많으면 교수님이 영어로 말한 용어 → 영어 표기 + 한글 발음은 별칭.",
         "- 한국어 용어가 많으면 교수님이 한국어로 말한 용어 → 한국어 표기 (영어로 번역하지 않음).", ""]
    for t, v in sorted(sp.items(), key=lambda kv: -(kv[1]["english"] + sum(c for _, c in kv[1]["hangul"]) + sum(c for _, c in kv[1]["korean"]))):
        tot = v["english"] + sum(c for _, c in v["hangul"]) + sum(c for _, c in v["korean"])
        if tot < 2:
            continue
        L.append(f"- {t} · 영어 {v['english']} · 한글 발음 " + (", ".join(f"{a}({c})" for a, c in v["hangul"]) or "0")
                 + " · 한국어 " + (", ".join(f"{a}({c})" for a, c in v["korean"]) or "0"))
    (od / "spoken.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    corpus = (" ".join(t for _, t in texts) + " " + nb_text).lower()
    rows = [(t, e) for t, e in bt.items() if len(t) > 3 and (" " in t or t.lower() in cand)
            and B._term_re(t.lower()).search(corpus)]
    rows.sort(key=lambda kv: (-len(kv[1]["kind"]), -kv[1]["count"]))
    L = [f"# {subject}: 원서 용어 중 중간고사 자료·전사본에도 나오는 것 (철자 확인용, 절 번호 없음)", ""]
    L += [f"- {t} · {'/'.join(sorted(e['kind']))}" + (f" · {'; '.join(e['where'])}" if e["where"] else "") for t, e in rows[:300]]
    (od / "book.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    wp, used = whisper_pairs(subject)
    L = [f"# {subject}: 중간고사 샘플 Whisper ↔ NotebookLM 다르게 적힌 낱말 (2번 이상)", "",
         f"- 샘플: {', '.join(used) or '없음 (화면의 \"중간고사 샘플 전사\"로 먼저 돌리세요)'}",
         "- NotebookLM도 틀릴 수 있다. 어느 쪽이 맞는지는 자료·원서·문맥으로 판단하고, 한쪽이 확실히 틀린 것만 교정 규칙 후보로.",
         "- 형식: Whisper → NotebookLM · 횟수 · 예시", ""]
    L += [f"- {w} → {r} · {c}회 · {e}" for w, r, c, e in wp[:200]]
    (od / "whisper.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return {"materials": len(texts), "candidates": len(cand), "spoken": len(sp), "book": len(rows),
            "whisper_pairs": len(wp), "samples": used, "dir": od}


def apply(subject):
    """proposal.json → 검사 → suggestions.json. 다시 실행하면 이 명령의 대기 중 제안은 새로 계산."""
    od = out_dir(subject)
    pr = C.load_json(od / "proposal.json")
    if not isinstance(pr, dict):
        sys.exit(f"proposal.json이 없거나 형식이 틀립니다: {od / 'proposal.json'}")
    sub = C.subject(subject)
    glo = C.load_json(C.subject_dir(subject) / "glossary.json", {"terms": []})
    known = C.glossary_keys(glo)
    stop = {C.term_key(w) for w in sub.get("term_stopwords") or []}
    fixes = {r["wrong"] for r in C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]}
    C.drop_source_suggestions(subject, BOOTSTRAP_SOURCE, origin="bootstrap", types=None)
    items, skipped, warns = [], [], []
    for t in pr.get("terms") or []:
        t = t if isinstance(t, dict) else {"term": t}
        term = str(t.get("term") or "").strip()
        if not term or C.term_key(term) in known | stop:
            skipped.append(term)
            continue
        ev = {"source": BOOTSTRAP_SOURCE, "detail": str(t.get("evidence") or "")[:200]}
        items.append({"type": "term", "origin": "bootstrap", "evidence": [ev], "claude": _claude(t),
                      "data": {"term": term, "aliases": [a for a in t.get("aliases") or [] if isinstance(a, str)],
                               "from": "bootstrap", "deck": "중간고사 자료·원서", "pages": [],
                               "score": int(t.get("score") or 0), "in_title": bool(t.get("in_title")),
                               "recommended": bool(t.get("recommended", True))}})
    for r in pr.get("fix_rules") or []:
        wrong, right = str(r.get("wrong") or "").strip(), str(r.get("right") or "").strip()
        if len(wrong.replace(" ", "")) <= 2 or not right or wrong == right or C.term_key(wrong) in stop or wrong in fixes:
            skipped.append(f"{wrong} → {right}")
            continue
        for w in RV.rule_warnings(subject, wrong, right):
            warns.append(f"{wrong} → {right}: {w['text']}")
        items.append({"type": "fix_rule", "origin": "bootstrap", "claude": _claude(r),
                      "evidence": [{"source": BOOTSTRAP_SOURCE, "segments": str(r.get("evidence") or "")[:200]}],
                      "data": {"wrong": wrong, "right": right}})
    for n in pr.get("notation") or []:
        if isinstance(n, dict) and n.get("spoken") and n.get("written"):
            items.append({"type": "notation", "origin": "bootstrap", "evidence": [{"source": BOOTSTRAP_SOURCE}], "claude": _claude(n),
                          "data": {"spoken": n["spoken"], "written": n["written"]}})
    added = C.add_suggestions(subject, items)
    return added, skipped, warns, A.auto_resolve(subject)


def _claude(x):
    """proposal.json 항목의 자동 승인 판단 {"auto", "why", "count"} (auto가 없으면 None → 프로그램 기준만)."""
    if not isinstance(x, dict) or "auto" not in x:
        return None
    out = {"auto": bool(x["auto"]), "why": str(x.get("why") or "").strip()}
    if isinstance(x.get("count"), int):
        out["count"] = x["count"]
    return out


def main():
    C.utf8_stdout()
    a = sys.argv[1:]
    if not a or a[0].startswith("--"):
        sys.exit(__doc__)
    subject = a[0]
    C.subject(subject)
    ex = []
    if "--exclude" in a:
        ex = [x for x in a[a.index("--exclude") + 1:] if not x.startswith("--")]
    if "--list" in a:
        src = sources(subject, ex)
        n_use = sum(1 for x in src if x["use"])
        print(f"{subject}: 출처 {len(src)}개 중 {n_use}개 사용")
        for x in src:
            mark = "사용" if x["use"] else "제외"
            print(f"- [{mark}] [{x['kind']}] {x['name']}" + (f" — {x['why']}" if x["why"] else ""))
        print(f"중간고사 샘플 Whisper 전사: {len(samples(subject))}개")
    elif "--extract" in a:
        r = extract(subject, ex)
        print(f"정리 완료 → {r['dir']}: 자료 {r['materials']}개 · 용어 후보 {r['candidates']}개 · 말한 방식 {r['spoken']}개"
              f" · 원서 용어 {r['book']}개 · Whisper↔NotebookLM 차이 {r['whisper_pairs']}개 (샘플 {len(r['samples'])}개)")
    elif "--apply" in a:
        added, skipped, warns, auto = apply(subject)
        print(f"제안 {added}개 추가" + (f" · 제외 {len(skipped)}개 (이미 있음·일반 단어·2글자 이하)" if skipped else ""))
        for w in warns:
            print("경고:", w)
        print("제안 분류: " + A.summary_line(auto))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
