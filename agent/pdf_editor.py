"""Editor PDF: potong halaman, hapus halaman, dan TULIS/TIMPA teks di atas halaman.

Kenapa modul ini ada
--------------------
Bot pernah mengaku "gua potong PDF-nya, cuma sisain lembar ke-2" padahal
fungsi potong PDF sama sekali tidak ada. Semua operasi di sini nyata dan
selalu menghasilkan file baru yang bisa diverifikasi (cek ukuran & jumlah
halaman sebelum diklaim berhasil).

Kemampuan:
1. extract_pages   - ambil halaman tertentu (mis. "2" atau "1,3")
2. delete_pages    - buang halaman tertentu
3. overlay_text    - TULIS teks di posisi tertentu di atas halaman
4. fill_form_fields- isi AcroForm (kotak isian PDF) yang sebenarnya
5. merge / rotate  - gabung beberapa PDF, putar halaman

PENTING: PDF hasil scan adalah GAMBAR. Menulis teks di atasnya tetap
bekerja (teks ditempel sebagai lapisan baru), tapi MEMBACA isinya butuh
OCR. Fungsi read_page_text di sini jujur melaporkan kalau halaman tidak
punya teks sama sekali.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject
except Exception:  # pragma: no cover
    PdfReader = PdfWriter = None  # type: ignore[assignment]
    RectangleObject = None  # type: ignore[assignment]


# Standar ukuran halaman (poin). 1 pt = 1/72 inci.
PAGE_SIZES = {
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
    "f4": (609.45, 935.43),  # folio, umum di Indonesia
}


class PdfEditError(RuntimeError):
    """Kesalahan yang pesannya aman ditampilkan ke pengguna."""


def _require_pypdf() -> None:
    if PdfReader is None or PdfWriter is None:
        raise PdfEditError("pypdf belum terpasang. Jalankan: pip install pypdf")


def parse_page_selection(spec: str, total_pages: int) -> List[int]:
    """Ubah "2" / "1,3" / "2-4" / "1,3-5" jadi daftar indeks 0-based.

    Nomor halaman yang diketik manusia mulai dari 1, jadi dikurangi 1.
    Nomor di luar jangkauan DITOLAK, bukan diam-diam diabaikan — supaya
    tidak pernah terjadi "berhasil" padahal halamannya tidak ada.
    """
    spec = (spec or "").strip().lower()
    if not spec:
        raise PdfEditError("Nomor halaman kosong. Contoh: 2 atau 1,3 atau 2-4")

    picked: List[int] = []
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise PdfEditError(f"Rentang halaman tidak valid: {chunk}") from exc
            if lo > hi:
                lo, hi = hi, lo
            picked.extend(range(lo, hi + 1))
        else:
            try:
                picked.append(int(chunk))
            except ValueError as exc:
                raise PdfEditError(f"Nomor halaman bukan angka: {chunk}") from exc

    bad = [p for p in picked if p < 1 or p > total_pages]
    if bad:
        raise PdfEditError(
            f"Halaman {', '.join(map(str, bad))} tidak ada. PDF ini punya {total_pages} halaman."
        )
    # buang duplikat, pertahankan urutan
    seen = set()
    ordered = []
    for p in picked:
        if p not in seen:
            seen.add(p)
            ordered.append(p - 1)
    return ordered


def page_count(source: str | Path) -> int:
    _require_pypdf()
    try:
        return len(PdfReader(str(source)).pages)
    except Exception as exc:
        raise PdfEditError(f"Gagal membaca PDF: {str(exc)[:120]}") from exc


def extract_pages(source: str | Path, dest: str | Path, spec: str) -> Dict[str, Any]:
    """Ambil hanya halaman tertentu dari PDF."""
    _require_pypdf()
    total = page_count(source)
    idxs = parse_page_selection(spec, total)

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for i in idxs:
        writer.add_page(reader.pages[i])

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        writer.write(fh)

    return {
        "file": dest.name,
        "source_pages": total,
        "kept_pages": len(idxs),
        "page_numbers": [i + 1 for i in idxs],
        "size_bytes": dest.stat().st_size,
    }


def delete_pages(source: str | Path, dest: str | Path, spec: str) -> Dict[str, Any]:
    """Buang halaman tertentu, sisakan sisanya."""
    _require_pypdf()
    total = page_count(source)
    drop = set(parse_page_selection(spec, total))
    keep = [i for i in range(total) if i not in drop]
    if not keep:
        raise PdfEditError("Semua halaman dihapus. Sisakan minimal satu halaman.")

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for i in keep:
        writer.add_page(reader.pages[i])

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        writer.write(fh)

    return {
        "file": dest.name,
        "source_pages": total,
        "kept_pages": len(keep),
        "removed_pages": sorted(i + 1 for i in drop),
        "size_bytes": dest.stat().st_size,
    }


def overlay_text(
    source: str | Path,
    dest: str | Path,
    entries: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """TULIS teks di atas halaman PDF.

    ``entries`` adalah daftar, masing-masing:
        {"page": 1,            # nomor halaman 1-based (boleh "all")
         "text": "Halo",
         "x": 60, "y": 700,    # posisi dalam poin, asal dari KIRI-BAWAH
         "size": 11, "bold": False,
         "color": [0, 0, 0],   # RGB 0..1
         "align": "left"}      # left | center | right

    Teks ditempel sebagai lapisan baru di atas halaman, jadi tetap bekerja
    pada PDF hasil scan sekalipun (isinya gambar).
    """
    _require_pypdf()
    try:
        from reportlab.pdfgen import canvas as _rl_canvas
    except Exception as exc:  # pragma: no cover
        raise PdfEditError("reportlab belum terpasang, tidak bisa menulis teks.") from exc

    if not entries:
        raise PdfEditError("Tidak ada teks yang mau ditulis.")

    reader = PdfReader(str(source))
    total = len(reader.pages)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    # Kelompokkan entri per halaman
    per_page: Dict[int, List[Dict[str, Any]]] = {}
    for item in entries:
        page_field = item.get("page", 1)
        if isinstance(page_field, str) and page_field.lower() in ("all", "semua"):
            targets = list(range(total))
        else:
            try:
                pnum = int(page_field)
            except (TypeError, ValueError) as exc:
                raise PdfEditError(f"Nomor halaman tidak valid: {page_field}") from exc
            if pnum < 1 or pnum > total:
                raise PdfEditError(f"Halaman {pnum} tidak ada. PDF ini punya {total} halaman.")
            targets = [pnum - 1]
        for t in targets:
            per_page.setdefault(t, []).append(item)

    written = 0
    for page_idx, items in per_page.items():
        target_page = writer.pages[page_idx]
        width = float(target_page.mediabox.width)
        height = float(target_page.mediabox.height)

        buf = io.BytesIO()
        c = _rl_canvas.Canvas(buf, pagesize=(width, height))
        for it in items:
            txt = str(it.get("text") or "")
            if not txt:
                continue
            size = float(it.get("size", 11))
            x = float(it.get("x", 60))
            y = float(it.get("y", height - 80))
            color = it.get("color") or [0, 0, 0]
            try:
                r, g, b = [float(v) for v in color[:3]]
            except Exception:
                r, g, b = 0.0, 0.0, 0.0
            c.setFillColorRGB(r, g, b)
            font = "Helvetica-Bold" if it.get("bold") else "Helvetica"
            c.setFont(font, size)

            align = str(it.get("align") or "left").lower()
            if align in ("center", "center_", "tengah"):
                c.drawCentredString(x, y, txt)
            elif align in ("right", "kanan"):
                c.drawRightString(x, y, txt)
            else:
                c.drawString(x, y, txt)
            written += 1
        c.save()
        buf.seek(0)

        overlay_reader = PdfReader(buf)
        target_page.merge_page(overlay_reader.pages[0])

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        writer.write(fh)

    return {
        "file": dest.name,
        "pages": total,
        "texts_written": written,
        "size_bytes": dest.stat().st_size,
    }


def add_blank_page(
    source: str | Path,
    dest: str | Path,
    size: str = "a4",
    entries: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Tambah satu halaman kosong baru (opsional langsung ditulisi)."""
    _require_pypdf()
    try:
        from reportlab.pdfgen import canvas as _rl_canvas
    except Exception as exc:  # pragma: no cover
        raise PdfEditError("reportlab belum terpasang.") from exc

    w, h = PAGE_SIZES.get((size or "a4").lower(), PAGE_SIZES["a4"])
    buf = io.BytesIO()
    c = _rl_canvas.Canvas(buf, pagesize=(w, h))
    for it in entries or []:
        txt = str(it.get("text") or "")
        if not txt:
            continue
        c.setFont("Helvetica-Bold" if it.get("bold") else "Helvetica", float(it.get("size", 11)))
        c.drawString(float(it.get("x", 60)), float(it.get("y", h - 80)), txt)
    c.save()
    buf.seek(0)

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_page(PdfReader(buf).pages[0])

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        writer.write(fh)

    return {"file": dest.name, "pages": page_count(dest), "size_bytes": dest.stat().st_size}


def fill_form_fields(source: str | Path, dest: str | Path, values: Dict[str, str]) -> Dict[str, Any]:
    """Isi kotak isian AcroForm yang benar-benar ada di PDF."""
    _require_pypdf()
    reader = PdfReader(str(source))
    fields = reader.get_fields() or {}
    available = list(fields.keys())
    if not available:
        raise PdfEditError(
            "PDF ini tidak punya kotak isian yang bisa diisi (bukan PDF form). "
            "Kalau mau menulis teks bebas di atas halaman, pakai mode tulis teks."
        )

    unknown = [k for k in values if k not in available]
    if unknown:
        raise PdfEditError(
            f"Isian tidak dikenal: {', '.join(unknown)}. Yang ada: {', '.join(available[:15])}"
        )

    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        writer.update_page_form_field_values(page, values)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        writer.write(fh)

    return {"file": dest.name, "filled": list(values.keys()), "available": available}


def read_page_text(source: str | Path, page: int = 1) -> Dict[str, Any]:
    """Baca teks satu halaman. Jujur kalau halaman tidak punya teks (hasil scan)."""
    _require_pypdf()
    reader = PdfReader(str(source))
    total = len(reader.pages)
    if page < 1 or page > total:
        raise PdfEditError(f"Halaman {page} tidak ada. PDF ini punya {total} halaman.")
    txt = (reader.pages[page - 1].extract_text() or "").strip()
    return {
        "page": page,
        "total_pages": total,
        "chars": len(txt),
        "text": txt,
        "is_scanned": len(txt) == 0,
        "note": (
            "Halaman ini tidak punya lapisan teks — kemungkinan hasil scan/gambar. "
            "Teks TIDAK BISA dibaca tanpa OCR, tapi MENULIS di atasnya tetap bisa."
            if not txt
            else ""
        ),
    }


# ====================================================================== #
# OCR — membaca PDF hasil scan (halaman gambar tanpa lapisan teks)
# ====================================================================== #
OCR_LANGS_DEFAULT = "ind+eng"


class OcrUnavailableError(PdfEditError):
    """Dipakai kalau tesseract/poppler belum terpasang di server."""


def ocr_available() -> Dict[str, Any]:
    """Cek apakah OCR siap dipakai, tanpa melempar error."""
    info: Dict[str, Any] = {"ready": False, "engine": "", "langs": [], "reason": ""}
    try:
        import pytesseract  # noqa: WPS433
    except Exception:
        info["reason"] = "pytesseract belum terpasang (pip install pytesseract)."
        return info
    try:
        ver = pytesseract.get_tesseract_version()
        langs = list(pytesseract.get_languages(config="") or [])
    except Exception as exc:
        info["reason"] = f"tesseract tidak bisa dijalankan: {str(exc)[:120]}"
        return info
    info.update({"ready": True, "engine": f"tesseract {ver}", "langs": langs})
    if not ({"ind", "eng"} & set(langs)):
        info["ready"] = False
        info["reason"] = f"bahasa ind/eng tidak tersedia (ada: {', '.join(langs[:8])})"
    return info


def _render_pdf_page_png(source: str | Path, page: int, dpi: int = 200) -> bytes:
    """Render satu halaman PDF jadi PNG memakai PyMuPDF (tanpa binary poppler)."""
    try:
        import pymupdf  # noqa: WPS433
    except Exception:
        try:
            import fitz as pymupdf  # type: ignore # noqa: WPS433
        except Exception as exc:
            raise OcrUnavailableError(
                "pymupdf belum terpasang, tidak bisa mengubah halaman PDF jadi gambar."
            ) from exc
    doc = pymupdf.open(str(source))
    try:
        if page < 1 or page > doc.page_count:
            raise PdfEditError(f"Halaman {page} tidak ada. PDF ini punya {doc.page_count} halaman.")
        zoom = dpi / 72.0
        pix = doc.load_page(page - 1).get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        doc.close()


def ocr_page(
    source: str | Path,
    page: int = 1,
    langs: str = OCR_LANGS_DEFAULT,
    dpi: int = 200,
) -> Dict[str, Any]:
    """Baca teks satu halaman lewat OCR. Selalu jujur soal sumber teksnya."""
    _require_pypdf()
    status = ocr_available()
    if not status["ready"]:
        raise OcrUnavailableError(status["reason"])

    import pytesseract  # noqa: WPS433
    import io as _io

    total = page_count(source)
    if page < 1 or page > total:
        raise PdfEditError(f"Halaman {page} tidak ada. PDF ini punya {total} halaman.")

    # Kalau halaman SUDAH punya lapisan teks, jangan buang waktu OCR — pakai itu.
    native = read_page_text(source, page)
    if not native["is_scanned"]:
        return {
            "page": page, "total_pages": total, "text": native["text"],
            "chars": native["chars"], "source": "text-layer", "engine": "",
            "confidence": None, "dpi": None,
        }

    png = _render_pdf_page_png(source, page, dpi=dpi)
    try:
        from PIL import Image  # noqa: WPS433
        img = Image.open(_io.BytesIO(png))
    except Exception as exc:
        raise OcrUnavailableError(f"Pillow gagal membuka gambar hasil render: {exc}") from exc

    try:
        text = pytesseract.image_to_string(img, lang=langs) or ""
    except Exception as exc:
        raise OcrUnavailableError(f"OCR gagal dijalankan: {str(exc)[:140]}") from exc

    text = text.strip()
    confidence = None
    try:
        data = pytesseract.image_to_data(img, lang=langs, output_type=pytesseract.Output.DICT)
        vals = [int(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0]
        if vals:
            confidence = round(sum(vals) / len(vals), 1)
    except Exception:
        pass

    return {
        "page": page,
        "total_pages": total,
        "text": text,
        "chars": len(text),
        "source": "ocr",
        "engine": status["engine"],
        "langs": langs,
        "confidence": confidence,
        "dpi": dpi,
    }


def ocr_document(
    source: str | Path,
    pages: Optional[str] = None,
    langs: str = OCR_LANGS_DEFAULT,
    dpi: int = 200,
    max_pages: int = 30,
) -> Dict[str, Any]:
    """OCR beberapa halaman sekaligus.

    ``pages`` kosong = semua halaman (dibatasi ``max_pages`` supaya tidak
    menggantung lama di server kecil). Spesifikasi sama dengan extract_pages.
    """
    _require_pypdf()
    total = page_count(source)
    if pages:
        idxs = [i + 1 for i in parse_page_selection(pages, total)]
    else:
        idxs = list(range(1, min(total, max_pages) + 1))

    results = []
    for p in idxs:
        try:
            results.append(ocr_page(source, p, langs=langs, dpi=dpi))
        except PdfEditError as exc:
            results.append({"page": p, "text": "", "chars": 0, "source": "error", "error": str(exc)})

    combined = "\n\n".join(
        f"--- Halaman {r['page']} ---\n{r['text']}" for r in results if r.get("text")
    )
    return {
        "file": Path(source).name,
        "total_pages": total,
        "pages_processed": len(results),
        "total_chars": sum(r.get("chars", 0) for r in results),
        "pages": results,
        "text": combined,
    }
