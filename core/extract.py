"""从常见文稿格式里抽出纯文本 —— 创新点通常写在 Word / PDF / Markdown 里。

只用标准库处理 .docx（docx 本质是 zip + xml），PDF 才需要 pypdf / pymupdf，
没装就给一句明确的安装提示，不静默失败。
"""
import io
import os
import re
import zipfile
from html import unescape

# 直接按文本读的扩展名
TEXT_EXT = {
    ".txt", ".text", ".md", ".markdown", ".rst", ".tex", ".bib",
    ".csv", ".tsv", ".log", ".json", ".yaml", ".yml", ".py", ".ini",
}

MAX_BYTES = 8 * 1024 * 1024        # 8MB，创新点写作用不了更大
MAX_CHARS = 200_000                # 抽完再兜一次，避免把整本书塞进 prompt


class ExtractError(Exception):
    pass


def extract_bytes(filename, data):
    """给定文件名和字节，返回纯文本。"""
    if len(data) > MAX_BYTES:
        raise ExtractError(f"文件太大（{len(data) / 1048576:.1f}MB），上限 8MB")
    ext = os.path.splitext(filename or "")[1].lower()

    if ext == ".docx":
        text = _docx(data)
    elif ext == ".doc":
        raise ExtractError("老版 .doc 读不了，请在 Word 里「另存为 .docx」再试")
    elif ext == ".pdf":
        text = _pdf(data)
    elif ext in (".rtf",):
        raise ExtractError("RTF 读不了，另存为 .docx 或 .txt 再试")
    else:
        # 其余一律当文本试：.txt / .md / 无扩展名 / 不认识的
        text = _decode(data)
        if "\x00" in text[:2000]:
            raise ExtractError("这看起来是二进制文件，换成 .txt / .md / .docx / .pdf")

    text = _tidy(text)
    if not text.strip():
        raise ExtractError("文件里没抽到文字（可能是扫描版 PDF 或空的）")
    return text[:MAX_CHARS]


def extract_path(path):
    """按路径读本地文件，CLI 用。"""
    if not os.path.isfile(path):
        raise ExtractError(f"找不到文件：{path}")
    with open(path, "rb") as f:
        return extract_bytes(path, f.read())


def _decode(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _tidy(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)          # 行尾空格
    text = re.sub(r"\n{3,}", "\n\n", text)          # 连续空行压成一段
    return text.strip()


def _docx(data):
    """docx 就是 zip：读 word/document.xml，按段落切行。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ExtractError("这个 .docx 打不开（文件损坏，或者是改了扩展名的 .doc）")

    names = zf.namelist()
    if "word/document.xml" not in names:
        raise ExtractError("这个 .docx 结构不认识，试试另存一份")

    xml = zf.read("word/document.xml").decode("utf-8", "replace")
    # 段落、行内换行、制表、表格单元格 → 对应的空白
    xml = xml.replace("</w:p>", "\n").replace("<w:br/>", "\n")
    xml = xml.replace("<w:br />", "\n").replace("</w:tc>", "\t").replace("</w:tr>", "\n")
    text = re.sub(r"<[^>]+>", "", xml)             # 去掉剩下的所有标签
    text = unescape(text)                          # &amp; &lt; 之类
    # 宋体等字体会把中文字形塞进 w:sym，量不大，忽略即可
    return text


def _pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None

    if PdfReader is not None:
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages[:60]:        # 研究方案不会超过 60 页
                pages.append(page.extract_text() or "")
            return "\n".join(pages)
        except Exception as e:
            raise ExtractError(f"PDF 解析失败：{e}")

    try:
        import fitz                                  # pymupdf
    except ImportError:
        raise ExtractError("读 PDF 需要 pypdf，装一下：pip install pypdf")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n".join(p.get_text() for p in doc)
    except Exception as e:
        raise ExtractError(f"PDF 解析失败：{e}")
