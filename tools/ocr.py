"""그림 속 글자 OCR (RapidOCR, 무료·로컬 CPU).

모델(검출·방향·한국어 인식)은 .venv의 rapidocr/models에 이미 받아져 있어 인터넷 없이 동작한다.
OCR 결과는 잡음이 섞인 텍스트다 (예: 광합성 → 광합셩). 용어로 쓸 때는 슬라이드 텍스트·문맥으로 확인할 것.
"""
import logging, re

_engine = None


def engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR, LangRec, OCRVersion, ModelType
        _engine = RapidOCR(params={
            "Global.log_level": "error",
            "Global.use_cls": False,  # 방향 분류기가 정상 한국어 줄을 뒤집힌 것으로 오판하는 문제 방지
            "Rec.lang_type": LangRec.KOREAN,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
        })
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    return _engine


def read(image):
    """이미지(경로·PNG bytes·numpy 배열) → (텍스트, 글자 수(공백 제외), 평균 신뢰도 0~1)."""
    r = engine()(image)
    txts, scores = list(r.txts or []), list(r.scores or [])
    text = "\n".join(t.strip() for t in txts if t.strip())
    chars = len(re.sub(r"\s+", "", text))
    score = round(sum(scores) / len(scores), 3) if scores else 0.0
    return text, chars, score
