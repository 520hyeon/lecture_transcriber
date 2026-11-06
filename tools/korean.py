"""한국어 사전 확인 (무료 로컬 형태소 분석기 kiwipiepy). 자동 승인(tools/auto.py)이 씀.

    python tools/korean.py 광합성 포토신세시스 …     낱말마다 사전에 있는지 출력

dictionary_word(낱말) → True(사전에 있는 실제 낱말) / False(사전에 없음) / None(분석기를 쓸 수 없음 → 호출하는 쪽이 안전하게 처리)
"""
import sys
from functools import lru_cache

_kiwi = None
_error = None


def analyzer():
    """Kiwi 인스턴스 (처음 한 번만 만듦). 설치돼 있지 않거나 모델을 못 읽으면 None."""
    global _kiwi, _error
    if _kiwi is None and _error is None:
        try:
            from kiwipiepy import Kiwi
            _kiwi = Kiwi()
        except Exception as e:  # 패키지 없음, 모델 파일 없음 등
            _error = f"{type(e).__name__}: {e}"
    return _kiwi


def unavailable_reason():
    analyzer()
    return _error


@lru_cache(maxsize=4096)
def dictionary_word(word):
    """낱말(띄어쓰기 포함 가능)이 사전에 있는 실제 낱말들로만 이루어졌는지. 분석기가 없으면 None."""
    kiwi = analyzer()
    if kiwi is None:
        return None
    parts = [w for w in str(word).split() if w]
    if not parts:
        return False
    return all(_known(kiwi, w) for w in parts)


def _known(kiwi, w):
    """형태소로 나눴을 때 사전에 없는 형태소(oov)가 하나도 없으면 실제 낱말.
    광합성·최대값(최대+값)은 True, 포토신세시스·광학성처럼 사전에 없는 낱말은 False."""
    toks = kiwi.tokenize(w)
    return bool(toks) and not any(t.oov for t in toks)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if analyzer() is None:
        sys.exit(f"형태소 분석기를 쓸 수 없습니다 ({_error}) → 한국어끼리 바꾸는 교정 규칙은 모두 사람 확인으로 분류됩니다")
    for w in sys.argv[1:]:
        print(f"{w}\t{'사전에 있음' if dictionary_word(w) else '사전에 없음'}")


if __name__ == "__main__":
    main()
