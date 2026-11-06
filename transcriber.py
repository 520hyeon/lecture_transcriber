"""강의 녹음 전사 엔진 (웹 서버와 독립적으로 동작).

단독 실행 (복원·교정 작업 없이 Whisper만):
    python transcriber.py 과목명_261028.m4a            # 파일명으로 과목을 알면 과목 프롬프트 빌더로 전사
    python transcriber.py 녹음.m4a --subject 과목명 --out 결과.txt
"""
import os, site, time, wave, gc, json, shutil, subprocess

# --- Windows에서 pip로 설치한 CUDA DLL 경로 등록 (반드시 faster_whisper import보다 먼저) ---
for p in site.getsitepackages():
    for sub in ("nvidia\\cublas\\bin", "nvidia\\cudnn\\bin"):
        d = os.path.join(p, sub)
        if os.path.isdir(d):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]

import numpy as np
from faster_whisper import WhisperModel

SR = 16000
MODEL_NAME = "large-v3"
DEFAULT_COMPUTE = "float16"
FALLBACK_COMPUTE = "int8_float16"  # VRAM 부족 시

FFMPEG_HELP = (
    "ffmpeg를 찾을 수 없습니다. PowerShell에서 `winget install Gyan.FFmpeg` 를 실행해 설치한 뒤 "
    "run.bat을 다시 실행하세요. (수동 설치: https://www.gyan.dev/ffmpeg/builds/ 에서 받아 bin 폴더를 PATH에 추가)"
)


class Cancelled(Exception):
    pass


# ---------- 오디오 ----------

def find_ffmpeg():
    return shutil.which("ffmpeg")


def load_wav(path):
    """16kHz 모노 16bit wav를 float32 배열로 읽기 (av 라이브러리를 쓰지 않음)."""
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "wav는 16kHz, 모노, 16bit여야 합니다"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def _is_whisper_wav(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
    except Exception:
        return False


def convert_to_wav(src, dst, cancel=None):
    """ffmpeg로 16kHz 모노 16bit wav 변환. cancel(threading.Event)이 켜지면 중단."""
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError(FFMPEG_HELP)
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", dst]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    while True:
        try:
            _, err = p.communicate(timeout=0.3)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                p.kill()
                p.communicate()
                raise Cancelled()
    if p.returncode != 0:
        msg = err.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("ffmpeg 변환 실패: " + (msg[-1] if msg else f"코드 {p.returncode}"))


def prepare_audio(src, tmp_wav, cancel=None):
    """아무 오디오/영상 파일 → float32 16kHz 배열. 이미 맞는 wav면 변환 생략."""
    if src.lower().endswith(".wav") and _is_whisper_wav(src):
        return load_wav(src)
    convert_to_wav(src, tmp_wav, cancel)
    return load_wav(tmp_wav)


def filter_audio(audio, af):
    """float32 16kHz 배열에 ffmpeg 필터(-af)를 적용 (재전사 전처리: highpass, afftdn, dynaudnorm). 파일을 만들지 않음."""
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError(FFMPEG_HELP)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
           "-af", af, "-f", "s16le", "-ar", str(SR), "-ac", "1", "pipe:1"]
    p = subprocess.run(cmd, input=pcm, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("ffmpeg 전처리 실패: " + (msg[-1] if msg else f"코드 {p.returncode}"))
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# ---------- 텍스트 ----------

def fmt_ts(sec):
    s = int(sec)
    h, m, ss = s // 3600, s % 3600 // 60, s % 60
    return f"[{h}:{m:02d}:{ss:02d}]" if h else f"[{m:02d}:{ss:02d}]"


def srt_ts(sec):
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def apply_fix(text, fix):
    for a, b in (fix or {}).items():
        if a:
            text = text.replace(a, b)
    return text


def line_of(seg):
    return f"{fmt_ts(seg['start'])} {seg['text']}"


def write_srt(segments, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(segments, 1):
            f.write(f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n{s['text']}\n\n")


def write_txt(segments, path):
    with open(path, "w", encoding="utf-8") as f:
        for s in segments:
            f.write(line_of(s) + "\n")


class ResultWriter:
    """세그먼트가 나올 때마다 txt(기존 형식)와 jsonl(시각 정보 포함)에 바로 기록."""

    def __init__(self, txt_path, jsonl_path=None):
        self.txt = open(txt_path, "w", encoding="utf-8")
        self.jsonl = open(jsonl_path, "w", encoding="utf-8") if jsonl_path else None

    def write(self, seg):
        self.txt.write(line_of(seg) + "\n")
        self.txt.flush()
        if self.jsonl:
            self.jsonl.write(json.dumps(seg, ensure_ascii=False) + "\n")
            self.jsonl.flush()

    def close(self):
        self.txt.close()
        if self.jsonl:
            self.jsonl.close()


# ---------- 모델 ----------

def is_oom(e):
    s = str(e).lower()
    return "out of memory" in s or "cublas_status_alloc_failed" in s or "cudaerrormemoryallocation" in s


class Transcriber:
    """WhisperModel을 하나만 들고 재사용. OOM이면 int8_float16으로 다시 로드."""

    def __init__(self, on_notice=None):
        self.model = None
        self.compute_type = None
        self.on_notice = on_notice or (lambda msg: print("[알림]", msg))

    def unload(self):
        if self.model is not None:
            try:
                self.model.model.unload_model()
            except Exception:
                pass
            self.model = None
            gc.collect()

    def load(self, compute_type=DEFAULT_COMPUTE):
        self.unload()
        try:
            self.model = WhisperModel(MODEL_NAME, device="cuda", compute_type=compute_type)
        except Exception as e:
            if compute_type != FALLBACK_COMPUTE and is_oom(e):
                self.on_notice(f"GPU 메모리 부족으로 {compute_type} 로드 실패 → {FALLBACK_COMPUTE}로 다시 로드합니다.")
                return self.load(FALLBACK_COMPUTE)
            raise
        self.compute_type = compute_type
        return compute_type

    def transcribe(self, audio, language="ko", beam_size=5, initial_prompt=None, hotwords=None, fix=None,
                   vad=True, no_speech_threshold=0.5):
        """세그먼트 dict {idx, start, end, text}를 하나씩 내보내는 제너레이터.
        도중에 OOM이 나면 int8_float16으로 다시 로드하고, 마지막 세그먼트 끝 시각부터 이어서 전사.
        재전사(복원)에서는 vad=False, no_speech_threshold를 높여(완화) 작은 소리도 받아 적게 한다."""
        if self.model is None:
            self.load()
        offset, idx = 0.0, 0
        while True:
            try:
                segs, _info = self.model.transcribe(
                    audio[int(offset * SR):],
                    language=language or None,
                    beam_size=beam_size,
                    best_of=5,
                    temperature=[0.0, 0.2, 0.4],
                    vad_filter=vad,
                    vad_parameters=dict(min_silence_duration_ms=500) if vad else None,
                    condition_on_previous_text=False,
                    repetition_penalty=1.1,
                    no_speech_threshold=no_speech_threshold,
                    initial_prompt=initial_prompt or None,
                    hotwords=hotwords or None,
                )
                for s in segs:
                    text = apply_fix(s.text.strip(), fix)
                    seg = {"idx": idx, "start": round(s.start + offset, 2), "end": round(s.end + offset, 2), "text": text,
                           "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                           "compression_ratio": round(s.compression_ratio, 2)}
                    resume_at = seg["end"]
                    idx += 1
                    yield seg
                return
            except Exception as e:
                if self.compute_type == FALLBACK_COMPUTE or not is_oom(e):
                    raise
                offset = resume_at if idx else 0.0
                self.on_notice(f"전사 중 GPU 메모리 부족 → {FALLBACK_COMPUTE}로 모델을 다시 로드해 "
                               f"{fmt_ts(offset)}부터 이어서 전사합니다.")
                self.load(FALLBACK_COMPUTE)


# ---------- CLI ----------

def _cli():
    import argparse, tempfile
    ap = argparse.ArgumentParser(description="강의 녹음 전사 (웹 없이)")
    ap.add_argument("file")
    ap.add_argument("--subject", help="과목 (기본: 파일명 <과목>_<YYMMDD>에서 인식, 모르면 프롬프트 없이)")
    ap.add_argument("--out", help="결과 txt 경로 (기본: 원본 옆에 _whisper.txt)")
    args = ap.parse_args()

    from tools import common as C
    import pipeline as PL
    subject = args.subject or C.subject_from_filename(args.file)
    pr = PL.builder_options(subject, {"beam_size": C.settings().get("beam_size")}) if subject else {}
    print(f"Whisper 프롬프트: {'과목 ' + subject if subject else '없음 (과목을 모름)'}")
    out = args.out or os.path.splitext(args.file)[0] + "_whisper.txt"

    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        audio = prepare_audio(args.file, os.path.join(td, "a.wav"))
    dur = len(audio) / SR
    print(f"오디오 로드 완료 ({dur/60:.1f}분 분량)")

    tr = Transcriber()
    print(f"모델 로드 완료: {tr.load()} ({time.time()-t0:.0f}초)")

    w = ResultWriter(out)
    try:
        for seg in tr.transcribe(audio, pr.get("language") or "ko", int(pr.get("beam_size") or 5),
                                 pr.get("prompt"), pr.get("hotwords"), pr.get("fix")):
            w.write(seg)
            print(f"{seg['end']/dur*100:5.1f}%  {line_of(seg)}")
    finally:
        w.close()
    print(f"완료 ({time.time()-t0:.0f}초). 저장 위치: {out}")


if __name__ == "__main__":
    _cli()
