from __future__ import annotations
import os, re
from pathlib import Path
from docx import Document as DocxDocument
from pypdf import PdfReader

MAX_EXTRACTED_CHARS = int(os.getenv('MAX_EXTRACTED_CHARS', '2000000'))


def _ocr_page(page, page_number: int, fitz, pytesseract, Image):
    """Render and preprocess a page, then rebuild text using OCR word boxes.

    Rebuilding each line from word boxes avoids the common Tesseract plain-text
    failure where adjacent words are concatenated or line breaks disappear.
    """
    from PIL import ImageOps, ImageEnhance, ImageFilter
    dpi = max(180, min(int(os.getenv('OCR_DPI', '300')), 400))
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
    image = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(1.5)
    image = image.filter(ImageFilter.SHARPEN)
    # Mild upscale helps small printed text without excessive memory use.
    if image.width < 2200:
        scale = min(1.6, 2200 / image.width)
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)
    languages = os.getenv('OCR_LANGUAGES', 'eng+hin').strip() or 'eng'
    config = '--oem 3 --psm 3'
    data = pytesseract.image_to_data(image, lang=languages, config=config, output_type=pytesseract.Output.DICT)
    lines = {}
    count = len(data.get('text', []))
    for i in range(count):
        word = str(data['text'][i]).strip()
        try: confidence = float(data['conf'][i])
        except (TypeError, ValueError): confidence = -1
        if not word or confidence < 0: continue
        key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        x = int(data['left'][i]); width = int(data['width'][i])
        lines.setdefault(key, []).append((x, width, word))
    output=[]
    for key in sorted(lines):
        words=sorted(lines[key], key=lambda item:item[0])
        line=''
        previous_right=None
        for x,width,word in words:
            gap = None if previous_right is None else x-previous_right
            # Preserve normal word boundaries; use a single space for small gaps
            # and two spaces only for clearly separated columns.
            if line:
                line += ('  ' if gap is not None and gap > max(28, int(image.width*0.018)) else ' ')
            line += word
            previous_right=x+width
        if line.strip(): output.append(line.strip())
    return '\n'.join(output).strip()


def _ocr_pdf(path: Path) -> str:
    """OCR scanned/mixed PDFs page-by-page with preprocessing and layout spacing."""
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError('This PDF appears to be scanned. Install OCR dependencies with: pip install pymupdf pytesseract pillow') from exc
    binary = os.getenv('TESSERACT_CMD', '').strip()
    if binary: pytesseract.pytesseract.tesseract_cmd = binary
    page_limit = int(os.getenv('OCR_MAX_PAGES', '150'))
    result=[]
    try:
        with fitz.open(path) as pdf:
            if len(pdf) > page_limit:
                raise ValueError(f'PDF has {len(pdf)} pages; OCR limit is {page_limit}. Split the file and upload smaller sections.')
            for index,page in enumerate(pdf):
                text=_ocr_page(page,index+1,fitz,pytesseract,Image)
                if text: result.append(f'[Page {index+1}]\n{text}')
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError('Tesseract OCR executable was not found. Install Tesseract OCR and set TESSERACT_CMD in backend/.env if it is outside PATH.') from exc
    except Exception as exc:
        if 'tesseract' in str(exc).lower() and ('not found' in str(exc).lower() or 'failed' in str(exc).lower()):
            raise RuntimeError('Tesseract OCR is not installed or OCR language data is missing. Install Tesseract and the configured language packs (eng, hin).') from exc
        raise
    return '\n\n'.join(result).strip()


def extract_text(path: Path, filename: str, *, ocr: bool = True) -> tuple[str, str]:
    ext = Path(filename).suffix.lower(); method = 'text-extraction'
    if ext == '.txt': text = path.read_text(encoding='utf-8-sig', errors='replace')
    elif ext == '.docx':
        doc = DocxDocument(path); blocks = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells): blocks.append(' | '.join(cells))
        text = '\n\n'.join(blocks)
    elif ext == '.pdf':
        reader = PdfReader(str(path))
        page_text=[]; needs_ocr=[]
        for index,page in enumerate(reader.pages):
            extracted=(page.extract_text() or '').strip()
            page_text.append(extracted)
            if len(re.sub(r'\s+', '', extracted)) < 35: needs_ocr.append(index)
        if ocr and needs_ocr:
            ocr_text=_ocr_pdf(path)
            # OCR output is page-labelled; use it for sparse/scanned pages while
            # retaining higher-quality selectable text from normal PDF pages.
            ocr_pages=re.split(r'(?=\[Page \d+\]\n)', ocr_text)
            for index in needs_ocr:
                marker=f'[Page {index+1}]'
                match=next((block for block in ocr_pages if block.startswith(marker)), '')
                if match: page_text[index]=match[len(marker):].strip()
            method='hybrid-pdf-text+ocr' if any(page_text) else 'tesseract-ocr'
        else: method='pdf-text-layer'
        text='\n\n'.join(t for t in page_text if t).strip()
    else: raise ValueError('Supported formats are PDF, DOCX and TXT.')
    text = re.sub(r'\r\n?', '\n', text).strip()
    if not text: raise ValueError('No readable text was detected. For scanned PDFs, confirm Tesseract and the required language packs are installed.')
    if len(text) > MAX_EXTRACTED_CHARS: raise ValueError(f'Document is too large to process. Maximum extracted text is {MAX_EXTRACTED_CHARS:,} characters.')
    return text, method


def chunks(text: str, limit: int = 3500) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    result=[]; current=''
    for paragraph in paragraphs:
        while len(paragraph)>limit:
            if current: result.append(current); current=''
            cut=paragraph.rfind(' ',0,limit); cut=cut if cut>0 else limit
            result.append(paragraph[:cut].strip()); paragraph=paragraph[cut:].strip()
        if current and len(current)+len(paragraph)+2>limit: result.append(current); current=paragraph
        else: current=f'{current}\n\n{paragraph}'.strip()
    if current: result.append(current)
    return result


def export_docx(text: str, path: Path):
    doc=DocxDocument()
    for paragraph in re.split(r'\n\s*\n', text):
        if paragraph.strip(): doc.add_paragraph(paragraph.strip())
    doc.save(path)
