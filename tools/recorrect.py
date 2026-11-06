"""/recorrect 보조: 강의자료를 등록한 뒤 "강의자료 미등록"으로 교정했던 작업을 다시 교정할 준비.

    python tools/recorrect.py <과목>                 대상 목록 (교정 완료 + 교정할 때 강의자료 미등록 + 지금은 등록됨)
    python tools/recorrect.py <과목> <작업이름>      준비: context.md를 새 강의자료 인덱스로 다시 만들고 prev_edits.md(기존 교정 요약) 작성

Whisper는 다시 돌리지 않는다. 기존 edits(문장 교정)는 그대로 두고, Claude Code가 edits.recorrect.json에
문단·슬라이드 위치(paragraphs)와 슬라이드를 보고 새로 알게 된 교정만 쓴다 → apply_edits.py가 기존 edits 위에 덧붙여 md를 다시 만든다.
"""
import sys
from pathlib import Path

try:
    from . import common as C
    from . import work as W
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import common as C
    import work as W


def candidates(subject):
    """다시 교정할 작업 [{"name", "source_file", "applied", "decks_now"}]. 지금 등록된 강의자료가 없으면 []."""
    decks = W._decks(subject)
    if not decks:
        return []
    out = []
    for j in W.list_works():
        if j.get("subject") != subject or j.get("status") != "done":  # 대체된 작업(status "superseded")도 여기서 빠짐
            continue
        # materials_registered: apply_edits.py가 교정할 때 강의자료가 등록돼 있었는지 기록 (다시 교정하면 True가 됨)
        reg = j.get("materials_registered")
        if reg is False or (reg is None and not (j.get("context") or {}).get("decks_registered")):
            out.append({"name": j["name"], "source_file": j["source_file"], "applied": j.get("applied"),
                        "decks_now": len(decks), "command": f"/recorrect {subject}"})
    out.sort(key=lambda x: x["name"])
    return out


def prepare(name):
    """context.md 다시 만들기 + prev_edits.md (기존 문단 제목·교정 요약: 문단을 새로 나눌 때 참고)."""
    job = W.load_job(name) or sys.exit(f"작업이 없습니다: {name}")
    for p in W.edits_files(name):
        C.backup_file(p, f"{name}_{p.stem}")
    meta = W.rebuild_context(name)
    r = C.load_json(W.work_dir(name) / "result.json") or {}
    L = [f"# 기존 교정 요약: {name}", "",
         "기존 edits(문장 교정)는 그대로 적용된다. edits.recorrect.json에는 문단·슬라이드 위치와 새로 고칠 문장만 쓴다.", "",
         "## 기존 문단 (번호 범위 · 제목)"]
    L += [f"- {p['from']}–{p['to']} · {p['heading']}" for p in r.get("paragraphs") or []] or ["- (없음)"]
    changed = [s for s in r.get("segments") or [] if s.get("text") is not None]
    L += ["", f"## 기존 교정 {len(changed)}문장 (번호 · 이유)"]
    L += [f"- {s['i']} · {s.get('reason') or ''}" for s in changed[:400]]
    (W.work_dir(name) / "prev_edits.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return meta


def main():
    C.utf8_stdout()
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not a:
        sys.exit(__doc__)
    subject = a[0]
    C.subject(subject)
    if len(a) == 1:
        cand = candidates(subject)
        if not W._decks(subject):
            print(f"{subject}: 등록된 강의자료가 없습니다. 먼저 /register-slides로 등록하세요.")
            return
        if not cand:
            print(f"{subject}: 다시 교정할 작업이 없습니다 (강의자료 미등록으로 교정한 작업이 없음).")
            return
        print(f"{subject}: 다시 교정할 작업 {len(cand)}개 (지금 등록된 강의자료 {cand[0]['decks_now']}개)")
        for c in cand:
            print(f"- {c['name']} · {c['source_file']} · 교정 {str(c['applied'] or '')[:16]}")
        return
    meta = prepare(a[1])
    print(f"준비 완료: jobs/{a[1]}/context.md (후보 강의자료: {', '.join(meta['candidate_decks']) or '없음'})"
          f" · jobs/{a[1]}/prev_edits.md")
    if not meta["candidate_decks"]:
        print("경고: 전사본과 겹치는 강의자료를 찾지 못했습니다. 슬라이드 위치는 확실할 때만 넣으세요.")


if __name__ == "__main__":
    main()
