"""Pembuat surat PDF yang RAPI dari data mentah (KTP, form, tabel).

Kenapa modul ini ada
--------------------
Pendekatan lama (menulis teks di atas PDF scan yang sudah ada) hasilnya
selalu jelek: teks ditumpuk di satu titik, keluar tepi halaman, dan
menimpa kop surat. Modul ini mengambil jalan yang benar: membuat dokumen
BARU yang tersusun rapi — kop, judul, tabel data, paragraf, tanda tangan.

Prinsip yang dipegang:
1. Teks SELALU di-wrap sesuai lebar kolom. Tidak ada teks keluar halaman.
2. Data panjang (alamat, NIK) dipotong per baris dengan label sejajar.
3. Setiap surat menyertakan blok tanda tangan + tempat/tanggal.
4. Hasil akhir selalu bisa dirender jadi gambar bukti (lihat pdf_editor).
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, LEGAL, LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm, mm
    from reportlab.platypus import (
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except Exception as exc:  # pragma: no cover
    raise RuntimeError("reportlab belum terpasang, tidak bisa membuat surat PDF.") from exc


PAGE_FORMATS = {
    "a4": A4,
    "f4": (609.45, 935.43),  # folio, tidak selalu ada di reportlab
    "folio": (609.45, 935.43),
    "letter": LETTER,
    "legal": LEGAL,
}


class LetterBuildError(RuntimeError):
    """Kesalahan aman ditampilkan ke pengguna."""


# --------------------------------------------------------------------- #
# Gaya dokumen
# --------------------------------------------------------------------- #
def _styles(font_body: str = "Helvetica", font_bold: str = "Helvetica-Bold") -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "kop_nama": ParagraphStyle(
            "kop_nama", parent=base["Title"], fontName=font_bold, fontSize=15,
            leading=18, alignment=TA_CENTER, spaceAfter=2,
        ),
        "kop_alamat": ParagraphStyle(
            "kop_alamat", parent=base["Normal"], fontName=font_body, fontSize=8.5,
            leading=11, alignment=TA_CENTER, textColor=colors.HexColor("#444444"),
        ),
        "judul": ParagraphStyle(
            "judul", parent=base["Heading1"], fontName=font_bold, fontSize=13,
            leading=16, alignment=TA_CENTER, spaceBefore=10, spaceAfter=2,
        ),
        "subjudul": ParagraphStyle(
            "subjudul", parent=base["Normal"], fontName=font_body, fontSize=9.5,
            leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#555555"),
        ),
        "body": ParagraphStyle(
            "body", parent=base["BodyText"], fontName=font_body, fontSize=10.5,
            leading=15.5, alignment=TA_JUSTIFY, spaceAfter=8,
        ),
        "label": ParagraphStyle(
            "label", fontName=font_body, fontSize=10.5, leading=15,
        ),
        "nilai": ParagraphStyle(
            "nilai", fontName=font_bold, fontSize=10.5, leading=15,
        ),
        "kecil": ParagraphStyle(
            "kecil", fontName=font_body, fontSize=8, leading=10,
            textColor=colors.HexColor("#666666"),
        ),
        "tanda_tangan": ParagraphStyle(
            "tanda_tangan", fontName=font_body, fontSize=10.5, leading=15,
            alignment=TA_LEFT,
        ),
    }


def _esc(txt: Any) -> str:
    """Escape karakter khusus supaya Paragraph tidak rusak."""
    s = "" if txt is None else str(txt)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------- #
# Pembuat surat
# --------------------------------------------------------------------- #
def build_letter(
    dest: str | Path,
    title: str,
    *,
    kop_nama: str = "",
    kop_alamat: str = "",
    nomor: str = "",
    data: Optional[Sequence[Tuple[str, Any]]] = None,
    pembuka: str = "",
    penutup: str = "",
    kota: str = "",
    tanggal: Optional[str] = None,
    jabatan_ttd: str = "",
    nama_ttd: str = "",
    catatan: str = "",
    page_format: str = "a4",
    margin_cm: float = 2.2,
    font_body: str = "Helvetica",
    font_bold: str = "Helvetica-Bold",
) -> Dict[str, Any]:
    """Bangun satu surat PDF lengkap.

    Parameter
    ---------
    data : daftar pasangan (label, nilai) yang dicetak sebagai baris rata.
    pembuka/penutup : paragraf bebas, otomatis di-justify & di-wrap.
    tanggal : format Indonesia (mis. "16 November 2026"); default hari ini.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    st = _styles(font_body, font_bold)
    pagesize = PAGE_FORMATS.get((page_format or "a4").lower(), A4)
    m = margin_cm * cm

    doc = SimpleDocTemplate(
        str(dest),
        pagesize=pagesize,
        leftMargin=m,
        rightMargin=m,
        topMargin=m * 0.85,
        bottomMargin=m * 0.85,
        title=title,
        author=kop_nama or nama_ttd or "",
        subject=title,
    )

    flow: List[Any] = []
    usable_width = pagesize[0] - 2 * m

    # ---------- KOP ----------
    has_kop = bool(kop_nama or kop_alamat)
    if has_kop:
        if kop_nama:
            flow.append(Paragraph(_esc(kop_nama).upper(), st["kop_nama"]))
        if kop_alamat:
            flow.append(Paragraph(_esc(kop_alamat), st["kop_alamat"]))
        rule = Table([[""]], colWidths=[usable_width], rowHeights=[2])
        rule.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (-1, -1), 1.6, colors.black),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        flow.append(Spacer(1, 4))
        flow.append(rule)
        flow.append(Spacer(1, 10))

    # ---------- JUDUL ----------
    if title:
        flow.append(Paragraph(_esc(title).upper(), st["judul"]))
    if nomor:
        flow.append(Paragraph(f"Nomor: {_esc(nomor)}", st["subjudul"]))
    flow.append(Spacer(1, 12))

    # ---------- PEMBUKA ----------
    if pembuka:
        flow.append(Paragraph(_esc(pembuka), st["body"]))

    # ---------- TABEL DATA ----------
    if data:
        rows: List[List[Any]] = []
        for label, value in data:
            rows.append([
                Paragraph(_esc(label), st["label"]),
                Paragraph(":", st["label"]),
                Paragraph(_esc(value), st["nilai"]),
            ])
        label_w = usable_width * 0.30
        colon_w = usable_width * 0.04
        value_w = usable_width - label_w - colon_w
        tbl = Table(rows, colWidths=[label_w, colon_w, value_w])
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        flow.append(Spacer(1, 2))
        flow.append(tbl)
        flow.append(Spacer(1, 8))

    # ---------- PENUTUP ----------
    if penutup:
        flow.append(Paragraph(_esc(penutup), st["body"]))

    if catatan:
        flow.append(Spacer(1, 4))
        flow.append(Paragraph(_esc(catatan), st["kecil"]))

    # ---------- TANDA TANGAN ----------
    if nama_ttd or jabatan_ttd:
        tgl_txt = tanggal or datetime.now().strftime("%d %B %Y")
        blok: List[Any] = [Spacer(1, 18)]
        tempat_tanggal = f"{kota}, {tgl_txt}" if kota else tgl_txt
        blok.append(Paragraph(_esc(tempat_tanggal), st["tanda_tangan"]))
        if jabatan_ttd:
            blok.append(Paragraph(_esc(jabatan_ttd), st["tanda_tangan"]))
        blok.append(Spacer(1, 52))  # ruang untuk tanda tangan basah
        blok.append(Paragraph(f"<b>{_esc(nama_ttd)}</b>" if nama_ttd else "", st["tanda_tangan"]))

        ttd_table = Table([[blok]], colWidths=[usable_width * 0.62])
        ttd_table.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        # tempatkan di kanan
        outer = Table([["", ttd_table]], colWidths=[usable_width * 0.38, usable_width * 0.62])
        outer.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        flow.append(KeepTogether(outer))

    doc.build(flow)

    return {
        "file": dest.name,
        "title": title,
        "rows": len(data or []),
        "size_bytes": dest.stat().st_size,
        "pagesize": f"{pagesize[0]:.0f}x{pagesize[1]:.0f}pt",
    }


# --------------------------------------------------------------------- #
# Surat Pernyataan Jual Beli Valas (template siap pakai)
# --------------------------------------------------------------------- #
def build_valas_statement(
    dest: str | Path,
    ktp: Dict[str, Any],
    *,
    nilai: str = "",
    kurs: str = "",
    mata_uang: str = "USD",
    bank: str = "",
    rekening: str = "",
    kota: str = "",
    tanggal: Optional[str] = None,
    nomor: str = "",
    page_format: str = "a4",
    extra_rows: Optional[Sequence[Tuple[str, Any]]] = None,
) -> Dict[str, Any]:
    """Bangun Surat Pernyataan Jual Beli Valas dari data KTP.

    ``ktp`` adalah dict hasil pembacaan KTP, mis.:
        {"NIK": "...", "Nama": "...", "Tempat/Tgl Lahir": "...",
         "Alamat": "...", "RT/RW": "...", ...}
    Kunci dicocokkan dengan toleransi (huruf besar/kecil & spasi).
    """
    def pick(*names: str) -> str:
        norm = {str(k).strip().lower().replace("_", " "): v for k, v in ktp.items()}
        for n in names:
            key = n.strip().lower().replace("_", " ")
            if key in norm and norm[key]:
                return str(norm[key]).strip()
        # coba pencocokan longgar
        for n in names:
            key = n.strip().lower()
            for k, v in norm.items():
                if key in k and v:
                    return str(v).strip()
        return ""

    nama = pick("nama", "name", "nama lengkap")
    nik = pick("nik", "nomor induk kependudukan")
    ttl = pick("tempat/tgl lahir", "tempat tanggal lahir", "ttl", "tempat lahir")
    jenis_kelamin = pick("jenis kelamin", "kelamin", "gender")
    alamat = pick("alamat", "address")
    rt_rw = pick("rt/rw", "rt rw")
    kel = pick("kelurahan/desa", "kelurahan", "desa")
    kec = pick("kecamatan")
    agama = pick("agama", "religion")
    pekerjaan = pick("pekerjaan", "occupation")

    alamat_lengkap = alamat
    if rt_rw:
        alamat_lengkap += f", RT/RW {rt_rw}" if alamat_lengkap else f"RT/RW {rt_rw}"
    if kel:
        alamat_lengkap += f", Kel. {kel}"
    if kec:
        alamat_lengkap += f", Kec. {kec}"

    rows: List[Tuple[str, Any]] = []
    if nama:
        rows.append(("Nama Lengkap", nama))
    if nik:
        rows.append(("NIK", nik))
    if ttl:
        rows.append(("Tempat / Tgl Lahir", ttl))
    if jenis_kelamin:
        rows.append(("Jenis Kelamin", jenis_kelamin))
    if agama:
        rows.append(("Agama", agama))
    if pekerjaan:
        rows.append(("Pekerjaan", pekerjaan))
    if alamat_lengkap:
        rows.append(("Alamat", alamat_lengkap))
    if nilai:
        # Jangan dobel mata uang: "IDR 250.000.000" + "USD" -> "IDR 250.000.000"
        _nilai_txt = str(nilai).strip()
        _mu = (mata_uang or "").strip().upper()
        if _mu and _mu not in _nilai_txt.upper():
            # Kalau nilai sudah punya kode mata uang lain (IDR/USD/EUR...), biarkan
            _has_ccy = any(c in _nilai_txt.upper() for c in ("IDR", "USD", "SGD", "EUR", "JPY", "GBP", "AUD"))
            if not _has_ccy:
                _nilai_txt = f"{_mu} {_nilai_txt}"
        rows.append(("Nilai Transaksi", _nilai_txt))
    if kurs:
        rows.append(("Kurs", f"{kurs} per {mata_uang}"))
    if bank:
        rows.append(("Bank", bank))
    if rekening:
        rows.append(("No. Rekening", rekening))
    for extra in extra_rows or []:
        rows.append((str(extra[0]), extra[1]))

    pembuka = (
        "Yang bertanda tangan di bawah ini, dengan ini menyatakan dengan sebenar-benarnya "
        "bahwa data diri berikut adalah benar dan masih berlaku:"
    )
    penutup = (
        "Surat pernyataan ini dibuat untuk keperluan transaksi jual beli valuta asing dan "
        "digunakan sebagaimana mestinya. Demikian surat pernyataan ini dibuat dengan sadar, "
        "tanpa paksaan dari pihak mana pun, dan dapat dipertanggungjawabkan secara hukum."
    )

    return build_letter(
        dest,
        title="Surat Pernyataan Jual Beli Valuta Asing",
        kop_alamat="",
        nomor=nomor,
        data=rows,
        pembuka=pembuka,
        penutup=penutup,
        kota=kota,
        tanggal=tanggal,
        jabatan_ttd="Yang Menyatakan,",
        nama_ttd=nama,
        catatan=(
            "Lampiran: fotokopi KTP pemilik identitas. "
            "Dokumen ini dibuat otomatis dari data yang diberikan."
        ),
        page_format=page_format,
    )
