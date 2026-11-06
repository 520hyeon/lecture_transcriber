"""강의 전사 웹 서버 (127.0.0.1 전용). 실행: run.bat 또는 python server.py"""
import asyncio, ctypes, json, os, queue, re, shutil, socket, subprocess, sys, threading, time, traceback, uuid, webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import transcriber as T  # CUDA DLL 경로 등록 + faster_whisper import
import pipeline as PL  # 프롬프트 빌더 옵션, 환각·누락 구간 재전사(복원)
from tools import common as C  # 과목 데이터 (data/)
from tools import prompt as P  # 프롬프트 빌더
from tools import work as W  # 교정 작업 폴더 (jobs/)
from tools import review as RV  # 제안 승인 화면 검사 (교정 규칙 영향·경고)
from tools import recorrect as RC  # /recorrect 대상
from tools import auto as A  # 제안 자동 승인

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
import uvicorn

HOST = "127.0.0.1"
PORT = int(os.environ.get("TRANSCRIBE_PORT", "8765"))
BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "outputs"
JOB_DIR = OUT_DIR / ".jobs"
UP_DIR = BASE / "uploads"
for d in (OUT_DIR, JOB_DIR, UP_DIR):
    d.mkdir(parents=True, exist_ok=True)

ACTIVE = ("queued", "converting", "transcribing", "restoring")
AUDIO_TYPES = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
               ".ogg": "audio/ogg", ".webm": "audio/webm", ".flac": "audio/flac"}


# ---------- SSE 브로드캐스트 ----------

class Hub:
    def __init__(self):
        self.clients = set()
        self.loop = None

    def publish(self, event, data):
        if self.loop is None:
            return
        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.loop.call_soon_threadsafe(self._put, msg)

    def _put(self, msg):
        for q in list(self.clients):
            q.put_nowait(msg)


hub = Hub()


# ---------- 시스템 (전원 / 절전) ----------

class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def system_info():
    on_ac, pct = True, None
    try:
        ps = _PowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(ps)):
            on_ac = ps.ACLineStatus != 0  # 0=배터리, 1=전원, 255=알 수 없음
            pct = None if ps.BatteryLifePercent == 255 else ps.BatteryLifePercent
    except Exception:
        pass
    return {"on_ac": on_ac, "battery": pct, "ffmpeg": bool(T.find_ffmpeg()), "ffmpeg_help": T.FFMPEG_HELP}


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def keep_awake(on):
    """현재 스레드가 작업하는 동안 Windows 절전 진입 방지."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


# ---------- 모델 ----------

model_state = {"state": "loading", "compute_type": None, "error": None}


def set_model_state(**kw):
    model_state.update(kw)
    hub.publish("model", model_state)


def on_engine_notice(msg):
    if current_job is not None:
        current_job.notice(msg)
    hub.publish("notice", {"message": msg})


engine = T.Transcriber(on_notice=on_engine_notice)
model_ready = threading.Event()


def load_model():
    set_model_state(state="loading")
    try:
        ct = engine.load(T.DEFAULT_COMPUTE)
        set_model_state(state="ready", compute_type=ct, error=None)
    except Exception as e:
        traceback.print_exc()
        set_model_state(state="error", error=str(e))
    model_ready.set()


# ---------- 작업 ----------

def safe_stem(name):
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(name).stem).strip(" .") or "audio"
    return stem[:80]


class Job:
    FIELDS = ("id", "filename", "options", "status", "created", "started", "finished", "duration",
              "progress", "eta", "segments", "txt_name", "notices", "error", "compute_type", "work",
              "restore", "audio", "upload", "resume_pending")

    def __init__(self, **kw):
        self.id = uuid.uuid4().hex[:12]
        self.filename = ""
        self.options = {}
        self.status = "queued"
        self.created = time.time()
        self.started = self.finished = self.duration = self.eta = None
        self.progress = 0.0
        self.segments = 0
        self.txt_name = ""
        self.notices = []
        self.error = None
        self.compute_type = None
        self.work = None  # 교정 작업 이름 (jobs/<work>/)
        self.restore = None  # 복원 진행({done, total}) → 끝나면 통계
        self.audio = None  # 보관한 녹음 파일 이름 (outputs/.jobs/<id>.audio.<확장자>, 오디오 재생·복원용)
        self.upload = None  # 전사할 녹음 (uploads/<이름>). 서버가 꺼져도 남아 있으면 다시 켤 때 이어서 처리할 수 있음
        self.resume_pending = False  # 서버가 꺼져 끝나지 못한 작업 → 다시 켜면 이어서 처리할지 물음
        self.__dict__.update(kw)
        self.upload_path = None
        self.cancel = threading.Event()

    @property
    def txt_path(self): return OUT_DIR / self.txt_name
    @property
    def srt_path(self): return self.txt_path.with_suffix(".srt")
    @property
    def jsonl_path(self): return JOB_DIR / f"{self.id}.jsonl"
    @property
    def raw_path(self): return JOB_DIR / f"{self.id}.raw.jsonl"  # 복원 전 Whisper 원본
    @property
    def restore_path(self): return JOB_DIR / f"{self.id}.restore.json"
    @property
    def audio_path(self): return JOB_DIR / self.audio if self.audio else None

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.FIELDS}
        d["cancelling"] = self.cancel.is_set() and self.status in ACTIVE
        wj = W.load_job(self.work) if self.work else None
        d["work_status"] = wj["status"] if wj else None
        return d

    def save(self):
        (JOB_DIR / f"{self.id}.json").write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")

    def publish(self):
        self.save()
        hub.publish("job", self.to_dict())

    def notice(self, msg):
        self.notices.append(msg)
        self.publish()

    def read_segments(self, raw=False):
        p = self.raw_path if raw and self.raw_path.exists() else self.jsonl_path
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]


def shown(segs):
    """txt·srt·화면용: 청취 불가 자리(missing)는 [청취 불가 MM:SS–MM:SS]로."""
    return [{**s, "text": f"[청취 불가 {W.ts(s['start'])}–{W.ts(s['end'])}]"} if s.get("missing") else s for s in segs]


jobs: dict[str, Job] = {}
job_queue: "queue.Queue[Job]" = queue.Queue()
current_job: Job | None = None


def load_saved_jobs():
    for p in JOB_DIR.glob("*.json"):
        if "." in p.stem:  # <id>.restore.json 등은 작업 기록이 아님
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if "id" not in d or "status" not in d:
                continue
            d.pop("cancelling", None)
            job = Job(**d)
            if job.status in ACTIVE + ("interrupted",):  # 서버가 꺼질 때 진행 중이었거나 대기열에 있던 작업
                if job.status in ACTIVE:  # 녹음이 uploads/에 남아 있으면 이어서 처리할지 물음
                    job.resume_pending = bool(job.upload and (UP_DIR / job.upload).exists())
                elif job.resume_pending and not (job.upload and (UP_DIR / job.upload).exists()):
                    job.resume_pending = False
                segs = job.read_segments()
                job.status, job.segments, job.eta = "interrupted", len(segs), None
                if segs and job.duration:
                    job.progress = min(1.0, segs[-1]["end"] / job.duration)
                job.save()
            jobs[job.id] = job
        except Exception:
            traceback.print_exc()
    with prep_lock:  # 준비 목록에서 올린 파일이 사라진 항목은 뺌
        items = [x for x in prep_items() if x["source"] != "upload" or Path(x["path"]).exists()]
        prep_save(items)
    keep = {PREP_PATH.name} | {Path(x["path"]).name for x in items if x["source"] == "upload"} \
        | {j.upload for j in jobs.values() if j.resume_pending}
    for p in UP_DIR.iterdir():  # 처리 못 한 업로드 잔여물 정리 (준비 목록·이어서 처리할 녹음은 남김)
        if p.name not in keep:
            try: p.unlink()
            except OSError: pass


def unique_txt_name(stem):
    base = f"{stem}_{datetime.now():%Y%m%d_%H%M}"
    name, n = f"{base}.txt", 2
    taken = {j.txt_name for j in jobs.values()}
    while (OUT_DIR / name).exists() or name in taken:
        name, n = f"{base}_{n}.txt", n + 1
    return name


def run_job(job: Job):
    global current_job
    current_job = job
    tmp_wav = UP_DIR / f"{job.id}_16k.wav"
    keep_awake(True)
    try:
        job.started = time.time()
        job.status = "converting"
        job.publish()
        if not model_ready.is_set():
            job.notice("모델 로딩이 끝나기를 기다리는 중…")
            model_ready.wait()
        if model_state["state"] != "ready":
            raise RuntimeError("모델 로드 실패: " + str(model_state["error"]))

        audio = T.prepare_audio(str(job.upload_path), str(tmp_wav), job.cancel)
        job.duration = len(audio) / T.SR
        if job.cancel.is_set():
            raise T.Cancelled()

        o = job.options
        subject = job_subject(job)  # 과목(준비 목록에서 고른 것, 없으면 파일명)을 알면 과목 데이터로 프롬프트를 만듦
        if subject:
            o = job.options = PL.builder_options(subject, o)
            info = o.get("prompt_info")
            if o["prompt_source"] == "manual":
                job.notices.append(f"수동 Whisper 프롬프트({subject}, 과목 관리 → 고급)로 전사합니다")
            elif info:
                t = info["tokens"]
                job.notices.append(f"과목 프롬프트 빌더({subject}): {t['total']}/{t['limit']}토큰"
                                   + (f", 자리가 없어 뺀 항목 {len(info['cut'])}개" if info["cut"] else ""))
        else:
            job.notices.append("과목 없이 전사합니다: Whisper 프롬프트 없이, 교정 작업도 만들지 않습니다")
        job.status = "transcribing"
        job.compute_type = engine.compute_type
        job.publish()
        t_start = time.time()
        writer = T.ResultWriter(str(job.txt_path), str(job.jsonl_path))
        segments = []
        try:
            for seg in engine.transcribe(audio, o.get("language") or None, int(o.get("beam_size") or 5),
                                         o.get("prompt"), o.get("hotwords"), o.get("fix")):
                writer.write(seg)
                segments.append(seg)
                job.segments = len(segments)
                job.progress = min(1.0, seg["end"] / job.duration) if job.duration else 0.0
                el = time.time() - t_start
                job.eta = el * (1 - job.progress) / job.progress if job.progress > 0.01 else None
                if engine.compute_type != job.compute_type:
                    job.compute_type = engine.compute_type
                    set_model_state(compute_type=engine.compute_type)
                hub.publish("segment", {"job": job.id, "seg": seg, "line": T.line_of(seg)})
                hub.publish("progress", {"id": job.id, "progress": job.progress, "eta": job.eta,
                                         "segments": job.segments, "compute_type": job.compute_type})
                if job.cancel.is_set():
                    break
        finally:
            writer.close()
            T.write_srt(segments, str(job.srt_path))
        if job.cancel.is_set():
            job.status = "cancelled"
        elif o.get("sample"):  # 중간고사 샘플: Whisper 결과만 과목 데이터에 저장 (복원·교정 작업 없음)
            job.status, job.progress = "done", 1.0
            save_sample(job, segments)
        else:
            keep_audio(job)
            restore_segments(job, audio, segments)
            job.status, job.progress = "done", 1.0
            try:
                if make_work(job):
                    job.notices.append(f"교정 대기: Claude Code에서 {W.correct_command(job.work)} 를 실행하세요.")
            except Exception as e:
                traceback.print_exc()
                job.notices.append(f"교정 작업 폴더를 만들지 못했습니다: {e}")
    except T.Cancelled:
        job.status = "cancelled"
    except Exception as e:
        traceback.print_exc()
        job.status, job.error = "error", str(e)
    finally:
        keep_awake(False)
        job.finished, job.eta = time.time(), None
        current_job = None
        for p in (tmp_wav, job.upload_path):
            try:
                if p: Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        job.upload = None
        job.publish()


def keep_audio(job):
    """업로드한 녹음을 outputs/.jobs/<id>.audio.<확장자>로 보관 (결과 화면 오디오 재생, 나중에 다시 복원할 때)."""
    src = Path(job.upload_path) if job.upload_path else None
    if not src or not src.exists():
        return
    name = f"{job.id}.audio{src.suffix.lower()}"
    try:
        src.replace(JOB_DIR / name)
        job.audio = name
    except OSError as e:
        job.notices.append(f"녹음 파일을 보관하지 못해 결과 화면에서 재생할 수 없습니다: {e}")


def restore_segments(job, audio, segments):
    """환각·누락 의심 구간 재전사. 원본은 <id>.raw.jsonl에 두고 jsonl·txt·srt를 복원본으로 바꿈."""
    if not segments or not C.settings()["restore_enabled"]:
        return segments
    job.status, job.restore, job.eta = "restoring", {"done": 0, "total": None}, None
    job.publish()

    def progress(done, total, rg):
        job.restore = {"done": done, "total": total}
        hub.publish("progress", {"id": job.id, "progress": job.progress, "eta": None, "segments": job.segments,
                                 "compute_type": job.compute_type, "restore": job.restore})

    try:
        final, summ = PL.restore(engine, audio, segments, job.options, job.cancel, progress)
    except T.Cancelled:
        raise
    except Exception as e:
        traceback.print_exc()
        job.restore = None
        job.notices.append(f"복원 중 오류가 나서 Whisper 결과를 그대로 씁니다: {e}")
        return segments
    with open(job.raw_path, "w", encoding="utf-8") as f:
        for s in segments:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(job.jsonl_path, "w", encoding="utf-8") as f:
        for s in final:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    C.save_json(job.restore_path, summ)
    T.write_txt(shown(final), str(job.txt_path))
    T.write_srt(shown(final), str(job.srt_path))
    st = job.restore = summ["stats"]
    job.segments = len(final)
    job.notices.append(f"환각·누락 의심 {st['regions']}곳 재전사: 복원 {st['restored']} · 원래 문장 유지 {st['kept']}"
                       f" · 환각만 제거 {st['dropped']} · 청취 불가 {st['missing']}곳 ({st['missing_seconds']:.0f}초)")
    return final


def sample_path(subject, filename):
    return C.subject_dir(subject) / "cache" / "samples" / f"{Path(filename).name}.json"


def save_sample(job, segments):
    """중간고사 샘플 Whisper 전사 → data/<과목>/cache/samples/<녹음 파일명>.json (/bootstrap-glossary가 NotebookLM 전사본과 비교)."""
    subject = job.options["subject"]
    C.save_json(sample_path(subject, job.filename), {
        "source_file": Path(job.filename).name, "job_id": job.id, "created": C.now_iso(), "duration": job.duration,
        "whisper": {k: job.options.get(k) for k in ("prompt", "hotwords", "language", "beam_size", "prompt_source")},
        "segments": [{k: x.get(k) for k in ("start", "end", "text", "avg_logprob", "compression_ratio", "no_speech_prob")}
                     for x in segments]})
    job.notices.append(f"중간고사 샘플 저장: {subject} · {Path(job.filename).name} → /bootstrap-glossary {subject}에서 NotebookLM 전사본과 비교합니다")


def job_subject(job):
    """전사 작업의 과목: 준비 목록에서 고른 과목(options.subject, None이면 과목 없이) → 없으면 파일명 <과목>_<YYMMDD>."""
    o = job.options or {}
    return o["subject"] if "subject" in o else C.subject_from_filename(job.filename)


def make_work(job, subject=None):
    """전사 결과로 교정 작업 폴더를 만든다. 과목은 준비 목록에서 고른 과목, 없으면 파일명(<과목>_<YYMMDD>)에서 자동 인식."""
    subject = subject or job_subject(job)
    if not subject:
        return None
    job.work = W.create_work(job.read_segments(), job.filename, subject, server_job=job.id, options=job.options,
                             duration=job.duration, compute_type=job.compute_type,
                             raw=job.read_segments(raw=True), restore=C.load_json(job.restore_path),
                             audio=job.audio_path if job.audio_path and job.audio_path.exists() else None,
                             transcribed=datetime.fromtimestamp(job.created).isoformat(timespec="seconds"))
    return job.work


def worker():
    while True:
        job = job_queue.get()
        if job.status == "queued" and not job.cancel.is_set():
            run_job(job)


# ---------- 앱 ----------

@asynccontextmanager
async def lifespan(app):
    hub.loop = asyncio.get_running_loop()
    C.ensure_data()
    load_saved_jobs()
    try:
        for n in W.refresh_versions():  # 같은 녹음을 다시 전사한 작업 → 이전 작업은 "대체됨"
            print(f"교정 작업 대체 상태 갱신: {n}")
    except Exception:
        traceback.print_exc()
    threading.Thread(target=load_model, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()

    async def poll_system():
        while True:
            await asyncio.sleep(20)
            hub.publish("system", system_info())

    async def watch_works():  # Claude Code가 edits.json을 쓰거나 apply_edits.py가 끝나면 화면 갱신
        seen = {}
        while True:
            await asyncio.sleep(2)
            try:
                for w in W.list_works():
                    st = W.work_state(w["name"])
                    if st and seen.get(w["name"]) != st["mtime"]:
                        if w["name"] in seen:
                            hub.publish("work", st)
                        seen[w["name"]] = st["mtime"]
            except Exception:
                traceback.print_exc()
    task = asyncio.create_task(poll_system())
    task2 = asyncio.create_task(watch_works())
    if not os.environ.get("TRANSCRIBE_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    yield
    task.cancel()
    task2.cancel()


app = FastAPI(lifespan=lifespan, title="강의 전사")


def get_job(job_id) -> Job:
    if job_id not in jobs:
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    return jobs[job_id]


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


def snapshot():
    return {"model": model_state, "system": system_info(),
            "active": [j.to_dict() for j in sorted(jobs.values(), key=lambda j: j.created) if j.status in ACTIVE],
            "resumable": [j.to_dict() for j in resumable()]}


@app.get("/api/state")
def state():
    return snapshot()


@app.get("/api/events")
async def events(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    hub.clients.add(q)

    async def gen():
        try:
            yield f"retry: 2000\nevent: snapshot\ndata: {json.dumps(snapshot(), ensure_ascii=False)}\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            hub.clients.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- 전사 준비 목록: 올린 파일·기말고사 녹음 → 확인(과목·길이·이미 처리했는지) → "전사하기" ----------

PREP_PATH = UP_DIR / "prep.json"  # 서버를 껐다 켜도 남음 (올린 파일은 uploads/prep_<id>.<확장자>)
NO_SUBJECT = "-"  # 과목 없이 전사 (Whisper 프롬프트 없음, 교정 작업 없음)
prep_lock = threading.Lock()


def prep_items():
    return (C.load_json(PREP_PATH, {}) or {}).get("items", [])


def prep_save(items):
    C.save_json(PREP_PATH, {"items": items})


def ffprobe_path():
    ff = shutil.which("ffprobe")
    if not ff and T.find_ffmpeg():
        p = Path(T.find_ffmpeg())
        p = p.with_name("ffprobe" + p.suffix)
        ff = str(p) if p.exists() else None
    return ff


def probe_duration(path):
    """녹음 길이(초). ffprobe가 없거나 읽지 못하면 None."""
    ff = ffprobe_path()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return round(float(r.stdout.strip()), 1)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def new_prep_item(name, source, path, pid=None):
    subject = C.subject_from_filename(name)
    return {"id": pid or uuid.uuid4().hex[:12], "filename": name, "source": source, "path": str(path),
            "size": Path(path).stat().st_size, "duration": probe_duration(path),
            "subject": subject, "subject_auto": subject, "checked": True, "added": C.now_iso()}


def processed_map():
    """녹음 파일 이름 → 처리 상태. 대체되지 않은 교정 작업이나 끝난 전사가 있으면 done, 대기열에 있으면 active."""
    out = {}
    for w in W.list_works():
        if w.get("status") != "superseded":
            out[w["source_file"]] = {"state": "done", "work": w["name"], "work_status": w.get("status")}
    for j in jobs.values():
        if (j.options or {}).get("sample"):
            continue
        name = Path(j.filename).name
        if j.status in ACTIVE:
            out[name] = {"state": "active", "job": j.id}
        elif j.status == "done" and name not in out:
            out[name] = {"state": "done", "job": j.id}
    return out


def speed_ratio():
    """끝난 전사의 (걸린 시간 ÷ 녹음 길이) 중앙값 (변환·복원 포함, 5분 이상 녹음만). 기록이 없으면 0.15."""
    rs = sorted((j.finished - j.started) / j.duration for j in jobs.values()
                if j.status == "done" and j.started and j.finished and (j.duration or 0) > 300)
    return round(rs[len(rs) // 2], 3) if rs else 0.15


def glossary_counts():
    return {n: len(C.load_json(C.subject_dir(n) / "glossary.json", {"terms": []})["terms"]) for n in C.subject_names()}


def needs_bootstrap(x, counts=None):
    """용어집이 빈 과목의 녹음인데 [그래도 전사]를 누르지 않았으면 True (/bootstrap-glossary를 먼저 실행하도록)."""
    counts = counts if counts is not None else glossary_counts()
    subj = x.get("subject")
    return bool(subj and subj != NO_SUBJECT and counts.get(subj) == 0 and not x.get("allow_empty_glossary"))


def prep_state():
    done = processed_map()
    counts = glossary_counts()
    items = []
    for x in prep_items():
        st = done.get(x["filename"])
        items.append({**x, "exists": Path(x["path"]).exists(), "processed": st, "needs_bootstrap": needs_bootstrap(x, counts)})
    return {"items": items, "ratio": speed_ratio(), "subjects": C.subject_names(), "no_subject": NO_SUBJECT, "glossary": counts}


def publish_prep():
    hub.publish("prep", {"count": len(prep_items())})


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """올린 파일은 바로 전사하지 않고 "전사 준비 목록"에 넣음 (과목·길이 확인 후 "전사하기")."""
    added = []
    for f in files:
        name = Path(f.filename or "audio").name
        pid = uuid.uuid4().hex[:12]
        path = UP_DIR / f"prep_{pid}{Path(name).suffix.lower()[:10]}"
        with open(path, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        added.append(await asyncio.to_thread(new_prep_item, name, "upload", path, pid))
    with prep_lock:
        prep_save(prep_items() + added)
    publish_prep()
    return {**prep_state(), "added": len(added)}


@app.get("/api/prep")
def get_prep():
    return prep_state()


@app.put("/api/prep/{pid}")
async def put_prep(pid: str, request: Request):
    """과목 바꾸기(subject: 과목 이름 | "-" 과목 없이 | "" 선택 필요), 체크(checked), 용어집이 비어도 전사(allow_empty_glossary)."""
    b = await request.json()
    with prep_lock:
        items = prep_items()
        x = next((x for x in items if x["id"] == pid), None)
        if not x:
            raise HTTPException(404, "준비 목록에 없는 파일입니다")
        if "subject" in b:
            s = str(b["subject"] or "")
            if s and s != NO_SUBJECT:
                get_subject_or_404(s)
            x["subject"] = s or None
        if "checked" in b:
            x["checked"] = bool(b["checked"])
        if "allow_empty_glossary" in b:
            x["allow_empty_glossary"] = bool(b["allow_empty_glossary"])
        prep_save(items)
    return prep_state()


@app.delete("/api/prep/{pid}")
def delete_prep(pid: str):
    """준비 목록에서 빼기. 올린 파일이면 uploads/에서도 지움 (기말고사_녹음본 원본은 건드리지 않음)."""
    with prep_lock:
        items = prep_items()
        x = next((x for x in items if x["id"] == pid), None)
        if not x:
            raise HTTPException(404, "준비 목록에 없는 파일입니다")
        if x["source"] == "upload" and Path(x["path"]).resolve().parent == UP_DIR.resolve():
            Path(x["path"]).unlink(missing_ok=True)
        prep_save([y for y in items if y["id"] != pid])
    publish_prep()
    return prep_state()


def finals_folder():
    return C.root() / C.settings()["finals_recordings"]


@app.get("/api/finals")
def finals_list():
    """기말고사_녹음본의 녹음을 과목별로: new(처리 안 함) · prep(준비 목록에 있음) · active(대기열) · done(처리됨)."""
    folder = finals_folder()
    done = processed_map()
    in_prep = {Path(x["path"]).resolve() for x in prep_items() if x["source"] == "finals"}
    groups, other = {n: [] for n in C.subject_names()}, []
    if folder.is_dir():
        for p in sorted(folder.iterdir(), key=lambda p: W._natural(p.name)):
            if not p.is_file() or p.suffix.lower() not in AUDIO_TYPES:
                continue
            k = C.lecture_key(p.name)
            st = done.get(p.name) or {}
            row = {"file": p.name, "size": p.stat().st_size, "lecture": k, "work": st.get("work"),
                   "work_status": st.get("work_status"), "job": st.get("job"),
                   "state": "prep" if p.resolve() in in_prep else st.get("state", "new")}
            (groups[k["subject"]] if k else other).append(row)
    return {"folder": str(folder), "exists": folder.is_dir(),
            "subjects": [{"name": n, "recordings": rows} for n, rows in groups.items()], "other": other}


@app.post("/api/prep/finals")
async def add_finals(request: Request):
    """기말고사_녹음본에서 고른 녹음을 준비 목록에 넣음 (원본은 읽기만, 전사할 때 uploads/로 복사)."""
    b = await request.json()
    folder = finals_folder()
    want = [f for f in b.get("files") or [] if isinstance(f, str)]
    bad = [f for f in want if Path(f).name != f or not (folder / f).is_file()]
    if bad or not want:
        raise HTTPException(400, f"기말고사_녹음본에 없는 파일: {', '.join(bad)}" if bad else "녹음을 고르세요")
    with prep_lock:
        have = {Path(x["path"]).resolve() for x in prep_items()}
        new = [f for f in want if (folder / f).resolve() not in have]
    added = await asyncio.to_thread(lambda: [new_prep_item(f, "finals", folder / f) for f in new])
    with prep_lock:
        prep_save(prep_items() + added)
    publish_prep()
    return {**prep_state(), "added": len(added)}


def base_options():
    """Whisper 기본 옵션. 프롬프트는 전사를 시작할 때 과목 프롬프트 빌더(과목 관리 → 고급: 수동 지정)로 채움."""
    return {"prompt": "", "hotwords": "", "fix": {}, "language": "ko", "prompt_source": "none",
            "beam_size": max(1, min(10, int(C.settings().get("beam_size") or 5)))}


@app.post("/api/prep/start")
def start_prep(b: dict = Body(...)):
    """준비 목록에서 고른 파일을 대기열에 넣음. 과목을 알면 과목 프롬프트 빌더로 전사 (기말고사 원본은 uploads/로 복사)."""
    ids = [x for x in b.get("ids") or [] if isinstance(x, str)]
    base = base_options()
    with prep_lock:  # 목록에서 먼저 빼고 (파일 복사는 잠금 밖에서)
        items = prep_items()
        pick = [x for x in items if x["id"] in ids]
        if not pick:
            raise HTTPException(400, "전사할 파일을 고르세요")
        need = [x["filename"] for x in pick if not x.get("subject")]
        if need:
            raise HTTPException(400, f"과목을 고르세요: {', '.join(need)}")
        gone = [x["filename"] for x in pick if not Path(x["path"]).exists()]
        if gone:
            raise HTTPException(400, f"파일이 없습니다: {', '.join(gone)}")
        counts = glossary_counts()
        empty = sorted({x["subject"] for x in pick if needs_bootstrap(x, counts)})
        if empty:
            raise HTTPException(400, "용어집이 비어 있는 과목입니다: " + ", ".join(empty) + " — Claude Code에서 "
                                + ", ".join(f"/bootstrap-glossary {n}" for n in empty) + " 을 먼저 실행하거나 [그래도 전사]를 누르세요")
        prep_save([x for x in items if x["id"] not in ids])
    created, failed = [], []
    for x in pick:
        opts = dict(base, subject=None if x["subject"] == NO_SUBJECT else x["subject"])
        job = Job(filename=x["filename"], options=opts, duration=x.get("duration"))
        job.txt_name = unique_txt_name(safe_stem(job.filename))
        job.upload_path = UP_DIR / f"{job.id}{Path(x['filename']).suffix.lower()[:10]}"
        try:
            if x["source"] == "upload":
                Path(x["path"]).replace(job.upload_path)
            else:
                shutil.copyfile(x["path"], job.upload_path)  # 기말고사_녹음본 원본은 읽기만
        except OSError as e:
            failed.append(x)
            hub.publish("notice", {"message": f"{x['filename']}을(를) 준비하지 못했습니다: {e}"})
            continue
        job.upload = job.upload_path.name
        jobs[job.id] = job
        job.publish()
        job_queue.put(job)
        created.append(job.to_dict())
    if failed:  # 실패한 것은 준비 목록에 되돌림
        with prep_lock:
            prep_save(prep_items() + failed)
    publish_prep()
    return {"jobs": created, **prep_state()}


# ---------- 이어서 처리 (서버가 꺼져 끝나지 못한 대기열) ----------

def resumable():
    return sorted((j for j in jobs.values() if j.resume_pending), key=lambda j: j.created)


@app.post("/api/resume")
async def resume_jobs(request: Request):
    """action "resume": 끝나지 못한 작업을 처음부터 다시 대기열에 / "discard": 이어서 처리하지 않음 (녹음 복사본 삭제)."""
    b = await request.json()
    go = b.get("action") == "resume"
    n = 0
    for job in resumable():
        path = UP_DIR / job.upload if job.upload else None
        if go and path and path.exists():
            job.__dict__.update(status="queued", progress=0.0, segments=0, eta=None, started=None, finished=None,
                                error=None, restore=None, resume_pending=False)
            job.notices = ["서버가 꺼져 끝나지 못한 작업을 처음부터 다시 전사합니다"]
            job.cancel = threading.Event()
            job.upload_path = path
            job.publish()
            job_queue.put(job)
            n += 1
        else:
            if path:
                path.unlink(missing_ok=True)
            job.resume_pending, job.upload = False, None
            job.publish()
    hub.publish("resume", {"items": []})
    return {"ok": True, "resumed": n}


# ---------- 강의 목록 (메인 화면: 강의마다 다음 할 일 하나) ----------

@app.get("/api/awaiting")
def awaiting():
    """교정 대기 작업 (오래된 것부터). /correct는 작업마다 새 대화에서 하나씩 실행한다."""
    return {"items": W.awaiting_list()}


NEXT_ORDER = {"active": 0, "review": 1, "correct": 2, "error": 3, "complete": 4, "none": 5}


@app.get("/api/lectures")
def lectures():
    """강의별 진행: 전사 중(active) → 확인 필요(review) → /correct 대기(correct, 오래된 것부터) → 완료(complete, 최근 것부터).
    교정 작업(대체되지 않은 것)마다 한 줄, 교정 작업이 없는 전사(진행 중·실패·과목 없이)도 한 줄. 중간고사 샘플은 뺌."""
    rows, with_work = [], set()
    awaiting = {w["name"]: w for w in W.awaiting_list()}
    for w in W.list_works():
        if w.get("status") == "superseded":
            continue
        st = awaiting.get(w["name"]) or W.work_state(w["name"])
        sj = st.get("server_job")
        with_work.add(sj)
        rows.append({"kind": "work", "name": w["name"], "subject": w["subject"], "file": w["source_file"], "lecture": st["lecture"],
                     "duration": st["duration"], "transcribed": st["transcribed"], "next": st["next"] or "none",
                     "command": st["command"], "review": st.get("review"), "job": sj if sj in jobs else None,
                     "prev_part": st.get("prev_part"), "md_path": st.get("md_path"), "carried_manual": st.get("carried_manual")})
    for j in jobs.values():
        if j.id in with_work or (j.options or {}).get("sample") or j.work:
            continue
        nxt = "active" if j.status in ACTIVE else "error" if j.status in ("error", "interrupted", "cancelled") else "none"
        rows.append({"kind": "job", "name": Path(j.filename).stem, "subject": job_subject(j), "file": j.filename,
                     "lecture": C.lecture_key(j.filename), "duration": j.duration, "status": j.status, "progress": j.progress,
                     "restore": j.restore, "transcribed": datetime.fromtimestamp(j.created).isoformat(timespec="seconds"),
                     "next": nxt, "job": j.id})
    for r in rows:  # 같은 녹음을 다시 전사하는 중이면 위에 보이게
        if r["kind"] == "work" and r["job"] and jobs[r["job"]].status in ACTIVE:
            r["next"] = "active"
    todo = [r for r in rows if r["next"] not in ("complete", "none")]
    todo.sort(key=lambda r: (NEXT_ORDER.get(r["next"], 9), r["transcribed"] or ""))
    done = sorted((r for r in rows if r["next"] in ("complete", "none")), key=lambda r: r["transcribed"] or "", reverse=True)
    rows = todo + done
    return {"items": rows, "counts": {k: sum(1 for r in rows if r["next"] == k) for k in NEXT_ORDER}}


# ---------- 저장 공간 정리 (다시 만들 수 있는 파일만) ----------

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emf", ".wmf", ".webp"}
NEVER_DELETE = re.compile(r"^(whisper_raw\.jsonl|segments\.jsonl|edits.*\.json|result\.json|job\.json|manual\.json"
                          r"|.*\.m4a\.md|.*\.md)$", re.I)  # Whisper 원본·세그먼트·edits·교정본은 어떤 경우에도 지우지 않음


def _registered(subject, file):
    return C.deck_path(subject, file).exists() or C.ref_path(subject, file).exists()


def cleanup_files():
    """{key: [Path…]} — uploads: 준비 목록·대기열·이어서 처리에 쓰지 않는 업로드 잔여물,
    images: 등록을 마친 강의자료의 PNG·그림 캐시 (미리보기는 원본 PDF로 다시 만듦), backups: 파일별 최근 n개를 넘는 백업."""
    keep = {PREP_PATH.name, PREP_PATH.name + ".tmp"} | {Path(x["path"]).name for x in prep_items() if x["source"] == "upload"} \
        | {j.upload for j in jobs.values() if j.upload and (j.status in ACTIVE or j.resume_pending)}
    if current_job is not None:
        keep.add(f"{current_job.id}_16k.wav")
    out = {"uploads": [p for p in UP_DIR.iterdir() if p.is_file() and p.name not in keep], "images": [], "backups": []}
    for name in C.subject_names():
        cache = C.subject_dir(name) / "cache"
        if not cache.is_dir():
            continue
        for d in cache.iterdir():  # cache/<강의자료 파일명>/ (samples·bootstrap·book 폴더에는 그림이 없음)
            if d.is_dir() and d.name not in ("samples", "bootstrap", "book") and _registered(name, d.name):
                out["images"] += [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if C.settings().get("backup_prune_enabled"):
        out["backups"] = C.backups_to_prune()
    return out


def _size(paths):
    return sum(p.stat().st_size for p in paths if p.exists())


@app.get("/api/cleanup")
def cleanup_report():
    s = C.settings()
    f = cleanup_files()
    all_backups = [p for ps in C.backup_groups().values() for p in ps]
    return {"items": [
        {"key": "uploads", "label": "uploads/ 업로드 잔여물", "count": len(f["uploads"]), "bytes": _size(f["uploads"]),
         "note": "전사가 끝났거나 취소된 녹음 복사본·변환 wav. 준비 목록·대기열·이어서 처리할 녹음은 빼고 셉니다."},
        {"key": "images", "label": "강의자료 PNG·그림 캐시", "count": len(f["images"]), "bytes": _size(f["images"]),
         "note": "등록을 마친 강의자료만. 슬라이드 미리보기는 원본 PDF에서 다시 만듭니다 (텍스트·OCR 캐시는 남김)."},
        {"key": "backups", "label": f"backups/ 오래된 백업 (파일별 최근 {s['backup_keep_per_file']}개만 남김)",
         "count": len(f["backups"]), "bytes": _size(f["backups"]), "enabled": bool(s.get("backup_prune_enabled")),
         "note": (f"전체 백업 {len(all_backups)}개 ({_size(all_backups) / 1048576:.1f}MB)"
                  + ("" if s.get("backup_prune_enabled") else " · 꺼져 있음: data/settings.json의 backup_prune_enabled를 true로 바꾸면 정리합니다"))},
    ], "protected": "Whisper 원본(whisper_raw.jsonl·segments.jsonl), edits, 교정 결과, 과목 폴더의 교정본 md, 보관한 녹음은 지우지 않습니다."}


@app.post("/api/cleanup")
async def cleanup(request: Request):
    b = await request.json()
    want = [k for k in b.get("targets") or [] if k in ("uploads", "images", "backups")]
    if not want:
        raise HTTPException(400, "정리할 항목을 고르세요")
    roots = {"uploads": UP_DIR.resolve(), "images": C.DATA.resolve(), "backups": C.BACKUPS.resolve()}
    f = cleanup_files()
    done = {}
    for k in want:
        n = size = 0
        for p in f[k]:
            rp = p.resolve()
            if NEVER_DELETE.match(p.name) or not rp.is_relative_to(roots[k]) or rp.is_relative_to(W.JOBS.resolve()):
                continue  # 혹시라도 보호 대상이면 건너뜀
            try:
                sz = p.stat().st_size
                p.unlink()
                n, size = n + 1, size + sz
            except OSError:
                pass
        done[k] = {"count": n, "bytes": size}
    return {"ok": True, "done": done, **cleanup_report()}


@app.get("/api/jobs")
def list_jobs(limit: int = 50):
    js = sorted(jobs.values(), key=lambda j: j.created, reverse=True)[:limit]
    return {"jobs": [j.to_dict() for j in js]}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    job = get_job(job_id)
    segs = job.read_segments()
    return {"job": job.to_dict(), "segments": segs, "lines": [T.line_of(s) for s in shown(segs)]}


def _audio_response(path):
    if not path or not Path(path).exists():
        raise HTTPException(404, "녹음 파일이 없습니다 (이전 버전에서 전사했거나 기록을 지웠습니다)")
    return FileResponse(path, media_type=AUDIO_TYPES.get(Path(path).suffix.lower(), "application/octet-stream"))


@app.get("/api/jobs/{job_id}/audio")
def job_audio(job_id: str):
    return _audio_response(get_job(job_id).audio_path)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = get_job(job_id)
    if job.status in ACTIVE:
        job.cancel.set()
        if job.status == "queued":
            job.status, job.finished = "cancelled", time.time()
        job.publish()
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """기록 목록에서만 지움 (outputs/의 txt·srt는 남김)."""
    job = get_job(job_id)
    if job.status in ACTIVE:
        raise HTTPException(409, "진행 중인 작업은 먼저 취소하세요")
    jobs.pop(job_id)
    paths = [JOB_DIR / f"{job_id}.json", job.jsonl_path, job.raw_path, job.restore_path]
    if job.audio_path and not job.work:  # 교정 작업이 쓰는 녹음은 남김
        paths.append(job.audio_path)
    for p in paths:
        p.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, fmt: str = "txt"):
    job = get_job(job_id)
    path = job.srt_path if fmt == "srt" else job.txt_path  # txt는 전사 중에도 실시간으로 기록됨
    if fmt == "srt" and job.jsonl_path.exists():
        T.write_srt(shown(job.read_segments()), str(path))
    if not path.exists():
        raise HTTPException(404, "결과 파일이 없습니다")
    return FileResponse(path, filename=path.name, media_type="text/plain; charset=utf-8")


@app.post("/api/jobs/{job_id}/replace")
async def replace_in_job(job_id: str, request: Request):
    job = get_job(job_id)
    if job.status in ACTIVE:
        raise HTTPException(409, "전사가 끝난 뒤에 바꿀 수 있습니다")
    body = await request.json()
    find, repl = body.get("find", ""), body.get("replace", "")
    if not find:
        raise HTTPException(400, "찾을 단어를 입력하세요")
    segs, count = job.read_segments(), 0
    for s in segs:
        count += s["text"].count(find)
        s["text"] = s["text"].replace(find, repl)
    with open(job.jsonl_path, "w", encoding="utf-8") as f:
        for s in segs:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    T.write_txt(shown(segs), str(job.txt_path))
    T.write_srt(shown(segs), str(job.srt_path))
    return {"count": count, "lines": [T.line_of(s) for s in shown(segs)]}


# ---------- 교정 작업 ----------

def get_work_or_404(name):
    j = W.load_job(name)
    if not j:
        raise HTTPException(404, f"교정 작업이 없습니다: {name}")
    return j


@app.post("/api/jobs/{job_id}/work")
async def create_work(job_id: str, request: Request):
    """이미 끝난 전사로 교정 작업 폴더 만들기 (파일명으로 과목을 못 찾으면 화면에서 고름)."""
    job = get_job(job_id)
    if job.status in ACTIVE:
        raise HTTPException(409, "전사가 끝난 뒤에 만들 수 있습니다")
    b = await request.json()
    subject = (b.get("subject") or "").strip() or None
    if subject:
        get_subject_or_404(subject)
    if job.work and W.load_job(job.work) and not b.get("recreate"):
        return W.work_state(job.work)
    try:
        if not make_work(job, subject):
            raise HTTPException(400, "파일명에서 과목을 알 수 없습니다. 과목을 고르세요")
    except ValueError as e:
        raise HTTPException(400, str(e))
    job.publish()
    return W.work_state(job.work)


@app.get("/api/work/{name}")
def work_detail(name: str):
    j = get_work_or_404(name)
    r = C.load_json(W.work_dir(name) / "result.json") or W.base_result(name)
    rs = C.load_json(W.work_dir(name) / "restore.json") or {}
    s = C.settings()
    nb = C.root() / j["subject"] / s["midterm_dir"] / s["transcripts_dir"] / f"{j['source_file']}.md"
    return {"state": W.work_state(name), "job": j, "result": r, "manual": W.load_manual(name),
            "review": W.review_state(name, r), "notebooklm_existing": str(nb) if nb.exists() else None,
            "restore_regions": rs.get("regions") or [],
            "handwritten": bool(C.subject(j["subject"]).get("handwritten"))}


@app.get("/api/work/{name}/audio")
def work_audio(name: str):
    return _audio_response(get_work_or_404(name).get("audio"))


@app.post("/api/work/{name}/manual")
async def work_manual(name: str, request: Request):
    """직접 듣고 입력한 문장 저장 (text가 비면 지움). transcript.txt와, 교정 결과가 있으면 과목 폴더의 md도 다시 만든다."""
    get_work_or_404(name)
    b = await request.json()
    try:
        items = W.set_manual(name, int(b.get("i")), b.get("text") or "")
    except FileExistsError as e:
        raise HTTPException(409, f"같은 이름의 md가 이미 있어 저장하지 못했습니다: {e}")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "manual": items}


@app.post("/api/work/{name}/revert")
async def work_revert(name: str, request: Request):
    """문장 하나를 원본으로 되돌리기 (revert=false면 교정본으로 다시). 과목 폴더의 md도 다시 조립."""
    get_work_or_404(name)
    b = await request.json()
    try:
        W.set_reverted(name, int(b.get("i")), bool(b.get("revert", True)))
    except FileExistsError as e:
        raise HTTPException(409, f"같은 이름의 md가 이미 있어 저장하지 못했습니다: {e}")
    except (FileNotFoundError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "reverted": C.load_json(W.work_dir(name) / "result.json")["reverted"]}


@app.post("/api/work/{name}/review")
async def work_review(name: str, request: Request):
    """확인 목록 항목: {"id", "status": "kept"(그대로 둠) | "open"} 또는 {"complete": true|false} ([완료] / 취소)."""
    get_work_or_404(name)
    b = await request.json()
    try:
        if "complete" in b:
            rs = W.complete_review(name, bool(b["complete"]))
        else:
            rs = W.set_review_item(name, str(b.get("id") or ""), str(b.get("status") or ""))
    except FileExistsError as e:
        raise HTTPException(409, f"같은 이름의 md가 이미 있어 저장하지 못했습니다: {e}")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    hub.publish("work", W.work_state(name))
    return {"ok": True, "review": rs}


def _deck_or_404(name, file):
    get_subject_or_404(name)
    d = C.load_json(C.deck_path(name, file))
    if not d:
        raise HTTPException(404, f"등록되지 않은 강의자료: {file}")
    return d


@app.get("/api/subjects/{name}/decks/{file}/pages/{page}")
def deck_page(name: str, file: str, page: int):
    """슬라이드 미리보기용 페이지 정보 (제목·요약·용어, 그림을 만들 수 있는지)."""
    d = _deck_or_404(name, file)
    p = next((x for x in d["pages"] if x["page"] == page), None)
    if not p:
        raise HTTPException(404, f"{file}에 {page}쪽이 없습니다")
    return {"file": file, "deck_title": d.get("title"), "pages_total": d.get("pages_total"), **p,
            "png": str(d.get("source", "")).lower().endswith(".pdf")}


@app.get("/api/subjects/{name}/decks/{file}/png/{page}")
def deck_page_png(name: str, file: str, page: int):
    """PDF 페이지 PNG (캐시에 있으면 그대로, 없으면 원본 PDF를 읽기 전용으로 열어 렌더링)."""
    d = _deck_or_404(name, file)
    cache = C.cache_dir(name, file)
    for p in (cache / f"p{page:03d}.png", cache / "view" / f"p{page:03d}.png"):
        if p.exists():
            return FileResponse(p, media_type="image/png")
    src = C.root() / str(d.get("source", ""))
    if src.suffix.lower() != ".pdf" or not src.exists():
        raise HTTPException(404, "미리보기 그림을 만들 수 없습니다")
    import pymupdf
    with pymupdf.open(src) as doc:
        if not 1 <= page <= doc.page_count:
            raise HTTPException(404, f"{page}쪽이 없습니다")
        pg = doc[page - 1]
        zoom = C.settings()["render_long_side"] / max(pg.rect.width, pg.rect.height)
        out = cache / "view" / f"p{page:03d}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(out)
    return FileResponse(out, media_type="image/png")


# ---------- 과목 ----------

SUBJECT_STR = ("summary", "language")
SUBJECT_BOOL = ("has_slides", "handwritten", "keep_code_identifiers", "prefer_english_terms")
SUBJECT_LIST = ("rules", "reference_dirs", "ignore_dirs", "ignore_files", "professor_phrases", "term_stopwords")


def _str_list(v):
    return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def get_subject_or_404(name):
    try:
        return C.subject(name)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/subjects")
def list_subjects():
    return {"settings": C.settings(),
            "templates": [{"id": k, "label": v["label"], "summary": v["summary"]} for k, v in C.SUBJECT_TEMPLATES.items()],
            "subjects": [{"name": n, "rules": C.subject(n), "status": subject_status(n)} for n in C.subject_names()]}


@app.post("/api/subjects")
async def post_subject(request: Request):
    b = await request.json()
    try:
        rules = C.create_subject(b.get("name"), b.get("template") or "korean")
    except ValueError as e:
        raise HTTPException(400, str(e))
    name = str(b.get("name")).strip()
    return {"ok": True, "name": name, "rules": rules, "status": subject_status(name)}


@app.delete("/api/subjects/{name}")
def del_subject(name: str):
    try:
        moved = C.delete_subject(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "backup": str(moved) if moved else None}


def subject_status(name):
    """과목 카드 상태 + 다시 교정할 작업(/recorrect), 중간고사 샘플 수, /bootstrap-glossary 명령."""
    st = C.subject_status(name)
    try:
        st["recorrect"] = RC.candidates(name)
    except Exception:
        traceback.print_exc()
        st["recorrect"] = []
    st["recorrect_command"] = f"/recorrect {name}"
    st["samples"] = len(list((C.subject_dir(name) / "cache" / "samples").glob("*.json")))
    st["bootstrap_command"] = f"/bootstrap-glossary {name}"
    return st


@app.put("/api/subjects/{name}")
async def put_subject(name: str, request: Request):
    cur = get_subject_or_404(name)
    b = await request.json()
    for k in SUBJECT_STR:
        if k in b:
            cur[k] = str(b[k] or "").strip()
    for k in SUBJECT_BOOL:
        if k in b:
            cur[k] = bool(b[k])
    for k in SUBJECT_LIST:
        if k in b:
            cur[k] = _str_list(b[k])
    if "books" in b:
        cur["books"] = [{"path": x["path"].strip(), "note": str(x.get("note") or "").strip()}
                        for x in b["books"] if isinstance(x, dict) and str(x.get("path") or "").strip()]
    if "notation" in b:
        cur["notation"] = [{"spoken": x["spoken"].strip(), "written": str(x.get("written") or "").strip()}
                           for x in b["notation"] if isinstance(x, dict) and str(x.get("spoken") or "").strip()]
    if isinstance(b.get("manual_prompt"), dict):  # 고급: 프롬프트 빌더 대신 수동 지정
        mp = b["manual_prompt"]
        cur["manual_prompt"] = {"enabled": bool(mp.get("enabled")), "prompt": str(mp.get("prompt") or "").strip(),
                                "hotwords": " ".join(str(mp.get("hotwords") or "").split())}
    subs = C.subjects()
    subs[name] = cur
    C.save_json(C.SUBJECTS_PATH, subs)
    return {"ok": True, "rules": cur, "status": subject_status(name)}


@app.get("/api/subjects/{name}/glossary")
def subject_glossary(name: str):
    get_subject_or_404(name)
    d = C.subject_dir(name)
    terms = C.load_json(d / "glossary.json", {"terms": []})["terms"]
    terms.sort(key=lambda t: (-t.get("priority", 3), -t.get("freq", 0), t["term"].lower()))
    fix = C.load_json(d / "fix.json", {"rules": []})["rules"]
    warn = {(x["wrong"], x["right"]): x["warnings"] for x in RV.fix_warnings(name)}
    return {"terms": terms, "fix": [{**r, "warnings": warn.get((r["wrong"], r["right"]), [])} for r in fix]}


@app.get("/api/subjects/{name}/prompt")
def subject_prompt(name: str):
    """프롬프트 빌더 미리보기: 실제로 들어갈 initial_prompt·hotwords, 토큰 수, 잘려 나간 항목."""
    get_subject_or_404(name)
    return P.build(name)


@app.get("/api/subjects/{name}/suggestions")
def subject_suggestions(name: str):
    """승인 대기 제안. 용어는 덱별로 묶어 보여 주므로 중요도순으로 정렬해서 보냄."""
    get_subject_or_404(name)
    items = [s for s in C.load_json(C.subject_dir(name) / "suggestions.json", {"items": []})["items"]
             if s.get("status") == "pending"]
    order = {"term": 0, "fix_rule": 1, "notation": 2, "progress": 3}
    items.sort(key=lambda s: (order.get(s["type"], 9), not s["data"].get("recommended"),
                              -(s["data"].get("score") or 0), str(s["data"].get("term", s["data"].get("wrong", ""))).lower()))
    RV.analyze_pending(name, items)  # 교정 규칙: 기존 전사본에서 바뀌는 문장 수·예시, 경고(조사·2글자 이하·번역 규칙)
    return {"items": items}


@app.post("/api/subjects/{name}/suggestions/resolve")
async def resolve_suggestions(name: str, request: Request):
    get_subject_or_404(name)
    b = await request.json()
    ids = lambda k: [x for x in b.get(k) or [] if isinstance(x, str)]
    changes = {k: v for k, v in (b.get("changes") or {}).items() if isinstance(v, dict)}
    done = C.resolve_suggestions(name, approve=ids("approve"), reject=ids("reject"), changes=changes, by="user")
    A.auto_resolve(name)  # 용어를 승인하면 그 용어를 바른 쪽으로 하는 교정 규칙이 자동 승인 대상이 될 수 있음
    return {"ok": True, **done, "status": subject_status(name)}


@app.get("/api/subjects/{name}/changes")
def subject_changes(name: str, limit: int = 300):
    """변경 기록 (최근 것부터): 자동 승인·직접 승인·거절, 되돌린 시각."""
    get_subject_or_404(name)
    return {"items": C.list_changes(name, limit)}


@app.post("/api/subjects/{name}/changes/{cid}/undo")
def undo_change(name: str, cid: str):
    get_subject_or_404(name)
    try:
        e = C.undo_change(name, cid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "change": e, "status": subject_status(name)}


# ---------- 중간고사 샘플 전사 (/bootstrap-glossary용) ----------

def midterm_recordings(name):
    """중간고사_녹음본에서 이 과목 녹음 [{file, size, notebooklm, sample, job}] (파일명 <과목>_<YYMMDD>[_NN])."""
    s = C.settings()
    rec = C.root() / s["midterm_recordings"]
    tr = C.root() / name / s["midterm_dir"] / s["transcripts_dir"]
    running = {j.filename: j for j in jobs.values() if (j.options or {}).get("sample") and j.status in ACTIVE}
    out = []
    if rec.is_dir():
        for p in sorted(rec.iterdir()):
            if p.is_file() and p.suffix.lower() in AUDIO_TYPES and C.subject_from_filename(p.name) == name:
                sp = sample_path(name, p.name)
                j = running.get(p.name)
                out.append({"file": p.name, "size": p.stat().st_size, "notebooklm": (tr / f"{p.name}.md").exists(),
                            "sample": (C.load_json(sp) or {}).get("created") if sp.exists() else None,
                            "job": j.id if j else None, "job_status": j.status if j else None})
    return out


def default_samples(recs, n):
    """기본 선택: 이미 샘플이 있으면 그것, 모자라면 NotebookLM 전사본이 있는 녹음 중 고르게 떨어진 것."""
    pick = [r["file"] for r in recs if r["sample"]][:n]
    pool = [r["file"] for r in recs if r["notebooklm"] and r["file"] not in pick] or \
           [r["file"] for r in recs if r["file"] not in pick]
    k = n - len(pick)
    if k > 0 and pool:
        idx = sorted({round(i * (len(pool) - 1) / max(1, k - 1)) for i in range(k)}) if k > 1 else [len(pool) // 2]
        pick += [pool[i] for i in idx]
    return pick[:n]


@app.get("/api/subjects/{name}/samples")
def list_samples(name: str):
    get_subject_or_404(name)
    recs = midterm_recordings(name)
    n = C.settings()["midterm_sample_count"]
    return {"recordings": recs, "default": default_samples(recs, n), "count": n,
            "folder": str(C.root() / C.settings()["midterm_recordings"])}


@app.post("/api/subjects/{name}/samples")
async def start_samples(name: str, request: Request):
    """고른 중간고사 녹음을 Whisper만 돌림 (원본은 읽기만: uploads/로 복사해서 전사, 복원·교정 작업 없음)."""
    sub = get_subject_or_404(name)
    b = await request.json()
    want = [f for f in b.get("files") or [] if isinstance(f, str)]
    recs = {r["file"]: r for r in midterm_recordings(name)}
    bad = [f for f in want if f not in recs]
    if bad or not want:
        raise HTTPException(400, f"중간고사 녹음이 아닙니다: {', '.join(bad)}" if bad else "녹음을 고르세요")
    folder = C.root() / C.settings()["midterm_recordings"]
    base = {**base_options(), "language": sub.get("language") or "ko"}
    created = []
    for f in want:
        if recs[f]["job"]:
            continue  # 이미 대기열에 있음
        o = {**PL.builder_options(name, base), "sample": "midterm"}
        job = Job(filename=f, options=o)
        job.txt_name = unique_txt_name(safe_stem(f) + "_샘플")
        job.upload_path = UP_DIR / f"{job.id}{Path(f).suffix.lower()[:10]}"
        shutil.copyfile(folder / f, job.upload_path)  # 원본은 건드리지 않음 (전사가 끝나면 복사본만 지움)
        job.upload = job.upload_path.name
        jobs[job.id] = job
        job.publish()
        job_queue.put(job)
        created.append(job.to_dict())
    return {"jobs": created}


@app.post("/api/work/{name}/notebooklm")
async def upload_notebooklm(name: str, file: UploadFile = File(...)):
    """/crosscheck용 NotebookLM 전사본을 jobs/<작업이름>/notebooklm.md로 저장 (과목 폴더에는 쓰지 않음)."""
    get_work_or_404(name)
    data = await file.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "UTF-8 텍스트(md) 파일만 올릴 수 있습니다")
    if len(text.strip()) < 200:
        raise HTTPException(400, "내용이 너무 짧습니다")
    out = W.work_dir(name) / "notebooklm.md"
    if out.exists():
        C.backup_file(out, f"{name}_notebooklm")
    out.write_text(text, encoding="utf-8")
    return {"ok": True, "path": f"jobs/{name}/notebooklm.md", "command": W.crosscheck_command(name), "chars": len(text)}


@app.put("/api/settings")
async def put_settings(request: Request):
    b = await request.json()
    s = C.load_json(C.SETTINGS_PATH, {}) or {}
    if "root" in b:
        r = str(b["root"] or "").strip()
        try:
            if not r:
                raise OSError("경로가 비어 있습니다")
            Path(r).mkdir(parents=True, exist_ok=True)  # 없으면 만듦
        except OSError as e:
            raise HTTPException(400, f"폴더를 쓸 수 없습니다: {r} ({e})")
        s["root"] = r
    if "beam_size" in b:
        s["beam_size"] = max(1, min(10, int(b["beam_size"] or 5)))
    C.save_json(C.SETTINGS_PATH, {**C.DEFAULT_SETTINGS, **s})
    return {"ok": True, "settings": C.settings()}


def main():
    with socket.socket() as s:  # 이미 실행 중이면 브라우저만 열기
        if s.connect_ex((HOST, PORT)) == 0:
            print(f"이미 실행 중입니다 → http://{HOST}:{PORT}")
            webbrowser.open(f"http://{HOST}:{PORT}")
            return
    print(f"강의 전사 서버: http://{HOST}:{PORT}  (종료: Ctrl+C)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
