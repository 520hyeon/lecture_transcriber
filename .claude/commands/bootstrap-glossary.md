---
description: 중간고사 자료(교수님 자료만)·원서·NotebookLM 전사본·샘플 Whisper 전사로 용어집·교정 사전 후보를 제안 (출처 목록을 먼저 확인받음)
argument-hint: <과목>   (예: 과목명)
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(.venv/Scripts/python.exe tools/bootstrap.py *)
---

# 용어집 시작하기: $ARGUMENTS

중간고사 범위 자료로 용어집·교정 사전·표기 규칙 **후보**를 만들어 `suggestions.json`에 올린다 (origin `bootstrap`).
`--apply`가 끝나면 프로그램(`tools/auto.py`)이 자동 승인 기준을 통과한 것은 바로 반영하고(변경 기록에 남고 되돌릴 수 있음),
나머지만 과목 관리 화면 "확인이 필요한 제안"에 남긴다. 이 명령이 용어집·교정 사전을 직접 바꾸지 않는다.
용어집이 빈 과목은 녹음을 전사하기 전에 이 명령을 먼저 실행한다 (용어집 없이 전사하면 오인식이 많다).

## 공통 규칙 (반드시 지킬 것)
- 채팅창에 자료 내용이나 후보 목록을 길게 출력하지 말 것. 결과는 파일로만 쓰고, 마지막에 2~3줄 요약만 말한다.
- **원본 PDF·PPTX·원서 md·NotebookLM 전사본 전체를 읽지 않는다.** `bootstrap.py --extract`가 만든 정리 파일만 읽는다.
  확인이 필요하면 `data/<과목>/cache/bootstrap/text/<파일>.txt`를 **Grep으로 찾아본다** (전체 Read 금지).
- **출처는 교수님 자료(슬라이드, 과제, 퀴즈, 실습 노트북), 원서, 중간고사 NotebookLM 전사본만.**
  사용자가 만든 정리 노트·요약·필기(`*_필기.pdf`, 개념정리노트, `*_학습진행요약.md`, 인수인계·유형정리·복기 노트 등)는 쓰지 않는다.
- 쓰는 파일은 `data/<과목>/cache/bootstrap/proposal.json`뿐이다. `glossary.json`, `fix.json`, `suggestions.json`, `subjects.json`, 강의 폴더는 직접 수정하지 않는다.
- 파이썬은 `.venv/Scripts/python.exe`로 실행한다 (프로젝트 루트 기준).

## 표기 규칙 (제안할 때 반드시 지킬 것)
`data/subjects.json`의 그 과목 `rules`·`notation`·`prefer_english_terms`를 먼저 읽는다.
- **`prefer_english_terms` 과목**: 교수님이 **영어로 말한** 용어만 영어 대표 표기 (한글 발음 표기는 별칭: photosynthesis ← 포토신세시스·포토 신세시스).
  교수님이 **한국어로 말한** 용어는 한국어 대표 표기 (광합성, 최댓값). 영어로 번역하지 않는다.
  어느 쪽인지는 `spoken.md`로 판단한다: 한글 발음 표기가 많으면 영어로 말한 것, 한국어 용어가 많으면 한국어로 말한 것. 애매하면 전사에 적힌 발음에 가까운 쪽.
  수식을 읽은 것은 수식 표기 (x 제곱 → x²).
- **한국어 과목**: 슬라이드 표기가 대표. 교수님이 따로 영어로 말한 용어는 용어 후보로 (별칭에 한글 발음).
  코드 과목(`keep_code_identifiers`)은 식별자를 코드 표기 그대로 (import, getName()).
- **손필기 과목**: 손필기 표기, 말로 한 수식은 표기 규칙 후보(`notation`)로 (x 제곱 → x²).
- 교정 규칙(`fix_rules`)은 Whisper가 같은 식으로 **2번 이상** 틀린 것만. 2글자 이하, 흔한 일반 낱말, 조사가 붙은 규칙(포토신세시스를 → photosynthesis를)은 넣지 않는다.
  **한국어로 말한 용어를 영어로 바꾸는 규칙은 넣지 않는다** (최대값 → maximum ✗, 최대값 → 최댓값 ○).

## 순서

1. 출처 목록:
   ```
   .venv/Scripts/python.exe tools/bootstrap.py <과목> --list
   ```
   목록(사용 / 제외와 이유 / "확인 필요")을 사용자에게 짧게 보여 주고, **"확인 필요" 파일을 쓸지, 더 뺄 파일이 있는지 묻고 멈춘다.**
   (예: `AL_Assignment1_written.pdf`처럼 사용자가 직접 쓴 답안일 수 있는 파일)

2. 승인되면 후보 정리 (뺄 파일은 `--exclude` 패턴으로):
   ```
   .venv/Scripts/python.exe tools/bootstrap.py <과목> --extract --exclude "*_written*" "노트.pdf"
   ```
   만들어지는 파일 (`data/<과목>/cache/bootstrap/`):
   - `materials.md` 자료별 페이지 제목 + 용어 후보 (횟수, 자료 수, 제목 여부, "한국어(English)" 짝, [용어집] 표시)
   - `spoken.md` 용어 후보마다 NotebookLM 전사본에서 영어로 / 한글 발음으로 / 한국어 용어로 적힌 횟수 (교수님이 어떻게 말하는지).
     소리만 비교한 것이라 엉뚱한 낱말이 섞여 있다 (예: term ← 비슷한 발음의 낱말). 명백히 같은 용어인 표기만 별칭으로 쓴다.
   - `book.md` 원서 용어 중 중간고사 자료·전사본에도 나오는 것 (철자 확인용, 절 번호 없음)
   - `whisper.md` 중간고사 샘플 Whisper 전사 ↔ 같은 녹음 NotebookLM 전사본에서 다르게 적힌 낱말 (교정 규칙 후보).
     샘플이 없으면 비어 있다 — 화면의 "중간고사 자료로 용어집 시작하기"에서 샘플 전사를 먼저 돌리라고 안내한다 (없어도 계속할 수 있음).

3. 정리 파일 4개를 읽고 `data/<과목>/cache/bootstrap/proposal.json` 작성:
   ```json
   {"terms": [{"term": "photosynthesis", "aliases": ["포토신세시스", "포토 신세시스"], "evidence": "Lecture07 제목, NotebookLM 한글 발음 12회",
               "in_title": true, "recommended": true, "auto": true, "why": "슬라이드 제목·여러 자료"}],
    "fix_rules": [{"wrong": "포토신세시스", "right": "photosynthesis", "evidence": "whisper.md 5회", "count": 5, "auto": true, "why": "실제 낱말이 아닌 오인식"}],
    "notation": [{"spoken": "x 제곱", "written": "x²", "auto": false, "why": "표기 규칙"}]}
   ```
   - `terms`: 이 과목의 전문용어만 (일반 영어 낱말·사람 이름·학교 이름·파일 이름 제외). 이미 [용어집]인 것은 빼되, 새 별칭이 있으면 같은 대표 표기로 넣는다.
     `recommended`: 여러 자료에 나오거나 페이지 제목에 나오고 표기가 확실한 것만 true.
   - `fix_rules`: `whisper.md`에서 Whisper 쪽이 확실히 틀린 것만. `evidence`에 근거(횟수·예시)를 짧게.
   - `notation`: 수식·기호를 말로 읽는 표현 (과목 표기 규칙에 이미 있는 것은 빼기).
   - **자동 승인 판단** `auto`·`why` (모든 항목): `auto: true`는 교수님 자료에 여러 번·제목에 나온 용어, 틀린 쪽이 실제 낱말이 아니고
     바른 쪽이 용어집 대표 표기(이번에 제안하는 용어 포함)인 교정 규칙에만. 양쪽 다 실제 낱말인 규칙, 번역 규칙, 근거 1회, 표기 규칙은 `false`.
     교정 규칙의 `count`는 근거 횟수(정수). 프로그램이 같은 기준으로 한 번 더 검사한다.
   - 후보가 너무 많으면 중요한 것부터 용어 150개, 교정 규칙 50개 안쪽으로.

4. 제안 올리기:
   ```
   .venv/Scripts/python.exe tools/bootstrap.py <과목> --apply
   ```
   경고(번역 규칙, 대표 표기 아님 등)가 나오면 그 항목을 proposal.json에서 고치거나 빼고 다시 실행한다.
   다시 실행하면 이 명령의 대기 중 제안은 새로 계산된다.

5. 2~3줄 요약 (한국어): 쓴 출처 수(뺀 것 포함), 제안한 용어·교정 규칙·표기 규칙 수, 자동 승인·사람 확인 수
   (사람 확인이 있으면 과목 관리 화면 "확인이 필요한 제안"에서 확인하라는 안내).
