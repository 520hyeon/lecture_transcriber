"""전사 뒤처리: 프롬프트 빌더 옵션, 환각·누락 의심 구간 재전사(복원). server.py가 씀 (GPU 모델은 server의 engine).

복원 순서 (구간마다):
  1. 의심 구간을 앞뒤 restore_pad_seconds 여유를 두고 잘라 ffmpeg 전처리 (highpass, afftdn, dynaudnorm)
  2. vad_filter=False, no_speech_threshold 완화로 두 번 재전사
       plain   initial_prompt·hotwords 없이
       prompt  이번 강의 프롬프트(프롬프트 빌더) + 바로 앞 문장
  3. tools/restore.py의 환각 검사를 통과한 결과를 고름. 둘 다 실패하면 [청취 불가]로 남김.

복원만 다시 (감지 기준을 바꿨을 때 등, Whisper 전체 재전사 없이):
    python pipeline.py restore <전사 작업 id>     # 서버를 끄고 실행 (GPU 메모리, 작업 기록 파일을 같이 씀)
  outputs/.jobs/<id>.raw.jsonl(원본)과 보관한 녹음으로 복원을 다시 하고 jsonl·txt·srt·교정 작업 폴더를 새로 만든다.
"""
import json, socket, sys, tempfile
from datetime import datetime
from pathlib import Path

import transcriber as T
from tools import common as C
from tools import prompt as P
from tools import restore as R


def builder_options(subject, base=None):
    """프롬프트 빌더로 Whisper 옵션 만들기 (beam_size 등 나머지는 base 그대로).
    과목 관리 → 고급에서 수동 프롬프트를 켜 두면(subjects.json "manual_prompt".enabled) 그 값을 그대로 쓴다."""
    sub = C.subject(subject)
    o = dict(base or {})
    mp = sub.get("manual_prompt") or {}
    if mp.get("enabled"):
        o.update({"subject": subject, "prompt_source": "manual", "prompt": mp.get("prompt") or "",
                  "hotwords": mp.get("hotwords") or "", "fix": P.fix_dict(subject),
                  "language": sub.get("language") or "ko", "prompt_info": None})
        o["beam_size"] = int(o.get("beam_size") or 5)
        return o
    b = P.build(subject)
    o.update({"subject": subject, "prompt_source": "builder", "prompt": b["prompt"], "hotwords": b["hotwords"],
              "fix": P.fix_dict(subject), "language": sub.get("language") or "ko",
              "prompt_info": {"tokens": b["tokens"], "cut": b["cut"]}})
    o["beam_size"] = int(o.get("beam_size") or 5)
    return o


def retranscribe(engine, audio, rg, attempt, o, s, prev_text=""):
    """구간 하나를 다시 전사. 가운데가 구간 안에 드는 세그먼트만 (원래 시각으로) 돌려줌."""
    dur = len(audio) / T.SR
    a, b = max(0.0, rg["start"] - s["restore_pad_seconds"]), min(dur, rg["end"] + s["restore_pad_seconds"])
    clip = audio[int(a * T.SR):int(b * T.SR)]
    try:
        clip = T.filter_audio(clip, s["restore_audio_filter"])
    except Exception as e:  # 전처리에 실패하면 원래 소리로
        print("[복원] 전처리 생략:", e)
    if attempt == "plain":
        prompt = hot = None
    else:
        prompt = " ".join(x for x in (o.get("prompt"), prev_text[-120:]) if x) or None
        hot = o.get("hotwords") or None
    out = []
    for x in engine.transcribe(clip, o.get("language") or None, int(o.get("beam_size") or 5), prompt, hot, o.get("fix"),
                               vad=False, no_speech_threshold=s["restore_no_speech_threshold"]):
        x = {**x, "start": round(x["start"] + a, 2), "end": round(x["end"] + a, 2)}
        if rg["start"] - 0.3 <= (x["start"] + x["end"]) / 2 <= rg["end"] + 0.3:
            out.append(x)
    return out, [prompt, hot]


def restore(engine, audio, segments, o, cancel=None, on_progress=None):
    """Whisper 결과 → (최종 세그먼트, 복원 요약). 최종 세그먼트: 복원 문장은 restored, 청취 불가 자리는 missing."""
    s = C.settings()
    if not s["restore_enabled"] or not segments:
        return segments, R.summary([], len(segments))
    lang = o.get("language") or ""
    levels = R.AudioLevels(audio, s["voice_margin_db"])
    regions = R.detect(segments, levels, s, lang, [o.get("prompt"), o.get("hotwords")])
    suspect = {i for rg in regions for i in rg["segs"]}
    if on_progress:
        on_progress(0, len(regions), None)
    for k, rg in enumerate(regions):
        if cancel is not None and cancel.is_set():
            raise T.Cancelled()
        before = [x for x in segments if x["end"] <= rg["start"] + 0.01 and x["idx"] not in suspect]
        after = [x for x in segments if x["start"] >= rg["end"] - 0.01 and x["idx"] not in suspect]
        prev = before[-1]["text"] if before else ""
        neighbors = [x["text"] for x in before[-1:] + after[:1]]
        tries = []
        for attempt in ("plain", "prompt"):
            segs, used = retranscribe(engine, audio, rg, attempt, o, s, prev)
            tries.append(R.evaluate(attempt, segs, s, lang, used if attempt == "prompt" else [], neighbors))
        R.choose(rg, tries, s)
        if on_progress:
            on_progress(k + 1, len(regions), rg)
    return R.assemble(segments, regions), R.summary(regions, len(segments))


# ---------- CLI: 복원만 다시 ----------

def _jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def restore_job(job_id):
    from tools import work as W
    jd = C.BASE / "outputs" / ".jobs"
    jp = jd / f"{job_id}.json"
    job = C.load_json(jp)
    if not job:
        sys.exit(f"전사 작업 기록이 없습니다: {jp}")
    audio_path = jd / job["audio"] if job.get("audio") else None
    if not audio_path or not audio_path.exists():
        sys.exit("보관한 녹음이 없습니다 (Phase 3 이전에 전사한 작업은 다시 전사하세요)")
    raw_p, cur_p = jd / f"{job_id}.raw.jsonl", jd / f"{job_id}.jsonl"
    raw = _jsonl(raw_p if raw_p.exists() else cur_p)
    engine = T.Transcriber()
    print("모델 로드:", engine.load())
    with tempfile.TemporaryDirectory() as td:
        audio = T.prepare_audio(str(audio_path), str(Path(td) / "a.wav"))
    final, summ = restore(engine, audio, raw, job["options"],
                          on_progress=lambda d, n, rg: print(f"  {d}/{n}" + (f" {W.ts(rg['start'])}–{W.ts(rg['end'])} {rg['outcome']}" if rg else "")))
    shown = [{**x, "text": f"[청취 불가 {W.ts(x['start'])}–{W.ts(x['end'])}]"} if x.get("missing") else x for x in final]
    if not raw_p.exists():
        _write_jsonl(raw_p, raw)
    _write_jsonl(cur_p, final)
    C.save_json(jd / f"{job_id}.restore.json", summ)
    txt = C.BASE / "outputs" / job["txt_name"]
    T.write_txt(shown, str(txt))
    T.write_srt(shown, str(txt.with_suffix(".srt")))
    st = summ["stats"]
    job.update(restore=st, segments=len(final))
    job["notices"] = (job.get("notices") or []) + [
        f"복원 다시 실행: 의심 {st['regions']}곳 → 복원 {st['restored']} · 원래 문장 유지 {st['kept']}"
        f" · 환각만 제거 {st['dropped']} · 청취 불가 {st['missing']}곳 ({st['missing_seconds']:.0f}초)"]
    C.save_json(jp, job)
    print(job["notices"][-1])
    if job.get("work") and W.load_job(job["work"]):
        old = W.edits_files(job["work"])
        name = W.create_work(final, job["filename"], W.load_job(job["work"])["subject"], server_job=job_id,
                             options=job["options"], duration=job.get("duration"), compute_type=job.get("compute_type"),
                             raw=raw, restore=summ, audio=audio_path,
                             transcribed=datetime.fromtimestamp(job["created"]).isoformat(timespec="seconds") if job.get("created") else None)
        print(f"교정 작업 다시 만듦: jobs/{name}" + (f" — 세그먼트 번호가 바뀌었을 수 있으니 /correct를 다시 실행하세요 ({', '.join(p.name for p in old)} 있음)" if old else ""))


def _cli():
    C.utf8_stdout()
    if len(sys.argv) != 3 or sys.argv[1] != "restore":
        sys.exit("사용법: python pipeline.py restore <전사 작업 id>")
    with socket.socket() as sk:
        if sk.connect_ex(("127.0.0.1", 8765)) == 0:
            sys.exit("전사 서버가 실행 중입니다. 서버를 끈 뒤 다시 실행하세요 (GPU 메모리와 작업 기록을 같이 씀)")
    C.ensure_data()
    restore_job(sys.argv[2])


if __name__ == "__main__":
    _cli()
