"""Whisper 프롬프트 빌더: 과목 데이터로 initial_prompt + hotwords를 만든다.

faster-whisper는 hotwords와 initial_prompt를 각각 최대 223토큰까지만 쓰고, 프롬프트가 길수록 한 번에 받아 적을 수 있는
글자 수(전체 448토큰)가 줄어든다. 그래서 둘을 합쳐 prompt_token_limit(기본 223) 안에서만, Whisper 토크나이저로 실제로 재서
우선순위대로 채운다:
  ① 진도 위치 다음 페이지들의 용어 (등록된 강의자료)   → hotwords
  ② 과목 핵심 용어 (용어집 우선순위 → 최근 등장 → 빈도) → hotwords
  ③ 교수님 고정 표현                                   → initial_prompt
initial_prompt는 "<과목> 강의입니다." 한 문장으로 시작한다 (항상 들어감).

    python tools/prompt.py <과목>      # 실제로 들어갈 프롬프트와 잘려 나간 항목 보기
"""
import sys
from pathlib import Path

try:
    from . import common as C
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import common as C

TOKENIZER_REPO = "Systran/faster-whisper-large-v3"  # transcriber.MODEL_NAME = large-v3
_counter = None


def token_counter():
    """(텍스트 → 토큰 수 함수, 실측 여부). 모델 폴더의 tokenizer.json을 쓰고, 없으면 바이트 수로 어림."""
    global _counter
    if _counter is None:
        try:
            from huggingface_hub import try_to_load_from_cache
            from tokenizers import Tokenizer
            path = try_to_load_from_cache(TOKENIZER_REPO, "tokenizer.json")
            if not isinstance(path, str):
                raise FileNotFoundError("tokenizer.json이 캐시에 없습니다")
            tok = Tokenizer.from_file(path)
            _counter = (lambda t: len(tok.encode(" " + t.strip(), add_special_tokens=False).ids) if t.strip() else 0, True)
        except Exception:
            _counter = (lambda t: (len(t.strip().encode("utf-8")) + 1) // 2 if t.strip() else 0, False)
    return _counter


def _progress_terms(subject, s):
    """① 진도 위치 다음 페이지들의 용어 (진도 위치가 없으면 빈 목록)."""
    d = C.subject_dir(subject)
    prog = C.load_json(d / "progress.json", {}) or {}
    if not prog.get("deck"):
        return []
    deck = C.load_json(C.deck_path(subject, prog["deck"]))
    if not deck:
        return []
    start = int(prog.get("page") or 0)
    out = []
    for p in deck["pages"]:
        if start < p["page"] <= start + s["prompt_progress_pages"]:
            out += [(t, f"{prog['deck']} p.{p['page']}") for t in p.get("terms", []) + p.get("figure_terms", [])]
    return out


def _glossary_terms(subject, s):
    """② 용어집: 우선순위 높은 순 → 최근 등장 → 빈도."""
    glo = C.load_json(C.subject_dir(subject) / "glossary.json", {"terms": []})["terms"]
    glo = [t for t in glo if t.get("priority", 3) >= s["prompt_min_priority"]]
    glo.sort(key=lambda t: (-t.get("priority", 3), -_date_num(t.get("last_seen")), -t.get("freq", 0)))
    return [(t["term"], f"용어집 (우선순위 {t.get('priority', 3)})") for t in glo]


def _date_num(d):
    try:
        return int(str(d).replace("-", "")[:8])
    except ValueError:
        return 0


def build(subject, limit=None):
    """{"prompt", "hotwords", "tokens": {"prompt", "hotwords", "total", "limit", "measured"}, "items": [...], "cut": [...]}
    items: {"kind": progress|glossary|phrase, "text", "source", "tokens", "used"}"""
    s = C.settings()
    sub = C.subject(subject)
    limit = limit or s["prompt_token_limit"]
    count, measured = token_counter()
    base = f"{subject} 강의입니다."
    hot, phrases, items, seen = [], [], [], set()

    cands = [("progress", t, src) for t, src in _progress_terms(subject, s)]
    cands += [("glossary", t, src) for t, src in _glossary_terms(subject, s)]
    cands += [("phrase", p, "교수님 고정 표현") for p in sub.get("professor_phrases") or []]

    def total(h, ph):
        return count(" ".join([base] + ph)) + count(", ".join(h))

    for kind, text, src in cands:
        text = " ".join(str(text).split())
        key = C.term_key(text)
        if not text or (kind != "phrase" and key in seen):
            continue
        seen.add(key)
        h2, p2 = (hot + [text], phrases) if kind != "phrase" else (hot, phrases + [text])
        t2 = total(h2, p2)
        used = t2 <= limit
        before = total(hot, phrases)
        if used:
            hot, phrases = h2, p2
        items.append({"kind": kind, "text": text, "source": src, "tokens": t2 - before if used else count(text), "used": used})
    prompt = " ".join([base] + phrases)
    hotwords = ", ".join(hot)
    tp, th = count(prompt), count(hotwords)
    return {"subject": subject, "prompt": prompt, "hotwords": hotwords,
            "tokens": {"prompt": tp, "hotwords": th, "total": tp + th, "limit": limit, "measured": measured},
            "items": items, "cut": [x["text"] for x in items if not x["used"]]}


def fix_dict(subject):
    """교정 사전(fix.json) → Whisper 출력에 바로 적용할 {틀린: 바른} (기존 프리셋 FIX와 같은 방식)."""
    return {r["wrong"]: r["right"] for r in C.load_json(C.subject_dir(subject) / "fix.json", {"rules": []})["rules"]
            if r.get("wrong") and r.get("right")}


def main():
    C.utf8_stdout()
    if len(sys.argv) != 2:
        sys.exit("사용법: python tools/prompt.py <과목>")
    b = build(sys.argv[1])
    t = b["tokens"]
    print(f"토큰 {t['total']}/{t['limit']} (initial_prompt {t['prompt']} + hotwords {t['hotwords']})"
          + ("" if t["measured"] else " — 토크나이저가 없어 어림값"))
    print("initial_prompt:", b["prompt"])
    print("hotwords:", b["hotwords"] or "(없음)")
    if b["cut"]:
        print(f"잘림 {len(b['cut'])}개:", ", ".join(b["cut"]))


if __name__ == "__main__":
    main()
