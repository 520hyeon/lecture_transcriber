"""과목 데이터 공용 함수 (server.py와 tools/*.py가 같이 씀).

data/
├─ settings.json        루트 경로 등 전체 설정
├─ subjects.json        과목별 규칙 (화면에서 수정)
└─ <과목>/
   ├─ glossary.json     용어집
   ├─ fix.json          교정 사전
   ├─ progress.json     진도 위치
   ├─ suggestions.json  제안 (tools/auto.py가 자동 승인 / 사람 확인으로 분류)
   ├─ changes.json      변경 기록: 승인·거절 (자동 승인 포함, 승인은 되돌리기 가능)
   ├─ decks/<파일명>.json  강의 슬라이드·손필기 페이지 인덱스 (위치 표시 대상)
   ├─ refs/<파일명>.json   참고 자료 용어 (ipynb, docx, 정리 노트; 위치 표시 안 함)
   └─ cache/<파일명>/      페이지 텍스트·PNG 캐시 (git 제외)
"""
import fnmatch, json, re, sys, threading, uuid
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
BACKUPS = BASE / "backups"
SETTINGS_PATH = DATA / "settings.json"
SUBJECTS_PATH = DATA / "subjects.json"
VENV_PY = ".venv/Scripts/python.exe"  # Claude Code 명령에서 쓰는 파이썬 경로 (프로젝트 루트 기준)

DEFAULT_SETTINGS = {
    "root": str(Path.home() / "Documents" / "Lectures"),  # 과목 폴더들이 있는 곳 (과목 관리 화면에서 변경)
    "finals_dir": "Finals_Scope",
    "midterm_dir": "Midterm_Scope",
    "materials_dir": "강의자료",
    "transcripts_dir": "전사본",
    "finals_recordings": "기말고사_녹음본",
    "midterm_recordings": "중간고사_녹음본",
    "slide_exts": [".pdf", ".pptx"],          # 강의 슬라이드로 등록 (페이지 인덱스 + 위치 표시)
    "reference_exts": [".ipynb", ".docx"],    # 참고 자료 (텍스트만, 용어 추출용)
    "midterm_exclude_patterns": ["*_필기.pdf"],  # 중간고사 자료에서 제외 (원본 슬라이드와 겹침)
    "render_long_side": 1280,                 # 슬라이드 PNG·OCR 해상도: 긴 변(px)
    "render_long_side_handwritten": 1600,     # 손필기 PNG 긴 변(px)
    "beam_size": 5,                           # Whisper beam size (과목 관리 → 고급, 클수록 느리지만 정확)
    # 그림 페이지 처리: 그림 영역을 먼저 OCR(RapidOCR) → 결과가 빈약한 페이지만 Claude가 PNG를 직접 봄
    "ocr_enabled": True,
    "ocr_min_region": 0.02,      # 이 비율(페이지 면적 대비) 이상인 그림만 OCR
    "figure_min_cover": 0.12,    # 그림이 이 비율 이상 덮는 페이지만 "그림 페이지"로 보고 이미지 확인 여부를 판정
    "ocr_min_chars": 80,         # 그림 속 OCR 글자 수(공백 제외)가 이보다 적으면 이미지 확인
    "ocr_min_density": 300,      # OCR 글자 수 ÷ 그림 면적 비율이 이보다 낮으면 이미지 확인 (도식·다이어그램)
    "ocr_min_score": 0.80,       # OCR 평균 신뢰도가 이보다 낮으면 이미지 확인
    # 교정 작업 폴더 (jobs/)
    "uncertain_logprob": -1.0,     # 세그먼트 avg_logprob가 이보다 낮으면 "불확실"(U)
    "uncertain_compression": 2.4,  # compression_ratio가 이보다 높으면 "불확실"(U)
    "context_glossary_max": 80,    # context.md에 넣을 용어집 용어 수 (우선순위순)
    "context_max_decks": 3,        # context.md에 페이지 인덱스를 다 넣을 후보 강의자료 수
    # 원서 참고 (tools/book.py): 전사본 핵심 용어로 원서 절을 찾아 의사코드·정의만 context.md에
    "book_max_sections": 2,              # 넣을 원서 절 수
    "book_excerpt_chars": 4000,          # 강의자료 미등록일 때 발췌 글자 수 한도
    "book_excerpt_chars_with_slides": 1500,  # 슬라이드가 등록돼 있을 때 (보조 자료라 짧게, 0이면 넣지 않음)
    # 제안 → 승인 / 중간고사 샘플 / 대조
    "midterm_sample_count": 2,           # 과목당 중간고사 샘플 전사 기본 개수 (화면에서 직접 고름)
    # 나뉜 녹음 (<과목>_<YYMMDD>_01, _02 …): 뒷부분을 교정할 때 앞부분 마지막 문단을 context.md에 넣는 줄 수
    "prev_part_lines": 8,
    # 정리: backups/는 파일별로 최근 n개만 남김 (기본 꺼짐. 켜면 백업할 때와 화면 "저장 공간 정리"에서 오래된 것을 지움)
    "backup_prune_enabled": False,
    "backup_keep_per_file": 10,
    "user_note_patterns": ["*_필기.pdf", "*개념정리노트*", "*정리노트*", "*_학습진행요약.md", "*_인수인계_*",
                           "*_유형정리*", "*_복기*"],  # 사용자가 만든 정리 노트 (용어집·교정 사전 출처에서 제외)
    # 프롬프트 빌더 (tools/prompt.py): initial_prompt + hotwords를 Whisper 토크나이저로 재서 이 안에서만 채움
    "prompt_token_limit": 223,
    "prompt_min_priority": 1,      # 이 우선순위 미만인 용어집 용어는 프롬프트에 넣지 않음
    "prompt_progress_pages": 10,   # 진도 위치 다음 몇 쪽의 용어를 먼저 넣을지
    # 환각·누락 감지와 복원 (tools/restore.py, pipeline.py)
    "restore_enabled": True,
    "repeat_min_times": 3,             # 같은 단어·구절이 연속 이만큼 반복되면 환각 의심
    "english_min_words": 4,            # language=ko인데 한글 없이 영어 단어가 이만큼 이상이면 번역 출력 의심
    "hallucination_compression": 2.4,  # compression_ratio가 이보다 높으면 환각 의심
    "hallucination_phrases": ["시청해주셔서 감사합니다", "시청해 주셔서 감사합니다", "구독과 좋아요",
                              "구독 좋아요", "좋아요와 구독", "MBC 뉴스", "자막 제공", "Thank you for watching"],
    "numbers_min_count": 12,       # 숫자 토큰이 이만큼 이상이고
    "numbers_min_ratio": 0.8,      # 전체 토큰의 이 비율 이상이면 숫자 나열 환각 의심 (배열 예시 "2, 8, 7, 1, 3, 5, 6, 4"는 통과)
    "counting_min_run": 10,        # 1씩 늘어나는 숫자가 이만큼 연속이면 숫자 세기 환각 의심
    "same_token_min_count": 16,    # 토큰이 이만큼 이상인 세그먼트에서
    "same_token_ratio": 0.5,       # 같은 형태의 토큰(숫자는 #)이 이 비율 이상이면 환각 의심
    "gap_min_seconds": 4.0,        # 세그먼트 사이 간격이 이 이상이고 소리가 있으면 누락 의심
    "sparse_min_seconds": 8.0,     # 이보다 긴 세그먼트가
    "sparse_max_cps": 1.2,         # 초당 이 글자 수(공백 제외) 미만이고 소리가 있으면 누락 의심
    "voice_margin_db": 12.0,       # 녹음 바닥 소음보다 이만큼(dB) 크면 "소리 있음"
    "voiced_min_ratio": 0.3,       # 구간의 이 비율 이상이 "소리 있음"이어야 누락 의심
    "silent_max_ratio": 0.1,       # 재전사가 모두 실패해도 이 비율 미만이면 소리 없는 구간의 환각으로 보고 지움
    "region_join_seconds": 1.0,    # 이보다 가까운 의심 구간은 하나로 합침
    "restore_pad_seconds": 1.5,    # 재전사할 때 앞뒤 여유
    "restore_no_speech_threshold": 0.8,  # 재전사 때 완화한 no_speech_threshold (기본 0.5)
    "restore_audio_filter": "highpass=f=80,afftdn=nf=-25,dynaudnorm=f=150:g=15",  # 재전사 전 ffmpeg 전처리
}

_ENG = ["교수님이 영어로 말했는데 한글 발음으로 적힌 용어만 영어로 표기한다 (포토신세시스 → photosynthesis, 마이토콘드리아 → mitochondria).",
        "교수님이 한국어 용어로 말한 것은 바른 한국어 표기로만 고치고 영어로 번역하지 않는다 (광학성 → 광합성, 최대값 → 최댓값).",
        "영어로 말했는지 한국어로 말했는지 애매하면 Whisper가 적은 발음에 가까운 쪽을 따른다.",
        "수식을 읽은 경우는 수식 표기로 쓴다 (x 제곱 → x², 루트 x → √x, n 분의 1 → 1/n). "
        "수식이 아니라 일반 낱말로 쓴 경우는 낱말 그대로 둔다.",
        "슬라이드는 영어, 강의는 한국어이고 교수님은 용어와 슬라이드 설명을 주로 영어로 말한다.",
        "원서 md는 용어 철자 확인과 참고용으로만 쓴다."]
_KOR = ["슬라이드와 수업 모두 한국어이고, 코드 파트와 주요 용어만 가끔 영어로 한다.",
        "용어는 슬라이드 표기를 따른다.",
        "교수님이 따로 말한 영어 용어는 용어집 제안에 추가한다."]

# 용어 제안에서 뺄 너무 일반적인 단어 (과목별로 subjects.json "term_stopwords"에서 편집)
_STOP_COMMON = ["true", "false", "example", "figure", "table", "value", "result", "input", "output"]
_STOP_ENG = _STOP_COMMON + ["values", "results", "step", "steps", "case", "cases", "problem", "method",
                            "data", "number", "definition", "lecture"]
_STOP_KOR = _STOP_COMMON + ["기능", "값", "예제", "예시", "데이터", "정보", "방법", "개념", "관계",
                            "구조", "결과", "사용", "문제", "경우", "형식", "종류",
                            "특징", "장점", "단점", "내용", "처리", "표현", "설명"]


def _subject(**kw):
    d = {"language": "ko", "summary": "", "rules": [], "has_slides": True, "handwritten": False,
         "keep_code_identifiers": False, "books": [], "reference_dirs": [], "ignore_dirs": [],
         "ignore_files": [], "notation": [], "professor_phrases": [],
         "prefer_english_terms": False,  # 용어 대표 표기를 영어로 (한국어는 별칭)
         "term_stopwords": []}
    d.update(kw)
    return d


DEFAULT_SUBJECTS = {}  # 처음에는 과목이 없음. 과목 관리 화면에서 직접 추가

# 과목을 추가할 때 고르는 시작 규칙 (추가한 뒤 화면에서 자유롭게 수정)
SUBJECT_TEMPLATES = {
    "korean": dict(label="한국어 강의", summary="슬라이드·수업 한국어 · 코드와 주요 용어만 가끔 영어",
                   rules=list(_KOR), term_stopwords=list(_STOP_KOR)),
    "english_terms": dict(label="영어 용어를 많이 쓰는 강의", summary="슬라이드 영어 · 강의 한국어 + 용어·슬라이드 설명 영어",
                          rules=list(_ENG), prefer_english_terms=True, term_stopwords=list(_STOP_ENG)),
    "code": dict(label="코드 실습 강의", summary="슬라이드·수업 한국어 · 코드 식별자는 그대로",
                 rules=_KOR + ["코드 식별자(변수명, 함수명, 키워드)는 코드 표기 그대로 유지한다 (예: import, return, getName())."],
                 keep_code_identifiers=True, term_stopwords=list(_STOP_KOR)),
    "handwritten": dict(label="판서·손필기 강의", summary="슬라이드 없음(판서) · 강의자료는 손필기 PDF · 말로 하는 수식은 수식 표기로",
                        rules=["슬라이드가 없고 판서로 수업한다. 강의자료는 손필기 PDF이다.",
                               "손필기 PDF는 텍스트 레이어가 깨져 있으므로 반드시 페이지 이미지로 읽는다.",
                               "말로 하는 수식은 표기 규칙에 따라 수식 표기로 바꾼다."],
                        has_slides=False, handwritten=True,
                        term_stopwords=_STOP_COMMON + ["값", "식", "수식", "예제", "예시", "문제", "결과", "계산", "풀이"],
                        notation=[{"spoken": "x 제곱", "written": "x²"},
                                  {"spoken": "루트 x", "written": "√x"}]),
}

_lock = threading.RLock()


# ---------- 입출력 ----------

def utf8_stdout():
    """Windows 콘솔(cp949)에서도 한글·기호가 깨지지 않게."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load_json(path, default=None):
    path = Path(path)
    with _lock:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path = Path(path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def backup_file(path, tag=None):
    """backups/<이름>_<YYYYMMDD_HHMMSS>[_n].<확장자> 로 복사. 여러 번 백업해도 덮어쓰지 않음."""
    path = Path(path)
    if not path.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    base, suffix = f"{tag or path.stem}_{datetime.now():%Y%m%d_%H%M%S}", path.suffix
    dst, n = BACKUPS / f"{base}{suffix}", 2
    while dst.exists():
        dst, n = BACKUPS / f"{base}_{n}{suffix}", n + 1
    dst.write_bytes(path.read_bytes())
    if settings().get("backup_prune_enabled"):
        prune_backups()
    return dst


_BACKUP_RE = re.compile(r"^(.*)_(\d{8}_\d{6})(?:_(\d+))?(\.[^.]*)?$")


def backup_groups():
    """backups/ 파일을 원래 파일별로 묶음 {(tag, 확장자): [Path, …]} (오래된 것부터)."""
    groups = {}
    if BACKUPS.is_dir():
        for p in BACKUPS.iterdir():
            m = _BACKUP_RE.match(p.name) if p.is_file() else None
            if m:
                groups.setdefault((m.group(1), m.group(4) or ""), []).append((m.group(2), int(m.group(3) or 1), p))
    return {k: [p for *_, p in sorted(v)] for k, v in groups.items()}


def backups_to_prune(keep=None):
    """파일별로 최근 keep개(기본 settings backup_keep_per_file)를 넘는 오래된 백업."""
    keep = max(1, int(keep or settings()["backup_keep_per_file"]))
    return [p for ps in backup_groups().values() for p in ps[:-keep]]


def prune_backups(keep=None):
    """파일별로 최근 keep개만 남기고 지움. (지운 개수, 바이트)"""
    n = size = 0
    for p in backups_to_prune(keep):
        try:
            sz = p.stat().st_size
            p.unlink()
            n, size = n + 1, size + sz
        except OSError:
            pass
    return n, size


# ---------- 설정 ----------

def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(load_json(SETTINGS_PATH, {}) or {})
    return s


def root():
    return Path(settings()["root"])


def subjects():
    return load_json(SUBJECTS_PATH, None) or DEFAULT_SUBJECTS


def subject_names():
    return list(subjects().keys())


def subject(name):
    subs = subjects()
    if name not in subs:
        raise KeyError(f"과목 '{name}'이(가) 없습니다. 가능한 과목: {', '.join(subs)}")
    return {**_subject(), **subs[name]}


def subject_dir(name):
    return DATA / name


def finals_materials_dir(name):
    s = settings()
    return root() / name / s["finals_dir"] / s["materials_dir"]


def finals_transcripts_dir(name):
    s = settings()
    return root() / name / s["finals_dir"] / s["transcripts_dir"]


def lecture_key(filename):
    """'과목명_261028_02.m4a' → {"subject", "date": "261028", "part": 2, "lecture": "과목명_261028"}.
    나뉜 녹음(_01, _02 …)은 같은 날 강의의 앞뒤 부분 (part 없으면 None). 형식이 맞지 않거나 없는 과목이면 None."""
    m = re.match(r"^(.+?)_(\d{6})(?:_(\d{1,2}))?$", Path(filename).name.split(".")[0])
    if not m or m.group(1) not in subjects():
        return None
    return {"subject": m.group(1), "date": m.group(2), "part": int(m.group(3)) if m.group(3) else None,
            "lecture": f"{m.group(1)}_{m.group(2)}"}


def subject_from_filename(filename):
    """'과목명_261028_01.m4a' → '과목명' (형식이 맞지 않으면 None)."""
    k = lecture_key(filename)
    return k["subject"] if k else None


# ---------- 강의 폴더 쓰기 보호 ----------

class WriteNotAllowed(Exception):
    pass


def check_root_write(path):
    """강의 폴더 안에서는 <과목>/Finals_Scope/전사본/ 바로 아래 파일만 쓸 수 있음."""
    p = Path(path).resolve()
    r = root().resolve()
    try:
        rel = p.relative_to(r)
    except ValueError:
        return p  # 강의 폴더 밖 (프로그램 폴더 등)
    s = settings()
    parts = rel.parts
    if len(parts) == 4 and parts[0] in subjects() and parts[1] == s["finals_dir"] and parts[2] == s["transcripts_dir"]:
        return p
    raise WriteNotAllowed(f"강의 폴더에는 <과목>\\{s['finals_dir']}\\{s['transcripts_dir']}\\ 에만 쓸 수 있습니다: {p}")


# ---------- 초기화 / 과목 추가·삭제 ----------

def _empty_files():
    return {
        "glossary.json": {"terms": []},
        "fix.json": {"rules": []},
        "progress.json": {"deck": None, "page": None, "updated": None, "history": []},
        "suggestions.json": {"items": []},
    }


def ensure_data():
    """data/ 뼈대를 만듦 (이미 있는 파일은 건드리지 않음)."""
    with _lock:
        DATA.mkdir(exist_ok=True)
        cur = load_json(SETTINGS_PATH, None)
        if cur is None or any(k not in cur for k in DEFAULT_SETTINGS):  # 새로 생긴 설정 항목을 파일에 채워 넣음 (먼저 백업)
            if cur is not None:
                backup_file(SETTINGS_PATH)
            save_json(SETTINGS_PATH, {**DEFAULT_SETTINGS, **(cur or {})})
        if not SUBJECTS_PATH.exists():
            save_json(SUBJECTS_PATH, DEFAULT_SUBJECTS)
        else:
            _fill_subject_keys()
        for name in subject_names():
            d = subject_dir(name)
            _ensure_subject_files(name)


def _ensure_subject_files(name):
    d = subject_dir(name)
    for sub in ("decks", "refs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    for fname, empty in _empty_files().items():
        if not (d / fname).exists():
            save_json(d / fname, empty)


def validate_subject_name(name):
    """과목 이름은 폴더 이름과 녹음 파일 이름(<과목>_<YYMMDD>.m4a)에 그대로 쓰임."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("과목 이름을 입력하세요")
    if len(name) > 40:
        raise ValueError("과목 이름은 40자 이내로 입력하세요")
    if re.search(r'[\\/:*?"<>|.\x00-\x1f]', name):
        raise ValueError('과목 이름에는 \\ / : * ? " < > | . 을 쓸 수 없습니다')
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)[1-9]", name):
        raise ValueError(f"'{name}'은(는) Windows에서 폴더 이름으로 쓸 수 없습니다")
    return name


def create_subject(name, template="korean"):
    """과목을 추가하고 data/<과목>/과 강의 폴더(<과목>/Finals_Scope/강의자료, 전사본)를 만듦."""
    name = validate_subject_name(name)
    if template not in SUBJECT_TEMPLATES:
        raise ValueError(f"없는 템플릿입니다: {template}")
    with _lock:
        subs = load_json(SUBJECTS_PATH, {}) or {}
        if any(n.lower() == name.lower() for n in subs):
            raise ValueError(f"이미 있는 과목입니다: {name}")
        tpl = {k: v for k, v in SUBJECT_TEMPLATES[template].items() if k != "label"}
        s = settings()
        try:
            finals_materials_dir(name).mkdir(parents=True, exist_ok=True)
            finals_transcripts_dir(name).mkdir(parents=True, exist_ok=True)
            (root() / s["finals_recordings"]).mkdir(parents=True, exist_ok=True)
            (root() / s["midterm_recordings"]).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"강의 폴더를 만들지 못했습니다: {e}")
        subs[name] = _subject(**tpl)
        save_json(SUBJECTS_PATH, subs)
        _ensure_subject_files(name)
    return subject(name)


def delete_subject(name):
    """과목을 목록에서 뺌. data/<과목>/은 backups/로 옮기고, 강의 폴더의 파일은 건드리지 않음."""
    with _lock:
        subs = load_json(SUBJECTS_PATH, {}) or {}
        if name not in subs:
            raise KeyError(f"과목 '{name}'이(가) 없습니다")
        backup_file(SUBJECTS_PATH)
        del subs[name]
        save_json(SUBJECTS_PATH, subs)
        d = subject_dir(name)
        moved = None
        if d.exists():
            moved = BACKUPS / f"deleted_subject_{name}_{datetime.now():%Y%m%d_%H%M%S}"
            BACKUPS.mkdir(exist_ok=True)
            d.rename(moved)
    return moved


def _fill_subject_keys():
    """나중에 생긴 과목 규칙 항목(term_stopwords 등)을 기존 subjects.json에 기본값으로 채움 (먼저 백업)."""
    subs = load_json(SUBJECTS_PATH, {})
    fill = {}
    for name, cur in subs.items():
        base = DEFAULT_SUBJECTS.get(name) or _subject()
        miss = {k: base.get(k, v) for k, v in _subject().items() if k not in cur}
        if miss:
            fill[name] = miss
    if fill:
        backup_file(SUBJECTS_PATH)
        for name, miss in fill.items():
            subs[name].update(miss)
        save_json(SUBJECTS_PATH, subs)
    return fill


# ---------- 용어집 ----------

def term_key(t):
    return re.sub(r"\s+", " ", t.replace("-", " ").strip().lower())


def glossary_keys(glo):
    """용어집의 모든 표기(대표 + 별칭) 키."""
    return {term_key(x) for t in glo["terms"] for x in [t["term"]] + (t.get("aliases") or [])}


def add_term(glo, term, source, priority=3, last_seen=None, aliases=()):
    """glossary dict에 용어를 추가(이미 있으면 출처·별칭만 합침). 우선순위 1(낮음)~5(높음).
    aliases: 같은 용어의 다른 표기 (한영 짝, 식물 광합성 → 광합성 등). 교정 때 대표 표기로 맞춤."""
    term = term.strip()
    if not term:
        return False
    k = term_key(term)
    aliases = [a.strip() for a in aliases if a.strip() and term_key(a) != k]
    for t in glo["terms"]:
        have = {term_key(x) for x in [t["term"]] + (t.get("aliases") or [])}
        if k in have:
            if source not in t["sources"]:
                t["sources"].append(source)
            new = [a for a in aliases if term_key(a) not in have]
            if new:
                t["aliases"] = (t.get("aliases") or []) + new
            return False
    glo["terms"].append({"term": term, "aliases": aliases, "sources": [source], "freq": 0,
                         "last_seen": last_seen, "priority": priority, "note": ""})
    return True


def add_suggestions(name, items):
    """suggestions.json에 제안 추가. 같은 종류·같은 내용의 대기 중 제안이 있으면 근거만 합침. 추가된 개수 반환."""
    path = subject_dir(name) / "suggestions.json"
    with _lock:
        data = load_json(path, {"items": []})
        pending = {(s["type"], _sugg_key(s)): s for s in data["items"] if s.get("status") == "pending"}
        added = 0
        for it in items:
            key = (it["type"], _sugg_key(it))
            if key in pending:
                old = pending[key]
                old.setdefault("evidence", []).extend(it.get("evidence", []))
                if it["type"] == "term":
                    _merge_term_data(old["data"], it["data"])
                continue
            s = {"id": "s_" + uuid.uuid4().hex[:8], "status": "pending", "created": now_iso(), **it}
            data["items"].append(s)
            pending[key] = s
            added += 1
        save_json(path, data)
        return added


def _merge_term_data(old, new):
    have = {term_key(x) for x in [old.get("term", "")] + (old.get("aliases") or [])}
    old["aliases"] = (old.get("aliases") or []) + [a for a in new.get("aliases") or [] if term_key(a) not in have]
    for k in ("in_title", "recommended"):
        old[k] = bool(old.get(k) or new.get(k))
    old["score"] = max(old.get("score") or 0, new.get("score") or 0)


def drop_source_suggestions(name, source, origin="register-slides", types=("term",)):
    """대기 중인 제안(types, None이면 모든 종류)에서 이 자료(source)의 근거를 뺌.
    근거가 이 자료뿐인 제안은 지움. 지운 개수 반환."""
    path = subject_dir(name) / "suggestions.json"
    with _lock:
        data = load_json(path, {"items": []})
        keep, dropped = [], 0
        for s in data["items"]:
            if s.get("status") == "pending" and (types is None or s["type"] in types) and s.get("origin") == origin:
                ev = [e for e in s.get("evidence", []) if e.get("source") != source]
                if not ev:
                    dropped += 1
                    continue
                s["evidence"] = ev
            keep.append(s)
        data["items"] = keep
        save_json(path, data)
        return dropped


def term_priority(d):
    """승인할 때 용어집 우선순위: 제목에 나오고 3쪽 이상 → 4, 기본 체크 → 3, 나머지 → 2."""
    if d.get("in_title") and len(d.get("pages") or []) >= 3:
        return 4
    return 3 if d.get("recommended") else 2


def resolve_suggestions(name, approve=(), reject=(), changes=None, by="user", reasons=None):
    """제안 승인·거절. 승인하면 종류별로 반영:
    term → glossary.json (대표 표기 + 별칭), fix_rule → fix.json, progress → progress.json (이전 위치는 history),
    notation → subjects.json "notation" (먼저 백업).
    changes: {제안 id: {"right": …} 또는 {"written": …} 또는 {"wrong": …}} 승인 화면에서 고친 값.
    by: "user"(승인 화면) | "auto"(tools/auto.py 자동 승인), reasons: {제안 id: [이유 …]}.
    승인·거절은 변경 기록(changes.json)에 남고, 승인은 되돌릴 수 있다 (undo_change)."""
    d = subject_dir(name)
    approve, reject, changes, reasons = set(approve), set(reject), changes or {}, reasons or {}
    with _lock:
        sugg = load_json(d / "suggestions.json", {"items": []})
        glo = load_json(d / "glossary.json", {"terms": []})
        fix = load_json(d / "fix.json", {"rules": []})
        prog = load_json(d / "progress.json", {}) or {"deck": None, "page": None, "updated": None, "history": []}
        subs = load_json(SUBJECTS_PATH, None) or DEFAULT_SUBJECTS
        today = datetime.now().strftime("%Y-%m-%d")
        done = {"approved": 0, "rejected": 0, "added": 0, "fix": 0, "notation": 0, "progress": None, "skipped": []}
        touched, log = set(), []
        for s in sugg["items"]:
            if s.get("status") != "pending" or s["id"] not in approve | reject:
                continue
            entry = {"id": "c_" + uuid.uuid4().hex[:8], "at": now_iso(), "by": by, "suggestion": s["id"], "type": s["type"],
                     "origin": s.get("origin"), "data": s["data"], "reason": "; ".join(reasons.get(s["id"]) or []),
                     "evidence": _evidence_text(s), "undone": None}
            if s["id"] in reject:
                s["status"], done["rejected"] = "rejected", done["rejected"] + 1
                s["resolved"], s["resolved_by"] = now_iso(), by
                log.append({**entry, "action": "reject"})
                continue
            data = {**s["data"], **{k: str(v).strip() for k, v in (changes.get(s["id"]) or {}).items()
                                    if k in ("wrong", "right", "written", "spoken") and str(v).strip()}}
            src = ", ".join(dict.fromkeys(e["source"] for e in s.get("evidence", []) if e.get("source"))) or s.get("origin", "")
            if s["type"] == "term":
                k = term_key(data["term"])
                old = next((t for t in glo["terms"] if k in {term_key(x) for x in [t["term"]] + (t.get("aliases") or [])}), None)
                undo = {"term": old["term"] if old else data["term"].strip(), "before": json.loads(json.dumps(old)) if old else None}
                done["added"] += add_term(glo, data["term"], src, priority=term_priority(data),
                                          last_seen=today, aliases=data.get("aliases") or [])
                touched.add("glossary")
            elif s["type"] == "fix_rule":
                wrong, right = data.get("wrong", "").strip(), data.get("right", "").strip()
                if not wrong or not right or wrong == right:
                    done["skipped"].append(f"{wrong} → {right}: 비어 있거나 같은 표기")
                    continue
                segs = "; ".join(f"{e['source']} {e['segments']}".strip() for e in s.get("evidence", []) if e.get("segments"))
                old = next((r for r in fix["rules"] if r["wrong"] == wrong), None)
                undo = {"wrong": wrong, "before": dict(old) if old else None}
                if old:  # 같은 틀린 표기가 이미 있으면 바른 표기를 새것으로 (출처는 합침)
                    old.update(right=right, source=", ".join(x for x in dict.fromkeys([old.get("source", ""), src]) if x),
                               evidence=segs or old.get("evidence", ""), added=today)
                else:
                    fix["rules"].append({"wrong": wrong, "right": right, "source": src, "evidence": segs, "added": today})
                done["fix"] += 1
                touched.add("fix")
            elif s["type"] == "progress":
                undo = {"before": json.loads(json.dumps(prog))}
                if prog.get("deck"):
                    prog.setdefault("history", []).append({"deck": prog["deck"], "page": prog.get("page"), "updated": prog.get("updated"),
                                                           "lecture": prog.get("lecture")})
                prog.update(deck=data.get("deck"), page=data.get("page"), updated=today, source=src, lecture=data.get("lecture"))
                done["progress"] = {"deck": prog["deck"], "page": prog["page"]}
                touched.add("progress")
            elif s["type"] == "notation":
                spoken, written = data.get("spoken", "").strip(), data.get("written", "").strip()
                if not spoken or not written:
                    done["skipped"].append(f"{spoken} → {written}: 비어 있음")
                    continue
                cur = subs.setdefault(name, _subject()).setdefault("notation", [])
                old = next((n for n in cur if n["spoken"] == spoken), None)
                undo = {"spoken": spoken, "before": old["written"] if old else None}
                if old:
                    old["written"] = written
                else:
                    cur.append({"spoken": spoken, "written": written})
                done["notation"] += 1
                touched.add("notation")
            s["status"], done["approved"] = "approved", done["approved"] + 1
            s["resolved"], s["resolved_by"] = now_iso(), by
            if data != s["data"]:
                s["approved_data"] = data  # 승인 화면에서 고친 값
            log.append({**entry, "action": "approve", "data": data, "undo": undo})
        if "glossary" in touched:
            save_json(d / "glossary.json", glo)
        if "fix" in touched:
            save_json(d / "fix.json", fix)
        if "progress" in touched:
            save_json(d / "progress.json", prog)
        if "notation" in touched:
            backup_file(SUBJECTS_PATH, "subjects")
            save_json(SUBJECTS_PATH, subs)
        save_json(d / "suggestions.json", sugg)
        if log:
            ch = load_json(d / "changes.json", {"items": []})
            ch["items"] += log
            save_json(d / "changes.json", ch)
        return done


def _evidence_text(s):
    return " / ".join(" ".join(x for x in (e.get("source"), e.get("pages") and f"p.{e['pages']}",
                                           e.get("segments") and f"#{e['segments']}", e.get("detail")) if x)
                      for e in s.get("evidence") or [])


def list_changes(name, limit=200):
    """변경 기록 (최근 것부터). data/<과목>/changes.json"""
    items = load_json(subject_dir(name) / "changes.json", {"items": []})["items"]
    return list(reversed(items))[:limit]


def undo_change(name, cid):
    """승인 되돌리기: 승인 직전 상태로 (용어집 항목·교정 규칙·진도 위치·표기 규칙), 제안은 "거절"로 바꿔 다시 자동 승인되지 않게.
    되돌릴 파일은 먼저 backups/에 백업. 그사이 같은 항목이 다시 바뀌었어도 승인 직전 상태로 돌린다."""
    d = subject_dir(name)
    with _lock:
        ch = load_json(d / "changes.json", {"items": []})
        e = next((x for x in ch["items"] if x["id"] == cid), None)
        if not e:
            raise KeyError(f"변경 기록이 없습니다: {cid}")
        if e.get("undone"):
            raise ValueError("이미 되돌린 변경입니다")
        if e.get("action") != "approve":
            raise ValueError("승인만 되돌릴 수 있습니다")
        u, t = e.get("undo") or {}, e["type"]
        if t == "term":
            path = d / "glossary.json"
            glo = load_json(path, {"terms": []})
            k = term_key(u["term"])
            idx = next((n for n, x in enumerate(glo["terms"]) if term_key(x["term"]) == k), None)
            if idx is None and u["before"] is None:
                raise ValueError(f"용어집에 '{u['term']}'이(가) 이미 없습니다")
            backup_file(path, f"{name}_glossary")
            if u["before"] is None:
                glo["terms"].pop(idx)
            elif idx is None:
                glo["terms"].append(u["before"])
            else:
                glo["terms"][idx] = u["before"]
            save_json(path, glo)
        elif t == "fix_rule":
            path = d / "fix.json"
            fix = load_json(path, {"rules": []})
            backup_file(path, f"{name}_fix")
            fix["rules"] = [r for r in fix["rules"] if r["wrong"] != u["wrong"]] + ([u["before"]] if u["before"] else [])
            save_json(path, fix)
        elif t == "progress":
            path = d / "progress.json"
            backup_file(path, f"{name}_progress")
            save_json(path, u["before"])
        elif t == "notation":
            subs = load_json(SUBJECTS_PATH, None) or DEFAULT_SUBJECTS
            cur = subs.setdefault(name, _subject()).setdefault("notation", [])
            backup_file(SUBJECTS_PATH, "subjects")
            cur[:] = [n for n in cur if n["spoken"] != u["spoken"]] + ([{"spoken": u["spoken"], "written": u["before"]}] if u["before"] else [])
            save_json(SUBJECTS_PATH, subs)
        sugg = load_json(d / "suggestions.json", {"items": []})
        for s in sugg["items"]:
            if s["id"] == e.get("suggestion"):
                s.update(status="rejected", undone=now_iso())
        save_json(d / "suggestions.json", sugg)
        e["undone"] = now_iso()
        save_json(d / "changes.json", ch)
        return e


def _sugg_key(s):
    d = s.get("data", {})
    if s["type"] == "term":
        return term_key(d.get("term", ""))
    if s["type"] == "fix_rule":
        return (d.get("wrong", ""), d.get("right", ""))
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


# ---------- 강의자료 ----------

def deck_path(name, filename):
    return subject_dir(name) / "decks" / f"{filename}.json"


def ref_path(name, filename):
    return subject_dir(name) / "refs" / f"{filename}.json"


def cache_dir(name, filename):
    return subject_dir(name) / "cache" / filename


def _ignored(p, base, ignore_dirs, ignore_files=()):
    return (any(part in ignore_dirs for part in p.relative_to(base).parts[:-1])
            or any(fnmatch.fnmatch(p.name, pat) for pat in ignore_files))


def list_materials(name):
    """등록 대상 파일 목록: [{file, path, kind(slide|handwritten|reference), state(new|changed|registered)}]."""
    s, sub = settings(), subject(name)
    slide_exts, ref_exts = set(s["slide_exts"]), set(s["reference_exts"])
    ignore, ignore_files = set(sub.get("ignore_dirs") or []), sub.get("ignore_files") or []
    found = []

    base = finals_materials_dir(name)
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if not p.is_file() or _ignored(p, base, ignore, ignore_files) or p.name.startswith("~$"):
                continue
            ext = p.suffix.lower()
            if ext in slide_exts:
                found.append((p, "handwritten" if sub.get("handwritten") else "slide"))
            elif ext in ref_exts:
                found.append((p, "reference"))
    for rd in sub.get("reference_dirs") or []:
        rbase = root() / name / rd
        if not rbase.is_dir():
            continue
        for p in sorted(rbase.rglob("*")):
            if p.is_file() and not _ignored(p, rbase, ignore, ignore_files) and p.suffix.lower() in slide_exts | ref_exts:
                found.append((p, "reference"))

    out = []
    for p, kind in found:
        meta = load_json(ref_path(name, p.name) if kind == "reference" else deck_path(name, p.name))
        st = p.stat()
        if not meta:
            state = "new"
        elif meta.get("source_size") != st.st_size or meta.get("source_mtime") != int(st.st_mtime):
            state = "changed"
        else:
            state = "registered"
        cached = load_json(cache_dir(name, p.name) / "pages.json") or {}
        fresh = (cached.get("source_size"), cached.get("source_mtime")) == (st.st_size, int(st.st_mtime))
        out.append({"file": p.name, "path": str(p), "kind": kind, "state": state,
                    "pages": (meta or {}).get("pages_total"),
                    "counts": cached.get("counts") if fresh else None})  # pdf_pages.py로 추출해 둔 예상치
    return out


def resolve_material(name, file):
    """파일명 또는 경로 → list_materials 항목. 목록에 없으면 None."""
    target = Path(file)
    for m in list_materials(name):
        if m["file"] == target.name and (not target.is_absolute() or Path(m["path"]) == target.resolve()):
            return m
    return None


def subject_status(name):
    d = subject_dir(name)
    mats = list_materials(name)
    sugg = load_json(d / "suggestions.json", {"items": []})
    return {
        "root_ok": (root() / name).is_dir(),
        "materials_dir": str(finals_materials_dir(name)),
        "materials": mats,
        "progress": load_json(d / "progress.json", {}),
        "glossary_count": len(load_json(d / "glossary.json", {"terms": []})["terms"]),
        "fix_count": len(load_json(d / "fix.json", {"rules": []})["rules"]),
        "pending_suggestions": sum(1 for s in sugg["items"] if s.get("status") == "pending"),
        "register_command": f"/register-slides {name}",
        "register_file_commands": {m["file"]: f'/register-slides {name} "{m["file"]}"' for m in mats},
    }


def midterm_excluded(path):
    return any(fnmatch.fnmatch(Path(path).name, pat) for pat in settings()["midterm_exclude_patterns"])
