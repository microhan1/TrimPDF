"""TrimPDF — PDF 여백 자동 제거 + 확대 도구

PDF 파일을 창에 드래그해서 놓으면 각 페이지의 상/하/좌/우 여백을 찾아 잘라내고,
원본 페이지 크기에 맞게 확대해서 '<파일명>_TrimPDF.pdf' 로 저장한다.
벡터(텍스트/도형)는 이미지로 바뀌지 않고 그대로 유지된다.
"""

import ctypes
import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, ttk

import numpy as np
import pymupdf

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

DETECT_DPI = 100          # 여백 판별용 렌더링 해상도
BLOCK = 4                 # 약 1mm 크기 블록 단위로 판정해서 스캔 잡티를 무시
BLOCK_FILL = 0.2          # 블록 안에서 어두운 점이 이 비율을 넘어야 내용 블록
BG_DELTA = 30             # 종이 배경보다 이만큼 어두워야 내용으로 봄
EDGE_FRAC = 0.015         # 페이지 가장자리 이 범위에 붙어 있는
EDGE_MAX_RUN = 0.03       # 이보다 얇은 띠는 스캔 그림자/테두리로 보고 무시
STRIP_MAX = 0.30          # 스캐너 배경 띠·제본선 그림자를 한쪽에서 걷어낼 수 있는 최대 폭
STRIP_DARKER = 20         # 종이 바탕보다 이만큼 어두운 줄을 띠 후보로 봄
STRIP_SMOOTH = 28         # 줄 안의 밝기 폭(15~65 백분위)이 이보다 좁으면 '고르게 이어진 띠'
TRANS_MAX = 0.015         # 띠 끝에서 비뚤게 찍힌 반쯤 어두운 줄을 더 걷어낼 최대 폭
TRANS_RUN = 0.10          # 한 줄에 이 비율 이상 길게 이어진 어두운 선이면 테두리 조각
CLUSTER_FILL = 0.6        # 블록 1개짜리 줄이라도 이웃과 합친 채움이 이 이상이면 작은 글자(쪽번호)
EDGE_ZONE = 0.02          # 가장자리 이 범위 안의 작은 뭉치는 쪽번호로 인정하지 않음
OUTPUT_SUFFIX = "_TrimPDF"


def _content_span(mask, edge_start=None, edge_end=None):
    """행(또는 열) 단위 내용 여부 배열에서 [시작, 끝) 범위를 찾는다.
    가장자리에 붙은 얇은 띠(스캔 그림자 등)는 본문과 떨어져 있으면 제외한다.
    edge_start/edge_end: 가장자리로 볼 블록 수 (None 이면 EDGE_FRAC 비율)."""
    n = mask.size
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    runs = list(zip(edges[::2], edges[1::2]))
    if not runs:
        return None
    default = max(1, int(n * EDGE_FRAC))
    es = default if edge_start is None else edge_start
    ee = default if edge_end is None else edge_end
    thin = max(2, int(n * EDGE_MAX_RUN))
    while len(runs) > 1 and runs[0][0] <= es and runs[0][1] - runs[0][0] < thin:
        runs.pop(0)
    while len(runs) > 1 and runs[-1][1] >= n - ee and runs[-1][1] - runs[-1][0] < thin:
        runs.pop()
    return runs[0][0], runs[-1][1]


def _longest_run(mask):
    """1차원 참/거짓 배열에서 가장 길게 이어진 참의 길이."""
    if not mask.any():
        return 0
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return int((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).max())


def _strip_side(gray, dark, bg, rows, from_end):
    """스캐너 배경 띠·제본선 그림자처럼 종이보다 어둡고 고르게 이어진 줄을 가장자리부터 센다.
    rows=True 면 위/아래(행), False 면 왼/오른쪽(열). 걷어내다 종이가 안 나오면(색면·사진) 0."""
    p15, p50, p65 = np.percentile(gray, [15, 50, 65], axis=1 if rows else 0)
    n = len(p50)
    order = list(range(n - 1, -1, -1)) if from_end else list(range(n))
    limit, k = int(n * STRIP_MAX), 0
    for i in order:
        if p50[i] < bg - STRIP_DARKER and p65[i] - p15[i] < STRIP_SMOOTH:
            k += 1
            if k > limit:
                return 0
        else:
            break
    if k == 0:
        return 0
    # 비뚤게 스캔돼 반쯤만 어두운 테두리 끝 줄도 조금 더 걷어낸다
    length = dark.shape[1] if rows else dark.shape[0]
    for i in order[k: k + max(1, int(n * TRANS_MAX))]:
        line = dark[i] if rows else dark[:, i]
        if p50[i] < bg - STRIP_DARKER or _longest_run(line) >= TRANS_RUN * length:
            k += 1
        else:
            break
    return k


def _small_mark_blocks(fill):
    """블록 1개짜리라도 이웃 블록과 붙어 있고 합친 채움이 충분한 블록(작은 쪽번호 등). 흩어진 먼지는 제외."""
    strong = fill > BLOCK_FILL
    weak = np.where(fill > 0.1, fill, 0.0)
    h, w = fill.shape
    pad_sum = np.pad(weak, 1)
    pad_cnt = np.pad((fill > 0.1).astype(np.int32), 1)
    win_sum = sum(pad_sum[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3))
    win_cnt = sum(pad_cnt[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3))
    return strong & (win_cnt >= 2) & (win_sum >= CLUSTER_FILL)


def find_content_rect(page, threshold=235, dpi=DETECT_DPI):
    """페이지를 렌더링해서 내용이 있는 영역을 page.rect 좌표(pt)로 돌려준다. 빈 페이지면 None."""
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]

    # 스캔본은 종이 배경이 순백이 아니므로 페이지마다 배경 밝기를 추정해서 기준을 낮춘다.
    # 배경이 종이 같지 않은(전면 사진/색면) 페이지는 원래 기준을 그대로 쓴다.
    bg = float(np.percentile(img, 75))
    cut = min(threshold, bg - BG_DELTA) if bg >= 200 else threshold
    H, W = img.shape
    dark_all = img < cut

    # 종이 페이지면 스캐너 배경 띠·제본선 그림자를 먼저 걷어낸다 (위아래 → 좌우 → 위아래 한 번 더)
    x0, y0, x1, y1 = 0, 0, W, H
    if bg >= 200:
        gray = img.astype(np.float32)
        mid = slice(W // 5, W - W // 5)
        y0 = _strip_side(gray[:, mid], dark_all[:, mid], bg, True, False)
        y1 = H - _strip_side(gray[:, mid], dark_all[:, mid], bg, True, True)
        x0 = _strip_side(gray[y0:y1], dark_all[y0:y1], bg, False, False)
        x1 = W - _strip_side(gray[y0:y1], dark_all[y0:y1], bg, False, True)
        y0 = max(y0, _strip_side(gray[:, x0:x1], dark_all[:, x0:x1], bg, True, False))
        y1 = min(y1, H - _strip_side(gray[:, x0:x1], dark_all[:, x0:x1], bg, True, True))

    dark = dark_all[y0:y1, x0:x1]
    h, w = dark.shape[0] // BLOCK, dark.shape[1] // BLOCK
    if h == 0 or w == 0:
        return None
    fill = dark[: h * BLOCK, : w * BLOCK].reshape(h, BLOCK, w, BLOCK).mean(axis=(1, 3))
    blocks = fill > BLOCK_FILL
    marks = _small_mark_blocks(fill)
    zy, zx = max(2, int(h * EDGE_ZONE)), max(2, int(w * EDGE_ZONE))
    marks[:zy, :] = False
    marks[-zy:, :] = False
    marks[:, :zx] = False
    marks[:, -zx:] = False
    rows = (blocks.sum(axis=1) >= 2) | marks.any(axis=1)     # 작은 쪽번호·꼬리말은 세로 범위에만 반영
    cols = blocks.sum(axis=0) >= 2
    # 띠를 걷어낸 쪽은 새 가장자리에 딱 붙은 조각만 버린다 (그 가까이 있는 쪽번호는 보존)
    ys = _content_span(rows, 1 if y0 > 0 else None, 1 if y1 < H else None)
    xs = _content_span(cols, 1 if x0 > 0 else None, 1 if x1 < W else None)
    if ys is None or xs is None:
        return None

    # 블록 경계에 걸친 옅은 글자(쪽번호 등)가 잘리지 않도록 한 블록씩 여유를 둔다
    by0, by1 = max(0, ys[0] - 1) * BLOCK + y0, min(h, ys[1] + 1) * BLOCK + y0
    bx0, bx1 = max(0, xs[0] - 1) * BLOCK + x0, min(w, xs[1] + 1) * BLOCK + x0
    sx = page.rect.width / pix.width
    sy = page.rect.height / pix.height
    return pymupdf.Rect(bx0 * sx, by0 * sy, bx1 * sx, by1 * sy) & page.rect


def view_to_unrotated(page, rect):
    """보이는(회전된) 좌표의 사각형을 회전 전(cropbox 기준) 좌표로 바꾼다.
    page.derotation_matrix 는 cropbox 가 mediabox 와 다를 때 어긋나서 직접 계산한다."""
    w, h = page.cropbox.width, page.cropbox.height
    rot = page.rotation % 360
    x0, y0, x1, y1 = rect
    if rot == 90:
        return pymupdf.Rect(y0, h - x1, y1, h - x0)
    if rot == 180:
        return pymupdf.Rect(w - x1, h - y1, w - x0, h - y0)
    if rot == 270:
        return pymupdf.Rect(w - y1, x0, w - y0, x1)
    return pymupdf.Rect(rect)


class PasswordProtectedError(ValueError):
    """암호가 걸린 PDF (화면에서 언어별 문구로 바꿔 보여준다)"""


def output_path_for(path):
    base, ext = os.path.splitext(path)
    return f"{base}{OUTPUT_SUFFIX}{ext or '.pdf'}"


def _raw_box(doc, xref, key):
    """페이지(없으면 상위 Pages 트리)에 저장된 박스 숫자를 PDF 원래 좌표 그대로 읽는다. 없으면 None."""
    for _ in range(32):
        typ, val = doc.xref_get_key(xref, key)
        if typ == "array":
            x0, y0, x1, y1 = [float(n) for n in val.strip("[]").split()][:4]
            return pymupdf.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        typ, parent = doc.xref_get_key(xref, "Parent")
        if typ != "xref":
            return None
        xref = int(parent.split()[0])
    return None


def _place_page(out, work, page, clip, size, scale_cap=None):
    """원본 page 의 clip 영역을 size 크기의 새 페이지 가운데에 비율을 유지해서 배치한다."""
    pw, ph = size
    new_page = out.new_page(width=pw, height=ph)
    if not page.get_contents():
        return
    scale = min(pw / clip.width, ph / clip.height)
    if scale_cap:
        scale = min(scale, scale_cap)
    tw, th = clip.width * scale, clip.height * scale
    target = pymupdf.Rect((pw - tw) / 2, (ph - th) / 2, (pw + tw) / 2, (ph + th) / 2)

    # show_pdf_page 는 clip 을 원본의 '회전된' page.rect 와 교차시키기 때문에
    # 회전 + cropbox 가 있는 페이지에서 내용이 어긋나거나 잘린다.
    # 그래서 작업용 사본의 페이지를 cropbox=mediabox, rotation=0 으로 정규화해서 사용한다.
    # PyMuPDF 의 page.mediabox 와 page.cropbox 는 세로축 기준이 서로 달라서, MediaBox 가 위아래로
    # 옮겨진 페이지(펼침면을 나눈 스캔본 등)에서 어긋난다. 그래서 PDF 에 저장된 원래 숫자로 계산한다.
    xref = work.page_xref(page.number)
    media = _raw_box(work, xref, "MediaBox") or pymupdf.Rect(page.mediabox)
    crop = _raw_box(work, xref, "CropBox") or media
    if not (crop & media).is_empty:
        crop = crop & media
    dx = crop.x0 - media.x0      # 가로: 왼쪽 끝끼리의 차이
    dy = media.y1 - crop.y1      # 세로: PDF 는 아래에서 위로 가는 좌표라 위쪽 끝끼리의 차이
    src_clip = view_to_unrotated(page, clip) + (dx, dy, dx, dy)
    work.xref_set_key(xref, "CropBox", f"[{media.x0:g} {media.y0:g} {media.x1:g} {media.y1:g}]")
    work.xref_set_key(xref, "Rotate", "0")
    new_page.show_pdf_page(target, work, page.number, clip=src_clip, rotate=-page.rotation)


def process_pdf(path, fit_to_page=True, padding=5.0, threshold=235, side_ratio=0.5,
                max_zoom=1.5, uniform_size=True, progress=None):
    """PDF 한 개를 처리하고 저장된 파일 경로를 돌려준다.

    fit_to_page=True  : 여백을 잘라낸 뒤 원본 페이지 크기로 확대(비율 유지, 가운데 정렬)
    fit_to_page=False : 여백만 잘라내고 잘린 크기 그대로 저장
    padding           : 잘라낼 때 내용 주변에 남겨둘 여백(pt, 1pt = 0.35mm)
    side_ratio        : 확대 시 가운데 정렬로 생기는 좌우 빈 공간을 얼마나 남길지(0~1).
                        1 이면 원본 페이지 크기 그대로, 0.5 면 페이지 폭을 줄여 좌우 빈 공간을 절반으로
    max_zoom          : 확대 배율 상한(0 이면 제한 없음). 내용이 작은 페이지가 과하게 커지는 것을 막는다
    uniform_size      : 확대 모드에서 모든 페이지를 같은 크기로 맞춘다(세로형/가로형은 각각 대표 크기)
    """
    src = pymupdf.open(path)
    if src.needs_pass:
        raise PasswordProtectedError("PDF is password-protected")
    work = pymupdf.open(path)
    out = pymupdf.open()
    total = src.page_count
    cap = max_zoom if fit_to_page and max_zoom and max_zoom > 0 else None

    # 1단계: 페이지마다 내용 영역과 결과 페이지 크기를 계산
    clips, sizes, rects = [], [], []
    for i, page in enumerate(src):
        W, H = page.rect.width, page.rect.height
        content = find_content_rect(page, threshold) if page.get_contents() else None
        if content is None:
            clip, size = None, (W, H)
        else:
            clip = pymupdf.Rect(content.x0 - padding, content.y0 - padding,
                                content.x1 + padding, content.y1 + padding) & page.rect
            if fit_to_page:
                scale = min(W / clip.width, H / clip.height)
                if cap:
                    scale = min(scale, cap)
                # 글자 비율은 유지하고, 페이지 폭을 줄여서 좌우 빈 공간을 side_ratio 만큼만 남긴다
                size = (W - (W - clip.width * scale) * (1 - side_ratio), H)
            else:
                size = (clip.width, clip.height)
        clips.append(clip)
        sizes.append(size)
        rects.append((W, H))
        if progress:
            progress(i + 1, total * 2)

    # 2단계: 페이지 크기 통일. 세로형/가로형 페이지를 나눠서 각 그룹의 중앙값 크기로 맞춘다
    if fit_to_page and uniform_size:
        for portrait in (True, False):
            group = [i for i, (W, H) in enumerate(rects) if (H >= W) == portrait]
            measured = [sizes[i] for i in group if clips[i] is not None] or [sizes[i] for i in group]
            if not measured:
                continue
            common = (float(np.median([s[0] for s in measured])), float(np.median([s[1] for s in measured])))
            for i in group:
                sizes[i] = common

    # 3단계: 새 페이지에 배치
    for i, page in enumerate(src):
        clip = clips[i]
        if clip is None and sizes[i] == rects[i]:
            # 빈 페이지는 그대로 복사
            out.insert_pdf(src, from_page=i, to_page=i)
        else:
            _place_page(out, work, page, clip or page.rect, sizes[i],
                        cap if clip is not None else None)
        if progress:
            progress(total + i + 1, total * 2)

    toc = src.get_toc(simple=False)
    if toc:
        try:
            out.set_toc(toc)
        except Exception:
            pass

    dst = output_path_for(path)
    out.save(dst, garbage=3, deflate=True)
    out.close()
    work.close()
    src.close()
    return dst


# ---------------------------------------------------------------- 화면(GUI)

COLORS = {
    "bg": "#EEF2F4",
    "surface": "#FFFFFF",
    "ink": "#17202B",
    "muted": "#5B6776",
    "faint": "#A3ADB7",
    "rule": "#D9E0E6",
    "dash": "#8E9BA7",
    "accent": "#0F766E",
    "accent_soft": "#DDF0EC",
    "ok": "#0F766E",
    "err": "#B42318",
}

LANGUAGES = [("ko", "한국어"), ("en", "English"), ("zh", "中文")]
FONT_CANDIDATES = {
    "ko": ("Malgun Gothic", "맑은 고딕", "Segoe UI"),
    "en": ("Segoe UI", "Malgun Gothic"),
    "zh": ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Malgun Gothic"),
}

STRINGS = {
    "ko": {
        "subtitle": "여백을 잘라내고 내용을 키워서 '파일명_TrimPDF.pdf'로 저장합니다.",
        "drop_title": "PDF 파일을 여기에 끌어다 놓으세요",
        "drop_title_drag": "여기에 놓으면 바로 처리합니다",
        "drop_title_nodnd": "클릭해서 PDF 파일을 선택하세요",
        "drop_sub": "여러 파일도 한 번에 · 클릭해서 파일 선택",
        "drop_sub_nodnd": "드래그&드롭을 쓰려면 tkinterdnd2 가 필요합니다",
        "card_mode": "처리 방식",
        "mode_fit": "여백 자르고 크게 키우기",
        "mode_crop": "여백만 자르기 (크기 그대로)",
        "card_crop": "자르기",
        "padding": "남길 여백",
        "padding_unit": "pt",
        "padding_tip": "찾은 내용 둘레에 남길 여유입니다. 5pt ≈ 1.8mm\n"
                       "글자가 가장자리에 붙어 답답하면 10 정도로 올리세요.",
        "threshold": "흰색 기준",
        "threshold_unit": "0~255",
        "threshold_tip": "이 값보다 어두운 부분을 내용으로 봅니다. (0 검정 ~ 255 흰색)\n"
                         "스캔 얼룩 때문에 여백이 덜 잘리면 200 정도로 낮추세요.",
        "hint": "옵션 이름에 마우스를 올리면 설명이 나옵니다",
        "card_zoom": "확대",
        "side": "좌우 빈 공간",
        "side_unit": "%",
        "side_tip": "내용이 세로로 길 때 좌우에 남는 빈 공간의 비율입니다.\n"
                    "0이면 페이지 폭을 내용에 맞추고, 100이면 원래 폭을 유지합니다.",
        "maxzoom": "최대 배율",
        "maxzoom_unit": "배",
        "maxzoom_tip": "작은 그림이나 몇 줄뿐인 페이지가 너무 커지지 않게 확대를 제한합니다.\n"
                       "0이면 제한하지 않습니다.",
        "uniform": "모든 페이지 크기 통일",
        "uniform_tip": "모든 페이지를 같은 크기로 맞춰 넘길 때 크기가 바뀌지 않게 합니다.\n"
                       "세로형·가로형 페이지는 각각 따로 맞춥니다.",
        "status_idle": "대기 중",
        "status_working": "처리 중 ({n}/{total}) · {name}",
        "status_done": "완료 · 결과 파일은 원본과 같은 폴더에 저장됐습니다",
        "results": "처리 결과",
        "empty": "아직 처리한 파일이 없습니다.",
        "skipped": "PDF가 아니라서 건너뜀 · {name}",
        "bad_options": "옵션 값이 올바르지 않아 기본값으로 처리합니다.",
        "err_password": "{name} · 암호가 걸린 PDF라서 처리할 수 없습니다",
        "dialog_title": "PDF 파일 선택",
        "filetype": "PDF 파일",
    },
    "en": {
        "subtitle": "Trims page margins, enlarges the content, and saves it as 'filename_TrimPDF.pdf'.",
        "drop_title": "Drop PDF files here",
        "drop_title_drag": "Release to start processing",
        "drop_title_nodnd": "Click to choose PDF files",
        "drop_sub": "Several files at once · or click to browse",
        "drop_sub_nodnd": "Drag and drop requires tkinterdnd2",
        "card_mode": "Mode",
        "mode_fit": "Trim and enlarge",
        "mode_crop": "Trim only (keep size)",
        "card_crop": "Trim",
        "padding": "Margin to keep",
        "padding_unit": "pt",
        "padding_tip": "Space left around the detected content. 5 pt ≈ 1.8 mm\n"
                       "Raise it to about 10 if text sits too close to the edge.",
        "threshold": "White level",
        "threshold_unit": "0–255",
        "threshold_tip": "Anything darker than this counts as content (0 black – 255 white).\n"
                         "If scan smudges keep margins from being trimmed, lower it to about 200.",
        "hint": "Hover over an option name for details",
        "card_zoom": "Enlarge",
        "side": "Side space",
        "side_unit": "%",
        "side_tip": "How much of the empty space on the left and right to keep when content is narrow.\n"
                    "0 fits the page width to the content; 100 keeps the original width.",
        "maxzoom": "Max zoom",
        "maxzoom_unit": "×",
        "maxzoom_tip": "Limits enlargement so pages with a small picture or a few lines don't get blown up.\n"
                       "0 means no limit.",
        "uniform": "Same size for all pages",
        "uniform_tip": "Gives every page the same size so it doesn't change as you flip pages.\n"
                       "Portrait and landscape pages are sized separately.",
        "status_idle": "Ready",
        "status_working": "Processing ({n}/{total}) · {name}",
        "status_done": "Done · Output files are saved next to the originals",
        "results": "Results",
        "empty": "No files processed yet.",
        "skipped": "Skipped (not a PDF) · {name}",
        "bad_options": "Some option values were invalid, so the defaults were used.",
        "err_password": "{name} · Password-protected PDFs can't be processed",
        "dialog_title": "Choose PDF files",
        "filetype": "PDF files",
    },
    "zh": {
        "subtitle": "裁掉页面边距、放大内容，并另存为“文件名_TrimPDF.pdf”。",
        "drop_title": "将 PDF 文件拖放到这里",
        "drop_title_drag": "松开即可开始处理",
        "drop_title_nodnd": "点击选择 PDF 文件",
        "drop_sub": "可一次拖入多个文件 · 或点击选择文件",
        "drop_sub_nodnd": "拖放功能需要 tkinterdnd2",
        "card_mode": "处理方式",
        "mode_fit": "裁边并放大",
        "mode_crop": "仅裁边（保持原大小）",
        "card_crop": "裁剪",
        "padding": "保留边距",
        "padding_unit": "pt",
        "padding_tip": "在检测到的内容周围保留的空白。5 pt ≈ 1.8 mm\n"
                       "如果文字太贴近边缘，可调到 10 左右。",
        "threshold": "白色阈值",
        "threshold_unit": "0–255",
        "threshold_tip": "比该值暗的部分视为内容（0 黑色 ~ 255 白色）。\n"
                         "如果扫描污点导致边距裁不干净，可降到 200 左右。",
        "hint": "将鼠标移到选项名称上可查看说明",
        "card_zoom": "放大",
        "side": "左右留白",
        "side_unit": "%",
        "side_tip": "内容偏窄时，左右空白保留的比例。\n"
                    "0 表示页面宽度贴合内容，100 表示保持原宽度。",
        "maxzoom": "最大倍数",
        "maxzoom_unit": "倍",
        "maxzoom_tip": "限制放大倍数，避免只有小图或几行字的页面被放得过大。\n"
                       "0 表示不限制。",
        "uniform": "所有页面统一尺寸",
        "uniform_tip": "让所有页面尺寸一致，翻页时大小不会变化。\n"
                       "纵向和横向页面分别统一。",
        "status_idle": "就绪",
        "status_working": "正在处理（{n}/{total}）· {name}",
        "status_done": "完成 · 结果文件已保存在原文件所在的文件夹",
        "results": "处理结果",
        "empty": "还没有处理过文件。",
        "skipped": "已跳过（不是 PDF）· {name}",
        "bad_options": "部分选项值无效，已使用默认值。",
        "err_password": "{name} · 该 PDF 设有密码，无法处理",
        "dialog_title": "选择 PDF 文件",
        "filetype": "PDF 文件",
    },
}

APPDATA_DIR = os.environ.get("APPDATA") or os.path.expanduser("~")
SETTINGS_PATH = os.path.join(APPDATA_DIR, "TrimPDF", "settings.json")
LEGACY_SETTINGS_PATH = os.path.join(APPDATA_DIR, "PDF_Resize", "settings.json")   # 이름을 바꾸기 전 설정 위치


def load_settings():
    # 새 위치에 설정이 없으면 예전(PDF_Resize) 위치의 설정을 이어받는다
    for path in (SETTINGS_PATH, LEGACY_SETTINGS_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def default_language():
    """처음 실행할 때는 Windows 표시 언어를 따른다."""
    try:
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        return {0x12: "ko", 0x04: "zh"}.get(primary, "en")
    except Exception:
        return "ko"


def enable_dpi_awareness():
    """Windows 화면 배율(125%, 150% 등)에서 창이 흐리게 늘어나지 않도록 한다."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def round_rect(canvas, x0, y0, x1, y1, r, **kw):
    """캔버스에 둥근 모서리 사각형을 그린다."""
    pts = [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r,
           x1, y1 - r, x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Tooltip:
    """마우스를 올리면 옵션 설명을 보여주는 말풍선. 문구는 보여줄 때마다 현재 언어로 가져온다."""

    def __init__(self, widget, get_text, font, scale):
        self.widget, self.get_text, self.font, self.scale = widget, get_text, font, scale
        self.tip = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + int(4 * self.scale)
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.get_text(), justify="left", wraplength=int(320 * self.scale),
                 bg=COLORS["ink"], fg="#FFFFFF", font=self.font,
                 padx=int(10 * self.scale), pady=int(7 * self.scale)).pack()

    def hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    def __init__(self, root, initial_files=()):
        self.root = root
        self.queue = queue.Queue()
        self.busy = False
        self.pending = []
        self.hovering = False
        self.dragging = False
        self.progress_value = 0.0
        self.s = root.winfo_fpixels("1i") / 96   # 화면 배율

        self.settings = load_settings()
        saved = self.settings.get("language")
        self.lang = saved if saved in STRINGS else default_language()
        self.families = set(tkfont.families(root))
        self.fonts = {name: tkfont.Font(root, size=size, weight=weight) for name, (size, weight) in {
            "title": (16, "bold"), "drop": (12, "bold"), "body": (10, "normal"),
            "small": (9, "normal"), "small_bold": (9, "bold"), "tiny": (8, "normal"), "file": (9, "normal"),
        }.items()}
        self.text_widgets = []          # (위젯, 문구 키) — 언어를 바꾸면 다시 채운다
        self.log_entries = []           # (종류, 문구 키 또는 None, 값)
        self.status_state = ("status_idle", {})

        root.title("TrimPDF")
        root.geometry(f"{self.px(600)}x{self.px(760)}")
        root.minsize(self.px(540), self.px(680))
        root.configure(bg=COLORS["bg"])
        style = ttk.Style(root)
        for name in ("Card.TRadiobutton", "Card.TCheckbutton"):
            style.configure(name, background=COLORS["surface"], foreground=COLORS["ink"], font=self.fonts["body"])

        outer = tk.Frame(root, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=self.px(20), pady=(self.px(16), self.px(18)))
        self.build_header(outer)
        self.build_drop_zone(outer)
        self.build_options(outer)
        self.build_progress(outer)
        self.build_results(outer)
        self.apply_language()

        root.after(100, self.poll)
        if initial_files:
            self.add_files(initial_files)

    def px(self, value):
        return int(round(value * self.s))

    # ---- 언어 ----
    def tr(self, key, **params):
        text = STRINGS[self.lang].get(key) or STRINGS["en"][key]
        return text.format(**params) if params else text

    def pick_font(self, lang):
        for family in FONT_CANDIDATES[lang]:
            if family in self.families:
                return family
        return "TkDefaultFont"

    def text_label(self, parent, key, font, **kw):
        widget = tk.Label(parent, text=self.tr(key), font=self.fonts[font], **kw)
        self.text_widgets.append((widget, key))
        return widget

    def set_language(self, lang):
        if lang == self.lang:
            return
        self.lang = lang
        self.settings["language"] = lang
        save_settings(self.settings)
        self.apply_language()

    def apply_language(self):
        family = self.pick_font(self.lang)
        for font in self.fonts.values():
            font.configure(family=family)
        # 파일 이름에는 화면 언어와 상관없이 한글이 섞일 수 있어서, 한글·한자를 제대로 그리는 글꼴로 표시한다
        self.fonts["file"].configure(family=self.pick_font("zh" if self.lang == "zh" else "ko"))
        for widget, key in self.text_widgets:
            widget.configure(text=self.tr(key))
        self.paint_language_switch()
        self.draw_drop_zone()
        self.render_status()
        self.render_log()

    # ---- 화면 구성 ----
    def build_header(self, parent):
        px = self.px
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(fill="x", pady=(0, px(12)))
        row.columnconfigure(0, weight=1)
        tk.Label(row, text="TrimPDF", font=self.fonts["title"], bg=COLORS["bg"],
                 fg=COLORS["ink"]).grid(row=0, column=0, sticky="w")
        self.build_language_switch(row).grid(row=0, column=1, sticky="e")
        self.text_label(row, "subtitle", "small", bg=COLORS["bg"], fg=COLORS["muted"],
                        anchor="w").grid(row=1, column=0, columnspan=2, sticky="w")

    def build_language_switch(self, parent):
        px = self.px
        border = tk.Frame(parent, bg=COLORS["rule"], padx=1, pady=1)
        inner = tk.Frame(border, bg=COLORS["rule"])
        inner.pack()
        self.lang_buttons = {}
        for i, (code, name) in enumerate(LANGUAGES):
            button = tk.Label(inner, text=name, font=(self.pick_font(code), 9), padx=px(10), pady=px(3),
                              cursor="hand2", takefocus=1, highlightthickness=1)
            button.pack(side="left", padx=(0 if i == 0 else 1, 0))
            button.bind("<Button-1>", lambda e, c=code: self.set_language(c))
            button.bind("<Return>", lambda e, c=code: self.set_language(c))
            button.bind("<space>", lambda e, c=code: self.set_language(c))
            self.lang_buttons[code] = button
        return border

    def paint_language_switch(self):
        for code, button in self.lang_buttons.items():
            selected = code == self.lang
            bg = COLORS["surface"] if selected else COLORS["bg"]
            button.configure(bg=bg, fg=COLORS["accent"] if selected else COLORS["muted"],
                             highlightbackground=bg, highlightcolor=COLORS["accent"])

    def build_drop_zone(self, parent):
        self.drop = tk.Canvas(parent, height=self.px(150), bg=COLORS["bg"], highlightthickness=0,
                              cursor="hand2", takefocus=1)
        self.drop.pack(fill="x")
        self.drop.bind("<Configure>", lambda e: self.draw_drop_zone())
        self.drop.bind("<Button-1>", lambda e: self.choose_files())
        self.drop.bind("<Return>", lambda e: self.choose_files())
        self.drop.bind("<space>", lambda e: self.choose_files())
        self.drop.bind("<Enter>", lambda e: self.set_drop_state(hovering=True))
        self.drop.bind("<Leave>", lambda e: self.set_drop_state(hovering=False))
        self.drop.bind("<FocusIn>", lambda e: self.draw_drop_zone())
        self.drop.bind("<FocusOut>", lambda e: self.draw_drop_zone())
        if HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<DropEnter>>", self.on_drag_enter)
            self.drop.dnd_bind("<<DropLeave>>", self.on_drag_leave)
            self.drop.dnd_bind("<<Drop>>", self.on_drop)

    def set_drop_state(self, hovering=None, dragging=None):
        if hovering is not None:
            self.hovering = hovering
        if dragging is not None:
            self.dragging = dragging
        self.draw_drop_zone()

    def draw_drop_zone(self):
        if not hasattr(self, "drop"):
            return
        c, px = self.drop, self.px
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20:
            return
        try:
            focused = self.root.focus_get() is c
        except (KeyError, tk.TclError):
            focused = False
        highlight = self.hovering or self.dragging or focused
        line_w = max(1, px(1.5))
        round_rect(c, px(1), px(1), w - px(1), h - px(1), px(12),
                   fill=COLORS["accent_soft"] if self.dragging else COLORS["surface"],
                   outline=COLORS["accent"] if highlight else COLORS["dash"],
                   width=line_w, dash=() if self.dragging else (4, 3))

        # 문서 아이콘 + 모서리 재단 표시
        cx, top = w / 2, px(24)
        dw, dh, fold = px(30), px(38), px(9)
        x0 = cx - dw / 2
        icon = COLORS["accent"] if highlight else COLORS["muted"]
        c.create_polygon(x0, top, x0 + dw - fold, top, x0 + dw, top + fold, x0 + dw, top + dh, x0, top + dh,
                         fill=COLORS["surface"], outline=icon, width=line_w)
        c.create_line(x0 + dw - fold, top, x0 + dw - fold, top + fold, x0 + dw, top + fold,
                      fill=icon, width=line_w)
        for k in range(3):
            y = top + px(17) + k * px(6)
            c.create_line(x0 + px(7), y, x0 + dw - px(7), y, fill=COLORS["dash"], width=line_w)
        gap, arm = px(6), px(7)
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            ex, ey = cx + sx * (dw / 2 + gap), top + dh / 2 + sy * (dh / 2 + gap)
            c.create_line(ex, ey, ex - sx * arm, ey, fill=COLORS["accent"], width=max(1, px(2)))
            c.create_line(ex, ey, ex, ey - sy * arm, fill=COLORS["accent"], width=max(1, px(2)))

        if self.dragging:
            title = self.tr("drop_title_drag")
        elif HAS_DND:
            title = self.tr("drop_title")
        else:
            title = self.tr("drop_title_nodnd")
        sub = self.tr("drop_sub" if HAS_DND else "drop_sub_nodnd")
        c.create_text(cx, top + dh + px(28), text=title, font=self.fonts["drop"], fill=COLORS["ink"])
        c.create_text(cx, top + dh + px(52), text=sub, font=self.fonts["small"], fill=COLORS["muted"])

    def card(self, parent, key):
        frame = tk.Frame(parent, bg=COLORS["surface"], highlightbackground=COLORS["rule"],
                         highlightcolor=COLORS["rule"], highlightthickness=1)
        frame.columnconfigure(2, weight=1)
        self.text_label(frame, key, "small_bold", bg=COLORS["surface"], fg=COLORS["muted"]).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=self.px(14), pady=(self.px(10), self.px(6)))
        return frame

    def option_row(self, card, row, key, var, lo, hi, step, fmt=None):
        px = self.px
        pady = (0, px(6))
        lab = self.text_label(card, key, "body", bg=COLORS["surface"], fg=COLORS["ink"], cursor="question_arrow")
        lab.grid(row=row, column=0, sticky="w", padx=(px(14), px(10)), pady=pady)
        extra = {"format": fmt} if fmt else {}
        spin = ttk.Spinbox(card, from_=lo, to=hi, increment=step, width=6, font=self.fonts["body"],
                           textvariable=var, **extra)
        spin.grid(row=row, column=1, sticky="w", pady=pady)
        unit = self.text_label(card, key + "_unit", "small", bg=COLORS["surface"], fg=COLORS["muted"])
        unit.grid(row=row, column=2, sticky="w", padx=(px(6), px(14)), pady=pady)
        Tooltip(lab, lambda: self.tr(key + "_tip"), self.fonts["small"], self.s)
        return [(lab, COLORS["ink"]), (spin, None), (unit, COLORS["muted"])]

    def build_options(self, parent):
        px = self.px
        box = tk.Frame(parent, bg=COLORS["bg"])
        box.pack(fill="x", pady=(px(14), 0))
        box.columnconfigure(0, weight=1, uniform="opt")
        box.columnconfigure(1, weight=1, uniform="opt")

        mode = self.card(box, "card_mode")
        mode.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.fit_var = tk.BooleanVar(value=True)
        fit = ttk.Radiobutton(mode, style="Card.TRadiobutton", variable=self.fit_var, value=True)
        fit.grid(row=1, column=0, columnspan=2, sticky="w", padx=(px(14), px(24)), pady=(0, px(12)))
        crop_only = ttk.Radiobutton(mode, style="Card.TRadiobutton", variable=self.fit_var, value=False)
        crop_only.grid(row=1, column=2, sticky="w", pady=(0, px(12)))
        self.text_widgets += [(fit, "mode_fit"), (crop_only, "mode_crop")]

        crop = self.card(box, "card_crop")
        crop.grid(row=1, column=0, sticky="nsew", pady=(px(10), 0), padx=(0, px(5)))
        self.pad_var = tk.DoubleVar(value=5)
        self.th_var = tk.IntVar(value=235)
        self.option_row(crop, 1, "padding", self.pad_var, 0, 100, 1)
        self.option_row(crop, 2, "threshold", self.th_var, 100, 254, 5)
        self.text_label(crop, "hint", "tiny", bg=COLORS["surface"], fg=COLORS["muted"],
                        anchor="w", justify="left").grid(row=3, column=0, columnspan=3, sticky="w",
                                                         padx=px(14), pady=(px(4), px(12)))

        zoom = self.card(box, "card_zoom")
        zoom.grid(row=1, column=1, sticky="nsew", pady=(px(10), 0), padx=(px(5), 0))
        self.side_var = tk.IntVar(value=50)
        self.zoom_var = tk.DoubleVar(value=1.5)
        self.uniform_var = tk.BooleanVar(value=True)
        self.zoom_widgets = []
        self.zoom_widgets += self.option_row(zoom, 1, "side", self.side_var, 0, 100, 10)
        self.zoom_widgets += self.option_row(zoom, 2, "maxzoom", self.zoom_var, 0, 10, 0.1, fmt="%.1f")
        chk = ttk.Checkbutton(zoom, style="Card.TCheckbutton", variable=self.uniform_var)
        chk.grid(row=3, column=0, columnspan=3, sticky="w", padx=px(14), pady=(px(2), px(12)))
        self.text_widgets.append((chk, "uniform"))
        Tooltip(chk, lambda: self.tr("uniform_tip"), self.fonts["small"], self.s)
        self.zoom_widgets.append((chk, None))
        self.fit_var.trace_add("write", lambda *_: self.update_zoom_state())

    def update_zoom_state(self):
        enabled = self.fit_var.get()
        for widget, color in self.zoom_widgets:
            if color is None:
                widget.state(["!disabled"] if enabled else ["disabled"])
            else:
                widget.configure(fg=color if enabled else COLORS["faint"])

    def build_progress(self, parent):
        px = self.px
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(fill="x", pady=(px(16), px(6)))
        self.status = tk.Label(row, font=self.fonts["file"], bg=COLORS["bg"], fg=COLORS["muted"], anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.percent = tk.Label(row, text="", font=("Consolas", 9), bg=COLORS["bg"], fg=COLORS["muted"])
        self.percent.pack(side="right")
        self.bar = tk.Canvas(parent, height=px(6), bg=COLORS["bg"], highlightthickness=0)
        self.bar.pack(fill="x")
        self.bar.bind("<Configure>", lambda e: self.draw_progress())

    def draw_progress(self):
        c = self.bar
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10:
            return
        r = h / 2
        round_rect(c, 0, 0, w, h, r, fill=COLORS["rule"], outline="")
        fill_w = w * self.progress_value / 100
        if fill_w > 0:
            round_rect(c, 0, 0, max(fill_w, h), h, r, fill=COLORS["accent"], outline="")

    def set_progress(self, value):
        self.progress_value = max(0.0, min(100.0, value))
        self.percent.config(text=f"{self.progress_value:.0f}%")
        self.draw_progress()

    def set_status(self, key, **params):
        self.status_state = (key, params)
        self.render_status()

    def render_status(self):
        key, params = self.status_state
        self.status.configure(text=self.tr(key, **params),
                              fg=COLORS["ink"] if key == "status_working" else COLORS["muted"])

    def build_results(self, parent):
        px = self.px
        self.text_label(parent, "results", "small_bold", bg=COLORS["bg"], fg=COLORS["muted"]).pack(
            anchor="w", pady=(px(16), px(6)))
        frame = tk.Frame(parent, bg=COLORS["surface"], highlightbackground=COLORS["rule"],
                         highlightcolor=COLORS["rule"], highlightthickness=1)
        frame.pack(fill="both", expand=True)
        self.log = tk.Text(frame, height=5, wrap="word", relief="flat", bd=0, bg=COLORS["surface"],
                           fg=COLORS["ink"], font=self.fonts["file"], padx=px(12), pady=px(8),
                           spacing1=px(2), spacing3=px(2), cursor="arrow")
        sb = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", COLORS["ok"]), ("err", COLORS["err"]), ("skip", COLORS["muted"]),
                           ("empty", COLORS["faint"]), ("name", COLORS["ink"])):
            self.log.tag_configure(tag, foreground=color)

    def add_log(self, kind, key=None, **params):
        self.log_entries.append((kind, key, params))
        self.render_log()

    def render_log(self):
        marks = {"ok": "✓", "err": "✕", "skip": "–"}
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        if not self.log_entries:
            self.log.insert("end", self.tr("empty"), "empty")
        for i, (kind, key, params) in enumerate(self.log_entries):
            if i:
                self.log.insert("end", "\n")
            text = self.tr(key, **params) if key else params["text"]
            self.log.insert("end", f"{marks[kind]}  ", kind)
            self.log.insert("end", text, "name" if kind == "ok" else kind)
        self.log.see("end")
        self.log.config(state="disabled")

    # ---- 입력 ----
    def on_drag_enter(self, event):
        self.set_drop_state(dragging=True)
        return event.action

    def on_drag_leave(self, event):
        self.set_drop_state(dragging=False)
        return event.action

    def on_drop(self, event):
        self.set_drop_state(dragging=False)
        self.add_files(self.root.tk.splitlist(event.data))
        return event.action

    def choose_files(self):
        files = filedialog.askopenfilenames(title=self.tr("dialog_title"),
                                            filetypes=[(self.tr("filetype"), "*.pdf")])
        if files:
            self.add_files(files)

    def add_files(self, files):
        pdfs = [f for f in files if f.lower().endswith(".pdf") and os.path.isfile(f)]
        for f in files:
            if f not in pdfs:
                self.add_log("skip", "skipped", name=os.path.basename(f) or f)
        if not pdfs:
            return
        self.pending.extend(pdfs)
        if not self.busy:
            self.start_worker()

    # ---- 처리 ----
    def start_worker(self):
        try:
            opts = dict(
                fit_to_page=self.fit_var.get(),
                padding=float(self.pad_var.get()),
                threshold=int(self.th_var.get()),
                side_ratio=min(max(int(self.side_var.get()), 0), 100) / 100,
                max_zoom=max(float(self.zoom_var.get()), 0.0),
                uniform_size=self.uniform_var.get(),
            )
        except (tk.TclError, ValueError):
            self.add_log("skip", "bad_options")
            opts = dict(fit_to_page=self.fit_var.get(), uniform_size=self.uniform_var.get())
        files, self.pending = self.pending, []
        self.busy = True
        threading.Thread(target=self.worker, args=(files, opts), daemon=True).start()

    def worker(self, files, opts):
        q = self.queue
        for n, path in enumerate(files, 1):
            name = os.path.basename(path)
            q.put(("status", ("status_working", {"n": n, "total": len(files), "name": name})))
            q.put(("progress", 0))
            try:
                dst = process_pdf(path, **opts,
                                  progress=lambda d, t: q.put(("progress", d * 100 / t)))
                q.put(("log", ("ok", None, {"text": os.path.basename(dst)})))
            except PasswordProtectedError:
                q.put(("log", ("err", "err_password", {"name": name})))
            except Exception as e:
                q.put(("log", ("err", None, {"text": f"{name} · {e}"})))
        q.put(("done", None))

    def poll(self):
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "status":
                    self.set_status(value[0], **value[1])
                elif kind == "progress":
                    self.set_progress(value)
                elif kind == "log":
                    self.add_log(value[0], value[1], **value[2])
                elif kind == "done":
                    self.busy = False
                    if self.pending:
                        self.start_worker()
                    else:
                        self.set_status("status_done")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    enable_dpi_awareness()
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    App(root, initial_files=sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
