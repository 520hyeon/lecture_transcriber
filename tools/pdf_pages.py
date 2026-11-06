"""강의자료 페이지 추출 (Claude Code의 /register-slides가 실행).

    python tools/pdf_pages.py --list [과목]                     등록 안 됐거나 바뀐 자료 목록
    python tools/pdf_pages.py <파일명|경로> --subject <과목>     페이지 텍스트·OCR + 이미지로 볼 페이지 PNG
      --all-png    모든 페이지를 이미지 확인으로 (손필기 과목은 자동)
      --text-only  참고 자료처럼 텍스트만 (ipynb, docx는 자동)

페이지마다 텍스트 출처를 정한다:
    pdf    PDF 텍스트만으로 충분
    ocr    그림 영역을 OCR(RapidOCR)해서 PDF 텍스트에 더함 (잡음 있음)
    image  OCR 결과가 빈약한 그림 페이지(도식 등)·손필기 → Claude가 PNG를 직접 봄
기준값은 data/settings.json의 ocr_* / figure_min_cover.

결과 (data/<과목>/cache/<파일명>/):
    pages.md   슬라이드·손필기: Claude가 읽을 페이지별 텍스트와 출처 표시
    text.md    참고 자료: 추출한 텍스트
    pages.json 페이지 정보 (출처, 글자 수, 그림 비중, OCR 글자 수·신뢰도, PNG 경로)
    ocr.json   OCR 결과 캐시 (기준값을 바꿔 다시 실행해도 OCR은 다시 하지 않음)
    pNNN.png   이미지 확인 페이지 (PPTX는 슬라이드 속 그림 파일 pNNN_k.*)
"""
import argparse, json, re, statistics, sys, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

KIND_LABEL = {"slide": "슬라이드", "handwritten": "손필기", "reference": "참고 자료"}
STATE_LABEL = {"new": "새 파일", "changed": "변경됨", "registered": "등록됨"}
SOURCE_LABEL = {"pdf": "PDF 텍스트", "ocr": "OCR", "image": "이미지 확인"}
_BAD = re.compile(r"[-�\x00-\x08\x0b\x0c\x0e-\x1f]")


def rel(p):
    try:
        return Path(p).resolve().relative_to(C.BASE).as_posix()
    except ValueError:
        return str(p)


def clean(s):
    return re.sub(r"[ \t]{2,}", " ", _BAD.sub("", s)).strip()


def counts(pages):
    c = {"pdf": 0, "ocr": 0, "image": 0}
    for p in pages:
        c[p["text_source"]] += 1
    return c


def counts_line(c):
    return f"텍스트 {c['pdf']}쪽 / OCR {c['ocr']}쪽 / 이미지 확인 {c['image']}쪽"


# ---------- OCR 판정 ----------

class OcrCache:
    def __init__(self, path):
        self.path = path
        self.data = C.load_json(path, {}) or {}
        self.dirty = False

    def get(self, key, make_png):
        if key not in self.data:
            import ocr
            text, chars, score = ocr.read(make_png())
            self.data[key] = {"text": text, "chars": chars, "score": score}
            self.dirty = True
        return self.data[key]

    def save(self):
        if self.dirty:
            C.save_json(self.path, self.data)


def judge(p, s, regions):
    """regions: [(cover, ocr결과)] → p에 text_source / needs_image / ocr_* 기록."""
    cover = sum(c for c, _ in regions)
    if regions:
        chars = sum(r["chars"] for _, r in regions)
        score = round(sum(r["score"] * r["chars"] for _, r in regions) / chars, 3) if chars else 0.0
        dens = round(chars / cover) if cover else 0
        p.update(ocr_chars=chars, ocr_score=score, ocr_density=dens)
        p["ocr_text"] = "\n".join(r["text"] for _, r in regions if r["text"])
        if cover >= s["figure_min_cover"]:
            weak = []
            if chars < s["ocr_min_chars"]:
                weak.append(f"{chars}자")
            if dens < s["ocr_min_density"]:
                weak.append(f"밀도 {dens}")
            if chars and score < s["ocr_min_score"]:
                weak.append(f"신뢰도 {score}")
            if weak:
                p["text_source"], p["needs_image"] = "image", "OCR 빈약 (" + ", ".join(weak) + ")"
                return
        p["text_source"] = "ocr" if chars else "pdf"
    else:
        p["text_source"] = "pdf"


# ---------- PDF ----------

def pdf_page_text(page):
    """글자 사이 간격으로 띄어쓰기를 복원한 텍스트 (한국어 슬라이드 PDF는 공백 문자가 빠져 있음)."""
    lines = []
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            out, prev = [], None
            for s in l["spans"]:
                for c in s["chars"]:
                    ch = c["c"]
                    if prev is not None and ch != " " and prev["c"] != " " \
                            and c["bbox"][0] - prev["bbox"][2] > s["size"] * 0.15:
                        out.append(" ")
                    out.append(ch)
                    prev = c
            line = clean("".join(out))
            if line:
                lines.append(line)
    return "\n".join(lines)


def pdf_pages(path, want_png, handwritten, out_dir, s, cache):
    import pymupdf
    doc = pymupdf.open(path)
    long_side = s["render_long_side_handwritten"] if handwritten else s["render_long_side"]
    use_ocr = s["ocr_enabled"] and not handwritten
    pages, todo = [], []
    for i, page in enumerate(doc, 1):
        area = page.rect.width * page.rect.height or 1
        boxes = [pymupdf.Rect(b["bbox"]) & page.rect for b in page.get_image_info()]
        cov = sum(b.get_area() for b in boxes) / area
        text = "" if handwritten else pdf_page_text(page)
        p = {"page": i, "chars": len(text), "image_cover": round(min(cov, 1.0), 2),
             "drawings": len(page.get_drawings()), "text": text}
        p["_regions"] = [b for b in boxes if b.get_area() / area >= s["ocr_min_region"]]
        pages.append(p)

    base_draw = statistics.median(p["drawings"] for p in pages) if pages else 0
    n_ocr = sum(1 for p in pages if use_ocr and p["_regions"])
    done = 0
    for p in pages:
        page = doc[p["page"] - 1]
        zoom = long_side / max(page.rect.width, page.rect.height)
        regions = p.pop("_regions")
        if handwritten:
            p["text_source"], p["needs_image"] = "image", "손필기"
        elif regions and use_ocr:
            res = []
            for b in regions:
                key = f"{p['page']}:{b.x0:.0f},{b.y0:.0f},{b.x1:.0f},{b.y1:.0f}@{long_side}"
                r = cache.get(key, lambda: page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=b).tobytes("png"))
                res.append((b.get_area() / (page.rect.width * page.rect.height), r))
            judge(p, s, res)
            done += 1
            if done % 10 == 0:
                print(f"  OCR {done}/{n_ocr}쪽…", flush=True)
        elif p["image_cover"] >= s["figure_min_cover"]:  # OCR을 끈 경우
            p["text_source"], p["needs_image"] = "image", f"그림 {round(p['image_cover'] * 100)}%"
        else:
            p["text_source"] = "pdf"
        if p["text_source"] != "image":
            if p["drawings"] > base_draw + 20:
                p["text_source"], p["needs_image"] = "image", "도형·도식"
            elif p["chars"] < 25 and not p.get("ocr_chars") and (p["image_cover"] > 0.02 or p["drawings"] > base_draw):
                p["text_source"], p["needs_image"] = "image", "텍스트 거의 없음"
            elif want_png:
                p["text_source"], p["needs_image"] = "image", "전체 이미지 요청"
        if p["text_source"] == "image":
            png = out_dir / f"p{p['page']:03d}.png"
            if not png.exists():
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(png)
            p["images"] = [rel(png)]
    return pages


# ---------- PPTX ----------

def _pptx_shapes(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _pptx_shapes(sh.shapes)
        else:
            yield sh


def pptx_pages(path, out_dir, s=None, cache=None, want_png=False):
    """PPTX는 슬라이드 전체를 그림으로 만들 수 없어서, 슬라이드 속 그림을 OCR하고 필요하면 그림 파일을 꺼냄.
    s가 None이면 텍스트만."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    prs = Presentation(path)
    area = (prs.slide_width * prs.slide_height) or 1
    pages = []
    for i, slide in enumerate(prs.slides, 1):
        texts, pics = [], []
        for sh in _pptx_shapes(slide.shapes):
            if sh.has_text_frame and sh.text_frame.text.strip():
                texts.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    texts.append(" | ".join(c.text for c in row.cells))
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pics.append((sh.image, (sh.width or 0) * (sh.height or 0) / area))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            texts.append("[노트] " + slide.notes_slide.notes_text_frame.text)
        text = clean("\n".join(texts))
        p = {"page": i, "chars": len(text), "text": text,
             "image_cover": round(min(sum(c for _, c in pics), 1.0), 2)}
        if s is not None:
            big = [(img, c) for img, c in pics if c >= s["ocr_min_region"]]
            if big and s["ocr_enabled"]:
                judge(p, s, [(c, cache.get(f"{i}:{img.sha1}", lambda img=img: img.blob)) for img, c in big])
            elif p["image_cover"] >= s["figure_min_cover"]:
                p["text_source"], p["needs_image"] = "image", f"그림 {round(p['image_cover'] * 100)}%"
            else:
                p["text_source"] = "pdf"
            if p["text_source"] != "image" and pics and p["chars"] < 25 and not p.get("ocr_chars"):
                p["text_source"], p["needs_image"] = "image", "텍스트 거의 없음"
            if want_png and pics:
                p["text_source"], p["needs_image"] = "image", "전체 이미지 요청"
            if p["text_source"] == "image":
                p["images"] = []
                for k, (img, _) in enumerate(pics, 1):
                    f = out_dir / f"p{i:03d}_{k}.{img.ext}"
                    if not f.exists():
                        f.write_bytes(img.blob)
                    p["images"].append(rel(f))
        pages.append(p)
    return pages


# ---------- 참고 자료 (텍스트만) ----------

def ipynb_text(path):
    nb = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for n, cell in enumerate(nb.get("cells", []), 1):
        src = "".join(cell.get("source", [])).strip()
        if src and cell.get("cell_type") in ("markdown", "code"):
            out.append(f"### [셀 {n} · {cell['cell_type']}]\n{src}")
    return "\n\n".join(out)


def docx_text(path):
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = ("".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t")) for p in root.iter(f"{{{ns['w']}}}p"))
    return "\n".join(clean(p) for p in paras if p.strip())


def reference_text(path, out_dir):
    ext = Path(path).suffix.lower()
    if ext == ".ipynb":
        return ipynb_text(path)
    if ext == ".docx":
        return docx_text(path)
    if ext == ".pdf":
        import pymupdf
        return "\n\n".join(f"### [p.{i}]\n{t}" for i, page in enumerate(pymupdf.open(path), 1)
                           if (t := pdf_page_text(page)))
    if ext == ".pptx":
        return "\n\n".join(f"### [슬라이드 {p['page']}]\n{p['text']}" for p in pptx_pages(path, out_dir) if p["text"])
    raise ValueError(f"지원하지 않는 형식: {ext}")


# ---------- 실행 ----------

def page_md(p, hand):
    head = f"## p.{p['page']} [출처: {SOURCE_LABEL[p['text_source']]}"
    if p.get("needs_image"):
        head += f" — {p['needs_image']} → {', '.join(Path(x).name for x in p.get('images', []))}"
    out = [head + "]"]
    if hand:
        out.append("(손필기: 텍스트 레이어가 깨져 있어 생략. PNG를 볼 것)")
    else:
        out.append(p["text"] or "(PDF 텍스트 없음)")
        if p.get("ocr_text"):
            out += ["[그림 속 글자 · OCR, 잡음 있음]", p["ocr_text"]]
    return out + [""]


def extract(name, mat, all_png=False, text_only=False):
    src = Path(mat["path"])
    st = src.stat()
    out_dir = C.cache_dir(name, mat["file"])
    meta_path = out_dir / "pages.json"
    old = C.load_json(meta_path)
    if old and (old.get("source_size"), old.get("source_mtime")) != (st.st_size, int(st.st_mtime)):
        for f in out_dir.iterdir():  # 원본이 바뀌었으면 캐시를 버림
            f.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.iterdir():  # 이미지 확인 대상은 기준값에 따라 바뀌므로 PNG는 매번 새로 고름
        if re.fullmatch(r"p\d{3}(_\d+)?\.\w+", f.name):
            f.unlink()
    kind = "reference" if text_only else mat["kind"]
    meta = {"file": mat["file"], "path": str(src), "kind": kind,
            "source_size": st.st_size, "source_mtime": int(st.st_mtime)}

    if kind == "reference":
        text = reference_text(src, out_dir)
        (out_dir / "text.md").write_text(f"# {mat['file']} — 참고 자료 텍스트\n\n{text}\n", encoding="utf-8")
        C.save_json(meta_path, {**meta, "chars": len(text)})
        print(f"[참고 자료] {mat['file']} · 텍스트 {len(text):,}자")
        print(f"- 텍스트: {rel(out_dir / 'text.md')}")
        print(f"- 작성할 파일: {rel(C.ref_path(name, mat['file']))}")
        print(f'- 작성 후: {C.VENV_PY} tools/check_deck.py {name} "{mat["file"]}"')
        return

    s = C.settings()
    hand = kind == "handwritten"
    cache = OcrCache(out_dir / "ocr.json")
    try:
        if src.suffix.lower() == ".pptx":
            pages = pptx_pages(src, out_dir, s, cache, all_png)
        else:
            pages = pdf_pages(src, all_png, hand, out_dir, s, cache)
    finally:
        cache.save()
    c = counts(pages)
    need = [p for p in pages if p["text_source"] == "image"]

    md = [f"# {mat['file']} — 페이지별 추출 텍스트 (총 {len(pages)}쪽, 종류: {KIND_LABEL[kind]})",
          counts_line(c),
          f"이미지로 확인할 페이지: {', '.join(str(p['page']) for p in need) or '없음'}",
          "OCR 텍스트는 잡음이 섞여 있다. 명백한 오류(예: 광합셩 → 광합성)는 문맥으로 바로잡아 이해하고, "
          "OCR에만 나오고 슬라이드 텍스트·문맥으로 확인되지 않는 단어는 terms에 넣지 않는다.", ""]
    for p in pages:
        md += page_md(p, hand)
    (out_dir / "pages.md").write_text("\n".join(md), encoding="utf-8")
    C.save_json(meta_path, {**meta, "pages_total": len(pages), "counts": c,
                            "pages": [{k: v for k, v in p.items() if k not in ("text", "ocr_text")} for p in pages]})

    print(f"[{KIND_LABEL[kind]}] {mat['file']} · {len(pages)}쪽")
    print(f"- 예상: {counts_line(c)}")
    if need:
        print(f"- 이미지 확인 페이지: {', '.join(str(p['page']) for p in need)}  (폴더: {rel(out_dir)}/)")
    print(f"- 텍스트: {rel(out_dir / 'pages.md')}")
    print(f"- 작성할 인덱스: {rel(C.deck_path(name, mat['file']))}")
    print(f'- 작성 후: {C.VENV_PY} tools/check_deck.py {name} "{mat["file"]}"')


def print_list(names):
    for name in names:
        mats = C.list_materials(name)
        todo = [m for m in mats if m["state"] != "registered"]
        print(f"== {name} ({C.finals_materials_dir(name)})")
        if not mats:
            print("  강의자료 없음")
        for m in todo:
            est = f"  ({counts_line(m['counts'])}, 추출됨)" if m.get("counts") else ""
            print(f"  [{STATE_LABEL[m['state']]}] [{KIND_LABEL[m['kind']]}] {m['file']}{est}")
        done = len(mats) - len(todo)
        if done:
            print(f"  등록 완료 {done}개")


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description="강의자료 페이지 추출")
    ap.add_argument("file", nargs="?", help="파일명 또는 경로 (--list면 과목 이름)")
    ap.add_argument("--subject", "-s")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all-png", action="store_true")
    ap.add_argument("--text-only", action="store_true")
    a = ap.parse_args()
    C.ensure_data()

    if a.list:
        print_list([a.file or a.subject] if (a.file or a.subject) else C.subject_names())
        return
    if not a.file:
        ap.error("파일을 지정하세요")
    name = a.subject
    if not name:  # 경로가 <루트>/<과목>/... 이면 과목을 알아냄
        try:
            name = Path(a.file).resolve().relative_to(C.root().resolve()).parts[0]
        except (ValueError, IndexError):
            ap.error("--subject <과목>을 지정하세요")
    C.subject(name)
    mat = C.resolve_material(name, a.file)
    if not mat:
        sys.exit(f"'{a.file}'은(는) {name}의 등록 대상 자료가 아닙니다. --list {name} 으로 목록을 확인하세요.")
    extract(name, mat, a.all_png, a.text_only)


if __name__ == "__main__":
    main()
