"""교정 작업 폴더 (전사 프로그램 → Claude Code /correct → apply_edits.py → 최종 md).

jobs/<작업이름>/            작업이름 = 녹음 파일명 기준 (과목명_261028)
├─ job.json         과목, 원본 파일, 녹음 경로, 길이, Whisper 프롬프트, 상태(awaiting 교정 대기 → done 완료), 복원 통계
├─ whisper_raw.jsonl Whisper 원본 (영구 보관, 수정하지 않음)
├─ segments.jsonl   복원을 거친 세그먼트 (번호 = transcript.txt 번호). restored: 재전사로 복원, missing: 청취 불가 자리
├─ restore.json     환각·누락 의심 구간과 재전사 시도 결과 (pipeline.py)
├─ transcript.txt   Claude Code가 읽는 압축 형식: 한 줄에 한 세그먼트 "번호|MM:SS|텍스트|표시"
│                   표시: U 불확실, R 복원됨, H 사용자가 직접 듣고 입력, 청취 불가는 "번호|MM:SS|[누락 MM:SS–MM:SS]|M"
├─ context.md       과목 규칙, 용어집 상위, 교정 사전, 진도 위치, 후보 강의자료 페이지 인덱스,
│                   원서 참고 (강의자료 미등록이면 원서 의사코드·정의로 대신, 등록돼 있으면 보조로 짧게)
├─ manual.json      결과 화면에서 직접 듣고 입력한 문장 {"items": {"번호": {"text", "updated"}}} (md에서 가장 우선)
├─ edits.json       Claude Code가 쓰는 고칠 곳 목록 (길면 edits.part01.json, edits.part02.json …)
│                   /recorrect → edits.recorrect*.json (문단·슬라이드 위치를 새로, 교정은 덧붙임)
│                   /crosscheck → edits.crosscheck*.json (약한 구간 교정만 덧붙임)
├─ notebooklm.md    결과 화면에서 올린 NotebookLM 전사본 (/crosscheck용)
├─ crosscheck.md    tools/crosscheck.py가 만든 약한 구간 ↔ NotebookLM 대조표
├─ result.json      apply_edits.py가 만든 교정 결과 (결과 화면, 문장 되돌리기, md 다시 조립)
│                   important: 중요 구간(시험·퀴즈·과제·강조), review: 확인 목록, review_memo: 중요 구간 밖 의심(처리 메모에만)
└─ review.json      확인 목록 표시 {"items": {"r88": {"status": "kept", "at"}}, "completed": [완료]를 누른 시각}
                    (고친 문장은 manual.json의 직접 입력. job.json "review" = {total, open, completed} 요약)

최종 md: <과목>/Finals_Scope/전사본/<원본파일명>.md (예: 과목명_261028.m4a.md)
"""
import json, re
from datetime import datetime
from pathlib import Path

try:
    from . import common as C
    from . import book as B
    from . import review as RV
    from . import auto as A
except ImportError:
    import common as C
    import book as B
    import review as RV
    import auto as A

JOBS = C.BASE / "jobs"
STATUS = {"awaiting": "교정 대기", "done": "교정 완료", "superseded": "대체됨"}


# ---------- 공용 ----------

def ts(sec):
    s = int(sec or 0)
    h, m, ss = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


def parse_ts(t):
    """"12:30" / "1:02:03" / 초(숫자) → 초. 형식이 틀리면 ValueError."""
    if isinstance(t, (int, float)):
        return float(t)
    parts = str(t).strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"시각 형식이 아닙니다: {t!r} (예: 12:30)")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + float(p)
    return sec


def work_dir(name):
    return JOBS / name


def load_job(name):
    return C.load_json(work_dir(name) / "job.json")


def save_job(name, data):
    C.save_json(work_dir(name) / "job.json", data)


def load_segments(name):
    p = work_dir(name) / "segments.jsonl"
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def edits_files(name):
    """edits.json, edits.part01.json … (이름순, edits.json이 먼저)."""
    d = work_dir(name)
    return sorted(d.glob("edits*.json"), key=lambda p: (p.name != "edits.json", p.name))


def load_manual(name):
    return (C.load_json(work_dir(name) / "manual.json", {}) or {}).get("items", {})


def seg_flag(seg, s=None):
    s = s or C.settings()
    if seg.get("missing"):
        return "M"
    if seg.get("restored"):
        return "R"
    lp, cr = seg.get("avg_logprob"), seg.get("compression_ratio")
    if (lp is not None and lp < s["uncertain_logprob"]) or (cr is not None and cr > s["uncertain_compression"]):
        return "U"
    return ""


def md_path(job):
    return C.finals_transcripts_dir(job["subject"]) / f"{job['source_file']}.md"


def correct_command(name):
    return f"/correct {name}"


# ---------- 작업 폴더 만들기 ----------

def work_name_for(filename, server_job=None):
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f\s]', "_", Path(filename).name.split(".")[0]).strip("._") or "lecture"
    name, n = stem, 2
    while work_dir(name).exists():
        j = load_job(name) or {}
        if server_job and j.get("server_job") == server_job:
            break
        name, n = f"{stem}_{n}", n + 1
    return name


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def create_work(segments, filename, subject, server_job=None, options=None, duration=None, compute_type=None,
                raw=None, restore=None, audio=None, transcribed=None):
    """전사 결과로 작업 폴더를 만들고 상태를 "교정 대기"로. 같은 전사 작업이면 같은 폴더를 다시 만든다(edits는 남김).
    segments: 복원을 거친 세그먼트, raw: Whisper 원본(없으면 segments), restore: pipeline.restore 요약, audio: 녹음 파일 경로,
    transcribed: 전사한 시각(ISO). 같은 녹음의 작업이 여럿이면 전사가 가장 늦은 것만 남기고 나머지는 "대체됨"."""
    C.subject(subject)  # 없는 과목이면 KeyError
    if not segments:
        raise ValueError("세그먼트가 없습니다")
    name = work_name_for(filename, server_job)
    d = work_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    _write_jsonl(d / "segments.jsonl", segments)
    _write_jsonl(d / "whisper_raw.jsonl", raw or segments)
    if restore:
        C.save_json(d / "restore.json", restore)
    else:
        (d / "restore.json").unlink(missing_ok=True)
    o = options or {}
    job = {
        "name": name, "subject": subject, "source_file": Path(filename).name, "server_job": server_job,
        "audio": str(audio) if audio else None,
        "duration": duration or segments[-1]["end"], "segments": len(segments),
        "whisper": {"prompt": o.get("prompt") or "", "hotwords": o.get("hotwords") or "",
                    "fix": o.get("fix") or {}, "language": o.get("language"), "beam_size": o.get("beam_size"),
                    "compute_type": compute_type, "preset": o.get("preset") or "",
                    "prompt_source": o.get("prompt_source") or "preset", "prompt_info": o.get("prompt_info")},
        "restore": (restore or {}).get("stats"),
        "status": "awaiting", "created": C.now_iso(), "applied": None, "md_path": None,
        "transcribed": transcribed,
    }
    old = load_job(name)
    if old and old.get("server_job") == server_job:  # 다시 만들기: 처리 기록은 유지
        job["created"] = old.get("created", job["created"])
    (d / "manual.json").unlink(missing_ok=True)  # 세그먼트 번호가 바뀔 수 있으므로 직접 입력은 지움
    (d / "transcript.txt").write_text(build_transcript(segments), encoding="utf-8")
    (d / "result.json").unlink(missing_ok=True)
    save_job(name, job)  # 버전 정리·앞뒤 부분 찾기가 이 작업을 볼 수 있게 먼저 저장
    resolve_versions(subject, job["source_file"])
    carried = carry_manual(name, subject, job["source_file"], segments)
    if carried["moved"]:
        (d / "transcript.txt").write_text(build_transcript(segments, load_manual(name)), encoding="utf-8")
    ctx, meta = build_context(subject, segments, name, job["duration"], job["source_file"])
    (d / "context.md").write_text(ctx, encoding="utf-8")
    job = load_job(name)
    job["context"] = meta
    if carried["from"]:
        job["carried_manual"] = carried
    save_job(name, job)
    return name


CARRY_MIN_OVERLAP = 0.5  # 이전 문장과 새 문장의 시간이 짧은 쪽 길이의 이 비율 이상 겹치면 같은 자리로 봄
CARRY_MAX_SHIFT = 1.0    # 겹치지 않아도 시작 시각이 이 초 안이면 같은 자리로 봄


def carry_manual(name, subject, source_file, segments):
    """같은 녹음을 다시 전사했을 때: 이전 작업들(최근 것부터)에서 직접 입력한 문장(H)을 시각으로 맞춰 새 작업 manual.json에 옮김.
    {"from": [이전 작업], "moved": [{"i", "start", "text", "from", "from_i"}], "unmatched": [{"start", "end", "text", "from", "from_i"}]}"""
    olds = [j for j in list_works() if j.get("subject") == subject and j.get("source_file") == source_file and j["name"] != name]
    olds.sort(key=lambda j: (transcribed_at(j), j["name"]), reverse=True)
    out = {"from": [], "moved": [], "unmatched": []}
    items = load_manual(name)
    for old in olds:
        man = load_manual(old["name"])
        if not man:
            continue
        try:
            by = {x["idx"]: x for x in load_segments(old["name"])}
        except FileNotFoundError:
            continue
        out["from"].append(old["name"])
        for k, m in sorted(man.items(), key=lambda kv: int(kv[0])):
            o = by.get(int(k))
            if not o:
                continue
            row = {"start": o["start"], "end": o["end"], "text": m["text"], "from": old["name"], "from_i": int(k)}
            best, score = None, 0.0
            for s in segments:
                ov = min(o["end"], s["end"]) - max(o["start"], s["start"])
                short = max(0.1, min(o["end"] - o["start"], s["end"] - s["start"]))
                r = ov / short if ov > 0 else (1.0 if abs(o["start"] - s["start"]) <= CARRY_MAX_SHIFT else 0.0)
                if r > score:
                    best, score = s, r
            key = str(best["idx"]) if best else None
            if not best or score < CARRY_MIN_OVERLAP:
                out["unmatched"].append(row)
            elif key in items:  # 더 최근 작업에서 이미 옮겼음
                if items[key]["text"] != m["text"] and not any(x["from_i"] == row["from_i"] and x["text"] == row["text"] for x in out["unmatched"]):
                    out["unmatched"].append(row)
            else:
                items[key] = {"text": m["text"], "updated": C.now_iso(), "carried_from": f"{old['name']}#{k}"}
                out["moved"].append({**row, "i": best["idx"], "start": best["start"]})
    if out["moved"]:
        C.save_json(work_dir(name) / "manual.json", {"items": items})
    return out


# ---------- 같은 녹음의 여러 작업 (다시 전사) ----------

def transcribed_at(j):
    """작업의 전사 시각: job.json "transcribed" → 전사 기록(outputs/.jobs/<id>.json)의 created → 작업 폴더를 만든 시각."""
    if j.get("transcribed"):
        return j["transcribed"]
    sj = C.load_json(C.BASE / "outputs" / ".jobs" / f"{j.get('server_job')}.json") if j.get("server_job") else None
    if sj and sj.get("created"):
        return datetime.fromtimestamp(sj["created"]).isoformat(timespec="seconds")
    return j.get("created") or ""


def resolve_versions(subject, source_file):
    """같은 과목·같은 녹음 파일의 작업 중 전사가 가장 늦은 것만 살리고 나머지는 "대체됨"(폴더는 남김).
    대체된 작업은 md를 쓰지 않고, /recorrect 대상·용어집 빈도(seen_in)·교정 규칙 영향 계산에서 빠진다. 바뀐 작업 이름 목록 반환."""
    works = [j for j in list_works() if j.get("subject") == subject and j.get("source_file") == source_file]
    if len(works) < 2:
        return []
    latest = max(works, key=lambda j: (transcribed_at(j), j.get("created") or "", j["name"]))
    changed = []
    for j in works:
        if j["name"] == latest["name"]:
            if j.get("status") == "superseded":  # 예전에 대체됐던 작업이 다시 가장 최근이 됨
                j["status"] = j.pop("status_before", None) or ("done" if j.get("applied") else "awaiting")
                j.pop("superseded_by", None)
                j.pop("superseded_at", None)
                save_job(j["name"], j)
                changed.append(j["name"])
            continue
        if j.get("status") == "superseded" and j.get("superseded_by") == latest["name"]:
            continue
        if j.get("status") != "superseded":
            j["status_before"] = j.get("status")
        j.update(status="superseded", superseded_by=latest["name"], superseded_at=C.now_iso())
        save_job(j["name"], j)
        changed.append(j["name"])
    gone = {j["name"] for j in works if j["name"] != latest["name"]}
    _drop_seen(subject, gone)
    return changed


def _drop_seen(subject, names):
    """용어집 seen_in(용어가 나온 작업)에서 대체된 작업을 빼고 freq를 다시 셈 (같은 강의를 두 번 세지 않게)."""
    path = C.subject_dir(subject) / "glossary.json"
    glo = C.load_json(path)
    if not glo or not any(set(t.get("seen_in") or []) & names for t in glo["terms"]):
        return
    C.backup_file(path, f"{subject}_glossary")
    for t in glo["terms"]:
        if set(t.get("seen_in") or []) & names:
            t["seen_in"] = [x for x in t["seen_in"] if x not in names]
            t["freq"] = len(t["seen_in"])
    C.save_json(path, glo)


def refresh_versions():
    """모든 작업을 녹음별로 묶어 대체 상태를 맞춤 (서버 시작 때). 바뀐 작업 이름 목록."""
    keys = {(j.get("subject"), j.get("source_file")) for j in list_works()}
    return [n for s, f in sorted(keys, key=str) for n in resolve_versions(s, f)]


def is_superseded(name_or_job):
    j = load_job(name_or_job) if isinstance(name_or_job, str) else name_or_job
    return bool(j) and j.get("status") == "superseded"


def _check_active(name):
    j = load_job(name)
    if is_superseded(j):
        raise ValueError(f"이 작업은 {j.get('superseded_by')}(으)로 대체됐습니다 (같은 녹음을 다시 전사). 새 작업에서 고치세요")


def build_transcript(segments, manual=None):
    s = C.settings()
    manual = manual or {}
    out = []
    for seg in segments:
        m = manual.get(str(seg["idx"]))
        if m:  # 사용자가 직접 듣고 입력한 문장
            out.append(f"{seg['idx']}|{ts(seg['start'])}|{m['text'].replace('|', '¦')}|H")
        elif seg.get("missing"):  # 재전사로도 복원하지 못한 청취 불가 구간
            out.append(f"{seg['idx']}|{ts(seg['start'])}|[누락 {ts(seg['start'])}–{ts(seg['end'])}]|M")
        else:
            text = re.sub(r"\s+", " ", seg["text"]).replace("|", "¦").strip()
            out.append(f"{seg['idx']}|{ts(seg['start'])}|{text}|{seg_flag(seg, s)}")
    return "\n".join(out) + "\n"


def base_result(name):
    """교정 전 결과 화면용 (result.json과 같은 모양, text는 모두 None)."""
    s = C.settings()
    return {"segments": [{"i": x["idx"], "start": x["start"], "end": x["end"], "orig": x["text"], "text": None,
                          "reason": "", "flag": seg_flag(x, s), "region": (x.get("restore") or {}).get("region")}
                         for x in load_segments(name)], "paragraphs": [], "inaudible": [], "reverted": []}


def set_manual(name, i, text):
    """직접 듣고 입력한 문장 저장 (빈 문자열이면 지움). transcript.txt와, 교정 결과가 있으면 md도 다시 만든다."""
    _check_active(name)
    segs = load_segments(name)
    if not any(x["idx"] == i for x in segs):
        raise ValueError(f"없는 문장 번호: {i}")
    items = load_manual(name)
    text = " ".join(str(text or "").split())
    if text:
        items[str(i)] = {"text": text, "updated": C.now_iso()}
    else:
        items.pop(str(i), None)
    C.save_json(work_dir(name) / "manual.json", {"items": items})
    (work_dir(name) / "transcript.txt").write_text(build_transcript(segs, items), encoding="utf-8")
    manual_to_suggestions(name, segs, items)
    if (work_dir(name) / "result.json").exists():
        update_review_summary(name)  # 확인 목록 문장을 직접 고치면 "고침"
        if (load_job(name) or {}).get("md_path"):
            write_md(name)
    return items


def manual_to_suggestions(name, segs=None, items=None):
    """직접 입력에서 바뀐 낱말을 교정 규칙 제안(origin "manual")으로 → 자동 승인 분류. 이 작업의 대기 중 manual 제안은 새로 계산."""
    job = load_job(name)
    segs = segs if segs is not None else load_segments(name)
    items = items if items is not None else load_manual(name)
    C.drop_source_suggestions(job["subject"], name, origin="manual", types=("fix_rule",))
    added = C.add_suggestions(job["subject"], RV.manual_suggestions(job["subject"], name, segs, items))
    A.auto_resolve(job["subject"])
    return added


# ---------- context.md ----------

def _decks(subject):
    out = []
    for p in sorted((C.subject_dir(subject) / "decks").glob("*.json"), key=lambda p: _natural(p.name)):
        d = C.load_json(p)
        if d and d.get("registered"):
            out.append(d)
    return out


def _natural(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


def _forms(term, alias_of):
    return [term] + alias_of.get(C.term_key(term), [])


def select_decks(subject, text, decks=None, prev_deck=None):
    """후보 강의자료: 진도 위치 덱(+다음 덱) + 앞부분 녹음의 마지막 위치 덱 + 전사본과 용어가 많이 겹치는 덱. [(덱, 일치 용어 수, 이유)]"""
    decks = decks if decks is not None else _decks(subject)
    if not decks:
        return []
    glo = C.load_json(C.subject_dir(subject) / "glossary.json", {"terms": []})
    alias_of = {}
    for t in glo["terms"]:
        forms = [t["term"]] + (t.get("aliases") or [])
        for f in forms:
            alias_of[C.term_key(f)] = forms
    low = text.lower()
    scored = []
    for d in decks:
        terms = {t for p in d["pages"] for t in p.get("terms", []) + p.get("figure_terms", [])}
        hit = sum(1 for t in terms if any(len(f) >= 2 and f.lower() in low for f in _forms(t, alias_of)))
        scored.append([d, hit, []])
    prog = C.load_json(C.subject_dir(subject) / "progress.json", {}) or {}
    names = [d["file"] for d in decks]
    if prog.get("deck") in names:
        i = names.index(prog["deck"])
        scored[i][2].append("진도 위치")
        if i + 1 < len(scored):
            scored[i + 1][2].append("진도 다음 자료")
    if prev_deck in names:
        scored[names.index(prev_deck)][2].append("앞부분 녹음의 마지막 위치")
    best = max(h for _, h, _ in scored)
    for row in scored:
        if row[1] and row[1] >= max(3, best * 0.3):
            row[2].append(f"용어 {row[1]}개 일치")
    picked = [r for r in scored if r[2]]
    picked.sort(key=lambda r: (not any("진도" in x or "앞부분" in x for x in r[2]), -r[1]))
    return [tuple(r) for r in picked[:C.settings()["context_max_decks"]]]


def part_works(subject, source_file):
    """나뉜 녹음(<과목>_<YYMMDD>_NN)의 바로 앞부분·뒷부분 작업 (대체되지 않은 것). (앞 job | None, [뒤 job …])"""
    k = C.lecture_key(source_file)
    if not k or not k["part"]:
        return None, []
    prev, nxt = None, []
    for j in list_works():
        o = C.lecture_key(j.get("source_file") or "")
        if j.get("subject") != subject or is_superseded(j) or not o or o["lecture"] != k["lecture"] or not o["part"]:
            continue
        if o["part"] == k["part"] - 1:
            prev = j
        elif o["part"] == k["part"] + 1:
            nxt.append(j)
    return prev, nxt


def prev_part_tail(job, n=None):
    """앞부분 녹음의 마지막 문단 몇 줄 → (줄 목록, 마지막 슬라이드 위치 dict | None, 교정됨 여부).
    교정 결과가 있으면 마지막 문단(제목·위치 포함)의 교정본, 없으면 복원 세그먼트 끝부분."""
    n = n or C.settings()["prev_part_lines"]
    name = job["name"]
    r = C.load_json(work_dir(name) / "result.json")
    if r and r.get("paragraphs") and job.get("status") == "done":
        manual = load_manual(name)
        by = {x["i"]: x for x in r["segments"]}
        p = r["paragraphs"][-1]
        rows = [by[i] for i in range(p["from"], p["to"] + 1) if i in by and by[i].get("flag") != "M"]
        lines = [f"[{ts(x['start'])}] {final_text(r, x, manual)}" for x in rows if final_text(r, x, manual)][-n:]
        sl = p.get("slide")
        return [f"(문단 제목) {p['heading']}"] + lines, sl, True
    try:
        segs = [x for x in load_segments(name) if not x.get("missing") and x["text"].strip()]
    except FileNotFoundError:
        return [], None, False
    return [f"[{ts(x['start'])}] {' '.join(x['text'].split())}" for x in segs[-n:]], None, False


def refresh_next_parts(name):
    """앞부분을 교정한 뒤: 아직 교정 대기인 뒷부분 작업의 context.md를 다시 만듦 (교정본 마지막 문단이 들어가게). 다시 만든 작업 이름."""
    job = load_job(name)
    if not job:
        return []
    _, nxt = part_works(job["subject"], job["source_file"])
    out = []
    for j in nxt:
        if j.get("status") == "awaiting":
            rebuild_context(j["name"])
            out.append(j["name"])
    return out


def build_context(subject, segments, name, duration, source_file=None):
    sub = C.subject(subject)
    d = C.subject_dir(subject)
    s = C.settings()
    text = " ".join(x["text"] for x in segments)
    prev = part_works(subject, source_file)[0] if source_file else None
    tail, prev_slide, prev_done = prev_part_tail(prev) if prev else ([], None, False)
    L = [f"# 교정 맥락: {name}", "",
         f"- 과목: {subject} · 녹음 길이 {ts(duration)} · 세그먼트 {len(segments)}개 (번호 {segments[0]['idx']}–{segments[-1]['idx']})",
         f"- 표시: U = Whisper 신뢰도가 낮은 문장 (avg_logprob < {s['uncertain_logprob']} 또는 compression_ratio > {s['uncertain_compression']})",
         "- 표시: R = 환각·누락 의심 구간을 재전사해 복원한 문장, M = 재전사로도 복원하지 못한 청취 불가 구간 (고치지 않는다),"
         " H = 사용자가 직접 듣고 입력한 문장 (고치지 않는다)",
         "", "## 과목 규칙"]
    L += [f"- {r}" for r in sub.get("rules") or []]
    if sub.get("keep_code_identifiers") and not any("식별자" in r for r in sub.get("rules") or []):
        L.append("- 코드 식별자(변수명, 함수명, 키워드)는 코드 표기 그대로 쓴다.")
    if sub.get("prefer_english_terms"):
        L.append("- 용어집의 대표 표기는 영어다. 교수님이 영어로 말해 별칭(한글 발음 등)으로 적혔으면 대표 표기로 고친다."
                 " 한국어 용어로 말한 것은 영어로 번역하지 않는다.")
    else:
        L.append("- 용어는 용어집의 대표 표기(슬라이드 표기)로 쓴다. 별칭으로 들리면 대표 표기로 고친다.")
    if sub.get("notation"):
        L += ["", "### 표기 규칙 (말 → 표기)"] + [f"- {n['spoken']} → {n['written']}" for n in sub["notation"]]

    glo = C.load_json(d / "glossary.json", {"terms": []})["terms"]
    glo.sort(key=lambda t: (-t.get("priority", 3), -t.get("freq", 0), t["term"].lower()))
    top = glo[:s["context_glossary_max"]]
    L += ["", f"## 용어집 (우선순위 상위 {len(top)}개 / 전체 {len(glo)}개)"]
    L += [f"- {t['term']}" + (f" (별칭: {', '.join(t['aliases'])})" if t.get("aliases") else "") for t in top] or ["- (없음)"]

    fix = C.load_json(d / "fix.json", {"rules": []})["rules"]
    L += ["", "## 교정 사전 (Whisper가 자주 틀리는 표기)"]
    L += [f"- {r['wrong']} → {r['right']}" for r in fix] or ["- (없음)"]

    prog = C.load_json(d / "progress.json", {}) or {}
    L += ["", "## 진도 위치",
          f"- {prog['deck']} p.{prog.get('page') or '?'} (갱신 {prog.get('updated') or '?'})" if prog.get("deck") else "- 미정"]

    if prev:  # 나뉜 녹음의 뒷부분: 앞부분 마지막 문단으로 흐름을 이어 감
        L += ["", f"## 앞부분 녹음 ({prev['source_file']}, 작업 {prev['name']})의 마지막 부분",
              "- 같은 날 강의의 바로 앞 녹음이다. 흐름을 잇는 참고용이며 교정 대상이 아니다 (번호도 이 작업과 무관)."
              " 첫 문단이 앞 내용을 이어 가면 제목과 슬라이드 위치를 자연스럽게 이어 붙인다.",
              "- " + ("교정본 마지막 문단" if prev_done else "아직 교정 전이라 Whisper 결과 끝부분 (인식 오류가 있을 수 있음)")
              + (f" · 마지막 위치 {prev_slide['file']} p.{prev_slide['range'][0]}–{prev_slide['range'][1]}" if prev_slide else "")]
        L += [f"  {x}" for x in tail] or ["  (내용 없음)"]

    decks = _decks(subject)
    picked = select_decks(subject, text, decks, prev_slide["file"] if prev_slide else None)
    meta = {"decks_registered": len(decks), "candidate_decks": [p[0]["file"] for p in picked]}
    if prev:
        meta["prev_part"] = {"name": prev["name"], "corrected": prev_done}
    hand = sub.get("handwritten")
    L += ["", "## 후보 강의자료 페이지" + (" (손필기)" if hand else "")]
    if not decks:
        L += ["- **강의자료 미등록**: 등록된 강의자료가 없다. paragraphs의 slide는 모두 null로 둔다."]
    elif not picked:
        L += ["- 전사본과 겹치는 강의자료를 찾지 못했다. 확실한 경우가 아니면 slide는 null로 둔다."]
    for deck, hit, why in picked:
        L += ["", f"### {deck['file']} — {deck.get('title', '')} ({deck.get('pages_total')}쪽 · {', '.join(why)})"]
        for p in deck["pages"]:
            terms = ", ".join(p.get("terms", []) + p.get("figure_terms", []))
            L.append(f"- p.{p['page']} {p.get('title', '')} — {p.get('summary', '')}" + (f" [{terms}]" if terms else ""))
    others = [x for x in decks if x["file"] not in meta["candidate_decks"]]
    if others:
        L += ["", "### 그 밖의 등록 자료 (제목만, 필요하면 decks/*.json을 직접 읽는다)"]
        L += [f"- {x['file']} — {x.get('title', '')} ({x.get('pages_total')}쪽)" for x in others]
    try:
        bl, bmeta = B.context_section(subject, text, bool(decks))
    except Exception as e:  # 원서 md를 못 읽어도 교정 작업은 만든다
        bl, bmeta = ["", "## 원서 참고", f"- 원서 md를 읽지 못했다: {e}"], []
    L += bl
    meta["book_sections"] = bmeta
    return "\n".join(L) + "\n", meta


def rebuild_context(name):
    """저장된 세그먼트로 context.md만 다시 만든다 (강의자료를 새로 등록했거나 용어집·교정 사전이 바뀐 뒤, /recorrect)."""
    job = load_job(name)
    if not job:
        raise FileNotFoundError(f"작업이 없습니다: {work_dir(name)}")
    ctx, meta = build_context(job["subject"], load_segments(name), name, job["duration"], job["source_file"])
    (work_dir(name) / "context.md").write_text(ctx, encoding="utf-8")
    job["context"] = meta
    save_job(name, job)
    return meta


# ---------- 결과 → md ----------

def final_text(r, seg, manual=None):
    m = (manual or {}).get(str(seg["i"]))
    if m:
        return m["text"]
    return seg["orig"] if seg["i"] in set(r.get("reverted") or []) or seg.get("text") is None else seg["text"]


def inaudible_list(r, manual=None):
    """청취 불가 구간 = M 줄(직접 입력한 것 제외) + /correct의 inaudible. 겹치거나 붙어 있으면 하나로.
    [{"start_s", "end_s", "guess", "sources"}] — sources: "auto"(재전사로도 복원 못 한 M 줄), "correct"(교정 중 Claude가 표시)"""
    manual = manual or {}
    items = [{"start_s": s["start"], "end_s": s["end"], "guess": "", "sources": ["auto"]} for s in r["segments"]
             if s.get("flag") == "M" and str(s["i"]) not in manual]
    items += [{"start_s": x["start_s"], "end_s": x["end_s"], "guess": x.get("guess") or "", "sources": ["correct"]}
              for x in r.get("inaudible") or []]
    items.sort(key=lambda x: (x["start_s"], x["end_s"]))
    out = []
    for x in items:
        if out and x["start_s"] <= out[-1]["end_s"] + 0.5:
            o = out[-1]
            o["end_s"] = max(o["end_s"], x["end_s"])
            o["guess"] = "; ".join(g for g in (o["guess"], x["guess"]) if g)
            o["sources"] += [k for k in x["sources"] if k not in o["sources"]]
        else:
            out.append(dict(x, sources=list(x["sources"])))
    return out


INAUDIBLE_SOURCE = {"auto": "자동 감지", "correct": "교정 중 표시"}
# edits.important[].kind — 교수님이 시험·퀴즈·과제를 언급하거나 "중요/꼭/시험에 나온다"고 강조한 구간
IMPORTANT_KIND = {"exam": "시험", "quiz": "퀴즈", "assignment": "과제", "emphasis": "강조"}
# 확인 목록 항목 종류 (edits.review[].kind + 중요 구간 안에서 프로그램이 찾은 것)
REVIEW_KIND = {"contradiction": "모순 의심", "recognition": "인식 실패", "uncertain": "불확실", "inaudible": "청취 불가"}
REVIEW_STATUS = {"open": "확인 안 함", "fixed": "고침", "kept": "그대로 둠"}


def important_of(important, i):
    """세그먼트 번호가 든 중요 구간 번호 (없으면 None)."""
    return next((n for n, x in enumerate(important or []) if x["from"] <= i <= x["to"]), None)


def build_review(r, claude_items):
    """확인 목록 = Claude가 적은 항목 (모순 의심은 강의 어디서나, 나머지는 중요 구간 안만)
    + 중요 구간 안의 청취 불가(M 줄·교정 중 inaudible)·불확실(U 중 교정하지 않은 것).
    중요 구간 밖의 인식 실패·불확실은 확인 목록에 올리지 않고 처리 메모에만 → (확인 목록, 메모에만 남길 항목)."""
    important = r.get("important") or []
    by = {s["i"]: s for s in r["segments"]}
    out, memo_only = {}, []
    for x in claude_items:
        k = important_of(important, x["i"])
        if x["kind"] != "contradiction" and k is None:
            memo_only.append({**x, "start": by[x["i"]]["start"]})
            continue
        out[x["i"]] = {**x, "important": k, "source": "claude"}
    hidden = hidden_by_inaudible(r)
    for n, p in enumerate(important):
        for i in range(p["from"], p["to"] + 1):
            s = by.get(i)
            if not s or i in out or s.get("text") == "":
                continue
            if s.get("flag") == "M":
                kind, why = "inaudible", "재전사로도 복원하지 못한 청취 불가"
            elif i in hidden:
                kind, why = "inaudible", "교정 중 알아들을 수 없다고 표시한 구간"
            elif s.get("flag") == "U" and s.get("text") is None:
                kind, why = "uncertain", "Whisper 신뢰도가 낮고 교정하지 않은 문장"
            else:
                continue
            out[i] = {"i": i, "kind": kind, "candidates": [], "why": why, "important": n, "source": "auto"}
    items = [{**out[i], "id": f"r{i}", "start": by[i]["start"], "end": by[i]["end"]} for i in sorted(out)]
    return items, memo_only


def review_state(name, r=None, manual=None):
    """확인 목록과 상태. 상태: 직접 입력(H)이 있으면 fixed, review.json에 kept/fixed, 아니면 open.
    {"items": [...], "total", "open", "completed"} (completed = [완료]를 누른 시각)"""
    r = r if r is not None else C.load_json(work_dir(name) / "result.json") or {}
    manual = manual if manual is not None else load_manual(name)
    st = C.load_json(work_dir(name) / "review.json", {}) or {}
    marks = st.get("items") or {}
    items = []
    for x in r.get("review") or []:
        status = "fixed" if str(x["i"]) in manual else (marks.get(x["id"]) or {}).get("status") or "open"
        items.append({**x, "status": status})
    return {"items": items, "total": len(items), "open": sum(1 for x in items if x["status"] == "open"),
            "completed": st.get("completed")}


def update_review_summary(name, reopen=False):
    """job.json "review" = {total, open, completed} (화면 목록이 result.json을 읽지 않게). reopen: 남은 항목이 있으면 [완료]를 취소."""
    rs = review_state(name)
    p = work_dir(name) / "review.json"
    if reopen and rs["open"] and rs["completed"]:
        st = C.load_json(p, {}) or {}
        st["completed"] = rs["completed"] = None
        C.save_json(p, st)
    job = load_job(name)
    job["review"] = {"total": rs["total"], "open": rs["open"], "completed": rs["completed"]}
    save_job(name, job)
    return rs


def set_review_item(name, rid, status):
    """확인 목록 항목 표시: kept(그대로 둠) / open(다시 확인 안 함으로). 고친 것은 직접 입력(manual)으로 저장한다."""
    _check_active(name)
    if status not in ("kept", "open"):
        raise ValueError(f"상태는 kept 또는 open: {status}")
    if not any(x["id"] == rid for x in review_state(name)["items"]):
        raise ValueError(f"확인 목록에 없는 항목: {rid}")
    p = work_dir(name) / "review.json"
    st = C.load_json(p, {}) or {}
    marks = st.setdefault("items", {})
    if status == "open":
        marks.pop(rid, None)
    else:
        marks[rid] = {"status": status, "at": C.now_iso()}
    C.save_json(p, st)
    rs = update_review_summary(name)
    if (load_job(name) or {}).get("md_path"):
        write_md(name)
    return rs


def complete_review(name, done=True):
    """확인 목록 [완료] (done=False면 취소). md를 다시 만든다 (처리 메모에 확인 결과)."""
    _check_active(name)
    p = work_dir(name) / "review.json"
    st = C.load_json(p, {}) or {}
    st["completed"] = C.now_iso() if done else None
    C.save_json(p, st)
    rs = update_review_summary(name)
    if (load_job(name) or {}).get("md_path"):
        write_md(name)
    return rs
# comprehension.weak_ranges[].kind — recognition: 인식 실패(NotebookLM 대조 대상), board: 판서 설명(말 자체가 조각남, 대조해도 나아지지 않음)
WEAK_KIND = {"recognition": "인식 실패", "board": "판서 설명(말 자체가 조각남)"}


def inaudible_by_source(ina_all):
    """(자동 감지 곳 수, 교정 중 표시만 있는 곳 수). 둘이 합쳐진 구간은 자동 감지로 센다."""
    auto = sum(1 for x in ina_all if "auto" in x["sources"])
    return auto, len(ina_all) - auto


def hidden_by_inaudible(r, manual=None):
    """/correct가 적은 청취 불가 구간 안에 (문장 가운데가) 드는 세그먼트 번호 → md에 넣지 않음. 직접 입력한 문장은 제외."""
    ivs = [(x["start_s"], x["end_s"]) for x in r.get("inaudible") or []]
    manual, rv = manual or {}, set(r.get("reverted") or [])
    return {s["i"] for s in r["segments"] if s.get("flag") != "M" and str(s["i"]) not in manual
            and not (s.get("text") == "" and s["i"] not in rv)  # 환각으로 지운 문장은 따로 셈
            and any(a <= (s["start"] + s["end"]) / 2 < b for a, b in ivs)}


def restored_regions(r):
    """복원된 구간 [(시작, 끝)] (같은 재전사 구간의 R 문장은 하나로)."""
    reg = {}
    for s in r["segments"]:
        if s.get("flag") == "R":
            k = s.get("region", ("i", s["i"]))
            a, b = reg.get(k, (s["start"], s["end"]))
            reg[k] = (min(a, s["start"]), max(b, s["end"]))
    return sorted(reg.values())


def _deck_label(file, hand):
    return f"필기: {file}" if hand else Path(file).stem


def _pages_label(pages):
    a, b = pages
    return f"p.{a}" if a == b else f"p.{a}–{b}"


def render_md(job, r, manual=None, review=None):
    sub = C.subject(job["subject"])
    hand = sub.get("handwritten")
    manual = manual or {}
    review = review or {"items": [], "completed": None}
    flagged = {x["i"]: x for x in review["items"] if x["kind"] == "contradiction" and x["status"] == "open"}
    segs = r["segments"]
    by_i = {s["i"]: s for s in segs}
    used = {}
    for p in r["paragraphs"]:
        sl = p.get("slide")
        if sl:
            a, b = sl["range"]
            lo, hi = used.get(sl["file"], (a, b))
            used[sl["file"]] = (min(lo, a), max(hi, b))
    ina_all = inaudible_list(r, manual)
    ina = list(ina_all)
    hidden = hidden_by_inaudible(r, manual)
    uncertain = [s for s in segs if s.get("flag") == "U"]
    restored = restored_regions(r)
    typed = [s for s in segs if str(s["i"]) in manual]
    ina_sec = round(sum(x["end_s"] - x["start_s"] for x in ina_all))
    ina_auto, ina_corr = inaudible_by_source(ina_all)

    today = (job.get("applied") or C.now_iso())[:10]
    refs = " · ".join(f"{f} {_pages_label(pg)}" for f, pg in used.items()) or "강의자료 미등록"
    out = [f"# {job['source_file']}",
           f"> 과목: {job['subject']} | 녹음 길이: {ts(job['duration'])} | 처리일: {today}",
           f"> 참고 자료: {refs} | 청취 불가 {len(ina_all)}곳 ({ina_sec}초)"
           + (f" (교정 중 표시 {ina_corr}곳 포함)" if ina_corr else "") + f" · 복원 {len(restored)}곳", ""]

    def ina_lines(upto):
        while ina and (upto is None or ina[0]["start_s"] < upto):
            x = ina.pop(0)
            out.append(f"**[{ts(x['start_s'])}]** [청취 불가 {ts(x['start_s'])}–{ts(x['end_s'])}]")

    for p in r["paragraphs"]:
        sl = p.get("slide")
        loc = ""
        if sl:
            q = "?" if sl.get("confidence") is not None and sl["confidence"] < 0.5 else ""
            loc = f" ({_deck_label(sl['file'], hand)} {_pages_label(sl['range'])}{q})"
        out += [f"## {p['heading']}{loc}"]
        at = {int(k): v for k, v in (sl or {}).get("at", {}).items()}
        for i in range(p["from"], p["to"] + 1):
            s = by_i[i]
            gone = s.get("flag") == "M" and str(i) not in manual
            ina_lines(s["end"] if gone else (s["start"] + s["end"]) / 2 if i in hidden else s["start"])
            if gone or i in hidden:
                continue  # 청취 불가 구간 (위에서 [청취 불가] 줄로 씀)
            text = final_text(r, s, manual)
            if not text:  # 환각·잡음으로 지운 문장
                continue
            mark = f"({_deck_label(sl['file'], hand)} p.{at[i]}) " if i in at else ""
            x = flagged.get(i)  # 확인하지 않은 모순 의심: 문장을 그대로 믿지 않게 후보를 붙임
            check = f" 〔확인 필요: {'/'.join(x['candidates'])}〕" if x else ""
            out.append(f"**[{ts(s['start'])}]** {mark}{text}{check}")
        out.append("")
    ina_lines(None)

    memo = []
    imp = r.get("important") or []
    if imp:  # 나중에 Claude가 시험 관련 내용을 찾을 때 쓰는 목록
        memo.append(f"- 중요 구간 {len(imp)}곳 (시험·퀴즈·과제 언급, 강조):")
        for x in imp:
            a, b = by_i.get(x["from"]), by_i.get(x["to"])
            when = f"{ts(a['start'])}–{ts(b['end'])}" if a and b else f"{x['from']}–{x['to']}"
            memo.append(f"  - {when} [{IMPORTANT_KIND.get(x['kind'], x['kind'])}] {x['summary']}")
    if review["items"]:
        cnt = {k: sum(1 for x in review["items"] if x["status"] == k) for k in REVIEW_STATUS}
        memo.append(f"- 확인 목록 {len(review['items'])}곳: 고침 {cnt['fixed']} · 그대로 둠 {cnt['kept']} · 확인 안 함 {cnt['open']}"
                    + (f" (확인 완료 {review['completed'][:10]})" if review.get("completed") else ""))
        for x in review["items"]:
            memo.append(f"  - {ts(x['start'])} [{REVIEW_KIND.get(x['kind'], x['kind'])}]"
                        + (f" 후보 {'/'.join(x['candidates'])}" if x.get("candidates") else "")
                        + f" — {REVIEW_STATUS[x['status']]}" + (f" ({x['why']})" if x.get("why") else ""))
    rest = r.get("review_memo") or []
    if rest:
        memo.append(f"- 중요 구간 밖 확인 의심 {len(rest)}곳 (확인 목록에 올리지 않음): "
                    + ", ".join(f"{ts(x['start'])} [{REVIEW_KIND.get(x['kind'], x['kind'])}]" + (f" {x['why']}" if x.get("why") else "")
                                for x in rest[:30]) + (" …" if len(rest) > 30 else ""))
    if not used:
        memo.append("- 강의자료 미등록 (슬라이드 위치 표시 없음)" if not (job.get("context") or {}).get("decks_registered")
                    else "- 슬라이드 위치를 정하지 못함 (강의자료와 겹치는 내용 없음)")
    rs = job.get("restore") or {}
    if rs.get("regions"):
        memo.append(f"- 자동 감지: 환각 의심 {rs.get('hallucinated', 0)}문장 · 누락 의심 {rs.get('gaps', 0) + rs.get('sparse', 0)}곳"
                    f" → 의심 구간 {rs['regions']}곳 재전사: 복원 {rs.get('restored', 0)} · 원래 문장 유지 {rs.get('kept', 0)}"
                    f" · 환각만 제거 {rs.get('dropped', 0)} · 청취 불가(재전사 실패) {rs.get('missing', 0)}")
    if restored:
        memo.append(f"- 복원 {len(restored)}곳: " + ", ".join(f"{ts(a)}–{ts(b)}" for a, b in restored[:40])
                    + (" …" if len(restored) > 40 else ""))
    if ina_all:
        memo.append(f"- 청취 불가 {len(ina_all)}곳 (총 {ina_sec}초): 자동 감지 {ina_auto}곳 · 교정 중 표시 {ina_corr}곳"
                    + (" (교정 중 표시 = 재전사 뒤 교정 단계에서 Claude가 알아들을 수 없다고 표시한 구간)" if ina_corr else ""))
    for x in ina_all:
        src = " + ".join(INAUDIBLE_SOURCE[k] for k in x["sources"])
        memo.append(f"  - {ts(x['start_s'])}–{ts(x['end_s'])} [{src}]" + (f" — (추정) {x['guess']}" if x.get("guess") else ""))
    if hidden:
        memo.append(f"- 청취 불가 구간 안이라 넣지 않은 {len(hidden)}문장: " + ", ".join(ts(by_i[i]["start"]) for i in sorted(hidden)))
    if typed:
        memo.append(f"- 직접 듣고 입력한 {len(typed)}문장: " + ", ".join(ts(s["start"]) for s in typed))
    if uncertain:
        memo.append(f"- 불확실 {len(uncertain)}문장: " + ", ".join(ts(s["start"]) for s in uncertain[:40])
                    + (" …" if len(uncertain) > 40 else ""))
    c = r.get("comprehension") or {}
    if c.get("score") is not None:
        memo.append(f"- 이해도 {c['score']}/100" + (f" — {c['reason']}" if c.get("reason") else ""))
    for w in c.get("weak_ranges") or []:
        label = WEAK_KIND.get(w.get("kind"), WEAK_KIND["recognition"])
        why = (w.get("why") or "").removeprefix(label).lstrip(" :")
        a, b = by_i.get(w["from"]), by_i.get(w["to"])  # 본문에서 찾을 수 있게 시각도 (번호는 본문에 없음)
        when = f" ({ts(a['start'])}–{ts(b['end'])})" if a and b else ""
        memo.append(f"  - 약한 구간 {w['from']}–{w['to']}{when} [{label}]" + (f": {why}" if why else ""))
    if c.get("need_notebooklm"):
        memo.append("- NotebookLM 전사본과 대조 필요 (/crosscheck)")
    removed = [s for s in segs if s.get("text") == "" and s["i"] not in set(r.get("reverted") or [])]
    if removed:
        memo.append(f"- 환각·잡음으로 지운 {len(removed)}문장: "
                    + ", ".join(f"{ts(s['start'])} \"{s['orig'][:30]}\"" for s in removed[:20]))
    changed = sum(1 for s in segs if s.get("text") is not None)
    rev = len(r.get("reverted") or [])
    memo.append(f"- 교정 {changed}문장" + (f" (그중 {rev}문장은 원본으로 되돌림)" if rev else "") + f" · 작업 {job['name']}")
    out += ["---", "## 처리 메모"] + memo
    return "\n".join(out) + "\n"


def write_md(name, overwrite=False):
    """result.json으로 md를 조립해 과목 폴더에 저장. 이 작업이 쓴 적 없는 같은 이름 파일이 있으면
    overwrite=True일 때만 (backups/에 백업한 뒤) 덮어씀. 저장 경로 반환."""
    job = load_job(name)
    if is_superseded(job):  # 같은 md를 새 작업이 쓰므로 대체된 작업은 절대 쓰지 않음
        raise ValueError(f"대체된 작업({name} → {job.get('superseded_by')})은 md를 쓰지 않습니다")
    r = C.load_json(work_dir(name) / "result.json")
    path = md_path(job)
    C.check_sophomore_write(path)
    if path.exists() and job.get("md_path") != str(path):  # 이 작업이 쓴 파일이 아님
        if not overwrite:
            raise FileExistsError(str(path))
        C.backup_file(path, f"{job['subject']}_{path.stem}")
    path.parent.mkdir(parents=True, exist_ok=True)
    manual = load_manual(name)
    path.write_text(render_md(job, r, manual, review_state(name, r, manual)), encoding="utf-8")
    job["md_path"] = str(path)
    save_job(name, job)
    return path


def set_reverted(name, i, revert):
    """결과 화면의 문장별 되돌리기. md를 다시 조립한다."""
    _check_active(name)
    p = work_dir(name) / "result.json"
    r = C.load_json(p)
    if not r:
        raise FileNotFoundError("아직 교정 결과가 없습니다")
    if not any(s["i"] == i and s.get("text") is not None for s in r["segments"]):
        raise ValueError(f"{i}번 문장은 교정된 문장이 아닙니다")
    rv = set(r.get("reverted") or [])
    (rv.add if revert else rv.discard)(i)
    r["reverted"] = sorted(rv)
    C.save_json(p, r)
    return write_md(name)


# ---------- 목록 ----------

def list_works():
    out = []
    if JOBS.is_dir():
        for d in JOBS.iterdir():
            j = C.load_json(d / "job.json") if d.is_dir() else None
            if j:
                out.append(j)
    return out


def work_state(name):
    """화면용 요약: 상태, 명령, edits 감지, 결과 유무, 파일 변경 시각(자동 갱신 감지용)."""
    j = load_job(name)
    if not j:
        return None
    d = work_dir(name)
    ef = edits_files(name)
    res = d / "result.json"
    mt = max([p.stat().st_mtime for p in [d / "job.json", res, d / "review.json", *ef] if p.exists()] or [0])
    edits_mtime = max([p.stat().st_mtime for p in ef] or [0])
    return {"name": name, "subject": j["subject"], "status": j["status"], "status_text": STATUS.get(j["status"], j["status"]),
            "command": correct_command(name), "edits": [p.name for p in ef],
            "edits_pending": bool(ef) and (not res.exists() or edits_mtime > res.stat().st_mtime),
            "has_result": res.exists(), "md_path": j.get("md_path"), "applied": j.get("applied"),
            "stats": j.get("stats"), "comprehension": j.get("comprehension"), "restore": j.get("restore"),
            "audio": bool(j.get("audio") and Path(j["audio"]).exists()), "mtime": mt,
            "notebooklm": (d / "notebooklm.md").exists(), "crosscheck_command": crosscheck_command(name),
            "materials_registered": j.get("materials_registered"),
            "superseded_by": j.get("superseded_by"), "superseded_at": j.get("superseded_at"),
            "source_file": j.get("source_file"), "duration": j.get("duration"), "segments": j.get("segments"),
            "transcribed": transcribed_at(j), "server_job": j.get("server_job"), "lecture": C.lecture_key(j.get("source_file") or ""),
            "review": j.get("review"), "next": next_action(j), "carried_manual": j.get("carried_manual")}


def next_action(j):
    """강의 목록의 다음 할 일: correct(/correct 복사) / review(확인 필요 n곳) / complete(완료) / None(대체됨)."""
    if j.get("status") == "awaiting":
        return "correct"
    if j.get("status") == "done":
        rv = j.get("review") or {}
        return "review" if rv.get("total") and not rv.get("completed") else "complete"
    return None


def awaiting_list():
    """교정 대기 작업 (전사가 오래된 것부터). /correct는 Pro 사용량 때문에 한 번에 하나씩 실행한다."""
    out = [work_state(j["name"]) for j in list_works() if j.get("status") == "awaiting"]
    out.sort(key=lambda w: (w["transcribed"] or "", w["name"]))
    for w in out:  # 나뉜 녹음: 앞부분이 아직 교정 대기면 표시 (앞부분부터 교정하면 뒷부분 context.md에 교정본이 들어감)
        prev = part_works(w["subject"], w["source_file"] or "")[0]
        w["prev_part"] = {"name": prev["name"], "status": prev.get("status")} if prev else None
    return out


def crosscheck_command(name):
    return f"/crosscheck {name} jobs/{name}/notebooklm.md" if (work_dir(name) / "notebooklm.md").exists()         else f"/crosscheck {name} <NotebookLM md 경로>"


def main():
    C.utf8_stdout()
    a = __import__("sys").argv[1:]
    if a == ["versions"]:
        changed = refresh_versions()
        print("대체 상태를 바꾼 작업: " + (", ".join(changed) if changed else "없음"))
        for j in sorted(list_works(), key=lambda j: j["name"]):
            if j.get("status") == "superseded":
                print(f"- {j['name']} → {j['superseded_by']} 로 대체됨 ({j['source_file']})")
        return
    if len(a) == 2 and a[0] == "context":
        meta = rebuild_context(a[1])
        print(f"context.md 다시 만듦: jobs/{a[1]}/context.md · 등록 강의자료 {meta['decks_registered']}개"
              f" · 후보 {', '.join(meta['candidate_decks']) or '없음'}"
              + (f" · 원서 참고 {len(meta['book_sections'])}절" if meta.get("book_sections") else ""))
    else:
        raise SystemExit("사용법: python tools/work.py context <작업이름>   (context.md만 다시 만들기)\n"
                         "        python tools/work.py versions          (같은 녹음을 다시 전사한 작업 → 이전 작업 \"대체됨\")")


if __name__ == "__main__":
    main()
