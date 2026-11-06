# lecture_transcriber

대학 강의 녹음을 **Claude가 자료 기반 답변에 쓸 고품질 전사본(md)**으로 만드는 로컬 도구.

사용자가 하는 일은 **녹음 올리기 → `/correct` 입력 → 확인 목록의 몇 곳만 듣고 고치기** 세 가지다. 나머지는 프로그램과 Claude가 처리하고,
사람의 판단이 꼭 필요할 때만 묻는다 (제안 자동 승인, 확인 목록). 품질 목표: 강의 전체를 알아들을 수 있는 수준, 시험·강조 부분은 정확하게.

## 응답 언어
- **사용자에게는 항상 한국어로 답한다.** (작업 요약, 질문, 오류 설명, 커밋 후 안내 모두. 코드·명령·파일 이름은 그대로)

## 역할 분담
- **전사 프로그램 (무료, 로컬)**: Whisper 전사, 누락 감지·복원, 프롬프트 빌더, 용어집 저장, 제안 자동 승인·변경 기록, 확인 목록 화면, 최종 md 조립
- **Claude Code**: 슬라이드·필기 읽기, 문맥 교정, 문단·슬라이드 위치 판단, 중요 구간·확인 목록(모순 의심 + 후보) 판단, 용어집 제안(자동 승인 판단 auto/why 포함)
  → `.claude/commands/*.md` 프로젝트 명령으로 실행
- 사용량 절약: Claude Code는 교정된 전체 텍스트를 다시 쓰지 않고 "고칠 곳 목록(edits.json)"만 쓴다. 최종 md는 파이썬 스크립트가 조립한다.

## 개발 규칙
- **유료 API 금지**: Anthropic API, anthropic SDK, API 키, 유료 클라우드 서비스를 쓰지 않는다.
- **강의 폴더는 읽기 전용**: 기존 파일을 수정·삭제·이동하지 않는다. 새 파일은 `<과목>\Finals_Scope\전사본\`에만 쓴다.
  (코드에서는 `tools/common.py`의 `check_root_write()`로 강제)
- 기존 기능(업로드, 실시간 진행, 대기열, 취소, OOM 대응, 절전 방지, 127.0.0.1 전용)과 `outputs/`의 기존 결과를 망가뜨리지 않는다.
  기존 데이터를 옮기거나 바꿀 때는 먼저 `backups/`에 백업한다 (`common.backup_file()`, 파일명에 날짜·시각).
- 새 패키지는 설치 전에 사용자에게 목록을 보여 주고 `.venv`에 설치한다.
- 파이썬: `.venv/Scripts/python.exe` (Python 3.14). Windows 콘솔은 cp949이므로 스크립트는 `common.utf8_stdout()`을 호출한다.

## 폴더 구조

### 강의 폴더 (루트는 `data/settings.json`의 `root`, 기본 `~/Documents/Lectures`)
```
<과목>\Finals_Scope\강의자료\     기말 범위 슬라이드/손필기/참고 자료 (등록 대상)
<과목>\Finals_Scope\전사본\       최종 교정본 md (<원본파일명>.md, 예: 과목명_261028.m4a.md)
<과목>\Midterm_Scope\강의자료\    중간고사 자료 (*_필기.pdf는 원본과 겹치므로 제외)
<과목>\Midterm_Scope\전사본\      NotebookLM 전사본 (*.m4a.md, 타임스탬프 없음, 인식 오류 있음)
기말고사_녹음본\, 중간고사_녹음본\   녹음 원본. 파일명 <과목>_<YYMMDD>[_NN].m4a → 과목 자동 인식
```
과목은 사용자가 화면 "과목 관리 → ＋ 과목 추가"에서 직접 추가한다 (`common.create_subject()`, 템플릿 `SUBJECT_TEMPLATES`).

### 프로그램 폴더
```
server.py, transcriber.py, static/index.html   웹 서버·전사 엔진·화면 (메인 "전사" / "과목 관리"(#manage) 두 화면)
pipeline.py         전사 뒤처리: 프롬프트 빌더 옵션, 환각·누락 의심 구간 재전사(복원) (server가 씀)
tools/common.py     경로·JSON·과목 데이터 공용 함수 (server와 tools가 같이 씀)
tools/prompt.py     프롬프트 빌더: initial_prompt + hotwords를 Whisper 토크나이저로 재서 223토큰 안에 우선순위대로 (`python tools/prompt.py <과목>`으로 미리보기)
tools/restore.py    환각·누락 감지, 재전사 결과 검사·선택, 최종 세그먼트 조립 (GPU 없이 동작)
tools/init_data.py  data/ 초기화 (서버 시작 때도 자동)
tools/pdf_pages.py  강의자료 페이지 텍스트·그림 OCR·이미지 확인 판정·PNG 렌더링
tools/ocr.py        RapidOCR 래퍼 (무료·로컬 CPU, 모델은 .venv에 있어 오프라인 동작)
tools/check_deck.py 인덱스 JSON 검사·등록, 새 용어 제안 추가 (--resuggest: 등록된 인덱스로 제안만 다시 계산)
tools/korean.py     한국어 사전 확인 (kiwipiepy 형태소 분석기, oov가 없으면 실제 낱말. 못 쓰면 None → 안전하게 사람 확인)
tools/auto.py       제안 자동 승인 분류 (apply_edits·check_deck·bootstrap·직접 입력·승인 화면에서 자동 실행, `python tools/auto.py <과목>|--all [--dry-run]`)
tools/terms.py      인덱스 → 용어 제안 후보 (거르기·대표 표기와 별칭 묶기·중요도)
tools/work.py       교정 작업 폴더 만들기(transcript.txt·context.md, 다시 전사하면 직접 입력(H) 옮기기), 최종 md 조립, 문장 되돌리기,
                    직접 입력 → 교정 규칙 제안, 확인 목록(build_review·review_state·[완료]), 강의 목록의 다음 할 일(next_action)
                    (`python tools/work.py context <작업이름>`: context.md만 다시 만들기)
tools/apply_edits.py  edits.json 검증 → 적용 → 최종 md → 제안 병합 → 작업 상태 "완료" (edits.recorrect·edits.crosscheck는 덧붙여 적용)
tools/book.py       원서 md → 절 색인(캐시), 전사본 관련 절의 의사코드·정의 발췌(번호 없음), 원서 용어 후보 (`excerpt <작업이름>` / `terms <과목>`)
tools/review.py     승인 화면 검사: 교정 규칙이 기존 전사본에서 바꾸는 문장 수·예시, 경고(2글자 이하·조사·번역 규칙·대표 표기 아님),
                    한글 발음 ↔ 영어 소리 비교(포토신세시스 ≈ photosynthesis), 직접 입력에서 바뀐 낱말 → 교정 규칙 제안
tools/recorrect.py  /recorrect 대상 목록·준비 (context.md 다시 만들기 + prev_edits.md)
tools/crosscheck.py /crosscheck 대조표: 약한 구간 ↔ NotebookLM 전사본의 같은 부분 (위치 비율 + 내용어 겹침으로 찾음)
tools/bootstrap.py  /bootstrap-glossary: 출처 목록(--list), 후보 정리(--extract), 제안 올리기(--apply)
data/               과목 데이터 (아래)
jobs/<작업이름>/    교정 작업 폴더 (아래 "교정 흐름", git 제외)
backups/            덮어쓰기 전 백업 (git 제외). `backup_prune_enabled`면 파일별 최근 `backup_keep_per_file`개만 남김 (기본 꺼짐)
uploads/            전사 준비 목록 `prep.json` + 올린 파일 `prep_<id>.<확장자>` + 대기열 녹음 복사본 `<전사 id>.<확장자>` (git 제외, 서버를 껐다 켜도 남음)
outputs/            기존 전사 결과 txt·srt, .jobs/ 작업 기록 (git 제외, 세그먼트 jsonl에 avg_logprob·no_speech_prob·compression_ratio 포함)
                    .jobs/<id>.jsonl = 복원본, <id>.raw.jsonl = Whisper 원본, <id>.restore.json = 복원 요약, <id>.audio.<확장자> = 보관한 녹음
```

## 데이터 형식 (`data/`)
- `settings.json`: `root`, 폴더 이름들, `slide_exts`(.pdf .pptx), `reference_exts`(.ipynb .docx), `midterm_exclude_patterns`, PNG 해상도,
  OCR 기준값 `ocr_enabled`, `ocr_min_region`, `figure_min_cover`, `ocr_min_chars`, `ocr_min_density`, `ocr_min_score`,
  교정 작업 `uncertain_logprob`(-1.0), `uncertain_compression`(2.4) → 불확실(U) 표시, `context_glossary_max`(80), `context_max_decks`(3),
  원서 참고 `book_max_sections`(2), `book_excerpt_chars`(4000, 강의자료 미등록), `book_excerpt_chars_with_slides`(1500, 보조),
  `midterm_sample_count`(2), Whisper `beam_size`(5, 과목 관리 → 전체 설정), 나뉜 녹음 `prev_part_lines`(8: 뒷부분 context.md에 넣을 앞부분 마지막 줄 수), 정리 `backup_prune_enabled`(false), `backup_keep_per_file`(10), `user_note_patterns`(사용자 정리 노트: *_필기.pdf, *개념정리노트*, *_학습진행요약.md, *_인수인계_*, *_유형정리*, *_복기* → 출처 제외),
  프롬프트 빌더 `prompt_token_limit`(223), `prompt_min_priority`(1), `prompt_progress_pages`(10),
  환각·누락 감지 `repeat_min_times`, `english_min_words`, `hallucination_compression`, `hallucination_phrases`,
  `numbers_min_count`, `numbers_min_ratio`, `counting_min_run`, `same_token_min_count`, `same_token_ratio`, `gap_min_seconds`, `sparse_min_seconds`, `sparse_max_cps`,
  `voice_margin_db`, `voiced_min_ratio`, `silent_max_ratio`, `region_join_seconds`, 재전사 `restore_enabled`, `restore_pad_seconds`, `restore_no_speech_threshold`, `restore_audio_filter`
- `subjects.json`: 과목별 규칙 (화면 "과목 규칙 편집"에서 수정)
  - `summary`, `rules`(교정 규칙 문장 목록), `language`(Whisper 언어)
  - `has_slides`, `handwritten`(모든 페이지를 이미지로 읽음), `keep_code_identifiers`
  - `books`: `[{"path": "원서/…pdf.md", "note": "…"}]` (과목 폴더 기준, `*` 가능)
  - `reference_dirs`, `ignore_dirs`, `ignore_files`(패턴)
  - `notation`: `[{"spoken": "x 제곱", "written": "x²"}]`
  - `professor_phrases`: 교수님 고정 표현 (Whisper 프롬프트용)
  - `term_stopwords`: 용어 제안에서 뺄 일반 단어 (개념, 내용, true …). 화면에서 편집
  - `manual_prompt`(선택): `{"enabled", "prompt", "hotwords"}` 켜면 프롬프트 빌더 대신 이 값 (과목 관리 → 고급, `pipeline.builder_options`)
  - `prefer_english_terms`: 용어 대표 표기를 영어로 (교정 때는 교수님이 영어로 말한 용어에만 적용, 한국어로 말한 용어는 번역하지 않음). 아니면 슬라이드 표기(한국어), 코드 식별자 유지 과목은 코드 키워드·코드 표기
- `<과목>/glossary.json`: `{"terms": [{"term", "aliases": [...], "sources": [...], "freq", "last_seen", "priority": 1~5, "note", "seen_in"?}]}`
  (`seen_in`: 교정본에 이 용어가 나온 작업 이름, `freq` = 그 수, `last_seen` = 최근 녹음 날짜. apply_edits.py가 갱신)
  (우선순위 1 낮음 ~ 5 높음, 기본 3.)
  `aliases`: 같은 용어의 다른 표기 (한영 짝 photosynthesis, 식물 광합성 …). 교정 때 대표 표기 `term`으로 맞춘다.
- `<과목>/fix.json`: `{"rules": [{"wrong", "right", "source", "evidence", "added"}]}`
- `<과목>/changes.json`: 변경 기록 `{"items": [{"id", "at", "by": "auto|user", "action": "approve|reject", "suggestion", "type", "origin", "data", "reason", "evidence", "undo"?, "undone"}]}`
  (`undo` = 승인 직전 상태. `common.undo_change`가 되돌리고 제안을 "거절"로 → 다시 자동 승인되지 않음)
- `<과목>/progress.json`: `{"deck", "page", "updated", "history": []}`
- `<과목>/suggestions.json`: `{"items": [{"id", "type": "term|fix_rule|progress|notation", "data": {...}, "evidence": [...], "origin", "status": "pending|approved|rejected", "created", "resolved"?,
  "resolved_by"?: "user|auto", "claude"?: {"auto", "why", "count"?}, "auto"?: {"auto", "reasons", "checked"}, "undone"?}]}`
  (`claude` = Claude가 제안에 적은 자동 승인 판단, `auto` = tools/auto.py 분류 결과와 이유. 진도 위치 data에는 `lecture`(YYMMDD_NN)도)
  용어 제안 `data`: `{"term", "aliases", "from": "slide|figure|reference", "deck", "pages", "score", "in_title", "recommended"}`
  (score = 나온 페이지 수 + 페이지 제목에 나오면 3. recommended = 2쪽 이상 또는 제목 → 승인 화면에서 기본 체크)
  origin: `register-slides`(강의자료 등록), `correct`(교정 중), `manual`(결과 화면 직접 입력에서 바뀐 낱말), `bootstrap`(/bootstrap-glossary)
  **자동 승인** (`tools/auto.py`, 제안이 생길 때마다): 교수님 자료(register-slides·bootstrap)의 recommended 용어, 강의 용어는 강의자료에도 있거나 근거 2회 이상 /
  교정 규칙은 경고 없음 + 바른 쪽이 용어집 대표 표기 + 틀린 쪽이 실제 낱말이 아님(용어집의 다른 용어·강의자료 인덱스·중간고사 NotebookLM·교정 작업에서 Claude가 고친 문장과 직접 입력에 없음,
  한국어 → 한국어 규칙은 틀린 쪽이 형태소 분석기(kiwipiepy) 사전에 있으면 Claude가 auto: true여도 사람 확인, 분석기를 쓸 수 없으면 모두 사람 확인) + 근거 2회 이상 / 진도 위치(가장 최근 강의 것만, 더 오래된 강의 것은 자동 거절).
  표기 규칙·Claude가 auto: false로 적은 것·예전에 거절하거나 되돌린 것은 사람 확인.
  승인 화면(과목 관리 "확인이 필요한 제안", 사람 확인으로 남은 것만 + 이유): 종류별 그룹(용어는 덱별)마다 "체크한 것 승인 / 체크 안 한 것 거절". 승인하면
  용어 → glossary.json, 교정 규칙 → fix.json, 진도 위치 → progress.json(이전 위치는 history), 표기 규칙 → subjects.json notation(백업 후).
  교정 규칙의 바른 표기·표기 규칙의 표기는 화면에서 고쳐서 승인할 수 있다 (`approved_data`에 기록).
  교정 규칙은 기존 전사본(교정 작업 Whisper 전사·교정본 md·중간고사 NotebookLM)에서 바뀌는 문장 수와 예시 1~2개를 보여 주고,
  경고(2글자 이하, 조사가 붙음, 한국어로 말한 용어를 영어로 바꿈, 바른 표기가 별칭)가 있으면 기본 체크 해제. 교정 사전 표에도 같은 경고.
  용어 승인 시 용어집 우선순위: 제목 + 3쪽 이상 → 4, 기본 체크 → 3, 나머지 → 2.
- `<과목>/cache/samples/<녹음 파일명>.json`: 중간고사 샘플 Whisper 전사 (화면 "중간고사 자료로 용어집 시작하기"에서 녹음을 골라 실행)
- `<과목>/cache/bootstrap/`: /bootstrap-glossary 정리 파일(materials.md, spoken.md, book.md, whisper.md, text/), Claude가 쓰는 proposal.json
- `<과목>/cache/book/<원서>.json`: 원서 절 색인 캐시
- `<과목>/decks/<파일명>.json`: 슬라이드·손필기 페이지 인덱스 (슬라이드 위치 표시 대상)
  `{"file", "title", "kind": "slide|handwritten", "pages_total", "source", "source_size", "source_mtime", "registered",
    "text_sources": {"pdf", "ocr", "image"},
    "pages": [{"page", "title", "summary", "terms", "figure_terms", "code_names"?, "unsure_terms"?, "text_source": "pdf|ocr|image"}]}`
  (`code_names`, `unsure_terms`는 용어 제안에 넣지 않음. `text_source`는 check_deck.py가 자동 기록)
- `<과목>/refs/<파일명>.json`: 참고 자료 용어 (위치 표시 안 함) `{"file", "summary", "terms", "kind": "reference", ...}`
- `<과목>/cache/<파일명>/`: `pdf_pages.py` 출력 (`pages.md`, `text.md`, `pages.json`, `pNNN.png`). git 제외, 지워도 다시 만들 수 있음.

**출처 제한**: 용어집·교정 사전·슬라이드 위치 표시의 출처는 **교수님 자료(슬라이드, 과제, 퀴즈, 실습 노트북), 원서, 강의 전사본**으로만 한정한다.
사용자가 직접 만든 정리 노트·요약·필기(`*_필기.pdf`, 개념정리노트, `*_학습진행요약.md` 등)는 쓰지 않는다.
예외: 손필기 과목(`handwritten`)의 필기 PDF는 판서를 옮긴 것이라 사용한다.

강의자료 종류:
- **슬라이드**: `Finals_Scope\강의자료`의 PDF·PPTX (손필기 과목이면 **손필기**)
- **참고 자료**: `Finals_Scope\강의자료`의 ipynb·docx와 `reference_dirs`의 파일. 텍스트만 추출해 용어 추출에만 쓴다.

## 과목 규칙 요약 (정확한 내용은 `data/subjects.json`)
- 과목별 규칙은 추가할 때 고른 템플릿(한국어 강의, 영어 용어를 많이 쓰는 강의, 코드 실습 강의, 판서·손필기 강의)에서 시작하고 화면에서 수정한다.
- `prefer_english_terms` 과목: 교수님이 영어로 말했는데 한글 발음으로 적힌 용어만 영어 표기 (포토신세시스 → photosynthesis). 한국어로 말한 용어는 번역하지 않음. 수식을 읽은 것은 수식 표기 (x 제곱 → x²).
- 한국어 과목: 슬라이드 표기를 따르고, 교수님이 따로 말한 영어 용어는 용어집 제안에 추가.
- `keep_code_identifiers` 과목: 코드 식별자를 코드 표기 그대로 유지.
- `handwritten` 과목: 손필기 PDF는 텍스트 레이어가 깨져 있어 반드시 페이지 이미지로 읽음. 말로 하는 수식은 수식 표기로.

## Claude Code 명령 (`.claude/commands/`)
명령 이름은 영어, 명령 파일 안의 설명과 지시는 한국어.

모든 명령 공통 규칙:
- 채팅창에 전사 내용을 길게 출력하지 않는다. 결과는 파일로만 쓰고 마지막에 2~3줄 요약만.
- 교정할 때 원본 PDF를 다시 열지 않고 `decks/*.json` 인덱스만 쓴다 (슬라이드 등록 명령 제외).
- 들리지 않은 부분을 지어내지 않는다. 추정은 별도 필드에만.
- **교정은 작업마다 새 대화에서 실행하는 것을 권장** (Claude Code `/clear` 또는 새 채팅). 긴 대화가 쌓이면 Pro 사용량이 늘어난다.
  `/correct`는 한 번에 하나씩이 원칙이고 일괄 교정 명령은 만들지 않는다. 메인 화면 "강의" 목록(할 일이 있는 것 위, /correct 대기는 오래된 순)에서 명령을 하나씩 복사한다.

| 명령 | 상태 | 하는 일 |
|---|---|---|
| `/register-slides <과목> <파일명>` | 사용 가능 | 파일 하나를 `pdf_pages.py`로 추출 → 예상치 보여 주고 확인 → 페이지 인덱스(decks)·참고 자료 용어(refs) 작성 → `check_deck.py`로 검사·등록, 새 용어 제안 (figure_terms·일반 단어 제외, 별칭 묶기, 중요도) |
| `/correct <작업이름>` | 사용 가능 | `context.md`·`transcript.txt`를 읽고 고칠 곳·중요 구간(`important`)·확인 목록(`review`)·제안(auto/why)만 `edits.json`(길면 `edits.partNN.json`)에 씀 → `tools/apply_edits.py`로 검증·조립·자동 승인 |
| `/recorrect <과목>` | 사용 가능 | 강의자료 등록 후 "강의자료 미등록"으로 교정한 전사본에 문단·슬라이드 위치를 다시 매김 → `edits.recorrect.json` (기존 교정 유지) |
| `/bootstrap-glossary <과목>` | 사용 가능 | 출처 목록 확인 → `bootstrap.py --extract` 정리 파일만 읽고 `proposal.json` → `--apply`로 제안 (교수님 자료·원서·NotebookLM만, 표기 규칙 준수) |
| `/crosscheck <작업이름> <NotebookLM md>` | 사용 가능 | `crosscheck.py` 대조표(약한 구간만)를 읽고 `edits.crosscheck.json` (NotebookLM은 두 번째 후보) |

### 전사 → 복원 (Phase 3, 프로그램)
1. **프롬프트 빌더** (화면 "Whisper 프롬프트: 과목 프롬프트 빌더"가 기본): 파일명으로 과목을 알면 `tools/prompt.py`로
   initial_prompt(`<과목> 강의입니다.` + ③ 교수님 고정 표현)와 hotwords(① 진도 다음 페이지 용어 → ② 용어집: 우선순위·최근 등장·빈도)를
   Whisper 토크나이저로 재서 합계 223토큰 안에서만 채운다. 교정 사전(fix.json)을 Whisper 출력에 바로 적용. 과목을 모르면 Whisper 프롬프트 없이.
   과목 관리 "Whisper 프롬프트" 미리보기, 작업 화면 "Whisper 프롬프트"에 실제로 들어간 값과 잘린 항목이 보인다.
2. Whisper 전사 (기존 옵션, VAD 켬) → 상태 "복원 중"
3. **감지** (`tools/restore.py`): 환각 의심 = 같은 단어·구절 반복, 앞뒤 문장과 같은 문장, 프롬프트·핫워드 문구 그대로,
   language=ko인데 영어로만 된 출력(번역 의심), compression_ratio 과다, 알려진 환각 문구,
   숫자 나열(숫자 토큰 12개 이상·80% 이상), 숫자 세기(1씩 늘어나는 수 10개 이상 연속), 같은 형태 토큰 과다(16토큰 이상에서 한 토큰이 50% 이상, 숫자는 #).
   누락 의심 = 소리가 있는 4초 이상 간격, 소리가 있는데 글자가 너무 적은 긴 세그먼트(초당 1.2자 미만, 8초 이상).
   가까운 의심 구간은 하나로 합친다.
4. **재전사** (`pipeline.py`): 구간을 앞뒤 1.5초 여유로 잘라 ffmpeg 전처리 → vad 끔, no_speech_threshold 0.8로
   ① initial_prompt·hotwords 없이 1회, ② 이번 강의 프롬프트(+ 바로 앞 문장)로 1회 → 같은 환각 검사를 통과한 것 중 한국어·logprob 높은 쪽.
   결과: 복원(R) / 원래 문장 유지 / 환각만 제거(소리가 거의 없음) / 청취 불가(M, 둘 다 실패).
   영어로만 나온 것만 의심인 구간은 재전사로도 한국어가 안 나오면 실제 영어 발화(슬라이드 낭독)로 보고 원래 문장 유지.
   복원만 다시: 서버를 끄고 `.venv/Scripts/python.exe pipeline.py restore <전사 작업 id>` (원본·보관 녹음으로 다시 복원, 교정 작업 폴더도 새로).
5. 결과 화면: 녹음 재생(▶ 그 문장부터), 자동 감지 구간 목록(시도별 결과), 불확실·복원·청취 불가 목록, 문장별 ✎ 직접 입력(`manual.json`, md에 가장 우선).

### 교정 흐름 (Phase 2)
1. 전사가 끝나면 파일명(`<과목>_<YYMMDD>[_NN]`)으로 과목을 인식해 `jobs/<작업이름>/`을 만들고 상태 "교정 대기".
   과목을 모르거나 이전 전사면 화면의 "교정 작업 만들기"에서 과목을 골라 만든다.
   - `job.json`: 과목, 원본 파일명, 녹음 경로(`audio`), 길이, 전사 시각(`transcribed`), 사용한 Whisper 프롬프트·옵션, 상태(`awaiting` → `done`), 후보 강의자료, 복원 통계(`restore`), 처리 통계
   - **대체됨**(`superseded`, `superseded_by`): 같은 과목·같은 녹음 파일의 작업이 여럿이면 전사가 가장 늦은 것만 살아 있고 나머지는 대체됨
     (작업을 만들 때·서버 시작 때 자동, 수동은 `python tools/work.py versions`). 폴더는 지우지 않는다.
     대체된 작업은 읽기 전용: md를 쓰지 않고(되돌리기·직접 입력·apply_edits 거부), /recorrect 대상·용어집 빈도(seen_in)·교정 규칙 영향 계산에서 빠진다.
   - `whisper_raw.jsonl`: Whisper 원본 (영구 보관, 수정 안 함) · `segments.jsonl`: 복원을 거친 세그먼트 (`restored`, `missing`) · `restore.json`: 의심 구간과 시도 결과
   - `transcript.txt`: `번호|MM:SS|텍스트|표시` (표시 U 불확실 / R 복원 / M 청취 불가 `[누락 MM:SS–MM:SS]` / H 직접 입력)
   - `context.md`: 과목 규칙, 용어집 상위(대표 표기 + 별칭), 교정 사전, 진도 위치, 후보 강의자료 페이지 인덱스
     (진도 위치 덱과 다음 덱 + 앞부분 녹음의 마지막 위치 덱 + 전사본과 용어가 많이 겹치는 덱, 최대 `context_max_decks`개). 강의자료가 없으면 "강의자료 미등록".
     나뉜 녹음 `_02`, `_03` …은 "앞부분 녹음의 마지막 부분" 섹션: 바로 앞 부분 작업(대체되지 않은 것)이 교정 완료면 교정본 마지막 문단(제목·위치 포함),
     아니면 Whisper 끝부분 `prev_part_lines`줄. 앞부분을 apply_edits로 교정하면 교정 대기인 뒷부분의 context.md를 자동으로 다시 만든다 (`work.refresh_next_parts`).
   - 원서 참고 (원서가 있는 과목): 전사본 핵심 용어(용어집·교정 사전·영어 낱말 + 한글 발음으로 적힌 용어)로
     원서 절을 골라 의사코드·정의만 짧게. 강의자료 미등록이면 대신(4000자), 등록돼 있으면 보조(1500자). 절·그림·식 번호는 넣지 않는다 (수업과 판이 다를 수 있음).
2. 강의 목록의 "/correct 복사" → Claude Code에서 `/correct <작업이름>` → `edits.json` → `apply_edits.py`
   - **중요 구간** `important: [{"from", "to", "kind": "exam|quiz|assignment|emphasis", "summary", "why"}]` (시험·퀴즈·과제 언급, "중요/꼭/시험에 나온다" 강조)
   - **확인 목록** `review: [{"i", "kind": "contradiction|recognition|uncertain|inaudible", "candidates", "why"}]`: 모순 의심(후보 2개 이상, 추측해서 고치지 않음)은 강의 어디서나,
     나머지는 중요 구간 안만 (밖이면 `result.review_memo` → 처리 메모에만). 중요 구간 안의 M 줄·교정 중 inaudible·교정하지 않은 U는 프로그램이 자동 추가 (`work.build_review`).
   - 확인 목록이 비면 강의는 바로 "완료", 있으면 "확인 필요 n곳" → 결과 화면 확인 목록(▶, 후보 버튼, ✎ 수정 = 직접 입력, 그대로 둠) → [완료].
     상태는 `review.json` `{"items": {"r<번호>": {"status": "kept"}}, "completed"}` (직접 입력이 있으면 fixed), 요약은 `job.json` `review` `{total, open, completed}`.
   - 검사: 세그먼트 번호, 문단 누락·겹침, 등록된 강의자료·쪽 범위, 시각 형식. `"text": ""` + reason은 환각 문장 삭제.
   - `result.json`(원문·교정문·문단·되돌린 문장) 저장 → 최종 md를 `<과목>\Finals_Scope\전사본\<원본파일명>.md`로 저장
     (이 작업이 쓰지 않은 같은 이름 파일이 있으면 멈춤 → 사용자 확인 후 `--overwrite`, 기존 파일은 backups/에 백업)
   - 교정 중 제안(terms, fix_rules, progress, notation)은 `suggestions.json`에 origin `correct`로 추가 (다시 실행하면 이 작업 제안은 새로 계산) → 자동 승인 분류
3. 서버가 `jobs/`를 2초마다 확인해 edits.json 생성·적용 완료를 화면에 바로 반영.
   결과 화면: 확인 목록, 교정본/원본 토글(중요 구간 ★), 바뀐 단어 강조, "바뀐 문장만", 문장별 ↺ 되돌리기(md 즉시 다시 저장), 슬라이드 위치 클릭 → 페이지 미리보기,
   불확실·복원·청취 불가 목록, 오디오 재생, 직접 입력, 이해도·NotebookLM 대조 안내(NotebookLM md 올리기 → `jobs/<작업이름>/notebooklm.md`, /crosscheck 명령 복사).
   직접 입력에서 짧게 바뀐 낱말은 교정 규칙 제안(origin `manual`)으로 자동 추가.

### 제안 → 승인 · 용어집 시작 · 다시 교정 · 대조 (Phase 4)
- 덧붙이는 edits 파일 (apply_edits.py, 쓴 순서대로 기존 edits 위에):
  `edits.recorrect*.json` — paragraphs가 있으면 문단·슬라이드 위치를 바꿈, edits는 같은 번호 기존 교정을 덮어씀(원문과 같은 text면 취소)
  `edits.crosscheck*.json` — edits만 덮어씀(paragraphs 무시), comprehension이 있으면 바꿈
- 이해도 `comprehension.weak_ranges[].kind`: `recognition`(인식 실패: 환각 흔적·번역 출력·엉뚱한 단어·청취 불가) / `board`(판서 설명: 지시어로 설명해 말 자체가 조각남).
  `need_notebooklm`은 중요한 설명에 recognition 구간이 있을 때만 true. board 구간은 처리 메모에 "판서 설명(말 자체가 조각남)"으로 표시만 하고 /crosscheck 대조표에서 뺀다.
- /recorrect 대상 = 교정 완료 + 교정할 때 강의자료 미등록(`job.json` `materials_registered` false) + 지금은 등록됨. 과목 관리에 목록과 명령 표시.
- 중간고사 샘플 전사: 과목 관리에서 녹음을 직접 고름(기본 `midterm_sample_count`=2개, NotebookLM 전사본이 있는 녹음 중 고르게).
  원본은 uploads/로 복사해 Whisper만 (복원·교정 작업 없음) → `data/<과목>/cache/samples/`.
- /crosscheck 위치 찾기: 녹음 길이 대비 비율 ±15% 안에서 내용어(흔한 낱말 제외)가 가장 많이 겹치는 문장 묶음.
  일치도 0.22 미만이면 "찾지 못함"(다른 날 강의는 0.05~0.20, 같은 강의는 0.28 이상), 0.35 미만이면 "낮은 편" 표시.

최종 md: 제목·과목·녹음 길이·처리일·참고 자료·`청취 불가 n곳 (n초) · 복원 n곳` → `## 문단 제목 (덱 p.3–5)` → `**[MM:SS]** (덱 p.N) 문장` → `## 처리 메모`
(중요 구간(시각·종류·한 줄 요약), 확인 목록 결과, 중요 구간 밖 확인 의심, 강의자료 미등록, 자동 감지·복원 구간, 청취 불가와 추정, 불확실, 직접 입력, 이해도·약한 구간(번호와 시각), 지운 환각 문장, 교정·되돌림 수).
손필기는 `(필기: 파일명 p.N)`, 신뢰도 0.5 미만 위치는 `?`. 확인하지 않은 모순 의심 문장 끝에는 `〔확인 필요: A/B〕`.
청취 불가 = M 줄(직접 입력한 것 제외, 처리 메모에 "자동 감지") + edits의 `inaudible`("교정 중 표시")을 합친 것. 처리 메모·머리말에 둘을 나눠 적는다. `inaudible` 구간 안에 가운데가 드는 문장은 md에 넣지 않는다 (apply_edits.py가 경고).

### 일괄 처리 · 이어서 처리 · 교정 대기 · 정리 (Phase 5)
- **전사 준비 목록** (`uploads/prep.json`, 서버 `/api/prep*`): 올린 파일과 "기말고사 녹음본에서 고르기"가 모두 여기로 들어온다 (확인 화면 하나).
  파일마다 용량, 녹음 길이(ffprobe), 과목(파일명으로 인식, 드롭다운으로 변경, 모르면 "과목 선택 필요" → 체크 불가, "과목 없이"는 Whisper 프롬프트·교정 작업 없음, **용어집이 빈 과목**은 "/bootstrap-glossary 먼저" → [그래도 전사](`allow_empty_glossary`) 전에는 체크 불가·서버도 거부),
  이미 처리한 녹음 경고(대체되지 않은 교정 작업 또는 끝난 전사가 있음 → 다시 전사하면 이전 작업은 대체됨), ✕(올린 파일은 uploads/에서 삭제, 기말고사 원본은 그대로).
  "전사하기" → 체크한 것만 대기열 (예상 시간 = 녹음 길이 합 × 지난 전사의 걸린 시간/녹음 길이 중앙값). 고른 과목은 `options.subject`로 프롬프트 빌더·교정 작업에 쓴다.
  과목이 있으면 항상 과목 프롬프트 빌더, 기말고사 원본은 전사할 때 uploads/로 복사(원본은 읽기만).
- **다시 전사**: 새 작업을 만들 때 이전 작업들(최근 것부터)의 직접 입력(H)을 시각 겹침(짧은 쪽의 50% 이상, 또는 시작 1초 안)으로 새 세그먼트에 옮김 (`work.carry_manual`).
  못 찾은 것은 `job.json` `carried_manual.unmatched` → 결과 화면·강의 목록에 표시.
- 기말고사_녹음본 목록(`/api/finals`): 과목별, 상태 new / prep(준비 목록) / active(대기열) / done(처리됨). 처리 안 한 것만 기본 체크.
- **나뉜 녹음** `_01`, `_02`: 같은 날 강의의 앞뒤 부분. 별도 작업·별도 md. 화면 기록·교정 대기에서 같은 강의로 묶어 표시(`common.lecture_key`).
- **이어서 처리**: 대기열·진행 중 작업은 `upload`(uploads/의 녹음)를 기록해 둔다. 서버가 꺼졌다 켜지면 녹음이 남은 작업은 `resume_pending` →
  화면 위 "이어서 처리할까요?" (이어서 처리 = 처음부터 다시 대기열, 처리하지 않음 = 녹음 복사본 삭제).
- **강의** 목록(`/api/lectures`): 교정 작업(대체 안 된 것)과 교정 작업 없는 전사마다 한 줄, 다음 할 일 하나 (`next`: active 전사 중 / review 확인 필요 n곳 / correct /correct 복사(오래된 순) / complete 완료(최근 순)).
  나뉜 녹음은 앞부분이 아직 교정 대기면 안내. 전사 기록을 지운 작업도 작업 폴더만으로 결과·확인 화면을 연다.
- **저장 공간 정리**(과목 관리, `/api/cleanup`): uploads/ 잔여물(준비 목록·대기열·이어서 처리 녹음 제외), 등록을 마친 강의자료의 PNG·그림 캐시, (켜져 있으면) 오래된 백업만 지운다.
  `whisper_raw.jsonl`, `segments.jsonl`, edits, result.json, md, 보관한 녹음, `jobs/`는 절대 지우지 않는다 (서버가 이름·경로로 한 번 더 막음).

### /register-slides 흐름 (파일 하나씩이 기본)
1. `.venv/Scripts/python.exe tools/pdf_pages.py --list <과목>` → 등록할 자료 목록
2. `.venv/Scripts/python.exe tools/pdf_pages.py "<파일명>" --subject <과목>` → 로컬에서 추출·OCR, `예상: 텍스트 n쪽 / OCR n쪽 / 이미지 확인 n쪽` 출력
3. 예상치를 사용자에게 보여 주고 **진행 여부를 물은 뒤 멈춤**
4. 승인되면 Claude가 `pages.md`와 이미지 확인 페이지 PNG만 읽고 `decks/<파일명>.json`(또는 `refs/<파일명>.json`) 작성
5. `.venv/Scripts/python.exe tools/check_deck.py <과목> "<파일명>"` → 검사 통과 시 등록 + 페이지별 `text_source` 기록 + 새 용어를 `suggestions.json`에 추가

페이지 텍스트 출처 판정 (`pdf_pages.py`):
- **pdf**: PDF 텍스트만으로 충분
- **ocr**: 페이지 면적의 2% 이상인 그림을 RapidOCR로 읽어 PDF 텍스트에 더함. OCR 텍스트는 잡음이 있으므로, 명백한 오류는 문맥으로 바로잡고 확인되지 않는 단어는 용어로 쓰지 않는다 (`unsure_terms`).
- **image**: 그림이 12% 이상 덮는데 OCR 결과가 빈약한 페이지(80자 미만, 밀도 300 미만, 신뢰도 0.80 미만), 벡터 도식이 많은 페이지, 손필기 → PNG를 Claude가 직접 봄
- 기준값은 `data/settings.json`에서 조정. OCR 결과는 `cache/<파일명>/ocr.json`에 캐시되어 기준을 바꿔 다시 실행해도 빠르다.
- PPTX는 슬라이드 전체를 그림으로 만들 수 없어서 슬라이드 속 그림 파일을 OCR하고, 이미지 확인이면 그림 파일(`pNNN_k.*`)을 꺼낸다.
