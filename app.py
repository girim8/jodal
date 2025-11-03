# -*- coding: utf-8 -*-
# =============================================================
# app_part1.py — 공통 유틸/변환/폰트/PDF/HWP 파이프라인 (hwp5txt 우선 → unoconv 폴백)
# =============================================================

import os
import re
import io
import sys
import zipfile
import shutil
import tempfile
import subprocess
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse, unquote

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px

# =========================
# 전역 설정 & 공용 유틸
# =========================

st.set_page_config(
    page_title="조달입찰 분석 시스템",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <meta name="robots" content="noindex,nofollow">
    <meta name="googlebot" content="noindex,nofollow">
    """,
    unsafe_allow_html=True,
)

def _redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = re.sub(r'sk-[A-Za-z0-9_\-]{20,}', '[REDACTED_KEY]', text)
    text = re.sub(r'OPENAI_API_KEY\s*=\s*["\'].*?["\']', 'OPENAI_API_KEY="[REDACTED]"', text)
    return text

def _decode_best_effort(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")

def _is_url(val: str) -> bool:
    s = str(val).strip()
    return s.startswith("http://") or s.startswith("https://")

def _filename_from_url(url: str) -> str:
    try:
        path = urlparse(url).path
        if not path:
            return url
        return unquote(path.split("/")[-1]) or url
    except Exception:
        return url

def _which(cmd: str):
    try:
        return shutil.which(cmd)
    except Exception:
        return None

# =========================
# PDF 생성 (Markdown/텍스트 → PDF)
# =========================

@st.cache_resource
def _scan_local_fonts():
    fonts = []
    base = os.path.dirname(__file__) if "__file__" in globals() else "."
    fonts_dir = os.path.join(base, "fonts")
    if os.path.isdir(fonts_dir):
        for fname in os.listdir(fonts_dir):
            lower = fname.lower()
            if lower.endswith((".ttf", ".otf", ".ttc")):
                path = os.path.join(fonts_dir, fname)
                ext = "ttc" if lower.endswith(".ttc") else ("ttf" if lower.endswith(".ttf") else "otf")
                name = os.path.splitext(fname)[0]
                fonts.append((name, path, ext))
    fonts.sort(key=lambda x: {"ttf":0, "otf":1, "ttc":2}.get(x[2], 3))
    return fonts

@st.cache_resource
def _resolve_korean_font_candidates():
    cands = []
    cands.extend(_scan_local_fonts())
    sys_ttf = [
        ("/Library/Fonts/NanumGothic.ttf", "NanumGothic", "ttf"),
        ("/Library/Fonts/AppleGothic.ttf", "AppleGothic", "ttf"),
        (r"C:\\Windows\\Fonts\\malgun.ttf", "MalgunGothic", "ttf"),
        ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "NanumGothic", "ttf"),
    ]
    for p, n, ext in sys_ttf:
        if os.path.exists(p):
            cands.append((n, p, ext))
    sys_ttc = [
        ("/System/Library/Fonts/AppleSDGothicNeo.ttc", "AppleSDGothicNeo", "ttc"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "NotoSansCJK", "ttc"),
        ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "NotoSansCJK", "ttc"),
    ]
    for p, n, ext in sys_ttc:
        if os.path.exists(p):
            cands.append((n, p, ext))
    uniq, seen = [], set()
    for name, path, ext in cands:
        key = (name, path)
        if key not in seen:
            seen.add(key)
            uniq.append((name, path, ext))
    return uniq

# ---- ReportLab 경로 (TTF/OTF만)

def _pdf_via_reportlab_from_md(md_text: str, title: str, font_name: str, font_path: str, ext: str):
    if ext not in ("ttf", "otf"):
        return None, "ReportLab 경로는 TTF/OTF만 지원 (TTC 감지)"
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib import colors
    except Exception as e:
        return None, f"[reportlab 임포트 실패] {e}"

    try:
        pdfmetrics.registerFont(TTFont(font_name, font_path))
    except Exception as e:
        return None, f"TTF/OTF 폰트 등록 실패: {font_path} — {e}"

    styles = getSampleStyleSheet()
    base = ParagraphStyle(name="KBase", parent=styles["Normal"], fontName=font_name, fontSize=10.5, leading=14.5, alignment=TA_LEFT)
    h2 = ParagraphStyle(name="KH2", parent=base, fontSize=15, leading=19, spaceBefore=8, spaceAfter=6)
    h3 = ParagraphStyle(name="KH3", parent=base, fontSize=13, leading=17, spaceBefore=6, spaceAfter=4)
    code = ParagraphStyle(name="KCode", parent=base, fontName=font_name, fontSize=9.5, leading=13, backColor=colors.whitesmoke, borderPadding=(4,4,4,4))
    bullet = ParagraphStyle(name="KBullet", parent=base, leftIndent=12)

    def esc(s: str) -> str:
        return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\t","    "))

    lines = (md_text or "").replace("\r\n","\n").split("\n")
    flow = []
    if title:
        flow.append(Paragraph(esc(title), h2)); flow.append(Spacer(1, 6))

    in_code = False
    code_buf, table_buf = [], []

    def flush_code():
        if code_buf:
            block = esc("\n".join(code_buf)).replace("  ", "&nbsp;&nbsp;").replace("\n","<br/>")
            flow.append(Paragraph(block, code))
            flow.append(Spacer(1, 4))
            code_buf.clear()

    def flush_table():
        if not table_buf:
            return
        rows = []
        for row in table_buf:
            cells = [c.strip() for c in row.strip("|").split("|")]
            rows.append(cells)
        tbl = Table(rows, hAlign='LEFT')
        tbl.setStyle(TableStyle([
            ('FONT', (0,0), (-1,-1), font_name, 9.5),
            ('INNERGRID', (0,0), (-1,-1), 0.25, colors.lightgrey),
            ('BOX', (0,0), (-1,-1), 0.5, colors.lightgrey),
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f1f5ff")),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ]))
        flow.append(KeepTogether(tbl))
        flow.append(Spacer(1, 4))
        table_buf.clear()

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                in_code = False
                flush_code()
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line); continue

        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            table_buf.append(line); continue
        else:
            flush_table()

        if line.startswith("## "):
            flow.append(Paragraph(esc(line[3:].strip()), h2)); flow.append(Spacer(1, 2)); continue
        if line.startswith("### "):
            flow.append(Paragraph(esc(line[4:].strip()), h3)); flow.append(Spacer(1, 2)); continue

        if re.match(r"^\s*[-*]\s+", line):
            flow.append(Paragraph("• " + esc(re.sub(r"^\s*[-*]\s+","",line)), bullet)); continue
        if re.match(r"^\s*\d+\.\s+", line):
            flow.append(Paragraph(esc(line), bullet)); continue

        if line.strip()=="":
            flow.append(Spacer(1,4))
        else:
            flow.append(Paragraph(esc(line), base))

    flush_code(); flush_table()

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4,
                                leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
        doc.build(flow)
        buffer.seek(0)
        return buffer.read(), f"OK[ReportLab Markdown] font={font_name} ({font_path})"
    except Exception as e:
        return None, f"ReportLab PDF 실패: {e}"

# ---- Pillow 경로 (TTC 포함)

def _pdf_via_pillow_text(text: str, title: str, font_path: str, ext: str):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        return None, f"[Pillow 임포트 실패] {e}"

    DPI = 300
    A4_W, A4_H = int(8.27 * DPI), int(11.69 * DPI)
    L, R, T, B = int(0.6 * DPI), int(0.6 * DPI), int(0.7 * DPI), int(0.7 * DPI)
    MAX_W = A4_W - L - R

    font = None
    last_err = None
    if ext == "ttc":
        for idx in range(0, 8):
            try:
                font = ImageFont.truetype(font_path, size=22, index=idx)
                break
            except Exception as e:
                last_err = e
        if font is None:
            return None, f"Pillow 폰트 로드 실패(TTC): {last_err}"
    else:
        try:
            font = ImageFont.truetype(font_path, size=22)
        except Exception as e:
            return None, f"Pillow 폰트 로드 실패: {e}"

    line_gap = 10
    title_font = font.font_variant(size=28)

    def wrap(draw, text, f):
        words = text.split(" ")
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip() if cur else w
            if draw.textlength(test, font=f) <= MAX_W:
                cur = test
            else:
                if cur: lines.append(cur)
                if draw.textlength(w, font=f) > MAX_W:
                    acc = ""
                    for ch in w:
                        if draw.textlength((acc+ch), font=f) <= MAX_W:
                            acc += ch
                        else:
                            lines.append(acc if acc else ch)
                            acc = ch
                    cur = acc
                else:
                    cur = w
        if cur: lines.append(cur)
        return lines

    blocks = []
    if title: blocks.append(("__TITLE__", title))
    for p in (text or "").replace("\r\n","\n").split("\n\n"):
        if p: blocks.append(("P", p))

    pages = []
    img = Image.new("L", (A4_W, A4_H), 255)
    draw = ImageDraw.Draw(img)
    y = T

    if blocks and blocks[0][0] == "__TITLE__":
        for ln in wrap(draw, blocks[0][1], title_font):
            draw.text((L, y), ln, font=title_font, fill=0); y += title_font.size + line_gap
        y += 10; blocks = blocks[1:]

    for _, para in blocks:
        lines = []
        for ln in para.split("\n"):
            lines.extend(wrap(draw, ln, font))
        hline = font.size + line_gap
        needed = len(lines) * hline + 8
        if y + needed > (A4_H - B):
            pages.append(img)
            img = Image.new("L", (A4_W, A4_H), 255)
            draw = ImageDraw.Draw(img)
            y = T
        for ln in lines:
            draw.text((L, y), ln, font=font, fill=0); y += hline
        y += 8
    pages.append(img)

    try:
        bio = BytesIO()
        pages[0].save(bio, format="PDF", save_all=True, append_images=pages[1:])
        bio.seek(0)
        return bio.read(), "OK[Pillow text]"
    except Exception as e:
        return None, f"Pillow PDF 저장 실패: {e}"


def markdown_to_pdf_korean(md_text: str, title: str|None=None):
    cands = _resolve_korean_font_candidates()
    if not cands:
        return None, "사용할 한글 폰트를 찾지 못했습니다. ./fonts 에 TTF 한 개만 넣어두면 깔끔합니다."
    name, path, ext = cands[0]
    pdf, dbg = _pdf_via_reportlab_from_md(md_text or "", title or "", name, path, ext)
    if pdf:
        return pdf, dbg
    return _pdf_via_pillow_text(md_text or "", title or "", path, ext)


def text_to_pdf_bytes_korean(text: str, title: str|None=None):
    return markdown_to_pdf_korean(text, title=title)

# =========================
# PDF/HWPX/PDF텍스트 추출
# =========================

def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(BytesIO(file_bytes))
        texts = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            texts.append(txt)
        return "\n".join(texts).strip()
    except Exception as e:
        return f"[PDF 추출 실패] {e}"


def extract_text_from_hwpx_bytes(hwpx_bytes: bytes) -> str:
    """HWPX(ZIP) → section XML에서 텍스트 추출"""
    try:
        import xml.etree.ElementTree as ET
    except Exception as e:
        return f"[HWPX 파싱 실패] xml 모듈 오류: {e}"

    try:
        buf = BytesIO(hwpx_bytes)
        with zipfile.ZipFile(buf, 'r') as zf:
            section_files = [n for n in zf.namelist() if n.startswith("Contents/") and n.lower().endswith(".xml")]
            section_files.sort()
            out_lines = []
            for name in section_files:
                try:
                    xml_data = zf.read(name)
                    root = ET.fromstring(xml_data)
                    text_parts = []
                    for elem in root.iter():
                        tag = elem.tag
                        if tag.endswith("t") or tag.endswith("para") or tag.endswith("run"):
                            if elem.text and elem.text.strip():
                                text_parts.append(elem.text.strip())
                    if text_parts:
                        out_lines.append("\n".join(text_parts))
                except Exception as e:
                    out_lines.append(f"[{name} 파싱 경고] {e}")
            return "\n\n".join(out_lines).strip()
    except Exception as e:
        return f"[HWPX ZIP 열기 실패] {e}"

# =========================
# HWP 변환 파이프라인 (우선순위: hwp5txt → unoconv)
# =========================

# --- 1) hwp5txt: .hwp → 텍스트

def _run_hwp5txt_to_text(file_bytes: bytes, filename: str):
    tool = _which("hwp5txt")
    if not tool:
        return "", "hwp5txt 미설치"
    in_fd, in_path = tempfile.mkstemp(suffix=".hwp"); os.close(in_fd)
    with open(in_path, "wb") as f:
        f.write(file_bytes)
    try:
        cp = subprocess.run([tool, in_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        if cp.returncode == 0 and cp.stdout:
            txt = cp.stdout.strip()
            try: os.remove(in_path)
            except Exception: pass
            return txt, "OK[hwp5txt]"
        dbg = f"hwp5txt 실패: {cp.stderr[:200]}"
        return "", dbg
    except subprocess.TimeoutExpired:
        return "", "hwp5txt 타임아웃"
    except Exception as e:
        return "", f"hwp5txt 오류: {e}"
    finally:
        try: os.remove(in_path)
        except Exception: pass

# --- 2) unoconv/soffice: .hwp → PDF → 텍스트


def _run_unoconv_hwp_to_pdf_bytes(file_bytes: bytes, filename: str):
    unoconv_bin = _which("unoconv")
    soffice_bin = _which("soffice") or _which("libreoffice")
    in_fd, in_path = tempfile.mkstemp(suffix=".hwp"); os.close(in_fd)
    with open(in_path, "wb") as f:
        f.write(file_bytes)

    out_dir = tempfile.mkdtemp()
    out_pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(filename))[0] + ".pdf")

    try:
        if unoconv_bin:
            cp = subprocess.run([unoconv_bin, '-f', 'pdf', '-o', out_dir, in_path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
            if cp.returncode != 0:
                raise RuntimeError(f"unoconv 실패: {cp.stderr.decode(errors='ignore')[:200]}")
        elif soffice_bin:
            cp = subprocess.run([soffice_bin, '--headless', '--convert-to', 'pdf', '--outdir', out_dir, in_path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        else:
            raise FileNotFoundError("unoconv/soffice 미설치")

        if not os.path.exists(out_pdf):
            cand = next((os.path.join(out_dir, fn) for fn in os.listdir(out_dir) if fn.lower().endswith('.pdf')), None)
            if not cand:
                raise FileNotFoundError("PDF 결과 파일을 찾지 못함 (LibreOffice에서 HWP 미지원일 수 있음)")
            out_pdf = cand

        with open(out_pdf, 'rb') as rf:
            pdf_bytes = rf.read()
        return pdf_bytes, "OK[unoconv/soffice→PDF]"
    finally:
        try: os.remove(in_path)
        except Exception: pass
        try: shutil.rmtree(out_dir, ignore_errors=True)
        except Exception: pass


def _convert_hwp_priority(file_bytes: bytes, filename: str):
    """
    1) hwp5txt → TXT 직접 추출 (우선)
    2) unoconv/soffice → PDF 변환 후 PyPDF2 텍스트 추출 (폴백)
    """
    # 1) hwp5txt
    txt1, dbg1 = _run_hwp5txt_to_text(file_bytes, filename)
    if txt1:
        pdf_bytes1, dbg_pdf1 = text_to_pdf_bytes_korean(txt1, title=os.path.basename(filename) + " (hwp5txt 추출)")
        gen_pdf1 = (os.path.splitext(os.path.basename(filename))[0] + "_txt_extract.pdf", pdf_bytes1) if pdf_bytes1 else None
        return txt1, gen_pdf1, f"{dbg1} → {dbg_pdf1}"

    # 2) unoconv/soffice
    try:
        pdf_bytes2, dbg2 = _run_unoconv_hwp_to_pdf_bytes(file_bytes, filename)
        if pdf_bytes2:
            txt2 = extract_text_from_pdf(pdf_bytes2)
            gen_pdf2 = (os.path.splitext(os.path.basename(filename))[0] + "_converted.pdf", pdf_bytes2)
            return txt2, gen_pdf2, f"{dbg2} → PDF텍스트 추출"
    except Exception as e:
        dbg2 = f"unoconv/soffice 실패: {e}"

    return "", None, f"hwp5txt 실패 · {dbg1} / {dbg2 if 'dbg2' in locals() else 'unoconv 미시도'}"

# =========================
# 업로드 소스 처리 (HWP 파이프라인 통합)
# =========================

DOC_EXTS = {".doc",".docx",".hwp",".hwpx",".xls",".xlsx",".pdf",".txt",".md",".csv",".log"}


def handle_uploaded_source_files(uploaded_files):
    combined_texts, convert_logs, generated_pdfs = [], [], []
    for f in uploaded_files:
        name = f.name
        data = f.read()
        ext = os.path.splitext(name)[1].lower()

        if ext == ".pdf":
            txt = extract_text_from_pdf(data)
            convert_logs.append(f"📄 {name}: PDF 텍스트 추출 완료 ({len(txt)} chars)")
            combined_texts.append(f"\n\n===== [{name}] =====\n{_redact_secrets(txt)}\n")

        elif ext == ".hwpx":
            txt = extract_text_from_hwpx_bytes(data)
            pdf_bytes, dbg = text_to_pdf_bytes_korean(txt, title=os.path.splitext(name)[0] + " (HWPX 추출)")
            if pdf_bytes:
                generated_pdfs.append((f"{os.path.splitext(name)[0]}_hwpx_extract.pdf", pdf_bytes))
            convert_logs.append(f"📝 {name}: HWPX 파싱 완료 → {dbg}")
            combined_texts.append(f"\n\n===== [{name}] (HWPX→TXT) =====\n{_redact_secrets(txt)}\n")

        elif ext == ".hwp":
            txt, gen_pdf, dbg = _convert_hwp_priority(data, name)
            if txt:
                combined_texts.append(f"\n\n===== [{name}] (HWP→TXT) =====\n{_redact_secrets(txt)}\n")
            if gen_pdf and gen_pdf[1]:
                generated_pdfs.append(gen_pdf)
            convert_logs.append(f"📝 {name}: {dbg}")
            if not txt:
                convert_logs.append(f"🛑 {name}: 텍스트 추출 실패")

        elif ext in [".txt", ".csv", ".md", ".log"]:
            txt = _decode_best_effort(data)
            combined_texts.append(f"\n\n===== [{name}] =====\n{_redact_secrets(txt)}\n")
            convert_logs.append(f"🗒️ {name}: 텍스트 로드 완료 (auto-decode)")

        elif ext in [".docx"]:
            try:
                import docx
                doc = docx.Document(BytesIO(data))
                txt = "\n".join([p.text for p in doc.paragraphs])
                combined_texts.append(f"\n\n===== [{name}] =====\n{_redact_secrets(txt)}\n")
                convert_logs.append(f"📝 {name}: DOCX 텍스트 추출 완료")
            except Exception as e:
                convert_logs.append(f"⚠️ {name}: DOCX 텍스트 추출 실패 - {e}")

        else:
            convert_logs.append(f"ℹ️ {name}: 지원하지 않는 형식(원본 그대로 참조)")
    return "\n".join(combined_texts).strip(), convert_logs, generated_pdfs


# -*- coding: utf-8 -*-
# =============================================================
# app_part2.py — UI, 차트, 첨부 매트릭스, 로그인, GPT 래퍼 (1/2)
# (이 파일은 app_part1.py 아래에 그대로 이어붙여 한 파일로 사용 가능합니다)
# =============================================================

import os as _os
import re as _re
from io import BytesIO as _BytesIO
from datetime import datetime as _dt

import streamlit as st
import pandas as _pd
import numpy as _np
import plotly.express as px

# ----- 여기서부터 app_part1.py 내 심볼을 사용합니다 -----
# handle_uploaded_source_files, markdown_to_pdf_korean, text_to_pdf_bytes_korean, _redact_secrets, DOC_EXTS 등

# =========================
# OpenAI 래퍼 (폴백 루프)
# =========================

API_KEYS = []  # 환경변수 OPENAI_API_KEY 권장. 여기는 비워둡니다.

def call_gpt_with_fallback(messages, temperature=0.3, max_tokens=2000, model="gpt-4.1"):
    try:
        from openai import OpenAI
    except Exception:
        raise Exception("openai 미설치: pip install openai")

    keys = []
    if _os.environ.get("OPENAI_API_KEY"):
        keys.append(_os.environ["OPENAI_API_KEY"])
    keys.extend([k for k in API_KEYS if k and not str(k).startswith("sk-REPLACE_")])

    if not keys:
        raise Exception("API 키가 없습니다. OPENAI_API_KEY 환경변수를 설정하세요.")

    guardrail_system = {
        "role": "system",
        "content": (
            "안전 가드레일을 준수하세요. 민감정보/API키는 노출 금지. "
            "외부 웹 다운로드/크롤링은 수행하지 말고, 사용자가 파일 업로드한 데이터만 분석하세요."
        ),
    }

    safe_messages = [guardrail_system]
    for m in messages:
        safe_messages.append({"role": m["role"], "content": _redact_secrets(m.get("content",""))})

    errors = []
    for idx, k in enumerate(keys, 1):
        try:
            client = OpenAI(api_key=k)
            resp = client.chat.completions.create(
                model=model,
                messages=safe_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content, idx
        except Exception as e:
            errors.append(f"[키 {idx}] {str(e)[:200]}")
            continue
    raise Exception("모든 키 실패:\n" + "\n".join(errors))

# =========================
# 시각화 컬러 & 업체 정규화
# =========================

VENDOR_COLOR_MAP = {
    "엘지유플러스": "#FF1493",
    "케이티": "#FF0000",
    "에스케이브로드밴드": "#FFD700",
    "에스케이텔레콤": "#1E90FF",
}
OTHER_SEQ = ["#2E8B57","#6B8E23","#556B2F","#8B4513","#A0522D","#CD853F","#228B22","#006400"]


def normalize_vendor(name: str) -> str:
    s = str(name) if _pd.notna(name) else ""
    if "엘지유플러스" in s or "LG유플러스" in s or "LG U" in s.upper():
        return "엘지유플러스"
    if s.startswith("케이티") or " KT" in s or s == "KT" or "주식회사 케이티" in s:
        return "케이티"
    if "브로드밴드" in s or "SK브로드밴드" in s:
        return "에스케이브로드밴드"
    if "텔레콤" in s or "SK텔레콤" in s:
        return "에스케이텔레콤"
    return s or "기타"

# =========================
# 첨부 링크 매트릭스
# =========================

def build_attachment_matrix(df_like: _pd.DataFrame, title_col: str) -> _pd.DataFrame:
    if title_col not in df_like.columns:
        return _pd.DataFrame(columns=[title_col,"본공고링크","제안요청서","공고서","과업지시서","규격서","기타"])
    buckets = {}
    def add_link(title, category, name, url):
        if title not in buckets:
            buckets[title] = {k:{} for k in ["본공고링크","제안요청서","공고서","과업지시서","규격서","기타"]}
        if url not in buckets[title][category]:
            buckets[title][category][url] = name

    n_cols = df_like.shape[1]
    for _, row in df_like.iterrows():
        title = str(row.get(title_col, "")).strip()
        if not title:
            continue
        for j in range(1, n_cols):
            url_col = df_like.columns[j]
            name_col = df_like.columns[j-1]
            url_val = row.get(url_col, None)
            name_val = row.get(name_col, None)
            if _pd.isna(url_val):
                continue
            raw = str(url_val).strip()
            if _is_url(raw):
                urls = [raw]
            else:
                toks = [u.strip() for u in raw.replace("\n",";").split(";")]
                urls = [u for u in toks if _is_url(u)]
                if not urls:
                    continue
            name_base = "" if _pd.isna(name_val) else str(name_val).strip()
            name_tokens = [n.strip() for n in name_base.replace("\n",";").split(";")] if name_base else []
            for k, u in enumerate(urls):
                disp_name = name_tokens[k] if k < len(name_tokens) and name_tokens[k] else (name_base or _filename_from_url(u))
                low_name = (disp_name or "").lower() + " " + _filename_from_url(u).lower()
                if ("제안요청서" in low_name) or ("rfp" in low_name):
                    add_link(title,"제안요청서",disp_name,u)
                elif ("공고서" in low_name) or ("공고문" in low_name):
                    add_link(title,"공고서",disp_name,u)
                elif "과업지시서" in low_name:
                    add_link(title,"과업지시서",disp_name,u)
                elif ("규격서" in low_name) or ("spec" in low_name):
                    add_link(title,"규격서",disp_name,u)
                elif ("http://" in u) or ("https://" in u):
                    add_link(title,"본공고링크",disp_name,u)
                else:
                    add_link(title,"기타",disp_name,u)

    def join_html(d):
        if not d: return ""
        return " | ".join([f"<a href='{url}' target='_blank' rel='nofollow noopener'>{name}</a>" for url, name in d.items()])

    rows = []
    for title, catmap in buckets.items():
        rows.append({
            title_col: title,
            "본공고링크": join_html(catmap["본공고링크"]),
            "제안요청서": join_html(catmap["제안요청서"]),
            "공고서": join_html(catmap["공고서"]),
            "과업지시서": join_html(catmap["과업지시서"]),
            "규격서": join_html(catmap["규격서"]),
            "기타": join_html(catmap["기타"]),
        })
    out_df = _pd.DataFrame(rows)
    out_df = out_df.sort_values(by=[title_col]).reset_index(drop=True)
    return out_df


def render_attachment_table_html(df_links: _pd.DataFrame, title_col: str,
                                 min_title_px: int = 360, wide_link_px: int = 440, narrow_px: int = 280) -> str:
    cols = [title_col,"본공고링크","제안요청서","공고서","과업지시서","규격서","기타"]
    present_cols = [c for c in cols if c in df_links.columns]
    def _th(c):
        if c == title_col: return f'<th class="col-title">{c}</th>'
        elif c in ["본공고링크","제안요청서","공고서"]: return f'<th class="col-linkwide">{c}</th>'
        else: return f'<th class="col-narrow">{c}</th>'
    def _td(c, v):
        cls = "col-title" if c == title_col else ("col-linkwide" if c in ["본공고링크","제안요청서","공고서"] else "col-narrow")
        val = "" if _pd.isna(v) else str(v)
        return f'<td class="{cls}">{val}</td>'
    html = []
    html.append(f"""
<style>
.attach-table {{
  width: 100%; border-collapse: collapse; table-layout: fixed; word-wrap: break-word; font-size: 11px; line-height: 1.35;
}}
.attach-table th, .attach-table td {{ border: 1px solid #d0d7de; padding: 6px 8px; vertical-align: top; }}
.attach-table thead th {{ background: #0d6efd; color: white; font-weight: 700; font-size: 11px; position: sticky; top: 0; z-index: 1; }}
.attach-table td a {{ text-decoration: none; color: #0b5ed7; }}
.attach-table td a:hover {{ text-decoration: underline; }}
.attach-table th.col-title, .attach-table td.col-title {{ min-width: {min_title_px}px; width: {min_title_px}px; white-space: normal; }}
.attach-table th.col-linkwide, .attach-table td.col-linkwide {{ min-width: {wide_link_px}px; width: {wide_link_px}px; white-space: normal; }}
.attach-table th.col-narrow, .attach-table td.col-narrow {{ min-width: {narrow_px}px; width: {narrow_px}px; white-space: normal; }}
</style>
<table class="attach-table">
  <thead><tr>{''.join([_th(c) for c in present_cols])}</tr></thead>
  <tbody>
""")
    for _, r in df_links.iterrows():
        html.append("<tr>")
        for c in present_cols:
            html.append(_td(c, r.get(c, "")))
        html.append("</tr>")
    html.append("</tbody></table>")
    return "\n".join(html)


def render_attachment_cards_html(df_links: _pd.DataFrame, title_col: str) -> str:
    cat_cols = ["본공고링크","제안요청서","공고서","과업지시서","규격서","기타"]
    present_cols = [c for c in cat_cols if c in df_links.columns]
    if title_col not in df_links.columns:
        return "<p>표시할 데이터가 없습니다.</p>"
    css = """
<style>
.attch-wrap { display: flex; flex-direction: column; gap: 14px; background: #eef6ff; padding: 8px; border-radius: 12px; }
.attch-card { border: 1px solid #cfe1ff; border-radius: 12px; padding: 12px 14px; background: #f4f9ff; box-shadow: 0 1px 3px rgba(13,110,253,0.05); }
.attch-title { font-weight: 700; margin-bottom: 8px; font-size: 13px; line-height: 1.4; word-break: break-word; color: #0b2e5b; }
.attch-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.attch-box { border: 1px solid #cfe1ff; border-radius: 10px; overflow: hidden; background: #ffffff; }
.attch-box-header { background: #0d6efd; color: #fff; font-weight: 700; font-size: 11px; padding: 6px 8px; display: flex; align-items: center; justify-content: space-between; }
.badge { background: rgba(255,255,255,0.2); color: #fff; padding: 0 6px; border-radius: 999px; font-size: 10px; }
.attch-box-body { padding: 8px; font-size: 12px; line-height: 1.45; word-break: break-word; color: #0b2447; }
.attch-box-body a { color: #0b5ed7; text-decoration: none; }
.attch-box-body a:hover { text-decoration: underline; }
.attch-box-body details summary { cursor: pointer; font-weight: 600; list-style: none; outline: none; color: #0b2447; }
.attch-box-body details summary::-webkit-details-marker { display: none; }
.attch-box-body details summary:after { content: "▼"; font-size: 10px; margin-left: 6px; color: #0b2447; }
</style>
"""
    html = [css, '<div class="attch-wrap">']
    for _, r in df_links.iterrows():
        title = str(r.get(title_col, "") or "")
        html.append('<div class="attch-card">')
        html.append(f'<div class="attch-title">{title}</div>')
        html.append('<div class="attch-grid">')
        for col in present_cols:
            raw = str(r.get(col, "") or "").strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split("|") if p.strip()]
            count = len(parts)
            if count <= 3:
                body_html = raw
            else:
                head = " | ".join(parts[:3])
                tail = " | ".join(parts[3:])
                body_html = head + f'<details style="margin-top:6px;"><summary>더보기 ({count-3})</summary>{tail}</details>'
            html.append('<div class="attch-box">')
            html.append(f'<div class="attch-box-header">{col} <span class="badge">{count}</span></div>')
            html.append(f'<div class="attch-box-body">{body_html}</div>')
            html.append('</div>')
        html.append('</div></div>')
    html.append("</div>")
    return "\n".join(html)

# =========================
# 차트 렌더
# =========================

def render_basic_analysis_charts(base_df: _pd.DataFrame):
    def pick_unit(max_val: float):
        if max_val >= 1_0000_0000_0000: return ("조원", 1_0000_0000_0000)
        elif max_val >= 100_000_000: return ("억원", 100_000_000)
        elif max_val >= 1_000_000: return ("백만원", 1_000_000)
        else: return ("원", 1)
    def apply_unit(values: _pd.Series, mode: str = "자동"):
        unit_map = {"원":("원",1),"백만원":("백만원",1_000_000),"억원":("억원",100_000_000),"조원":("조원",1_0000_0000_0000)}
        if mode == "자동":
            u, f = pick_unit(values.max() if len(values) else 0); return values / f, u
        else:
            u, f = unit_map.get(mode, ("원",1)); return values / f, u

    st.markdown("## 📊 기본 통계 분석")
    st.caption("※ 이하 모든 차트는 **낙찰자선정여부 == 'Y'** 기준으로 작성됩니다.")

    if "낙찰자선정여부" not in base_df.columns:
        st.warning("컬럼 '낙찰자선정여부'를 찾을 수 없습니다."); return

    dwin = base_df[base_df["낙찰자선정여부"] == "Y"].copy()
    if dwin.empty:
        st.warning("낙찰(Y) 데이터가 없습니다."); return

    for col in ["투찰금액","배정예산금액","투찰율"]:
        if col in dwin.columns:
            dwin[col] = _pd.to_numeric(dwin[col], errors="coerce")

    if "대표업체" in dwin.columns:
        dwin["대표업체_표시"] = dwin["대표업체"].map(normalize_vendor)
    else:
        dwin["대표업체_표시"] = "기타"

    st.markdown("### 1) 대표업체별 분포")
    unit_choice = st.selectbox("파이차트(투찰금액 합계) 표기 단위", ["자동","원","백만원","억원","조원"], index=0)
    col_pie1, col_pie2 = st.columns(2)

    with col_pie1:
        if "투찰금액" in dwin.columns:
            sum_by_company = dwin.groupby("대표업체_표시")["투찰금액"].sum().reset_index().sort_values("투찰금액", ascending=False)
            scaled_vals, unit_label = apply_unit(sum_by_company["투찰금액"].fillna(0), unit_choice)
            sum_by_company["표시금액"] = scaled_vals
            fig1 = px.pie(sum_by_company, names="대표업체_표시", values="표시금액",
                          title=f"대표업체별 투찰금액 합계 — 단위: {unit_label}",
                          color="대표업체_표시", color_discrete_map=VENDOR_COLOR_MAP, color_discrete_sequence=OTHER_SEQ)
            fig1.update_traces(
                hovertemplate="<b>%{label}</b><br>금액: %{value:,.2f} " + unit_label + "<br>비중: %{percent}",
                texttemplate="%{label}<br>%{value:,.2f} " + unit_label, textposition="auto")
            st.plotly_chart(fig1, use_container_width=True)
        else:
            st.info("투찰금액 컬럼이 없어 파이차트(금액)를 건너뜁니다.")

    with col_pie2:
        cnt_by_company = dwin["대표업체_표시"].value_counts().reset_index()
        cnt_by_company.columns = ["대표업체_표시","건수"]
        fig2 = px.pie(cnt_by_company, names="대표업체_표시", values="건수",
                      title="대표업체별 낙찰 건수",
                      color="대표업체_표시", color_discrete_map=VENDOR_COLOR_MAP, color_discrete_sequence=OTHER_SEQ)
        fig2.update_traces(hovertemplate="<b>%{label}</b><br>건수: %{value:,}건<br>비중: %{percent}",
                           texttemplate="%{label}<br>%{value:,}건", textposition="auto")
        st.plotly_chart(fig2, use_container_width=True)

    st.markdown("### 2) 투찰율 산점도  &  3) 업체/년도별 수주금액")
    col_scatter, col_bar3 = st.columns(2)
    with col_scatter:
        if "투찰율" in dwin.columns:
            dwin["공고게시일자_date"] = _pd.to_datetime(dwin.get("공고게시일자_date", _pd.NaT), errors="coerce")
            dplot = dwin.dropna(subset=["투찰율", "공고게시일자_date"]).copy()
            dplot = dplot[dplot["투찰율"] <= 300]
            hover_cols = [c for c in ["대표업체_표시","수요기관명","공고명","입찰공고명","입찰공고번호"] if c in dplot.columns]
            fig_scatter = px.scatter(dplot, x="공고게시일자_date", y="투찰율",
                                     hover_data=hover_cols, title="투찰율 산점도",
                                     color="대표업체_표시", color_discrete_map=VENDOR_COLOR_MAP, color_discrete_sequence=OTHER_SEQ)
            st.plotly_chart(fig_scatter, use_container_width=True)
        else:
            st.info("투찰율 컬럼 없음 - 산점도 생략")

    with col_bar3:
        if "투찰금액" in dwin.columns:
            dyear = dwin.copy()
            dyear["연도"] = _pd.to_datetime(dyear.get("공고게시일자_date", _pd.NaT), errors="coerce").dt.year
            dyear = dyear.dropna(subset=["연도"])
            by_vendor_year = dyear.groupby(["연도","대표업체_표시"])["투찰금액"].sum().reset_index()
            fig_vy = px.bar(by_vendor_year, x="연도", y="투찰금액", color="대표업체_표시",
                            barmode="group", title="업체/년도별 수주금액",
                            color_discrete_map=VENDOR_COLOR_MAP, color_discrete_sequence=OTHER_SEQ)
            fig_vy.update_traces(hovertemplate="<b>%{x}년</b><br>%{legendgroup}: %{y:,.0f} 원")
            st.plotly_chart(fig_vy, use_container_width=True)
        else:
            st.info("투찰금액 컬럼이 없어 '업체/년도별 수주금액'을 표시할 수 없습니다.")

    st.markdown("### 4) 연·분기별 배정예산금액 — 누적 막대 & 총합")
    col_stack, col_total = st.columns(2)

    if "배정예산금액" not in dwin.columns:
        with col_stack:
            st.info("배정예산금액 컬럼 없음 - 막대그래프 생략")
        return

    dwin["공고게시일자_date"] = _pd.to_datetime(dwin.get("공고게시일자_date", _pd.NaT), errors="coerce")
    g = dwin.dropna(subset=["공고게시일자_date"]).copy()
    if g.empty:
        with col_stack: st.info("유효한 날짜가 없어 그래프를 표시할 수 없습니다.")
        return

    g["연도"] = g["공고게시일자_date"].dt.year
    g["분기"] = g["공고게시일자_date"].dt.quarter
    g["연도분기"] = g["연도"].astype(str) + " Q" + g["분기"].astype(str)
    if "대표업체_표시" not in g.columns:
        g["대표업체_표시"] = g.get("대표업체", _pd.Series([""]*len(g))).map(normalize_vendor)

    title_col = "입찰공고명" if "입찰공고명" in g.columns else ("공고명" if "공고명" in g.columns else None)
    group_col = "대표업체_표시"
    if group_col not in g.columns:
        with col_stack:
            st.info("대표업체_표시 컬럼이 없어 누적 막대를 표시할 수 없습니다.")
        return

    with col_stack:
        grp = (
            g.groupby(["연도분기", group_col])["배정예산금액"]
            .sum().reset_index(name="금액합")
        )
        if not grp.empty:
            if title_col:
                title_map = (
                    g.groupby(["연도분기", group_col])[title_col]
                    .apply(lambda s: " | ".join(_pd.Series(s).dropna().astype(str).unique()[:10]))
                    .rename("입찰공고목록").reset_index()
                )
                grp = grp.merge(title_map, on=["연도분기", group_col], how="left")
                grp["입찰공고목록"] = grp["입찰공고목록"].fillna("")
            else:
                grp["입찰공고목록"] = ""

            grp["연"] = grp["연도분기"].str.extract(r"(\d{4})").astype(int)
            grp["분"] = grp["연도분기"].str.extract(r"Q(\d)").astype(int)
            grp = grp.sort_values(["연","분",group_col]).reset_index(drop=True)
            ordered_quarters = grp.sort_values(["연","분"])["연도분기"].unique()
            grp["연도분기"] = _pd.Categorical(grp["연도분기"], categories=ordered_quarters, ordered=True)

            custom = _np.column_stack([grp[group_col].astype(str).to_numpy(),
                                      grp["입찰공고목록"].astype(str).to_numpy()])

            fig_stack = px.bar(
                grp, x="연도분기", y="금액합",
                color=group_col, barmode="stack",
                title=f"연·분기별 배정예산금액 — 누적(스택) / 그룹: {group_col}",
                color_discrete_map=VENDOR_COLOR_MAP, color_discrete_sequence=OTHER_SEQ,
            )
            fig_stack.update_traces(
                customdata=custom,
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    f"{group_col}: %{{customdata[0]}}<br>"
                    "금액: %{{y:,.0f}} 원<br>"
                    "입찰공고명: %{{customdata[1]}}"
                )
            )
            fig_stack.update_layout(xaxis_title="연도분기", yaxis_title="배정예산금액 (원)",
                                    margin=dict(l=10,r=10,t=60,b=10))
            st.plotly_chart(fig_stack, use_container_width=True)
        else:
            st.info("그룹핑 결과가 비어 있습니다.")

    with col_total:
        grp_total = g.groupby("연도분기")["배정예산금액"].sum().reset_index(name="금액합")
        grp_total["연"] = grp_total["연도분기"].str.extract(r"(\d{4})").astype(int)
        grp_total["분"] = grp_total["연도분기"].str.extract(r"Q(\d)").astype(int)
        grp_total = grp_total.sort_values(["연","분"])
        if title_col:
            titles_total = g.groupby("연도분기")[title_col].apply(
                lambda s: " | ".join(_pd.Series(s).dropna().astype(str).unique()[:10])
            ).reindex(grp_total["연도분기"]).fillna("")
            custom2 = _np.stack([titles_total], axis=-1)
        else:
            custom2 = _np.stack([_pd.Series([""])], axis=-1)
        fig_bar = px.bar(grp_total, x="연도분기", y="금액합", title="연·분기별 배정예산금액 (총합)", text="금액합")
        fig_bar.update_traces(
            customdata=custom2,
            hovertemplate="<b>%{x}</b><br>총액: %{y:,.0f} 원<br>입찰공고명: %{customdata[0]}",
            texttemplate='%{text:,.0f}', textposition='outside', cliponaxis=False
        )
        st.plotly_chart(fig_bar, use_container_width=True)

# -*- coding: utf-8 -*-
# =============================================================
# app_part2.py — UI, 차트, 첨부 매트릭스, 로그인, GPT 래퍼 (2/2)
# (이 파일은 app_part1.py 및 app_part2.py(1/2) 아래에 그대로 이어붙여 한 파일로 사용 가능합니다)
# =============================================================

import os as _os
import re as _re
from io import BytesIO as _BytesIO
from datetime import datetime as _dt

import streamlit as st
import pandas as _pd
import numpy as _np
import plotly.express as px

# =========================
# 로그인 게이트
# =========================

def login_gate():
    import streamlit.components.v1 as components
    components.html(
        """
        <script>
        (function(){
          if (!window.__warned__) {
            alert("본 사이트는 All Rights Reserved이며, 허가받지 않은 사용자는 사용 시 법적인 책임을 집니다.");
            window.__warned__ = true;
          }
        })();
        </script>
        """,
        height=0,
    )
    st.title("🔐 로그인")
    emp = st.text_input("사번", value="", placeholder="예: 9999")
    dob = st.text_input("생년월일(YYMMDD)", value="", placeholder="예: 990101")
    col1, col2 = st.columns([1,1])
    with col1:
        if st.button("로그인", type="primary", use_container_width=True):
            if emp == "2855" and dob == "910518":  # 데모 자격
                st.session_state["authed"] = True
                st.success("로그인 성공")
                st.rerun()
            else:
                st.error("인증 실패. 사번/생년월일을 확인하세요.")
    with col2:
        st.info("SK브로드밴드 사번 4자리와 생년월일 6자리를 입력하세요. 문의 : 8girim@sk.com")


# =========================
# 메인 앱
# =========================

def main():
    # 세션 상태
    if "gpt_report_md" not in st.session_state:
        st.session_state["gpt_report_md"] = None
    if "generated_src_pdfs" not in st.session_state:
        st.session_state["generated_src_pdfs"] = []
    if "authed" not in st.session_state:
        st.session_state["authed"] = False
    if "chat_messages" not in st.session_state:
        st.session_state["chat_messages"] = []

    AUThed = st.session_state.get("authed", False)

    # 사이드바
    st.sidebar.title("📂 데이터 업로드")
    uploaded_file = st.sidebar.file_uploader(
        "filtered 시트가 포함된 병합 엑셀 업로드 (.xlsx)", type=["xlsx"], disabled=not AUThed
    )
    menu = st.sidebar.radio("# 📋 메뉴 선택", ["조달입찰결과현황","내고객 분석하기"], disabled=not AUThed)

    # GPT 상태
    _ok = bool(_os.environ.get("OPENAI_API_KEY"))
    st.sidebar.success("GPT 사용 가능" if _ok else "GPT 버튼 활성 (환경변수 필요)")

    gpt_extra_req = st.sidebar.text_area("🤖 GPT 추가 요구사항(선택)", height=120,
                                         placeholder="예) 'MACsec, SRv6 강조', '세부 일정 표 추가' 등",
                                         disabled=not AUThed)

    if not AUThed:
        login_gate()
        st.stop()

    if not uploaded_file:
        st.title("📊 조달입찰 분석 시스템")
        st.info("좌측에서 'filtered' 시트를 포함한 엑셀 파일을 업로드하세요.")
        st.stop()

    # 엑셀 로드
    df = _pd.read_excel(uploaded_file, sheet_name="filtered", engine="openpyxl")
    df_original = df.copy()

    # 필터
    st.sidebar.markdown(" 🔍 데이터 컬럼 확인")
    if st.sidebar.checkbox("컬럼명 보기", value=False):
        st.sidebar.write(df.columns.tolist())

    only_winner = st.sidebar.checkbox("(필터)낙찰자선정여부 = 'Y' 만 보기", value=True)

    if "대표업체" in df.columns:
        company_list = sorted(df["대표업체"].dropna().unique())
        selected_companies = st.sidebar.multiselect("대표업체 필터 (복수 가능)", company_list)
    else:
        selected_companies = []

    demand_col_sidebar = "수요기관명" if "수요기관명" in df.columns else ("수요기관" if "수요기관" in df.columns else None)
    if demand_col_sidebar:
        org_list = sorted(df[demand_col_sidebar].dropna().unique())
        selected_orgs = st.sidebar.multiselect(f"{demand_col_sidebar} 필터 (복수 가능)", org_list)
    else:
        selected_orgs = []

    st.sidebar.subheader("📆 공고게시일자 필터")
    if "공고게시일자_date" in df.columns:
        df["공고게시일자_date"] = _pd.to_datetime(df["공고게시일자_date"], errors="coerce")
    else:
        df["공고게시일자_date"] = _pd.NaT
    df["year"] = df["공고게시일자_date"].dt.year
    df["month"] = df["공고게시일자_date"].dt.month
    year_list = sorted([int(x) for x in df["year"].dropna().unique()])
    selected_years = st.sidebar.multiselect("연도 선택 (복수 가능)", year_list, default=[])
    month_list = list(range(1,13))
    selected_months = st.sidebar.multiselect("월 선택 (복수 가능)", month_list, default=[])
    st.sidebar.markdown("---")

    df_filtered = df.copy()
    if selected_years: df_filtered = df_filtered[df_filtered["year"].isin(selected_years)]
    if selected_months: df_filtered = df_filtered[df_filtered["month"].isin(selected_months)]
    if only_winner and "낙찰자선정여부" in df_filtered.columns:
        df_filtered = df_filtered[df_filtered["낙찰자선정여부"] == "Y"]
    if selected_companies and "대표업체" in df_filtered.columns:
        df_filtered = df_filtered[df_filtered["대표업체"].isin(selected_companies)]
    if selected_orgs and demand_col_sidebar:
        df_filtered = df_filtered[df_filtered[demand_col_sidebar].isin(selected_orgs)]

    # 페이지 분기
    if menu == "조달입찰결과현황":
        st.title("📑 조달입찰결과현황")

        dl_buf = _BytesIO()
        df_filtered.to_excel(dl_buf, index=False, engine="openpyxl"); dl_buf.seek(0)
        st.download_button(
            label="📥 필터링된 데이터 다운로드 (Excel)",
            data=dl_buf,
            file_name=f"filtered_result_{_dt.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        st.data_editor(df_filtered, use_container_width=True, key="result_editor", height=520)

        with st.expander("📊 기본 통계 분석(차트) 열기", expanded=False):
            render_basic_analysis_charts(df_filtered)

    else:
        st.title("🧑‍💼 내고객 분석하기")
        st.info("ℹ️ 이 메뉴는 사이드바 필터와 무관하게 **전체 원본 데이터**를 대상으로 검색합니다.")

        demand_col = None
        for col in ["수요기관명","수요기관","기관명"]:
            if col in df_original.columns:
                demand_col = col; break
        if not demand_col:
            st.error("⚠️ 수요기관 관련 컬럼을 찾을 수 없습니다."); st.stop()
        st.success(f"✅ 검색 대상 컬럼: **{demand_col}**")

        customer_input = st.text_input(f"고객사명을 입력하세요 ({demand_col} 기준, 쉼표로 복수 입력 가능)", help="예) 조달청, 국방부")

        with st.expander(f"📋 전체 {demand_col} 목록 보기 (검색 참고용)"):
            unique_orgs = sorted(df_original[demand_col].dropna().unique())
            st.write(f"총 {len(unique_orgs)}개 기관")
            search_org = st.text_input("기관명 검색", key="search_org_in_my")
            view_orgs = [o for o in unique_orgs if (search_org in str(o))] if search_org else unique_orgs
            st.write(view_orgs[:120])

        if customer_input:
            customers = [c.strip() for c in customer_input.split(",") if c.strip()]
            if customers:
                result = df_original[df_original[demand_col].isin(customers)]
                st.subheader(f"📊 검색 결과: {len(result)}건")
                if not result.empty:
                    rb = _BytesIO()
                    result.to_excel(rb, index=False, engine="openpyxl"); rb.seek(0)
                    st.download_button(
                        label="📥 결과 데이터 다운로드 (Excel)",
                        data=rb,
                        file_name=f"{'_'.join(customers)}_이력_{_dt.now().strftime('%Y%m%d_%H%M')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

                    st.data_editor(result, use_container_width=True, key="customer_editor", height=520)

                    st.markdown("---")
                    st.subheader("🔗 입찰공고명 기준으로 URL을 분류합니다.")
                    st.caption("(본공고링크/제안요청서/공고서/과업지시서/규격서/기타, URL 중복 제거)")

                    title_col_candidates = ["입찰공고명","공고명"]
                    title_col = next((c for c in title_col_candidates if c in result.columns), None)

                    if not title_col:
                        st.error("⚠️ '입찰공고명' 또는 '공고명' 컬럼을 찾을 수 없습니다.")
                    else:
                        attach_df = build_attachment_matrix(result, title_col)
                        if attach_df.empty:
                            st.info("분류할 수 있는 링크를 찾지 못했습니다.")
                        else:
                            use_compact = st.toggle("🔀 그룹형(Compact) 보기로 전환", value=True,
                                                    help="가로폭을 줄이고 읽기 좋게 카드형으로 표시")
                            if use_compact:
                                html = render_attachment_cards_html(attach_df, title_col)
                            else:
                                html = render_attachment_table_html(attach_df, title_col, 360, 440, 280)
                            st.markdown(html, unsafe_allow_html=True)

                            xbuf = _BytesIO()
                            with _pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
                                attach_df.to_excel(writer, index=False, sheet_name="attachments")
                            xbuf.seek(0)
                            st.download_button(
                                label="📥 첨부 링크 매트릭스 다운로드 (Excel)",
                                data=xbuf,
                                file_name=f"{'_'.join(customers)}_첨부링크_매트릭스_{_dt.now().strftime('%Y%m%d')}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                            )

                    # ===== GPT 분석 =====
                    st.markdown("---")
                    st.subheader("🤖 GPT 분석 (업로드한 파일 기반)")
                    st.caption("HWP 업로드 시 자동으로 **hwp5txt 우선 → 실패 시 unoconv(pdf) → PDF문자 추출** 순으로 처리합니다.")
                    src_files = st.file_uploader(
                        "분석할 파일 업로드 (여러 개 가능, PDF/HWP/HWPX/DOCX/TXT/CSV/MD/LOG 권장)",
                        type=["pdf","hwp","hwpx","docx","txt","csv","md","log"],
                        accept_multiple_files=True
                    )

                    # 기존 보고서 세션 노출
                    if st.session_state.get("gpt_report_md"):
                        st.markdown("### 📝 GPT 분석 보고서 (세션 보존)")
                        st.markdown(st.session_state["gpt_report_md"])

                        base_fname_prev = f"{'_'.join(customers) if customers else '세션'}_GPT분석_{_dt.now().strftime('%Y%m%d_%H%M')}"
                        md_bytes_prev = st.session_state["gpt_report_md"].encode("utf-8")
                        col_md_prev, col_pdf_prev = st.columns(2)
                        with col_md_prev:
                            st.download_button(
                                "📥 GPT 보고서 다운로드 (.md)",
                                data=md_bytes_prev,
                                file_name=f"{base_fname_prev}.md",
                                mime="text/markdown",
                                use_container_width=True,
                            )
                        with col_pdf_prev:
                            pdf_bytes_prev, dbg_prev = markdown_to_pdf_korean(st.session_state["gpt_report_md"], title="GPT 분석 보고서")
                            if pdf_bytes_prev:
                                st.download_button(
                                    "📥 GPT 보고서 다운로드 (.pdf)",
                                    data=pdf_bytes_prev,
                                    file_name=f"{base_fname_prev}.pdf",
                                    mime="application/pdf",
                                    use_container_width=True,
                                )
                                st.caption(f"PDF 생성 상태: {dbg_prev}")
                            else:
                                st.error(f"PDF 생성 실패: {dbg_prev}")

                        files = st.session_state.get("generated_src_pdfs") or []
                        if files:
                            st.markdown("### 🗂️ 변환된 간이 PDF 내려받기 (세션 보존)")
                            for i, (fname, pbytes) in enumerate(files):
                                st.download_button(
                                    label=f"📥 {fname}", data=pbytes, file_name=fname,
                                    mime="application/pdf", key=f"dl_srcpdf_prev_{i}", use_container_width=True,
                                )

                    # 보고서 생성
                    if st.button("🧠 GPT 분석 보고서 생성", type="primary", use_container_width=True):
                        try:
                            from openai import OpenAI  # 설치 체크
                        except Exception:
                            st.error("openai가 설치되어 있지 않습니다. `pip install openai` 후 다시 시도하세요.")
                        else:
                            if not src_files:
                                st.warning("먼저 분석할 파일을 업로드하세요.")
                            else:
                                with st.spinner("GPT가 업로드된 자료로 보고서를 작성 중..."):
                                    combined_text, logs, generated_pdfs = handle_uploaded_source_files(src_files)
                                    st.write("### 변환 로그")
                                    for line in logs:
                                        st.write("- " + line)
                                    if not combined_text.strip():
                                        st.error("업로드된 파일에서 텍스트를 추출하지 못했습니다.")
                                    else:
                                        safe_extra = _redact_secrets(gpt_extra_req or "")
                                        prompt = (
                                            "다음은 조달/입찰 관련 문서들의 텍스트입니다.\n"
                                            "핵심 요구사항, 기술/가격 평가 비율, 계약조건, 월과 일을 포함한 정확한 일정(입찰 마감/계약기간),\n"
                                            "공동수급/하도급/긴급공고 여부, 주요 장비/스펙, 주요 구간, 배정예산/추정가격/예가 등을\n"
                                            "표와 불릿으로 요약하세요.\n"
                                            f"추가 요구사항: {safe_extra}\n\n"
                                            "[문서 통합 텍스트 (일부만 사용해도 됨)]\n"
                                            f"{combined_text[:180000]}\n"
                                        )
                                        try:
                                            report, used = call_gpt_with_fallback(
                                                [
                                                    {"role": "system", "content": "당신은 SK브로드밴드 망설계/조달 제안 컨설턴트입니다."},
                                                    {"role": "user", "content": prompt},
                                                ],
                                                model="gpt-4.1",
                                                max_tokens=2000,
                                                temperature=0.4
                                            )
                                            st.markdown("### 📝 GPT 분석 보고서")
                                            st.markdown(report)

                                            st.session_state["gpt_report_md"] = report
                                            st.session_state["generated_src_pdfs"] = generated_pdfs

                                            base_fname = f"{'_'.join(customers)}_GPT분석_{_dt.now().strftime('%Y%m%d_%H%M')}"
                                            md_bytes = report.encode("utf-8")

                                            col_md, col_pdf = st.columns(2)
                                            with col_md:
                                                st.download_button(
                                                    "📥 GPT 보고서 다운로드 (.md)",
                                                    data=md_bytes,
                                                    file_name=f"{base_fname}.md",
                                                    mime="text/markdown",
                                                    use_container_width=True,
                                                )
                                            with col_pdf:
                                                pdf_bytes, dbg = markdown_to_pdf_korean(report, title="GPT 분석 보고서")
                                                if pdf_bytes:
                                                    st.download_button(
                                                        "📥 GPT 보고서 다운로드 (.pdf)",
                                                        data=pdf_bytes,
                                                        file_name=f"{base_fname}.pdf",
                                                        mime="application/pdf",
                                                        use_container_width=True,
                                                    )
                                                    st.caption(f"PDF 생성 상태: {dbg}")
                                                else:
                                                    st.error(f"PDF 생성 실패: {dbg}")

                                            if st.session_state["generated_src_pdfs"]:
                                                st.markdown("---")
                                                st.markdown("### 🗂️ 변환된 간이 PDF 내려받기")
                                                for i, (fname, pbytes) in enumerate(st.session_state["generated_src_pdfs"]):
                                                    st.download_button(
                                                        label=f"📥 {fname}", data=pbytes, file_name=fname,
                                                        mime="application/pdf", key=f"dl_srcpdf_now_{i}",
                                                        use_container_width=True,
                                                    )
                                        except Exception as e:
                                            st.error(f"보고서 생성 중 오류: {e}")

                    # ===== (2차) 참조 챗봇 =====
                    st.markdown("---")
                    st.subheader("💬 보고서/표 참조 챗봇")
                    st.caption("방금 생성된 **보고서(.md)**와 현재 **표(검색 결과 일부)**를 컨텍스트로 사용합니다.")
                    question = st.chat_input("질문을 입력하세요 (예: 핵심 리스크와 완화전략만 추려줘)")
                    if question:
                        st.session_state["chat_messages"].append({"role":"user","content":question})
                        ctx_df = result.head(200).copy()
                        with _pd.option_context('display.max_columns', None):
                            df_sample_csv = ctx_df.to_csv(index=False)[:20000]
                        report_ctx = st.session_state.get("gpt_report_md") or "(아직 보고서 없음)"
                        q_prompt = (
                            "다음은 컨텍스트입니다.\n"
                            "[요약 보고서(Markdown)]\n"
                            f"{report_ctx}\n\n"
                            "[표 데이터(일부 CSV)]\n"
                            f"{df_sample_csv}\n\n"
                            f"사용자 질문: {question}\n"
                            "컨텍스트에 근거해 한국어로 간결하고 조리 있게 답하세요. 표/불릿/간단한 테이블 활용 가능.\n"
                        )
                        try:
                            ans, _ = call_gpt_with_fallback(
                                [
                                    {"role":"system","content":"당신은 조달/통신 제안 분석 챗봇입니다. 컨텍스트만으로 답하고 모르면 모른다고 하세요."},
                                    {"role":"user","content":q_prompt},
                                ],
                                model="gpt-4.1-mini",
                                max_tokens=1200,
                                temperature=0.2
                            )
                            st.session_state["chat_messages"].append({"role":"assistant","content":ans})
                        except Exception as e:
                            st.session_state["chat_messages"].append({"role":"assistant","content":f"오류: {e}"})

                    for m in st.session_state["chat_messages"]:
                        if m["role"]=="user":
                            st.chat_message("user").markdown(m["content"])
                        else:
                            st.chat_message("assistant").markdown(m["content"])
        else:
            st.info("고객사명을 입력하면 자동 필터링됩니다.")


if __name__ == "__main__":
    # 전역 설정 & 공용 유틸 (app_part1.py)
    st.set_page_config(
        page_title="조달입찰 분석 시스템",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        """
        <meta name="robots" content="noindex,nofollow">
        <meta name="googlebot" content="noindex,nofollow">
        """,
        unsafe_allow_html=True,
    )

    main()
