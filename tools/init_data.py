"""data/ 폴더 초기화 (서버를 켤 때도 자동 실행됨). 이미 있는 파일은 건드리지 않는다.

    python tools/init_data.py

"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

if __name__ == "__main__":
    C.utf8_stdout()
    C.ensure_data()
    for name in C.subject_names():
        st = C.subject_status(name)
        print(f"{name}: 폴더 {'있음' if st['root_ok'] else '없음'} · 강의자료 {len(st['materials'])}개 · "
              f"용어 {st['glossary_count']} · 교정 규칙 {st['fix_count']} · 제안 {st['pending_suggestions']}")
