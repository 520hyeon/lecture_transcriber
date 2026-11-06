"""Claude Code가 작성한 강의자료 인덱스 검사·등록 (/register-slides 마지막 단계).

    python tools/check_deck.py <과목> <파일명>
    python tools/check_deck.py <과목> <파일명> --resuggest   등록된 인덱스로 용어 제안만 다시 계산

슬라이드·손필기 → data/<과목>/decks/<파일명>.json
    {"file": "Lecture07.pdf", "title": "덱 제목",
     "pages": [{"page": 1, "title": "...", "summary": "...", "terms": [...], "figure_terms": [...],
                "code_names": [...], "unsure_terms": [...]}, ...]}
    code_names·unsure_terms는 선택이고 용어 제안에 넣지 않음.
    각 페이지의 텍스트 출처 "text_source"(pdf / ocr / image)는 pdf_pages.py 결과로 자동 기록.
참고 자료 → data/<과목>/refs/<파일명>.json
    {"file": "Lab07.ipynb", "summary": "...", "terms": [...]}

통과하면 원본 정보(크기, 수정 시각, 쪽수, 등록일)를 채우고,
용어집에 없는 용어를 suggestions.json에 "새 용어" 제안으로 추가한다 (규칙은 tools/terms.py).
그다음 tools/auto.py가 자동 승인 대상(2쪽 이상·페이지 제목)을 바로 용어집에 넣고, 나머지만 사람 확인으로 남긴다.
이 자료에서 나온 대기 중 용어 제안은 지우고 다시 만든다 (다시 등록해도 중복되지 않음).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import terms as T
import auto as A

SUMMARY_MAX = 400


def _str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def check_deck(data, total):
    errs, warns = [], []
    pages = data.get("pages")
    if not isinstance(pages, list):
        return ['"pages" 배열이 없습니다'], warns
    seen = {}
    for n, p in enumerate(pages):
        where = f"pages[{n}]"
        if not isinstance(p, dict) or not isinstance(p.get("page"), int):
            errs.append(f'{where}: "page"(정수)가 없습니다')
            continue
        where = f"p.{p['page']}"
        if p["page"] in seen:
            errs.append(f"{where}: 중복된 페이지")
        seen[p["page"]] = p
        for k in ("title", "summary"):
            if not isinstance(p.get(k), str):
                errs.append(f'{where}: "{k}"(문자열)가 없습니다')
        for k in ("terms", "figure_terms"):
            p.setdefault(k, [])
            if not _str_list(p[k]):
                errs.append(f'{where}: "{k}"는 문자열 배열이어야 합니다')
        for k in ("code_names", "unsure_terms"):
            if k in p and not _str_list(p[k]):
                errs.append(f'{where}: "{k}"는 문자열 배열이어야 합니다')
        if isinstance(p.get("summary"), str) and len(p["summary"]) > SUMMARY_MAX:
            warns.append(f"{where}: 요약이 깁니다 ({len(p['summary'])}자, 권장 {SUMMARY_MAX}자 이하)")
    missing = sorted(set(range(1, total + 1)) - set(seen))
    extra = sorted(set(seen) - set(range(1, total + 1)))
    if missing:
        errs.append(f"빠진 페이지: {', '.join(map(str, missing))}")
    if extra:
        errs.append(f"없는 페이지 번호: {', '.join(map(str, extra))} (총 {total}쪽)")
    pages.sort(key=lambda p: p.get("page", 0) if isinstance(p, dict) else 0)
    return errs, warns


def check_ref(data):
    errs = []
    if not isinstance(data.get("summary"), str):
        errs.append('"summary"(문자열)가 없습니다')
    if not _str_list(data.get("terms")):
        errs.append('"terms"(문자열 배열)가 없습니다')
    return errs, []


def term_suggestions(name, data, kind):
    """인덱스로 용어 제안을 (다시) 만든다. 이 자료에서 나온 대기 중 제안은 먼저 지우고 새로 계산.
    거르기·묶기·중요도 규칙은 tools/terms.py."""
    file = data["file"]
    glo = C.load_json(C.subject_dir(name) / "glossary.json", {"terms": []})
    known = C.glossary_keys(glo)
    dropped = C.drop_source_suggestions(name, file)
    items = []
    for g in T.candidates(data, kind, C.subject(name)):
        if any(C.term_key(f) in known for f in [g.rep] + g.aliases):
            continue
        pages = sorted(g.pages)
        ev = {"source": file}
        if pages:
            ev["pages"] = ", ".join(map(str, pages))
        src = "reference" if kind == "reference" else ("slide" if "slide" in g.kinds else "figure")
        items.append({"type": "term", "origin": "register-slides", "evidence": [ev],
                      "data": {"term": g.rep, "aliases": g.aliases, "from": src, "deck": file, "pages": pages,
                               "score": g.score, "in_title": g.in_title, "recommended": g.recommended}})
    added = C.add_suggestions(name, items)
    print("제안 분류: " + A.summary_line(A.auto_resolve(name)))
    return added, items, dropped


def main():
    C.utf8_stdout()
    args = [a for a in sys.argv[1:] if a != "--resuggest"]
    if len(args) != 2:
        sys.exit("사용법: python tools/check_deck.py <과목> <파일명> [--resuggest]")
    name, file = args
    C.subject(name)
    mat = C.resolve_material(name, file)
    if not mat:
        sys.exit(f"'{file}'은(는) {name}의 등록 대상 자료가 아닙니다.")
    is_ref = mat["kind"] == "reference"
    path = C.ref_path(name, mat["file"]) if is_ref else C.deck_path(name, mat["file"])
    meta = C.load_json(C.cache_dir(name, mat["file"]) / "pages.json")
    if not meta:
        sys.exit(f"먼저 pdf_pages.py로 추출하세요: {C.VENV_PY} tools/pdf_pages.py \"{mat['file']}\" --subject {name}")
    try:
        data = C.load_json(path)
    except ValueError as e:
        sys.exit(f"오류: {path.name}이(가) 올바른 JSON이 아닙니다: {e}")
    if data is None:
        sys.exit(f"오류: {path} 파일이 없습니다")
    if "--resuggest" in sys.argv:
        if not data.get("registered"):
            sys.exit(f"{mat['file']}은(는) 아직 등록되지 않았습니다. --resuggest 없이 실행하세요.")
        bak = C.backup_file(C.subject_dir(name) / "suggestions.json", f"{name}_suggestions")
        added, items, dropped = term_suggestions(name, data, mat["kind"])
        print(f"용어 제안 다시 계산: {mat['file']} · 기존 제안 {dropped}개 삭제 → {len(items)}개"
              f" (기본 체크 {sum(i['data']['recommended'] for i in items)}개) · 백업 {bak.name if bak else '없음'}")
        _print_items(items)
        return

    data["file"] = mat["file"]
    errs, warns = check_ref(data) if is_ref else check_deck(data, meta.get("pages_total") or 0)
    for w in warns:
        print("경고:", w)
    if errs:
        print(f"오류 {len(errs)}개 — {path.name}을(를) 고친 뒤 다시 실행하세요:")
        for e in errs:
            print(" -", e)
        sys.exit(1)

    st = Path(mat["path"]).stat()
    rel_src = Path(mat["path"]).resolve().relative_to(C.root().resolve()).as_posix()
    data.update({"kind": mat["kind"], "source": rel_src, "source_size": st.st_size,
                 "source_mtime": int(st.st_mtime), "registered": C.now_iso()})
    if not is_ref:
        data["pages_total"] = meta["pages_total"]
        src_of = {p["page"]: p.get("text_source", "pdf") for p in meta.get("pages", [])}
        for p in data["pages"]:
            p["text_source"] = src_of.get(p["page"], "pdf")
        data["text_sources"] = meta.get("counts")
    C.save_json(path, data)
    added, items, _ = term_suggestions(name, data, mat["kind"])
    what = "참고 자료" if is_ref else f"{data['pages_total']}쪽"
    rec = sum(i["data"]["recommended"] for i in items)
    print(f"등록 완료: {mat['file']} ({what}) · 새 용어 제안 {added}개 추가 (기본 체크 {rec}개)"
          + (f" · 다른 자료에서 대기 중인 제안 {len(items) - added}개는 근거만 합침" if len(items) > added else ""))


def _print_items(items):
    for i in items:
        d = i["data"]
        print(f"  {'[v]' if d['recommended'] else '[ ]'} {d['score']:>2}  {d['term']}"
              + (f"  (별칭: {', '.join(d['aliases'])})" if d["aliases"] else "")
              + (f"  p.{','.join(map(str, d['pages']))}" if d["pages"] else "")
              + ("  *제목" if d["in_title"] else ""))


if __name__ == "__main__":
    main()
