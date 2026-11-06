"""강의자료 인덱스(decks/refs) → 용어 제안 후보 (check_deck.py와 server.py가 같이 씀).

1. 거르기
   - figure_terms는 제안에 넣지 않는다. 예외: 영어·코드 표기의 짧은 명사형 용어 (cell, gene, getName())
   - 문장·구절, 숫자·주소, 표·그림 캡션, "예시:사과" 같은 예시 라벨, code_names에 있는 이름 제외
   - 과목별 제외 단어(subjects.json "term_stopwords")와 같으면 제외
2. 묶기 (대표 표기 + 별칭)
   - "광합성(photosynthesis)" → 짝으로 보고 대표 표기 하나 + 별칭
     대표: 코드 식별자 유지 과목이면 코드 키워드·코드 표기, 영어 용어 과목(prefer_english_terms)이면 영어, 그 밖에는 한국어(슬라이드 표기)
   - 대소문자·하이픈·끝의 "()"만 다른 표기는 같은 용어 (print / print())
   - 제외 단어를 앞뒤에 붙인 표기는 별칭 (개념 광합성 → 광합성)
3. 중요도
   - 나온 페이지 수 + 페이지 제목(덱 제목 포함)에 나오면 가산
   - 여러 페이지(2쪽 이상)에 나오거나 페이지 제목에 나온 용어는 기본 체크(recommended)
"""
import re

import common as C

CODE_KEYWORDS = set("""abstract and as assert async await auto bool boolean break byte case catch char class const
continue def default del delete do double elif else enum except export extends extern false final finally float fn for
from func function global goto if implements import in instanceof int interface is lambda let long match mod module
mut namespace native new nil none not null or package pass private protected pub public raise record return self sizeof
static struct super switch template this throw throws trait true try type typedef union use using var virtual void
volatile while with yield""".split())

TITLE_BONUS = 3
RECOMMEND_PAGES = 2

_HANGUL = re.compile(r"[가-힣]")
_CODE = re.compile(r"^@?[A-Za-z_][\w.]*(\(\))?$")             # import, getName(), @decorator, os.path
_EN_TERM = re.compile(r"^[A-Za-z][A-Za-z0-9'\-]*( [A-Za-z0-9][A-Za-z0-9'\-]*){0,3}$")  # stem cell, T-cell
_PAIR = re.compile(r"^(.+?)\s*\(([^()]+)\)$")                  # 광합성(Photosynthesis)
_CAPTION = re.compile(r"^(표|그림|figure|fig\.|table|예제|example)\s*[\d.\-]+", re.I)
_SENTENCE_END = re.compile(r"(다|요|음|함|됨|임)[.!?]?$")


def _has_hangul(t):
    return bool(_HANGUL.search(t))


def norm(t):
    """묶기용 키: term_key + 끝의 "()" 제거."""
    k = C.term_key(t)
    return k[:-2].strip() if k.endswith("()") else k


def _flat(t):
    return norm(t).replace(" ", "")


def is_noise(t):
    """문장·구절, 숫자·주소, 캡션, 예시 라벨."""
    t = t.strip()
    if len(t) < 2 and not t.isalpha():
        return True
    if not re.search(r"[A-Za-z가-힣]", t):            # 숫자·기호만
        return True
    if re.search(r"\d+\s*번지", t) or re.fullmatch(r"[\d,.\s]+\D{0,2}", t):
        return True
    if _CAPTION.match(t):
        return True
    if ":" in t and _has_hangul(t):                    # 예시:사과
        return True
    if _has_hangul(t) and (_SENTENCE_END.search(t) and " " in t):  # 광합성은 빛이 필요하다
        return True
    if any(len(w) >= 3 and w.endswith("의") for w in t.split()[:-1]):  # 광합성의 효과, 세포의 구조
        return True
    if len(t.split()) > 4:
        return True
    return False


def figure_ok(t):
    """그림 라벨 중 예외적으로 제안할 수 있는 것: 영어·코드 표기의 짧은 명사형 용어."""
    t = t.strip()
    return not _has_hangul(t) and bool(_CODE.match(t) or _EN_TERM.match(t))


def split_pair(t, sub):
    """"X(Y)" → (대표, [별칭]). 짝이 아니면 (t, [])."""
    t = t.strip()
    m = _PAIR.match(t)
    if not m or t.endswith("()"):
        return t, []
    a, b = m.group(1).strip(), m.group(2).strip()
    if not a or not b:
        return t, []
    if sub.get("keep_code_identifiers"):
        for x, y in ((b, a), (a, b)):
            if x in CODE_KEYWORDS or (x.endswith("()") or x.startswith("@")) and _CODE.match(x):
                return x, [y]
    ha, hb = _has_hangul(a), _has_hangul(b)
    if ha != hb:
        ko, en = (a, b) if ha else (b, a)
        return (en, [ko]) if sub.get("prefer_english_terms") else (ko, [en])
    return a, [b]


class Group:
    def __init__(self, rep):
        self.rep, self.aliases, self.pages, self.kinds = rep, [], set(), set()
        self.forms = {}  # 표기 → 나온 페이지 수 (대표 고르기용)

    def keys(self):
        return {norm(self.rep)} | {norm(a) for a in self.aliases}

    def add_alias(self, a):
        if norm(a) != norm(self.rep) and all(norm(a) != norm(x) for x in self.aliases):
            self.aliases.append(a)

    def absorb(self, g):
        for a in [g.rep] + g.aliases:
            self.add_alias(a)
        self.pages |= g.pages
        self.kinds |= g.kinds
        for f, n in g.forms.items():
            self.forms[f] = self.forms.get(f, 0) + n


def _titles(data):
    out = [data.get("title") or ""]
    out += [p.get("title") or "" for p in data.get("pages", [])]
    return [t for t in out if t]


def _in_titles(g, titles):
    low = [t.lower() for t in titles]
    for form in [g.rep] + g.aliases:
        f = form.lower()
        f = f[:-2] if f.endswith("()") else f
        if not f:
            continue
        if _has_hangul(f):
            if any(f in t for t in low):
                return True
        elif any(re.search(r"(?<![a-z0-9_])" + re.escape(f) + r"(?![a-z0-9_])", t) for t in low):
            return True
    return False


def candidates(data, kind, sub):
    """인덱스 하나에서 용어 후보 묶음을 만든다. [Group] (점수순)."""
    stop = {norm(w) for w in sub.get("term_stopwords") or []}
    stop_flat = {s.replace(" ", "") for s in stop}
    code_names = set()
    raw = []  # (표기, 페이지, 종류)
    if kind == "reference":
        raw = [(t, None, "reference") for t in data.get("terms", [])]
    else:
        for p in data.get("pages", []):
            code_names |= {norm(x) for x in p.get("code_names") or []}
            raw += [(t, p["page"], "slide") for t in p.get("terms", [])]
            raw += [(t, p["page"], "figure") for t in p.get("figure_terms", []) if figure_ok(t)]

    groups, by_key = [], {}

    def find(keys):
        for k in keys:
            if k in by_key:
                return by_key[k]
        return None

    for t, page, k in raw:
        if not isinstance(t, str) or is_noise(t):
            continue
        rep, aliases = split_pair(t, sub)
        forms = [rep] + aliases
        if norm(rep) in stop or norm(rep) in code_names:
            continue
        g = find([norm(f) for f in forms])
        if g is None:
            g = Group(rep)
            groups.append(g)
        for a in forms:
            g.add_alias(a)
        if page is not None:
            g.pages.add(page)
        g.kinds.add(k)
        g.forms[rep] = g.forms.get(rep, 0) + 1
        for key in g.keys():
            by_key[key] = g

    # 제외 단어 + 용어 (개념 광합성) → 그 용어의 별칭
    # 대표 표기에만 붙인다 (식물 세포 ≠ cell의 별칭 "세포")
    flat_of = {}
    for g in groups:
        flat_of.setdefault(_flat(g.rep), g)
    merged = set()
    for g in groups:
        f = _flat(g.rep)
        for s in stop_flat:
            for base in (f[len(s):] if f.startswith(s) else None, f[:-len(s)] if f.endswith(s) else None):
                tgt = flat_of.get(base) if base and len(base) >= 2 else None
                if tgt is not None and tgt is not g and id(tgt) not in merged:
                    tgt.absorb(g)
                    merged.add(id(g))
                    break
            if id(g) in merged:
                break
    groups = [g for g in groups if id(g) not in merged]

    # 대표 표기: 슬라이드 짝 규칙으로 정해진 표기 우선, 같은 키 안에서는 가장 많이 나온 표기
    for g in groups:
        same = [f for f in g.forms if norm(f) == norm(g.rep)]
        if same:
            g.rep = max(same, key=lambda f: (g.forms[f], -len(f)))
        g.aliases = [a for a in g.aliases if norm(a) != norm(g.rep)]

    titles = _titles(data)
    for g in groups:
        g.in_title = _in_titles(g, titles)
        g.score = len(g.pages) + (TITLE_BONUS if g.in_title else 0)
        g.recommended = g.in_title or len(g.pages) >= RECOMMEND_PAGES
    groups.sort(key=lambda g: (-g.score, -len(g.pages), g.rep.lower()))
    return groups
