---
description: 강의자료를 등록한 뒤 "강의자료 미등록"으로 교정했던 전사본에 문단·슬라이드 위치를 다시 매김 (기존 교정은 유지, 덧붙이는 edits만 씀)
argument-hint: <과목>   (예: 과목명)
allowed-tools: Read, Write, Edit, Glob, Bash(.venv/Scripts/python.exe tools/recorrect.py *), Bash(.venv/Scripts/python.exe tools/apply_edits.py *)
---

# 다시 교정: $ARGUMENTS

Whisper는 다시 돌리지 않는다. 저장된 세그먼트와 기존 `edits*.json`(문장 교정)은 그대로 두고,
새로 등록된 강의자료 인덱스로 **문단·슬라이드 위치**와 **슬라이드를 보고 새로 알게 된 교정만** `edits.recorrect.json`에 쓴다.
`apply_edits.py`가 기존 edits 위에 덧붙여 md를 다시 만든다.

## 공통 규칙 (반드시 지킬 것)
- 채팅창에 전사 내용을 길게 출력하지 말 것. 결과는 파일로만 쓰고, 마지막에 2~3줄 요약만 말한다.
- **교정된 전체 텍스트를 다시 쓰지 않는다.** 기존 교정은 이미 적용된다. 새로 고칠 문장만 쓴다.
- 원본 PDF·PPTX·PNG를 열지 않는다. 강의자료 정보는 `context.md`와, 필요할 때만 `data/<과목>/decks/<파일명>.json`을 읽는다.
- 들리지 않은 부분을 지어내지 말 것.
- 쓰는 파일은 `jobs/<작업이름>/edits.recorrect.json`(길면 `edits.recorrect.part01.json` …)뿐이다.
  기존 `edits.json`·`edits.part*.json`, `segments.jsonl`, `context.md`, `transcript.txt`, `data/`, 강의 폴더는 수정하지 않는다.
- 과목 규칙(`/correct`의 4번)을 그대로 따른다. `prefer_english_terms` 과목은 교수님이 영어로 말했는데 한글 발음으로 적힌 용어만 영어로,
  한국어로 말한 용어는 한국어 맞춤법만 고친다.
- 파이썬은 `.venv/Scripts/python.exe`로 실행한다 (프로젝트 루트 기준).

## 순서

1. 대상 목록:
   ```
   .venv/Scripts/python.exe tools/recorrect.py <과목>
   ```
   - 등록된 강의자료가 없거나 대상이 없으면 그 사실을 말하고 멈춘다.
   - 대상이 여러 개면 목록을 보여 주고 **어느 작업부터 할지(또는 전부) 묻고 멈춘다.** 마음대로 전부 처리하지 않는다.

2. 작업 하나씩:
   ```
   .venv/Scripts/python.exe tools/recorrect.py <과목> <작업이름>
   ```
   - `context.md`를 새 강의자료 인덱스로 다시 만들고, `prev_edits.md`(기존 문단 제목·교정 요약)를 쓴다. 기존 edits는 backups/에 백업된다.
   - 이미 `edits.recorrect*.json`이 있으면 다시 할지 묻고 멈춘다.

3. `context.md`(후보 강의자료 페이지 인덱스), `prev_edits.md`, `transcript.txt`를 읽는다 (250줄이 넘으면 200줄씩 나눠 읽기).

4. `edits.recorrect.json` 작성 (`/correct`의 edits.json 형식과 같음):
   ```json
   {"paragraphs": [{"from": 0, "to": 12, "heading": "…", "slide": {"file": "Lecture07.pdf", "pages": "1-2", "confidence": 0.8, "at": {"5": 2}}}],
    "edits": [{"i": 152, "text": "고친 문장 전체", "reason": "슬라이드 표기: 파티션→PARTITION"}],
    "suggestions": {"progress": {"deck": "Lecture07.pdf", "page_to": 18}, "terms": [], "fix_rules": []}}
   ```
   - `paragraphs`: **모든 세그먼트를 빠짐없이·겹침 없이** 다시 나눈다 (기존 문단을 참고해도 되고 슬라이드에 맞게 바꿔도 된다). 이것이 기존 문단을 대신한다.
     `slide`는 확실할 때만, 아니면 `null`.
   - `edits`: 슬라이드를 보고 **새로** 알게 된 교정만 (용어 표기, 수식 기호). 기존 교정을 바꿀 때는 같은 번호로 고친 문장 전체를 쓰고,
     기존 교정을 취소하려면 원문 그대로를 `text`로 쓴다. 없으면 `[]`.
   - `inaudible`은 쓰지 않는다 (기존 것 유지). `comprehension`은 바뀌었을 때만. `important`·`review`도 쓰지 않는다 (기존 확인 목록 유지).
   - `suggestions.progress`: 이 강의가 끝난 강의자료 위치.

5. 검증·조립:
   ```
   .venv/Scripts/python.exe tools/apply_edits.py <작업이름>
   ```
   - 오류가 나면 `edits.recorrect.json`을 고쳐 다시 실행한다.
   - "같은 이름의 md가 이미 있습니다"로 멈추면 경로를 보여 주고 덮어써도 되는지 묻고 멈춘다. 허락하면 `--overwrite`.

6. 2~3줄 요약 (한국어): 슬라이드 위치를 매긴 문단 수, 새로 고친 문장 수, 진도 위치 제안, 저장한 md 경로, 남은 대상 작업 수.
