"""Claude Code가 쓴 edits.json 검증 → 원본에 적용 → 문단 조립 → 최종 md → 제안 병합 → 작업 상태 "완료".

    python tools/apply_edits.py <작업이름> [--overwrite]

jobs/<작업이름>/edits.json (길면 edits.part01.json, edits.part02.json …를 이름순으로 합침)
    {"edits": [{"i": 152, "text": "고친 문장 전체", "reason": "포토신세시스→photosynthesis"}],      바뀐 세그먼트만
                                                                       ("text": ""는 환각·잡음 문장 삭제)
     "paragraphs": [{"from": 40, "to": 58, "heading": "광합성 개요",
                     "slide": {"file": "Lecture07.pdf", "pages": "3-5", "confidence": 0.8,
                               "at": {"40": 3, "47": 4}}}],                                 slide·at은 선택
     "inaudible": [{"start": "12:30", "end": "12:45", "guess": "추정 내용(있을 때만)"}],
     "suggestions": {"terms": [...], "fix_rules": [{"wrong", "right", "evidence"}],
                     "progress": {"deck", "page_to"}, "notation": [{"spoken", "written"}]},
     "comprehension": {"score": 0~100, "weak_ranges": [{"from", "to", "why"}], "need_notebooklm", "reason"},
     "important": [{"from": 300, "to": 318, "kind": "exam", "summary": "한 줄 요약", "why": "\"시험에 나온다\""}],   시험·퀴즈·과제·강조
     "review": [{"i": 85, "kind": "contradiction", "candidates": ["증가", "감소"], "why": "뜻이 뒤집힘"}]}           확인 목록

확인 목록 = review의 모순 의심(강의 어디서나) + 중요 구간 안의 review 항목·청취 불가·불확실 (work.build_review).
중요 구간 밖의 인식 실패·불확실은 처리 메모에만. 확인 목록이 비면 작업은 바로 "완료", 있으면 화면에서 확인 → [완료].

덧붙이는 파일 (기존 edits는 그대로 두고 그 위에 적용, 나중에 쓴 파일이 우선):
    edits.recorrect*.json  /recorrect: paragraphs가 있으면 문단·슬라이드 위치를 이것으로 바꿈, inaudible이 있으면 바꿈,
                           edits는 같은 번호의 기존 교정을 덮어씀 (원문과 같은 text면 기존 교정을 취소)
    edits.crosscheck*.json /crosscheck: edits만 덮어씀 (paragraphs는 무시), comprehension이 있으면 바꿈
    두 파일 모두 important·review가 있으면 그것으로 바꿈 (나중에 쓴 파일이 우선)

검사: 세그먼트 번호 범위, 문단 누락·겹침, 등록된 강의자료·페이지 범위, 시각 형식.
같은 이름의 md가 과목 폴더에 이미 있고 이 작업이 쓴 파일이 아니면 멈춘다 → 사용자 확인 후 --overwrite.
다시 실행해도 된다 (결과 화면에서 되돌린 문장은 같은 교정이면 유지, 이 작업의 대기 중 제안은 새로 계산).
"""
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import work as W
import auto as A

LONG_EDIT = 3  # 교정문이 원문의 이 배수 + 40자보다 길면 경고 (내용 추가 의심)


def _ranges(nums):
    nums = sorted(nums)
    out, start = [], None
    for n, x in enumerate(nums):
        if start is None:
            start = x
        if n + 1 == len(nums) or nums[n + 1] != x + 1:
            out.append(f"{start}" if start == x else f"{start}–{x}")
            start = None
    return ", ".join(out)


OVERRIDE = ("edits.recorrect", "edits.crosscheck")


def _kind(p):
    return next((k.split(".")[1] for k in OVERRIDE if p.name.startswith(k)), "base")


def load_parts(name):
    """edits 파일들을 합침. 기본 파일(edits.json, edits.partNN.json) 다음에 덧붙이는 파일(recorrect·crosscheck)을 쓴 순서대로.
    edits 항목에는 rank(0 = 기본, 1… = 덧붙인 순서)를 붙여 check()에서 나중 것이 우선하게 한다."""
    files = W.edits_files(name)
    if not files:
        sys.exit(f"edits.json이 없습니다: {W.work_dir(name)}")
    base = [p for p in files if _kind(p) == "base"]
    over = sorted((p for p in files if _kind(p) != "base"), key=lambda p: (p.stat().st_mtime, p.name))
    merged = {"edits": [], "paragraphs": [], "inaudible": [], "important": [], "review": [],
              "suggestions": {"terms": [], "fix_rules": [], "progress": None, "notation": []}, "comprehension": [],
              "notes": []}
    re_paras, re_ina, over_comp, replace = None, None, None, {}
    for rank, p in enumerate(base + over):
        rank = 0 if p in base else rank - len(base) + 1
        kind = _kind(p)
        try:
            d = C.load_json(p)
        except ValueError as e:
            sys.exit(f"오류: {p.name}이(가) 올바른 JSON이 아닙니다: {e}")
        if not isinstance(d, dict):
            sys.exit(f"오류: {p.name}의 최상위는 JSON object여야 합니다")
        for k in ("edits", "paragraphs", "inaudible", "important", "review"):
            if not isinstance(d.get(k) or [], list):
                sys.exit(f'오류: {p.name}의 "{k}"는 배열이어야 합니다')
        merged["edits"] += [{**e, "_rank": rank, "_file": p.name} if isinstance(e, dict) else e for e in d.get("edits") or []]
        for k in ("important", "review"):
            if kind == "base":
                merged[k] += d.get(k) or []
            elif k in d:
                replace[k] = d.get(k) or []
        if kind == "base":
            merged["paragraphs"] += d.get("paragraphs") or []
            merged["inaudible"] += d.get("inaudible") or []
        elif kind == "recorrect":
            if d.get("paragraphs"):
                re_paras = (re_paras or []) + d["paragraphs"]
            if "inaudible" in d:
                re_ina = (re_ina or []) + (d.get("inaudible") or [])
        else:
            if d.get("paragraphs"):
                merged["notes"].append(f"{p.name}: crosscheck 파일의 paragraphs는 무시합니다")
            merged["inaudible"] += d.get("inaudible") or []
        sg = d.get("suggestions") or {}
        if isinstance(sg, dict):
            for k in ("terms", "fix_rules", "notation"):
                if isinstance(sg.get(k), list):
                    merged["suggestions"][k] += sg[k]
            if isinstance(sg.get("progress"), dict):
                merged["suggestions"]["progress"] = sg["progress"]
        if isinstance(d.get("comprehension"), dict):
            if kind == "base":
                merged["comprehension"].append(d["comprehension"])
            else:
                over_comp = d["comprehension"]
    if re_paras is not None:
        merged["paragraphs"] = re_paras
        merged["notes"].append("문단·슬라이드 위치는 edits.recorrect의 것을 씁니다")
    if re_ina is not None:
        merged["inaudible"] = re_ina
    if over_comp is not None:
        merged["comprehension"] = [over_comp]
    merged.update(replace)
    return files, merged


def check(m, segs, subject):
    errs, warns = [], []
    ids = {s["idx"] for s in segs}
    orig = {s["idx"]: s for s in segs}
    lo, hi = min(ids), max(ids)

    edits, ranks = {}, {}
    for n, e in enumerate(sorted(m["edits"], key=lambda e: e.get("_rank", 0) if isinstance(e, dict) else 0)):
        w = f"edits[{n}]"
        if not isinstance(e, dict) or not isinstance(e.get("i"), int) or isinstance(e.get("i"), bool):
            errs.append(f'{w}: "i"(정수)가 없습니다')
            continue
        i = e["i"]
        if i not in ids:
            errs.append(f"{w}: 없는 세그먼트 번호 {i} (범위 {lo}–{hi})")
            continue
        t, reason = e.get("text"), str(e.get("reason") or "").strip()
        if not isinstance(t, str) or (not t.strip() and not reason):
            errs.append(f'{w} (i={i}): "text"(고친 문장 전체)가 없습니다 (환각·잡음이라 지울 때는 "text": ""와 "reason"을 함께)')
            continue
        t = " ".join(t.split())
        rank = e.get("_rank", 0)
        if orig[i].get("missing"):
            warns.append(f"세그먼트 {i}: 청취 불가(M) 줄은 고칠 수 없어 교정을 무시합니다 (추정은 inaudible[].guess에)")
            continue
        if i in ranks and ranks[i] == rank and edits.get(i, {}).get("text", orig[i]["text"].strip()) != t:
            errs.append(f"{w}: 세그먼트 {i}의 교정이 두 번 있고 내용이 다릅니다 ({e.get('_file', '')})")
            continue
        if i in ranks and ranks[i] > rank:
            continue  # 나중에 쓴 파일(recorrect·crosscheck)의 교정이 우선
        ranks[i] = rank
        if t == orig[i]["text"].strip():
            edits.pop(i, None)  # 원문과 같으면 교정 없음 (덧붙인 파일에서는 기존 교정 취소)
            continue
        if len(t) > LONG_EDIT * len(orig[i]["text"]) + 40:
            warns.append(f"세그먼트 {i}: 교정문이 원문보다 많이 깁니다 ({len(orig[i]['text'])}자 → {len(t)}자). 내용을 덧붙이지 않았는지 확인하세요")
        edits[i] = {"text": t, "reason": reason}

    decks = {p.stem: C.load_json(p) for p in (C.subject_dir(subject) / "decks").glob("*.json")}
    decks = {d["file"]: d for d in decks.values() if d and d.get("registered")}
    paras, cover = [], {}
    for n, p in enumerate(m["paragraphs"]):
        w = f"paragraphs[{n}]"
        if not isinstance(p, dict) or not all(isinstance(p.get(k), int) for k in ("from", "to")):
            errs.append(f'{w}: "from"·"to"(정수)가 없습니다')
            continue
        a, b = p["from"], p["to"]
        if a > b or a not in ids or b not in ids:
            errs.append(f"{w}: 범위 {a}–{b}가 잘못됐습니다 (세그먼트 {lo}–{hi})")
            continue
        if not isinstance(p.get("heading"), str) or not p["heading"].strip():
            errs.append(f'{w} ({a}–{b}): "heading"이 없습니다')
            continue
        for i in range(a, b + 1):
            cover.setdefault(i, []).append(n)
        slide = p.get("slide")
        if slide:
            slide, e = _check_slide(slide, decks, a, b)
            if e:
                errs.append(f"{w} ({a}–{b}): {e}")
                continue
        paras.append({"from": a, "to": b, "heading": " ".join(p["heading"].split()), "slide": slide or None})
    missing = sorted(ids - set(cover))
    dup = sorted(i for i, v in cover.items() if len(v) > 1)
    if missing:
        errs.append(f"어느 문단에도 속하지 않은 세그먼트: {_ranges(missing)}")
    if dup:
        errs.append(f"두 문단 이상에 속한 세그먼트: {_ranges(dup)}")
    paras.sort(key=lambda p: p["from"])

    ina = []
    for n, x in enumerate(m["inaudible"]):
        try:
            a, b = W.parse_ts(x["start"]), W.parse_ts(x["end"])
            if a > b:
                raise ValueError("start가 end보다 늦습니다")
        except (KeyError, TypeError, ValueError) as e:
            errs.append(f"inaudible[{n}]: {e}")
            continue
        ina.append({"start": x["start"], "end": x["end"], "start_s": a, "end_s": b,
                    "guess": str(x.get("guess") or "").strip()})

    imp = []
    for n, x in enumerate(m["important"]):
        w = f"important[{n}]"
        if not isinstance(x, dict) or not all(_int(x.get(k)) for k in ("from", "to")):
            errs.append(f'{w}: "from"·"to"(정수)가 없습니다')
            continue
        a, b = x["from"], x["to"]
        if a > b or a not in ids or b not in ids:
            errs.append(f"{w}: 범위 {a}–{b}가 잘못됐습니다 (세그먼트 {lo}–{hi})")
            continue
        summary = " ".join(str(x.get("summary") or "").split())
        if not summary:
            errs.append(f'{w} ({a}–{b}): "summary"(무엇이 중요한지 한 줄 요약)가 없습니다')
            continue
        if x.get("kind") not in W.IMPORTANT_KIND:
            warns.append(f'{w} ({a}–{b}): kind {x.get("kind")!r} → "emphasis"로 씁니다 ({", ".join(W.IMPORTANT_KIND)})')
        imp.append({"from": a, "to": b, "kind": x.get("kind") if x.get("kind") in W.IMPORTANT_KIND else "emphasis",
                    "summary": summary, "why": " ".join(str(x.get("why") or "").split())})
    imp.sort(key=lambda x: x["from"])

    rev, seen = [], set()
    for n, x in enumerate(m["review"]):
        w = f"review[{n}]"
        if not isinstance(x, dict) or not _int(x.get("i")):
            errs.append(f'{w}: "i"(정수)가 없습니다')
            continue
        i, kind = x["i"], x.get("kind")
        if i not in ids:
            errs.append(f"{w}: 없는 세그먼트 번호 {i} (범위 {lo}–{hi})")
            continue
        if kind not in W.REVIEW_KIND:
            errs.append(f'{w} (i={i}): kind는 {", ".join(W.REVIEW_KIND)} 중 하나여야 합니다')
            continue
        cands = list(dict.fromkeys(str(c).strip() for c in x.get("candidates") or [] if str(c).strip()))
        if kind == "contradiction" and len(cands) < 2:
            errs.append(f'{w} (i={i}): 모순 의심은 "candidates"에 후보를 2개 이상 (예: ["증가", "감소"])')
            continue
        if i in seen:
            warns.append(f"{w}: 세그먼트 {i}가 확인 목록에 두 번 있어 처음 것만 씁니다")
            continue
        seen.add(i)
        if kind == "contradiction" and i in edits and any(c in edits[i]["text"] and c not in orig[i]["text"] for c in cands):
            warns.append(f"세그먼트 {i}: 모순 의심 문장의 후보 낱말을 교정으로 바꿨습니다 — 추측해서 고치지 말고 확인 목록에만 두세요"
                         " (확실한 교정이면 review에서 빼세요)")
        rev.append({"i": i, "kind": kind, "candidates": cands, "why": " ".join(str(x.get("why") or "").split())})
    return errs, warns, edits, paras, ina, imp, rev


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_slide(sl, decks, a, b):
    if not isinstance(sl, dict) or not isinstance(sl.get("file"), str):
        return None, 'slide에 "file"이 없습니다 (위치를 모르면 slide를 null로)'
    d = decks.get(sl["file"])
    if not d:
        return None, f"등록되지 않은 강의자료: {sl['file']} (등록된 자료: {', '.join(decks) or '없음'})"
    total = d.get("pages_total") or 0
    pg = str(sl.get("pages", "")).replace("–", "-").replace(" ", "")
    try:
        nums = [int(x) for x in pg.split("-")] if pg else []
    except ValueError:
        nums = []
    if not 1 <= len(nums) <= 2:
        return None, f'slide.pages 형식이 잘못됐습니다: {sl.get("pages")!r} (예: "3" 또는 "3-5")'
    rng = [nums[0], nums[-1]]
    if not 1 <= rng[0] <= rng[1] <= total:
        return None, f"slide.pages {pg}가 {sl['file']}의 쪽 범위(1–{total})를 벗어납니다"
    conf = sl.get("confidence")
    if conf is not None and not (isinstance(conf, (int, float)) and 0 <= conf <= 1):
        return None, "slide.confidence는 0~1 사이 숫자여야 합니다"
    at = {}
    raw = sl.get("at") or {}
    if isinstance(raw, list):
        raw = {str(x.get("i")): x.get("page") for x in raw if isinstance(x, dict)}
    for k, v in raw.items():
        try:
            i, page = int(k), int(v)
        except (TypeError, ValueError):
            return None, f"slide.at 항목이 잘못됐습니다: {k}: {v}"
        if not a <= i <= b or not 1 <= page <= total:
            return None, f"slide.at {i}: p.{page}가 문단 범위({a}–{b}) 또는 쪽 범위(1–{total})를 벗어납니다"
        at[str(i)] = page
    return {"file": sl["file"], "pages": pg, "range": rng, "confidence": conf, "at": at}, None


def comprehension(parts):
    if not parts:
        return None
    scores = [p["score"] for p in parts if isinstance(p.get("score"), (int, float))]
    return {"score": round(sum(scores) / len(scores)) if scores else None,
            "weak_ranges": [w for p in parts for w in p.get("weak_ranges") or [] if isinstance(w, dict)],
            "need_notebooklm": any(bool(p.get("need_notebooklm")) for p in parts),
            "reason": " / ".join(str(p["reason"]) for p in parts if p.get("reason"))}


def _claude(x):
    """제안에 Claude가 적은 자동 승인 판단 {"auto": bool, "why": str} (없으면 None → 프로그램 기준만)."""
    if not isinstance(x, dict) or "auto" not in x:
        return None
    return {"auto": bool(x["auto"]), "why": str(x.get("why") or "").strip()}


def suggestions(name, job, sg):
    """교정 중에 나온 제안을 suggestions.json에 (이 작업에서 나온 대기 중 제안은 지우고 새로) → 자동 승인 분류."""
    subject = job["subject"]
    C.drop_source_suggestions(subject, name, origin="correct", types=None)
    glo = C.load_json(C.subject_dir(subject) / "glossary.json", {"terms": []})
    known = C.glossary_keys(glo)
    stop = {C.term_key(w) for w in C.subject(subject).get("term_stopwords") or []}
    fixes = {C.term_key(r["wrong"]) for r in C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]}
    ev = {"source": name}
    items, skipped = [], 0
    for t in sg["terms"]:
        term, aliases = (t.get("term"), t.get("aliases") or []) if isinstance(t, dict) else (t, [])
        if not isinstance(term, str) or not term.strip() or C.term_key(term) in known | stop:
            skipped += 1
            continue
        e = {**ev, "segments": str(t.get("evidence") or "")} if isinstance(t, dict) and t.get("evidence") else dict(ev)
        items.append({"type": "term", "origin": "correct", "evidence": [e], "claude": _claude(t),
                      "data": {"term": term.strip(), "aliases": [a for a in aliases if isinstance(a, str)],
                               "from": "lecture", "deck": f"강의 {name}", "pages": [], "score": 0,
                               "in_title": False, "recommended": True}})
    for r in sg["fix_rules"]:
        if not isinstance(r, dict):
            continue
        wrong, right = str(r.get("wrong") or "").strip(), str(r.get("right") or "").strip()
        # 2글자 이하·흔한 일반 단어·이미 있는 규칙은 제외
        if len(wrong.replace(" ", "")) <= 2 or not right or wrong == right or C.term_key(wrong) in stop | fixes:
            skipped += 1
            continue
        items.append({"type": "fix_rule", "origin": "correct", "claude": _claude(r),
                      "evidence": [{**ev, "segments": str(r.get("evidence") or "")}],
                      "data": {"wrong": wrong, "right": right}})
    if sg["progress"] and sg["progress"].get("deck"):
        items.append({"type": "progress", "origin": "correct", "evidence": [dict(ev)],
                      "data": {"deck": sg["progress"]["deck"], "page": sg["progress"].get("page_to"),
                               "lecture": A.lecture_order(job["source_file"])}})
    for n in sg["notation"]:
        if isinstance(n, dict) and n.get("spoken") and n.get("written"):
            items.append({"type": "notation", "origin": "correct", "evidence": [dict(ev)], "claude": _claude(n),
                          "data": {"spoken": n["spoken"], "written": n["written"]}})
    added = C.add_suggestions(subject, items)
    return added, skipped, A.auto_resolve(subject)


def update_glossary_seen(name, job, result):
    """교정본에 나온 용어집 용어(대표 표기·별칭)의 등장 강의(seen_in)·빈도(freq = 등장 강의 수)·최근 등장일을 갱신.
    프롬프트 빌더가 우선순위 다음으로 최근성·빈도를 본다. 다시 실행해도 같은 강의는 한 번만 센다."""
    subject = job["subject"]
    path = C.subject_dir(subject) / "glossary.json"
    glo = C.load_json(path, {"terms": []})
    text = " ".join(W.final_text(result, s) or "" for s in result["segments"]).lower()
    m = re.match(r"^.+?_(\d{2})(\d{2})(\d{2})", job["source_file"])
    day = f"20{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else result["applied"][:10]
    n = 0
    for t in glo["terms"]:
        forms = [t["term"]] + (t.get("aliases") or [])
        if not any(f and f.lower() in text for f in forms):
            continue
        seen = t.setdefault("seen_in", [])
        if name not in seen:
            seen.append(name)
            n += 1
        t["freq"] = len(seen)
        if day > (t.get("last_seen") or ""):
            t["last_seen"] = day
    C.save_json(path, glo)
    return n


def main():
    C.utf8_stdout()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit("사용법: python tools/apply_edits.py <작업이름> [--overwrite]")
    name = args[0]
    job = W.load_job(name)
    if not job:
        sys.exit(f"작업이 없습니다: {W.work_dir(name)}")
    if W.is_superseded(job):
        sys.exit(f"멈춤: {name}은(는) {job.get('superseded_by')}(으)로 대체된 작업입니다 (같은 녹음을 다시 전사). "
                 f"새 작업에서 교정하세요: /correct {job.get('superseded_by')}")
    segs = W.load_segments(name)
    files, m = load_parts(name)
    errs, warns, edits, paras, ina, imp, rev = check(m, segs, job["subject"])
    for x in m["notes"]:
        print("참고:", x)
    for w in warns:
        print("경고:", w)
    if errs:
        print(f"오류 {len(errs)}개 — {', '.join(p.name for p in files)}을(를) 고친 뒤 다시 실행하세요:")
        for e in errs:
            print(" -", e)
        sys.exit(1)

    rpath = W.work_dir(name) / "result.json"
    prev = C.load_json(rpath) or {}
    prev_text = {s["i"]: s.get("text") for s in prev.get("segments", [])}
    reverted = [i for i in prev.get("reverted") or [] if i in edits and edits[i]["text"] == prev_text.get(i)]
    s = C.settings()
    result = {
        "segments": [{"i": x["idx"], "start": x["start"], "end": x["end"], "orig": x["text"],
                      "text": edits[x["idx"]]["text"] if x["idx"] in edits else None,
                      "reason": edits[x["idx"]]["reason"] if x["idx"] in edits else "",
                      "flag": W.seg_flag(x, s), "region": (x.get("restore") or {}).get("region")} for x in segs],
        "paragraphs": paras, "inaudible": ina, "reverted": reverted, "important": imp,
        "comprehension": comprehension(m["comprehension"]),
        "edits_files": [p.name for p in files], "applied": C.now_iso(),
    }
    result["review"], result["review_memo"] = W.build_review(result, rev)
    if result["review_memo"]:
        print(f"참고: 중요 구간 밖의 review 항목 {len(result['review_memo'])}개는 확인 목록에 올리지 않고 처리 메모에만 남깁니다 (모순 의심만 어디서나 올림)")
    manual = W.load_manual(name)
    hidden = sorted(W.hidden_by_inaudible(result, manual))
    if hidden:
        print(f"경고: inaudible 구간 안에 있어 md에 넣지 않는 문장 {len(hidden)}개: "
              + ", ".join(f"{i}({W.ts(result['segments'][i]['start'])})" for i in hidden)
              + " — 남겨야 할 문장이면 inaudible의 start·end를 그 문장 밖으로 고치세요")
    C.save_json(rpath, result)

    ina_all = W.inaudible_list(result, manual)
    job.update({"status": "done", "applied": result["applied"], "comprehension": result["comprehension"],
                "stats": {"edits": len(edits), "removed": sum(1 for e in edits.values() if not e["text"]),
                          "paragraphs": len(paras), "reverted": len(reverted),
                          "located": sum(1 for p in paras if p["slide"]), "inaudible": len(ina_all),
                          "inaudible_seconds": round(sum(x["end_s"] - x["start_s"] for x in ina_all)),
                          "inaudible_correct": W.inaudible_by_source(ina_all)[1],
                          "restored": len(W.restored_regions(result)), "hidden": len(hidden),
                          "uncertain": sum(1 for x in result["segments"] if x["flag"] == "U")}})
    job["materials_registered"] = bool((job.get("context") or {}).get("decks_registered"))
    W.save_job(name, job)
    rs = W.update_review_summary(name, reopen=True)
    job = W.load_job(name)  # review 요약이 들어간 job.json
    try:
        path = W.write_md(name, overwrite="--overwrite" in sys.argv)
    except FileExistsError as e:
        job["status"] = "awaiting"
        W.save_job(name, job)
        print(f"멈춤: 과목 폴더에 같은 이름의 md가 이미 있습니다 (이 작업이 만든 파일이 아님):\n  {e}\n"
              f"사용자에게 덮어써도 되는지 확인한 뒤 --overwrite를 붙여 다시 실행하세요 (기존 파일은 backups/에 백업됨).")
        sys.exit(2)
    added, skipped, auto = suggestions(name, job, m["suggestions"])
    update_glossary_seen(name, job, result)
    for n in W.refresh_next_parts(name):  # 나뉜 녹음: 교정 대기인 뒷부분의 context.md에 이 교정본 마지막 문단을 넣음
        print(f"뒷부분 작업 {n}의 context.md를 다시 만들었습니다 (이 작업의 마지막 문단 반영)")
    st = job["stats"]
    print(f"적용 완료: {name} · 교정 {st['edits']}문장 · 문단 {st['paragraphs']}개 (슬라이드 위치 {st['located']}개)"
          f" · 복원 {st['restored']}곳 · 청취 불가 {st['inaudible']}곳 ({st['inaudible_seconds']}초)"
          + (f" (교정 중 표시 {st['inaudible_correct']}곳 포함)" if st["inaudible_correct"] else "") +
          f" · 제안 {added}개 추가" + (f" ({skipped}개 제외)" if skipped else ""))
    print("제안 분류: " + A.summary_line(auto))
    print(f"중요 구간 {len(imp)}곳 · " + (f"확인 목록 {rs['total']}곳 (남은 것 {rs['open']}곳) → 화면에서 듣고 확인한 뒤 [완료]"
                                          if rs["total"] else "확인 목록 없음 → 완료"))
    if reverted:
        print(f"결과 화면에서 되돌린 {len(reverted)}문장은 그대로 유지")
    print(f"md: {path}")


if __name__ == "__main__":
    main()
