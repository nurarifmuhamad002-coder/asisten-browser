import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


class WorkspaceFileManager:
    """
    Mengelola file di dalam folder sandbox `./workspace_files`.
    Agent maupun user bisa membaca, membuat, mengubah, mengunduh, dan menghapus file di sini.
    File hasil download dari browser juga otomatis masuk ke folder ini.
    """

    def __init__(self, base_dir: str = "/home/user/workspace_files"):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._seed_starter_files()

    def _safe_path(self, filename: str) -> Path:
        clean_name = os.path.basename(filename.strip())
        if not clean_name or clean_name in (".", ".."):
            raise ValueError("Nama file tidak valid.")
        target = (self.base_dir / clean_name).resolve()
        if not str(target).startswith(str(self.base_dir)):
            raise ValueError("Akses di luar folder workspace ditolak.")
        return target

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
                "catatan": "Mohon aktifkan invoice pajak otomatis untuk bulan berjalan."
            }
            sample_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        readme_ws = self.base_dir / "catatan_workspace.md"
        if not readme_ws.exists():
            readme_ws.write_text(
                "# Workspace File Agent\n\n"
                "Folder ini adalah ruang kerja bersama antara **lu** dan **NusaOperator Agent**:\n"
                "- **Input Data**: Upload atau simpan file `.json` / `.csv` / `.txt` (seperti `profil_pengajuan.json`) untuk diisi otomatis ke formulir web.\n"
                "- **Output Riset**: Agent bisa merangkum informasi dari berbagai website lalu menyimpannya ke file `.md` atau `.csv` di sini.\n"
                "- **Download Otomatis**: Semua file yang di-download di dalam Cloud Browser otomatis tersimpan ke folder ini.\n",
                encoding="utf-8",
            )

    def list_files(self) -> List[Dict[str, Any]]:
        files = []
        for entry in sorted(self.base_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if entry.is_file() and not entry.name.startswith("."):
                stat = entry.stat()
                files.append(
                    {
                        "name": entry.name,
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
                content = "\n".join(chunks)
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
        target.write_text(content, encoding="utf-8")
        return {
            "name": target.name,
            "size": target.stat().st_size,
            "path": str(target),
        }

    def append_file(self, filename: str, content: str) -> Dict[str, Any]:
        target = self._safe_path(filename)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        sep = "\n" if existing and not existing.endswith("\n") else ""
        target.write_text(existing + sep + content, encoding="utf-8")
        return {
            "name": target.name,
            "size": target.stat().st_size,
            "path": str(target),
        }

    def save_bytes(self, filename: str, data: bytes) -> Dict[str, Any]:
        target = self._safe_path(filename)
        target.write_bytes(data)
        return {
            "name": target.name,
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
