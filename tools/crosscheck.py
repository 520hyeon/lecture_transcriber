"""/crosscheck 보조: 이해도가 낮은 구간(comprehension.weak_ranges)을 NotebookLM 전사본의 같은 부분과 나란히 놓은 대조표.

    python tools/crosscheck.py <작업이름> <NotebookLM md 경로> [--ranges 300-340,400-420]

NotebookLM 전사본은 타임스탬프 없는 한 덩어리라, 녹음 길이 대비 위치로 대략 찾은 뒤(앞뒤 15% 범위)
내용어(흔한 낱말을 뺀 한글·영어 낱말)가 가장 많이 겹치는 문장 묶음을 고른다. 결과: jobs/<작업이름>/crosscheck.md
Claude Code는 NotebookLM 전체가 아니라 이 대조표만 읽는다 (사용량 절약).
"""
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import review as RV
import work as W

WINDOW = 0.15   # 위치 추정 오차 (녹음 길이 대비)
MIN_SCORE = 0.22   # 같은 강의 0.28~0.85, 다른 날 강의 0.05~0.20 (흔한 낱말을 뺀 내용어 겹침)
SURE_SCORE = 0.35


def _tokens(text):
    """내용어: 한글 낱말(조사 뗌, 2글자 이상)과 영어 낱말."""
    out = []
    for w in re.findall(r"[가-힣]+|[A-Za-z]{2,}", text.lower()):
        w = RV.split_particle(w)[0]
        if len(w) >= 2:
            out.append(w)
    return out


def sentences(md_text):
    body = "\n".join(l for l in md_text.split("\n") if not l.startswith("#") and not l.startswith(">"))
    return [x.strip() for x in re.split(r"(?<=[.?!])\s+|\n+", body) if x.strip()]


def locate(sents, text, t0, t1, common=None):
    """전사본 구간(text, 녹음 위치 비율 t0~t1)과 가장 겹치는 NotebookLM 문장 묶음 → (시작, 끝, 점수).
    점수 = 구간 내용어 중 NotebookLM 묶음에도 있는 비율 (어느 강의에나 나오는 흔한 낱말은 뺌) − 길이 차이 벌점."""
    if not sents:
        return 0, 0, 0.0
    toks = [set(_tokens(s)) for s in sents]
    if common is None:
        cnt = {}
        for t in toks:
            for w in t:
                cnt[w] = cnt.get(w, 0) + 1
        common = {w for w, c in cnt.items() if c >= max(8, len(sents) * 0.02)}  # 문장의 2% 이상에 나오는 낱말
    want_t = set(_tokens(text)) - common
    if not want_t:
        return 0, 0, 0.0
    total = sum(len(s) for s in sents)
    pos, acc = [], 0
    for s in sents:
        pos.append(acc / total)
        acc += len(s)
    lo, hi = t0 - WINDOW, t1 + WINDOW
    idx = [k for k, p in enumerate(pos) if lo <= p <= hi] or list(range(len(sents)))
    want = max(40, len(re.sub(r"\s", "", text)))
    best = (idx[0], idx[0], 0.0)
    for a in idx:
        got, size = set(), 0
        for b in range(a, min(len(sents), a + 80)):
            got |= toks[b]
            size += len(re.sub(r"\s", "", sents[b]))
            if size < want * 0.6:
                continue
            if size > want * 1.6:
                break
            sc = len(want_t & got) / len(want_t) - 0.15 * abs(size - want) / want
            if sc > best[2]:
                best = (a, b, sc)
    return best[0], best[1], round(best[2], 2)


def parse_ranges(arg):
    out = []
    for part in arg.split(","):
        a, _, b = part.strip().partition("-")
        out.append({"from": int(a), "to": int(b or a), "why": "직접 지정"})
    return out


def build(name, nb_path, ranges=None):
    job = W.load_job(name) or sys.exit(f"작업이 없습니다: {name}")
    r = C.load_json(W.work_dir(name) / "result.json") or sys.exit("아직 교정 결과가 없습니다. /correct를 먼저 하세요.")
    nb_path = Path(nb_path)
    if not nb_path.is_absolute():
        nb_path = (C.BASE / nb_path) if (C.BASE / nb_path).exists() else nb_path.resolve()
    if not nb_path.exists():
        sys.exit(f"NotebookLM 전사본이 없습니다: {nb_path}")
    sents = sentences(nb_path.read_text(encoding="utf-8", errors="replace"))
    # 판서 설명(kind "board")은 말 자체가 조각난 것이라 NotebookLM과 대조해도 나아지지 않으므로 뺌
    ranges = ranges or [w for w in (r.get("comprehension") or {}).get("weak_ranges") or [] if w.get("kind") != "board"]
    if not ranges:
        sys.exit("인식 실패로 약한 구간(comprehension.weak_ranges, 판서 설명 제외)이 없습니다. --ranges 300-340 처럼 직접 지정하세요.")
    manual = W.load_manual(name)
    by = {s["i"]: s for s in r["segments"]}
    dur = job["duration"] or r["segments"][-1]["end"]
    L = [f"# NotebookLM 대조표: {name}", "",
         f"- NotebookLM 전사본: {nb_path} ({len(sents)}문장)",
         "- NotebookLM도 인식 오류가 있다. 정답이 아니라 **두 번째 후보**로만 쓴다.",
         "- 두 전사가 같은 내용을 다르게 적은 곳만 고친다. NotebookLM에만 있는 문장을 전사본에 옮겨 넣지 않는다 (세그먼트 합치기·내용 추가 금지).",
         "- 위치는 녹음 길이 대비 비율로 어림잡아 찾은 것이라 틀릴 수 있다. 일치도가 낮으면 그 구간은 고치지 않는다.", ""]
    for w in ranges:
        a, b = int(w["from"]), int(w["to"])
        segs = [by[i] for i in range(a, b + 1) if i in by]
        if not segs:
            continue
        lines = []
        for s in segs:
            m = manual.get(str(s["i"]))
            text = m["text"] if m else W.final_text(r, s)
            flag = "H" if m else s.get("flag") or ""
            if s.get("flag") == "M" and not m:
                text = f"[누락 {W.ts(s['start'])}–{W.ts(s['end'])}]"
            lines.append(f"{s['i']}|{W.ts(s['start'])}|{text}|{flag}")
        joined = " ".join(l.split("|")[2] for l in lines)
        k0, k1, sc = locate(sents, joined, segs[0]["start"] / dur, segs[-1]["end"] / dur)
        L += [f"## 구간 {a}–{b} ({W.ts(segs[0]['start'])}–{W.ts(segs[-1]['end'])})" + (f" · {w.get('why')}" if w.get("why") else ""),
              "", "### 전사본 (번호|시각|지금 문장|표시)", *lines, ""]
        if sc < MIN_SCORE:
            L += [f"### NotebookLM: 맞는 부분을 찾지 못함 (일치도 {sc}) → 이 구간은 고치지 않는다", ""]
            continue
        k0, k1 = max(0, k0 - 1), min(len(sents) - 1, k1 + 1)  # 앞뒤 한 문장씩 더
        sure = "" if sc >= SURE_SCORE else " · 일치도가 낮은 편: 같은 내용이 분명한 문장만 고친다"
        L += [f"### NotebookLM 후보 (일치도 {sc}, 앞뒤 한 문장 포함{sure})", "", " ".join(sents[k0:k1 + 1]), ""]
    out = W.work_dir(name) / "crosscheck.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out, len(ranges)


def main():
    C.utf8_stdout()
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        sys.exit(__doc__)
    ranges = None
    if "--ranges" in sys.argv:
        ranges = parse_ranges(sys.argv[sys.argv.index("--ranges") + 1])
        a = [x for x in a if x != sys.argv[sys.argv.index("--ranges") + 1]]
    out, n = build(a[0], " ".join(a[1:]), ranges)
    print(f"대조표 {n}구간 → {out}")


if __name__ == "__main__":
    main()
