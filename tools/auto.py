"""제안 자동 승인: 대기 중인 제안을 기준에 따라 자동 승인 / 사람 확인으로 나눈다.

    python tools/auto.py <과목>|--all [--dry-run]     대기 중인 제안을 다시 분류해 자동 승인 대상은 승인 (먼저 백업)

자동 승인 (변경 기록 changes.json에 남고 과목 관리 화면에서 되돌릴 수 있음):
  - 용어: 교수님 자료(슬라이드·과제·퀴즈·실습·원서)에서 나온 것 중 2쪽 이상·페이지 제목·여러 자료에 나온 것
          (/register-slides recommended, /bootstrap-glossary recommended). 강의에서만 나온 용어는 근거 2곳 이상이거나 강의자료에도 있을 때
  - 교정 규칙: 틀린 쪽이 실제로 쓰이는 낱말이 아니고, 바른 쪽이 용어집 대표 표기이고, 근거 2회 이상 (예: 포토신세시스 → photosynthesis)
  - 진도 위치 (더 최근 강의의 위치가 이미 있으면 자동 거절)
사람 확인:
  - 양쪽 다 실제 낱말인 교정 규칙 (정의 → 정리: 규칙으로 만들면 멀쩡한 문장까지 바뀜).
    한국어 → 한국어 규칙은 틀린 쪽을 형태소 분석기(tools/korean.py, kiwipiepy)로 검사해 사전에 있는 낱말이면 Claude가 auto: true여도 사람 확인,
    분석기를 쓸 수 없으면 모두 사람 확인
  - 한국어 용어를 영어로 바꾸는 규칙(번역), 조사가 붙거나 2글자 이하인 규칙, 바른 쪽이 용어집 대표 표기가 아닌 규칙
  - 표기 규칙 변경, 근거 1회뿐인 애매한 용어, 예전에 거절했거나 되돌린 제안
Claude가 제안에 적은 판단(item["claude"] = {"auto", "why"})이 false면 사람 확인. 없으면 프로그램 기준만 본다.
"실제로 쓰이는 낱말"은 용어집(다른 용어), 등록된 강의자료 인덱스, 중간고사 NotebookLM 전사본,
교정 작업에서 Claude가 고친 문장·사용자가 직접 입력한 문장에 나오는지로 판단한다 (교정본 md는 남은 오인식이 있어 쓰지 않음).
"""
import re, sys
from pathlib import Path

try:
    from . import common as C
    from . import review as RV
    from . import korean as KO
except ImportError:
    import common as C
    import review as RV
    import korean as KO

PROFESSOR_ORIGINS = ("register-slides", "bootstrap")  # 교수님 자료·원서에서 나온 제안


def evidence_count(s):
    """근거 횟수: claude count → 근거마다 "n회" 또는 세그먼트 번호 개수 (없으면 1)."""
    c = (s.get("claude") or {}).get("count")
    if isinstance(c, int) and not isinstance(c, bool) and c > 0:
        return c
    n = 0
    for e in s.get("evidence") or []:
        text = f"{e.get('segments') or ''} {e.get('detail') or ''}"
        m = re.search(r"(\d+)\s*회", text)
        nums = re.findall(r"\d+", str(e.get("segments") or ""))
        n += int(m.group(1)) if m else len(nums) if nums else 1
    return n


def lecture_order(source_file):
    """녹음 파일명 → 강의 순서 키 "YYMMDD_NN" (나뉜 녹음 부분 번호 포함). 형식이 아니면 None."""
    k = C.lecture_key(source_file or "")
    return f"{k['date']}_{k['part'] or 0:02d}" if k else None


def _lecture_date(work_name):
    """교정 작업 이름 → 강의 순서 키. 모르면 None."""
    j = C.load_json(C.BASE / "jobs" / str(work_name) / "job.json") if work_name else None
    return lecture_order((j or {}).get("source_file") or str(work_name or ""))


class Context:
    """분류에 쓰는 과목 데이터 (한 번만 읽음)."""

    def __init__(self, subject):
        self.subject = subject
        self.sub = C.subject(subject)
        d = C.subject_dir(subject)
        self.glossary = C.load_json(d / "glossary.json", {"terms": []})["terms"]
        self.rep, self.alias = RV._glossary_index(subject)
        self.fix = C.load_json(d / "fix.json", {"rules": []})["rules"]
        self.progress = C.load_json(d / "progress.json", {}) or {}
        sugg = C.load_json(d / "suggestions.json", {"items": []})["items"]
        self.rejected = {(s["type"], C._sugg_key(s)) for s in sugg if s.get("status") == "rejected"}
        self._texts = None
        self._material_terms = None

    def rep_of(self, word):
        """낱말 → 용어집 대표 표기 (대표 표기·별칭 모두, 없으면 None)."""
        k = C.term_key(word)
        return self.rep.get(k) or self.alias.get(k)

    def material_terms(self):
        """등록된 강의자료(decks)·참고 자료(refs)의 용어 키."""
        if self._material_terms is None:
            keys = set()
            d = C.subject_dir(self.subject)
            for p in list((d / "decks").glob("*.json")) + list((d / "refs").glob("*.json")):
                x = C.load_json(p) or {}
                for t in (x.get("terms") or []) + [t for pg in x.get("pages") or [] for t in pg.get("terms", []) + pg.get("figure_terms", [])]:
                    if isinstance(t, str):
                        keys.add(C.term_key(t))
            self._material_terms = keys
        return self._material_terms

    def texts(self):
        """실제로 쓰이는 낱말을 찾을 글 [(출처, 글)]: 강의자료 인덱스, 중간고사 NotebookLM 전사본, 교정본 md 본문."""
        if self._texts is None:
            out = []
            d = C.subject_dir(self.subject)
            for p in sorted((d / "decks").glob("*.json")) + sorted((d / "refs").glob("*.json")):
                x = C.load_json(p) or {}
                parts = [x.get("title") or "", x.get("summary") or "", " ".join(x.get("terms") or [])]
                for pg in x.get("pages") or []:
                    parts += [pg.get("title") or "", pg.get("summary") or "", " ".join(pg.get("terms", []) + pg.get("figure_terms", []))]
                out.append((f"강의자료 {x.get('file') or p.stem}", "\n".join(parts)))
            s = C.settings()
            folder = C.root() / self.subject / s["midterm_dir"] / s["transcripts_dir"]
            if folder.is_dir():
                for p in sorted(folder.glob("*.md")):
                    out.append((f"중간고사 NotebookLM {p.name}", p.read_text(encoding="utf-8", errors="replace")))
            # 교정본은 고치지 못한 오인식이 남아 있을 수 있어서, Claude가 고친 문장과 사용자가 직접 입력한 문장만 쓴다
            jobs = C.BASE / "jobs"
            for d in sorted(jobs.iterdir()) if jobs.is_dir() else []:
                j = C.load_json(d / "job.json") if d.is_dir() else None
                r = C.load_json(d / "result.json") if j else None
                if not r or j.get("subject") != self.subject or j.get("status") == "superseded":
                    continue
                man = (C.load_json(d / "manual.json", {}) or {}).get("items", {})
                text = [x["text"] for x in r["segments"] if x.get("text")] + [m["text"] for m in man.values()]
                out.append((f"교정 작업 {d.name}", "\n".join(text)))
            self._texts = out
        return self._texts

    def real_word(self, wrong, right):
        """틀린 쪽이 실제로 쓰이는 (다른 뜻의) 낱말인지 → 어디서 찾았는지 문자열, 아니면 None.
        같은 용어의 별칭(포토신세시스 → photosynthesis)은 같은 말을 다르게 적은 것이라 실제 낱말로 보지 않는다."""
        w = wrong.strip()
        rw, rr = self.rep_of(w), self.rep_of(right)
        if rw:
            return None if rr and C.term_key(rw) == C.term_key(rr) else f"용어집 '{rw}'"
        low = w.lower()
        for label, text in self.texts():
            if low in text.lower():
                return label
        return None


def classify(s, ctx):
    """(자동 승인 여부, 이유 목록). 자동 승인이 아니면 이유 = 사람 확인이 필요한 이유."""
    t, d = s["type"], s.get("data") or {}
    cl = s.get("claude") or {}
    if (t, C._sugg_key(s)) in ctx.rejected:
        return False, ["예전에 거절했거나 되돌린 제안"]
    if cl.get("auto") is False:
        return False, ["Claude 판단: " + (cl.get("why") or "사람 확인 필요")]
    why = [f"Claude: {cl['why']}"] if cl.get("why") else []

    if t == "term":
        n = evidence_count(s)
        if s.get("origin") in PROFESSOR_ORIGINS:
            src = "중간고사 교수님 자료·원서" if s.get("origin") == "bootstrap" else f"교수님 자료 {d.get('deck') or ''}".strip()
            if d.get("recommended"):
                pages = d.get("pages") or []
                return True, [f"{src}" + (f" {len(pages)}쪽" if pages else "") + (" · 페이지 제목" if d.get("in_title") else "")] + why
            return False, [f"{src}에 한 번만 나온 애매한 용어 (1쪽, 제목 아님)"]
        if s.get("origin") == "correct":
            if any(C.term_key(f) in ctx.material_terms() for f in [d.get("term", "")] + (d.get("aliases") or [])):
                return True, ["강의자료에도 있는 용어"] + why
            if n >= 2:
                return True, [f"강의에서 근거 {n}회"] + why
            return False, ["근거 1회뿐인 애매한 용어 (강의자료에 없음)"]
        return False, ["교수님 자료가 아닌 출처의 용어"]

    if t == "fix_rule":
        wrong, right = str(d.get("wrong") or "").strip(), str(d.get("right") or "").strip()
        out = []
        for w in RV.rule_warnings(ctx.subject, wrong, right, {"sub": ctx.sub, "gloss": (ctx.rep, ctx.alias), "fix": ctx.fix}):
            out.append(w["text"])
        if C.term_key(right) not in ctx.rep:
            out.append(f"바른 쪽 '{right}'가 용어집 대표 표기가 아님")
        where = ctx.real_word(wrong, right)
        if where:
            out.append(f"틀린 쪽 '{wrong}'도 실제로 쓰이는 낱말 ({where}) — 규칙으로 만들면 멀쩡한 문장까지 바뀔 수 있음")
        elif (RV.HANGUL.search(wrong) and not RV.LATIN.search(wrong) and RV.HANGUL.search(right) and not RV.LATIN.search(right)
              and not (ctx.rep_of(wrong) and ctx.rep_of(wrong) == ctx.rep_of(right))):
            # 한국어끼리 바꾸는 규칙(정의 → 정리): 틀린 쪽이 사전에 있는 실제 낱말이면 Claude가 auto: true여도 사람 확인.
            # 분석기를 쓸 수 없으면 실제 낱말인지 알 수 없으므로 모두 사람 확인 (안전한 쪽)
            known = KO.dictionary_word(wrong)
            if known is None:
                out.append("한국어 낱말끼리 바꾸는 규칙인데 형태소 분석기를 쓸 수 없어 실제 낱말인지 확인하지 못함")
            elif known:
                out.append(f"틀린 쪽 '{wrong}'이(가) 사전에 있는 실제 낱말 — 양쪽 다 실제 낱말인 규칙은 사람 확인")
        n = evidence_count(s)
        if n < 2:
            out.append("근거 1회")
        if out:
            return False, out
        return True, [f"'{wrong}'는 실제 낱말이 아님 · 바른 쪽이 용어집 표기 · 근거 {n}회"] + why

    if t == "progress":
        return True, ["진도 위치"] + why

    if t == "notation":
        return False, ["표기 규칙 변경은 사람 확인"]
    return False, [f"알 수 없는 종류: {t}"]


def _progress_decisions(items, ctx):
    """진도 위치 제안: 가장 최근 강의의 것 하나만 승인, 나머지와 이미 더 최근 강의 위치가 있는 것은 거절."""
    cur = ctx.progress.get("lecture") or ""
    dated = [(s, (s["data"].get("lecture") or _lecture_date((s.get("evidence") or [{}])[0].get("source")) or "")) for s in items]
    dated.sort(key=lambda x: (x[1], x[0].get("created") or ""))
    approve, reject = {}, {}
    if not dated:
        return approve, reject
    best, date = dated[-1]
    for s, dt in dated[:-1]:
        reject[s["id"]] = ["같은 과목에 더 최근 강의의 진도 위치 제안이 있음"]
    if cur and date and date < cur:
        reject[best["id"]] = [f"이미 더 최근 강의({cur})의 진도 위치가 있음"]
    else:
        approve[best["id"]] = ["진도 위치" + (f" (강의 {date})" if date else "")]
    return approve, reject


def auto_resolve(subject, dry_run=False):
    """대기 중인 제안을 분류: 자동 승인 대상은 승인(변경 기록에 남김), 나머지는 이유를 붙여 사람 확인으로 남김.
    {"approved", "rejected", "pending", "items": [(제안, "approve" | "reject" | "human", 이유)]}"""
    path = C.subject_dir(subject) / "suggestions.json"
    with C._lock:
        ctx = Context(subject)
        data = C.load_json(path, {"items": []})
        pending = [s for s in data["items"] if s.get("status") == "pending"]
        approve, reject, rows = {}, {}, []
        prog = [s for s in pending if s["type"] == "progress"]
        pa, pr = _progress_decisions([s for s in prog if classify(s, ctx)[0]], ctx)
        for s in pending:
            ok, why = classify(s, ctx)
            if s["type"] == "progress" and ok:
                ok, why = (s["id"] in pa), pa.get(s["id"]) or pr.get(s["id"]) or why
                if s["id"] in pr:
                    reject[s["id"]] = why
            if ok:
                approve[s["id"]] = why
            s["auto"] = {"auto": ok, "reasons": why, "checked": C.now_iso()}
            rows.append((s, "approve" if ok else "reject" if s["id"] in reject else "human", why))
        if dry_run:
            return {"approved": len(approve), "rejected": len(reject), "pending": len(pending) - len(approve) - len(reject), "items": rows}
        C.save_json(path, data)  # 사람 확인 이유를 화면에 보여 주려고 저장
        done = C.resolve_suggestions(subject, approve=approve, reject=reject,
                                     reasons={**approve, **reject}, by="auto") if approve or reject else {"approved": 0, "rejected": 0}
    return {"approved": done["approved"], "rejected": done["rejected"],
            "pending": len(pending) - done["approved"] - done["rejected"], "items": rows}


STATE = {"approve": "자동 승인", "reject": "자동 거절", "human": "사람 확인"}


def summary_line(r):
    return (f"자동 승인 {r['approved']}개" + (f" · 자동 거절 {r['rejected']}개" if r["rejected"] else "")
            + f" · 사람 확인 {r['pending']}개" + (" (과목 관리 화면 \"확인이 필요한 제안\")" if r["pending"] else ""))


def main():
    C.utf8_stdout()
    a = sys.argv[1:]
    names = C.subject_names() if "--all" in a else [x for x in a if not x.startswith("--")]
    if not names:
        sys.exit(__doc__)
    dry = "--dry-run" in a
    for name in names:
        C.subject(name)
        if not dry:
            d = C.subject_dir(name)
            for f in ("suggestions.json", "glossary.json", "fix.json", "progress.json"):
                C.backup_file(d / f, f"{name}_{Path(f).stem}")
        r = auto_resolve(name, dry_run=dry)
        print(f"{name}: " + ("(미리보기) " if dry else "") + summary_line(r))
        for s, state, why in r["items"]:
            d = s["data"]
            label = d.get("term") or (f"{d.get('wrong')} → {d.get('right')}" if s["type"] == "fix_rule" else
                                      f"{d.get('spoken')} → {d.get('written')}" if s["type"] == "notation" else
                                      f"{d.get('deck')} p.{d.get('page')}")
            print(f"  [{STATE[state]}] {s['type']}: {label} — {'; '.join(why)}")


if __name__ == "__main__":
    main()
