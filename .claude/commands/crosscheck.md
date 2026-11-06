---
description: 이해도가 낮은 구간만 NotebookLM 전사본과 대조해 교정 보완 (NotebookLM은 두 번째 후보로만)
argument-hint: <작업이름> <NotebookLM md 경로>   (예: 과목명_261028 jobs/과목명_261028/notebooklm.md)
allowed-tools: Read, Write, Edit, Glob, Bash(.venv/Scripts/python.exe tools/crosscheck.py *), Bash(.venv/Scripts/python.exe tools/apply_edits.py *)
---

# NotebookLM 대조: $ARGUMENTS

`comprehension.need_notebooklm`이 true인 작업에 쓴다. 인식 실패로 약한 구간(`weak_ranges` 중 `kind`가 `board`(판서 설명)가 아닌 것)만 NotebookLM 전사본의 같은 부분과 나란히 놓고,
두 전사가 같은 내용을 다르게 적은 곳만 고친다. 결과는 `edits.crosscheck.json`(덧붙이는 파일)이다.

## 공통 규칙 (반드시 지킬 것)
- 채팅창에 전사 내용을 길게 출력하지 말 것. 결과는 파일로만 쓰고, 마지막에 2~3줄 요약만 말한다.
- **NotebookLM 전사본 전체를 읽지 않는다.** `crosscheck.py`가 만든 `crosscheck.md`(대조표)만 읽는다.
- NotebookLM도 인식 오류가 있다. **정답이 아니라 두 번째 후보**로만 쓴다:
  - Whisper 문장이 뜻이 통하지 않고, NotebookLM 쪽이 같은 자리에서 뜻이 통하며 강의 맥락·용어집·강의자료와 맞을 때만 고친다.
  - 둘 다 뜻이 통하면 Whisper(지금 문장)를 둔다.
  - NotebookLM에만 있는 문장을 옮겨 넣지 않는다. 세그먼트를 합치거나 나누지 않는다. 한 세그먼트의 범위 안에서만 고친다.
  - `M` 줄은 고칠 수 없다. NotebookLM에 그 자리 내용이 있으면 `inaudible`에 같은 시각으로 `guess`만 적는다 ("NotebookLM: …").
  - `H` 줄(사용자가 직접 입력)은 고치지 않는다.
- 과목 표기 규칙을 지킨다 (`prefer_english_terms` 과목: 교수님이 영어로 말한 용어만 영어, 한국어로 말한 용어는 한국어).
- 쓰는 파일은 `jobs/<작업이름>/edits.crosscheck.json`뿐이다. 다른 파일·강의 폴더는 수정하지 않는다.
- 파이썬은 `.venv/Scripts/python.exe`로 실행한다 (프로젝트 루트 기준).

## 순서

1. 인자 확인: `<작업이름>`과 NotebookLM md 경로. 경로가 없으면 `jobs/<작업이름>/notebooklm.md`(화면에서 올린 파일)가 있는지 보고,
   없으면 화면의 "NotebookLM md 올리기"로 올리거나 경로를 알려 달라고 하고 멈춘다.
   이미 `edits.crosscheck*.json`이 있으면 다시 할지 묻고 멈춘다.

2. 대조표 만들기:
   ```
   .venv/Scripts/python.exe tools/crosscheck.py <작업이름> "<NotebookLM md 경로>"
   ```
   약한 구간을 직접 정하려면 `--ranges 300-340,400-420`을 붙인다 (사용자가 구간을 말했을 때만).

3. `jobs/<작업이름>/crosscheck.md`를 읽는다. 구간마다 "전사본(번호|시각|지금 문장|표시)"과 "NotebookLM 후보(일치도)"가 있다.
   일치도가 낮다고 표시된 구간은 고치지 않는다. 필요하면 `context.md`의 용어집·강의자료로 확인한다.

4. `edits.crosscheck.json` 작성:
   ```json
   {"edits": [{"i": 305, "text": "고친 문장 전체", "reason": "NotebookLM 대조: 비슷한 발음의 낱말→올바른 용어"}],
    "inaudible": [{"start": "29:56", "end": "30:26", "guess": "NotebookLM: 핵심 개념 설명 단계"}],
    "comprehension": {"score": 78, "weak_ranges": [], "need_notebooklm": false, "reason": "NotebookLM 대조로 partition 설명 보완"}}
   ```
   - `edits`: 약한 구간 안의 세그먼트만. 같은 번호의 기존 교정을 덮어쓴다 (고친 문장 **전체**). 바꿀 게 없으면 `[]`.
   - `paragraphs`는 쓰지 않는다 (기존 문단 유지).
   - `comprehension`: 대조 뒤의 이해도로 다시 매긴다 (아직 약한 구간이 남으면 그것만 `weak_ranges`에).
   - `review`(선택): 확인 목록(`/correct`의 review)의 인식 실패 항목이 대조로 확실해졌으면, 남길 항목만 담아 `review`를 다시 쓴다
     (쓰면 기존 review를 대신함, 안 쓰면 그대로). 모순 의심은 NotebookLM이 한쪽 후보를 지지해도 지우지 말고 `why`에 "NotebookLM: 낱말"처럼 덧붙인다.

5. 검증·조립:
   ```
   .venv/Scripts/python.exe tools/apply_edits.py <작업이름>
   ```
   오류가 나면 `edits.crosscheck.json`을 고쳐 다시 실행한다. md 덮어쓰기 확인이 나오면 경로를 보여 주고 묻고 멈춘다.

6. 2~3줄 요약 (한국어): 대조한 구간 수, 고친 문장 수, 추정을 적은 청취 불가 구간, 새 이해도, 저장한 md 경로.
