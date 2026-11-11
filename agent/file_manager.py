import csv
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from xml.sax.saxutils import escape as xml_escape


def _parse_markdown_blocks(raw_text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Memecah teks AI/Markdown menjadi judul + blok terstruktur (heading, paragraf, list, tabel)."""
    lines = (raw_text or "").replace("\r\n", "\n").split("\n")
    doc_title = "Dokumen Hasil Asisten AI"
    blocks: List[Dict[str, Any]] = []
    table_buf: List[List[str]] = []

    def flush_table() -> None:
        nonlocal table_buf
        if table_buf:
            # Buang baris pemisah |---|---|
            cleaned = [
                r for r in table_buf
                if not all(re.match(r"^:?-{2,}:?$", c.strip()) for c in r if c.strip())
            ]
            if cleaned:
                blocks.append({"type": "table", "rows": cleaned})
            table_buf = []

    for line in lines:
        s = line.strip()
        # Cek baris tabel pipa | A | B |
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            table_buf.append(cells)
            continue
        else:
            flush_table()

        if not s:
            continue

        h_match = re.match(r"^(#{1,3})\s+(.+)$", s)
        if h_match:
            lvl = len(h_match.group(1))
            txt = h_match.group(2).strip().replace("**", "")
            if lvl == 1 and doc_title == "Dokumen Hasil Asisten AI":
                doc_title = txt
            blocks.append({"type": "heading", "level": lvl, "text": txt})
            continue

        b_match = re.match(r"^([-*•]|\d+\.)\s+(.+)$", s)
        if b_match:
            blocks.append({"type": "bullet", "marker": b_match.group(1), "text": b_match.group(2).strip()})
            continue

        blocks.append({"type": "paragraph", "text": s})

    flush_table()
    return doc_title, blocks


def _build_docx_bytes(raw_text: str, fallback_title: str = "Dokumen Tugas") -> bytes:
    import docx
    from docx.shared import Pt, RGBColor

    doc_title, blocks = _parse_markdown_blocks(raw_text)
    if doc_title == "Dokumen Hasil Asisten AI" and fallback_title:
        doc_title = fallback_title

    doc = docx.Document()
    style_normal = doc.styles["Normal"]
    style_normal.font.name = "Calibri"
    style_normal.font.size = Pt(11)

    # Judul Utama jika belum ada H1 di blok pertama
    if not (blocks and blocks[0].get("type") == "heading" and blocks[0].get("level") == 1):
        h = doc.add_heading(doc_title, level=0)
        for run in h.runs:
            run.font.color.rgb = RGBColor(0x0B, 0x3D, 0x2E)

    def add_rich_runs(p: Any, text: str) -> None:
        parts = re.split(r"(\*\*[^*]+\*\*)", text)
        for part in parts:
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                r = p.add_run(part[2:-2])
                r.bold = True
            else:
                p.add_run(part)

    for b in blocks:
        btype = b["type"]
        if btype == "heading":
            doc.add_heading(b["text"], level=min(b["level"], 3))
        elif btype == "bullet":
            marker = b["marker"]
            style_name = "List Number" if marker[0].isdigit() else "List Bullet"
            p = doc.add_paragraph(style=style_name)
            add_rich_runs(p, b["text"])
        elif btype == "table":
            rows = b["rows"]
            max_cols = max(len(r) for r in rows)
            tbl = doc.add_table(rows=len(rows), cols=max_cols)
            tbl.style = "Table Grid"
            for r_i, row in enumerate(rows):
                for c_i in range(max_cols):
                    val = row[c_i] if c_i < len(row) else ""
                    cell = tbl.cell(r_i, c_i)
                    cell.text = ""
                    p = cell.paragraphs[0]
                    run = p.add_run(val.replace("**", ""))
                    if r_i == 0:
                        run.bold = True
        else:
            p = doc.add_paragraph()
            add_rich_runs(p, b["text"])

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _blocks_to_sheet_rows(blocks: List[Dict[str, Any]], raw_chunk: str) -> List[List[Any]]:
    """Mengubah daftar blok Markdown / CSV / Key-Value menjadi matriks baris Excel yang rapi."""
    rows_to_write: List[List[Any]] = []
    tables = [b["rows"] for b in blocks if b["type"] == "table"]
    if tables:
        # Gabungkan tabel DAN sertakan teks/poin kesimpulan di bawah/di antara tabel jika ada!
        for b in blocks:
            btype = b["type"]
            if btype == "table":
                if rows_to_write and rows_to_write[-1] != []:
                    rows_to_write.append([])
                rows_to_write.extend([[c.replace("**", "").strip() for c in r] for r in b["rows"]])
            elif btype == "heading":
                if rows_to_write and rows_to_write[-1] != []:
                    rows_to_write.append([])
                rows_to_write.append([b["text"]])
            else:
                txt = b.get("text", "").replace("**", "").strip()
                if txt:
                    if ":" in txt and len(txt.split(":", 1)[0]) <= 45:
                        k, v = txt.split(":", 1)
                        rows_to_write.append([k.strip(), v.strip()])
                    else:
                        rows_to_write.append([txt])
    else:
        non_empty = [ln.strip() for ln in (raw_chunk or "").splitlines() if ln.strip()]
        if len(non_empty) >= 2 and all("," in ln for ln in non_empty[:3]):
            reader = csv.reader(io.StringIO("\n".join(non_empty)))
            rows_to_write = [list(r) for r in reader if any(r)]
        else:
            rows_to_write.append(["No", "Bagian / Topik", "Isi / Keterangan"])
            cur_section = "Umum"
            num = 1
            for b in blocks:
                if b["type"] == "heading":
                    cur_section = b["text"]
                else:
                    txt = b.get("text", "").replace("**", "").strip()
                    if ":" in txt and len(txt.split(":", 1)[0]) <= 45:
                        k, v = txt.split(":", 1)
                        rows_to_write.append([num, k.strip(), v.strip()])
                    else:
                        rows_to_write.append([num, cur_section, txt])
                    num += 1
    if not rows_to_write:
        rows_to_write = [["Isi Dokumen"], [raw_chunk or ""]]
    return rows_to_write


def _coerce_excel_cell(val: Any, is_header: bool = False) -> Any:
    if is_header or not isinstance(val, str):
        return val
    s = val.strip()
    if re.fullmatch(r"-?\d+", s) and not (len(s) > 1 and s.startswith("0")):
        try:
            return int(s)
        except Exception:
            return val
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except Exception:
            return val
    return val


def _split_multi_sheet_sections(raw_text: str, default_title: str) -> List[Tuple[str, str]]:
    """
    Mendeteksi pemisahan Multi-Sheet di dalam konten Markdown/JSON:
    - JSON: {"sheets": [{"name": "Data Produk", "content": "..."}]}
    - Markdown: '# Sheet 1: Data Produk', '## Sheet: Analisis dan Rekomendasi', '=== Sheet: ... ==='
    """
    txt = (raw_text or "").strip()
    if txt.startswith("{"):
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict) and isinstance(obj.get("sheets"), list) and obj["sheets"]:
                out_json: List[Tuple[str, str]] = []
                for idx, sh in enumerate(obj["sheets"], 1):
                    nm = str(sh.get("name") or sh.get("title") or f"Sheet {idx}")
                    if isinstance(sh.get("rows"), list):
                        lines = ["| " + " | ".join(str(c) for c in r) + " |" for r in sh["rows"]]
                        out_json.append((nm, "\n".join(lines)))
                    else:
                        out_json.append((nm, str(sh.get("content") or "")))
                return out_json
        except Exception:
            pass

    sheet_hdr_re = re.compile(
        r"^(?:#{1,3}\s*|===\s*)(?:sheet\s*\d*\s*[:\-–]\s*|tab\s*\d*\s*[:\-–]\s*)(.+?)(?:\s*===)?$",
        flags=re.I,
    )
    sections: List[Tuple[str, List[str]]] = []
    cur_name: str = ""
    cur_lines: List[str] = []

    for ln in (raw_text or "").splitlines():
        m = sheet_hdr_re.match(ln.strip())
        if m:
            if cur_name or any(x.strip() for x in cur_lines):
                if cur_name:
                    sections.append((cur_name, cur_lines))
                elif any("|" in x for x in cur_lines):
                    sections.append((default_title, cur_lines))
            raw_nm = m.group(1).strip().strip("*").rstrip(".")
            cur_name = raw_nm or f"Sheet {len(sections) + 1}"
            cur_lines = []
        else:
            cur_lines.append(ln)

    if cur_name:
        sections.append((cur_name, cur_lines))

    if len(sections) >= 1:
        return [(nm, "\n".join(lns).strip()) for nm, lns in sections if "\n".join(lns).strip()]
    return [(default_title, raw_text or "")]


def _build_xlsx_bytes(raw_text: str, sheet_title: str = "Data") -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    header_fill = PatternFill("solid", fgColor="00695C")
    subhdr_fill = PatternFill("solid", fgColor="E0F2F1")
    zebra_fill = PatternFill("solid", fgColor="F5FAF8")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    subhdr_font = Font(name="Calibri", size=11, bold=True, color="004D40")
    body_font = Font(name="Calibri", size=11, color="111111")
    thin = Side(border_style="thin", color="CFD8DC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    sheet_sections = _split_multi_sheet_sections(raw_text, sheet_title)
    used_names: set[str] = set()

    for s_idx, (raw_sname, s_chunk) in enumerate(sheet_sections):
        clean_sname = re.sub(r"[\[\]\*:/\\?]", " ", raw_sname).strip()[:31] or f"Sheet{s_idx + 1}"
        if clean_sname.lower() in used_names:
            clean_sname = f"{clean_sname[:27]}_{s_idx + 1}"
        used_names.add(clean_sname.lower())

        ws = wb.active if s_idx == 0 else wb.create_sheet(title=clean_sname)
        ws.title = clean_sname

        _, blocks = _parse_markdown_blocks(s_chunk)
        rows_to_write = _blocks_to_sheet_rows(blocks, s_chunk)

        col_max: Dict[int, int] = {}
        prev_was_blank = True
        for r_idx, row in enumerate(rows_to_write, start=1):
            if not row or all(str(x).strip() == "" for x in row):
                ws.append([])
                prev_was_blank = True
                continue
            is_tbl_hdr = prev_was_blank and len(row) >= 2
            is_sec_hdr = len(row) == 1 and prev_was_blank and len(rows_to_write) > r_idx
            coerced_row = [_coerce_excel_cell(v, is_header=(is_tbl_hdr or is_sec_hdr)) for v in row]
            ws.append(coerced_row)
            prev_was_blank = False

            for c_idx, val in enumerate(coerced_row, start=1):
                s_val = str(val if val is not None else "")
                col_max[c_idx] = max(col_max.get(c_idx, 10), min(len(s_val) + 4, 58))
                cell = ws.cell(row=r_idx, column=c_idx)
                cell.border = border
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if is_tbl_hdr:
                    cell.fill = header_fill
                    cell.font = header_font
                elif is_sec_hdr:
                    cell.fill = subhdr_fill
                    cell.font = subhdr_font
                else:
                    if r_idx % 2 == 1:
                        cell.fill = zebra_fill
                    cell.font = body_font

        for c_idx, width in col_max.items():
            ws.column_dimensions[get_column_letter(c_idx)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_pdf_bytes(raw_text: str, fallback_title: str = "Dokumen Hasil AI") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    doc_title, blocks = _parse_markdown_blocks(raw_text)
    if doc_title == "Dokumen Hasil Asisten AI" and fallback_title:
        doc_title = fallback_title

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=42,
        rightMargin=42,
        topMargin=44,
        bottomMargin=44,
        title=doc_title,
    )
    styles = getSampleStyleSheet()
    st_title = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontSize=17,
        leading=22,
        textColor=colors.HexColor("#004D40"),
        spaceAfter=12,
    )
    st_h2 = ParagraphStyle(
        "DocH2",
        parent=styles["Heading2"],
        fontSize=13,
        leading=17,
        textColor=colors.HexColor("#00695C"),
        spaceBefore=10,
        spaceAfter=6,
    )
    st_body = ParagraphStyle(
        "DocBody",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=15,
        spaceAfter=6,
    )
    st_bullet = ParagraphStyle(
        "DocBullet",
        parent=st_body,
        leftIndent=16,
        spaceAfter=4,
    )

    def fmt_inline(text: str) -> str:
        esc = xml_escape(text)
        return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", esc)

    story: List[Any] = []
    if not (blocks and blocks[0].get("type") == "heading" and blocks[0].get("level") == 1):
        story.append(Paragraph(fmt_inline(doc_title), st_title))
        story.append(Spacer(1, 4))

    for b in blocks:
        btype = b["type"]
        if btype == "heading":
            st = st_title if b["level"] == 1 else st_h2
            story.append(Paragraph(fmt_inline(b["text"]), st))
        elif btype == "bullet":
            prefix = "• " if not b["marker"][0].isdigit() else f"{xml_escape(b['marker'])} "
            story.append(Paragraph(prefix + fmt_inline(b["text"]), st_bullet))
        elif btype == "table":
            rows = b["rows"]
            max_cols = max(len(r) for r in rows)
            avail_w = A4[0] - 84
            col_w = avail_w / max(max_cols, 1)
            tbl_data = []
            for r_i, r in enumerate(rows):
                row_cells = []
                for c_i in range(max_cols):
                    txt = r[c_i] if c_i < len(r) else ""
                    formatted = f"<b>{fmt_inline(txt.replace('**', ''))}</b>" if r_i == 0 else fmt_inline(txt)
                    row_cells.append(Paragraph(formatted, st_body))
                tbl_data.append(row_cells)
            tbl = Table(tbl_data, colWidths=[col_w] * max_cols, repeatRows=1)
            tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E0F2F1")),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B0BEC5")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("PADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.append(Spacer(1, 4))
            story.append(tbl)
            story.append(Spacer(1, 8))
        else:
            story.append(Paragraph(fmt_inline(b["text"]), st_body))

    if not story:
        story.append(Paragraph(fmt_inline(raw_text or "(Kosong)"), st_body))

    pdf.build(story)
    return buf.getvalue()


class WorkspaceFileManager:
    """
    Mengelola file di dalam folder sandbox `./workspace_files`.
    Mendukung baca & tulis otomatis format `.docx` (Word), `.xlsx` (Excel), `.pdf` (PDF),
    `.md`, `.csv`, `.json`, dan `.txt`.
    """

    def __init__(self, base_dir: str = "/home/user/workspace_files"):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._seed_starter_files()

    def _safe_path(self, filename: str) -> Path:
        raw = (filename or "").strip().replace("\\", "/").lstrip("/")
        if not raw or raw in (".", ".."):
            raise ValueError("Nama file/folder tidak valid.")
        target = (self.base_dir / raw).resolve()
        base_str = str(self.base_dir)
        if str(target) != base_str and not str(target).startswith(base_str + os.sep):
            raise ValueError("Akses di luar folder workspace ditolak.")
        return target

    def _rel_name(self, target: Path) -> str:
        return target.resolve().relative_to(self.base_dir).as_posix()

    def make_dir(self, dir_path: str) -> Dict[str, Any]:
        target = self._safe_path(dir_path)
        target.mkdir(parents=True, exist_ok=True)
        return {"name": self._rel_name(target), "path": str(target), "is_dir": True}

    def list_top_dirs(self) -> List[str]:
        out: List[str] = []
        for entry in sorted(self.base_dir.iterdir(), key=lambda p: p.name):
            if entry.is_dir() and not entry.name.startswith(".") and entry.name != "__pycache__":
                out.append(entry.name)
        return out

    def get_directory_tree(self, rel_root: str = "") -> str:
        root = self._safe_path(rel_root) if (rel_root or "").strip() else self.base_dir
        if not root.exists():
            raise FileNotFoundError(f"Folder '{rel_root}' tidak ditemukan di workspace.")
        if root.is_file():
            return f"{self._rel_name(root)} ({root.stat().st_size} B)"

        root_label = f"{root.name}/" if root != self.base_dir else "workspace_files/"
        lines: List[str] = [root_label]
        dir_count = 0
        file_count = 0

        def _walk(cur: Path, prefix: str) -> None:
            nonlocal dir_count, file_count
            children = [
                p for p in sorted(cur.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                if not p.name.startswith(".") and p.name != "__pycache__"
            ]
            for idx, ch in enumerate(children):
                is_last = idx == len(children) - 1
                branch = "└── " if is_last else "├── "
                next_pref = prefix + ("    " if is_last else "│   ")
                if ch.is_dir():
                    dir_count += 1
                    lines.append(f"{prefix}{branch}{ch.name}/")
                    _walk(ch, next_pref)
                else:
                    file_count += 1
                    lines.append(f"{prefix}{branch}{ch.name} ({ch.stat().st_size} B)")

        _walk(root, "")
        lines.append(f"\n[Terverifikasi di Disk: {dir_count} subfolder, {file_count} file]")
        return "\n".join(lines)

    def zip_directory(self, rel_dir: str) -> Dict[str, Any]:
        import zipfile

        folder = self._safe_path(rel_dir)
        if not folder.exists() or not folder.is_dir():
            raise FileNotFoundError(f"Folder '{rel_dir}' tidak ditemukan.")
        zip_name = f"{folder.name}.zip"
        zip_target = self._safe_path(zip_name)

        with zipfile.ZipFile(str(zip_target), "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Sertakan folder utama & seluruh subfolder (termasuk subfolder kosong!) dan seluruh file di dalamnya
            for cur_root, dirs, files in os.walk(str(folder)):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
                files = sorted(f for f in files if not f.startswith("."))
                rel_cur = Path(cur_root).resolve().relative_to(folder.parent).as_posix().rstrip("/") + "/"
                zinfo_dir = zipfile.ZipInfo(rel_cur)
                zinfo_dir.external_attr = 0o40755 << 16
                zf.writestr(zinfo_dir, b"")
                for fn in files:
                    full_f = Path(cur_root) / fn
                    arc_f = full_f.resolve().relative_to(folder.parent).as_posix()
                    zf.write(str(full_f), arcname=arc_f)

        return {
            "name": self._rel_name(zip_target),
            "size": zip_target.stat().st_size,
            "path": str(zip_target),
        }

    def _seed_starter_files(self) -> None:
        sample_json = self.base_dir / "profil_pengajuan.json"
        if not sample_json.exists():
            data = {
                "nama_lengkap": "Rizky Pratama",
                "email": "rizky.pratama@contoh-usaha.id",
                "telepon": "0812-8899-7744",
                "perusahaan": "PT Kreasi Digital Nusantara",
                "paket_layanan": "Cloud GPU Pro - Bulanan",
                "alamat_pengiriman": "Jl. Jenderal Sudirman Kav. 52, Jakarta Selatan",
                "catatan": "Mohon aktifkan invoice pajak otomatis untuk bulan berjalan.",
            }
            sample_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        readme_ws = self.base_dir / "catatan_workspace.md"
        if not readme_ws.exists():
            readme_ws.write_text(
                "# Workspace File Agent\n\n"
                "Folder ini adalah ruang kerja bersama antara **lu** dan **NusaOperator Agent**:\n"
                "- **Input Data**: Kirim foto/PDF/Word/Excel lewat Telegram atau upload ke sini.\n"
                "- **Output Dokumen Rapi**: Agent bisa langsung membuat file Word (`.docx`), Excel (`.xlsx`), PDF (`.pdf`), `.md`, atau `.csv`.\n",
                encoding="utf-8",
            )

    def list_files(self) -> List[Dict[str, Any]]:
        files = []
        all_files: List[Path] = []
        for cur_root, dirs, fnames in os.walk(str(self.base_dir)):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fn in fnames:
                if not fn.startswith("."):
                    all_files.append(Path(cur_root) / fn)

        for entry in sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True):
            stat = entry.stat()
            files.append(
                {
                    "name": self._rel_name(entry),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "ext": entry.suffix.lower().lstrip("."),
                }
            )
        return files

    def read_file(self, filename: str, max_chars: int = 20000) -> str:
        target = self._safe_path(filename)
        if not target.exists():
            raise FileNotFoundError(f"File '{filename}' tidak ditemukan di workspace.")

        ext = target.suffix.lower()
        content = ""
        try:
            if ext == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(str(target))
                pages = []
                for idx, p in enumerate(reader.pages[:30], start=1):
                    txt = (p.extract_text() or "").strip()
                    if txt:
                        pages.append(f"--- Halaman {idx} ---\n{txt}")
                content = "\n\n".join(pages)
            elif ext == ".docx":
                import docx
                doc = docx.Document(str(target))
                chunks = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                for tbl in doc.tables:
                    for row in tbl.rows:
                        cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                        if any(cells):
                            chunks.append(" | ".join(cells))
                content = "\n\n".join(chunks)
            elif ext in (".xlsx", ".xlsm"):
                import openpyxl
                wb = openpyxl.load_workbook(str(target), data_only=True, read_only=True)
                sheets_out = []
                for sname in wb.sheetnames[:8]:
                    ws = wb[sname]
                    rows_txt = [f"=== Sheet: {sname} ==="]
                    for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                        if r_idx >= 200:
                            rows_txt.append("... (baris selanjutnya dipotong)")
                            break
                        vals = ["" if v is None else str(v).strip() for v in row]
                        if any(vals):
                            rows_txt.append(" | ".join(vals))
                    sheets_out.append("\n".join(rows_txt))
                wb.close()
                content = "\n\n".join(sheets_out)
            else:
                content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"[File Biner: {target.name}, ukuran {target.stat().st_size} bytes]"
        except Exception as exc:
            return f"[Gagal mengekstrak teks {target.name}: {exc}]"

        if not content.strip() and ext == ".pdf":
            return ""
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n... [Dipotong, total {len(content)} karakter]"
        return content

    def write_file(self, filename: str, content: str) -> Dict[str, Any]:
        target = self._safe_path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        ext = target.suffix.lower()
        stem_title = target.stem.replace("_", " ").replace("-", " ").title()

        if ext == ".docx":
            target.write_bytes(_build_docx_bytes(content, fallback_title=stem_title))
        elif ext == ".xlsx":
            target.write_bytes(_build_xlsx_bytes(content, sheet_title=stem_title))
        elif ext == ".pdf":
            target.write_bytes(_build_pdf_bytes(content, fallback_title=stem_title))
        else:
            target.write_text(content, encoding="utf-8")

        return {
            "name": self._rel_name(target),
            "size": target.stat().st_size,
            "path": str(target),
        }

    def export_formatted(self, stem: str, content: str, fmt: str) -> Dict[str, Any]:
        clean_stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", (stem or "hasil_tugas").strip()).strip("_") or "hasil_tugas"
        clean_fmt = fmt.lower().lstrip(".")
        if clean_fmt not in ("docx", "xlsx", "pdf", "md"):
            clean_fmt = "docx"
        return self.write_file(f"{clean_stem}.{clean_fmt}", content)

    def append_file(self, filename: str, content: str) -> Dict[str, Any]:
        target = self._safe_path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        rel_nm = self._rel_name(target)
        if target.suffix.lower() in (".docx", ".xlsx", ".pdf"):
            prev = self.read_file(rel_nm) if target.exists() else ""
            return self.write_file(rel_nm, (prev + "\n\n" + content).strip())
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        sep = "\n" if existing and not existing.endswith("\n") else ""
        target.write_text(existing + sep + content, encoding="utf-8")
        return {
            "name": rel_nm,
            "size": target.stat().st_size,
            "path": str(target),
        }

    def save_bytes(self, filename: str, data: bytes) -> Dict[str, Any]:
        target = self._safe_path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {
            "name": self._rel_name(target),
            "size": target.stat().st_size,
            "path": str(target),
        }

    def delete_file(self, filename: str) -> bool:
        target = self._safe_path(filename)
        if target.exists():
            target.unlink()
            return True
        return False

    def get_file_path(self, filename: str) -> Path:
        target = self._safe_path(filename)
        if not target.exists():
            raise FileNotFoundError(f"File '{filename}' tidak ditemukan.")
        return target
