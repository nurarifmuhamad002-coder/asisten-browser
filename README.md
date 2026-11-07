---
title: NusaOperator Cloud Browser Agent
emoji: 🌐
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8000
---

# NusaOperator — Cloud Browser AI Agent (MVP)

Prototype **Cloud Web Browser Agent** (bergaya *OpenAI Operator / Manus*) yang menjalankan Chromium asli di server/sandbox, men-stream tampilan layar secara *real-time* ke browser pengguna, serta menjaga **login akun & persetujuan tindakan sensitif 100% di tangan pengguna (Human-in-the-Loop)**.

---

## Pemetaan Kemampuan ke Modul Kode

| Kemampuan Agent | Cara Kerja & Modul Kode |
| :--- | :--- |
| **1. Membuka berbagai website** | `agent/browser_engine.py` (`navigate`) — Playwright Persistent Context Chromium dengan dukungan multi-tab & cookie sesi tersimpan. |
| **2. Klik tombol & navigasi halaman** | `agent/browser_engine.py` (`click_element`, `scroll_page`, `go_back`) — Klik akurat berbasis indeks DOM + koordinat visual disertai animasi kursor AI. |
| **3. Mengetik & mengisi formulir** | `agent/browser_engine.py` (`type_into_element`, `select_dropdown`) — Mengisi input teks, textarea, dropdown `<select>`, dan submit form. |
| **4. Membaca tampilan website** | `agent/dom_inspector.py` (`inspect_page`) — Menggabungkan *Set-of-Marks* `[1..N]` elemen interaktif, ekstraksi teks bersih halaman, dan tangkapan layar JPEG untuk Vision LLM. |
| **5. Mencari & mengumpulkan informasi** | `agent/llm_providers.py` + `agent/orchestrator.py` — Menjelajah halaman pencarian/artikel, scroll bertahap, dan menyusun temuan. |
| **6. Mengelola file** | `agent/file_manager.py` — Baca/tulis/edit/upload/download file di `./workspace_files/` + tangkap otomatis file yang di-download dari dalam Cloud Browser. |
| **7. Memahami perintah & menentukan langkah** | `agent/llm_providers.py` — Menghasilkan rencana langkah kerja (`plan` checklist) dinamis di setiap putaran observasi. |
| **8. Menyelesaikan pekerjaan beberapa tahap** | `agent/orchestrator.py` — State machine *Observe -> Plan -> Guardrail -> Act* hingga `MAX_STEPS` sampai tujuan tuntas. |
| **9. Operasi akun milik user (Login & Izin Sensitif di tangan user)** | `agent/guardrails.py` + *Live Canvas Takeover* (`static/app.js`) — Masking kolom password, serah-terima layar otomatis saat login/OTP/CAPTCHA, serta gerbang persetujuan (*Approval Gate*) sebelum aksi pembayaran/hapus akun/publikasi. |

---

## Arsitektur Keamanan Human-in-the-Loop (HITL)

1. **Streaming Satu Port (CDP Screencast + WebSocket)**
   Menggunakan Chrome DevTools Protocol (`Page.startScreencast`) melalui WebSocket `/ws` sehingga tidak membutuhkan setup Xvfb/VNC yang berat dan langsung mulus di balik reverse proxy HTTPS.
2. **Privasi Kredensial & Login Takeover**
   - Kolom `input[type="password"]` otomatis disensor (`[PASSWORD_FIELD]`) pada pembacaan DOM agar isi password tidak pernah dikirim ke prompt AI.
   - Saat halaman login terdeteksi, status berubah ke `WAITING_HUMAN_TAKEOVER`. Klik mouse dan ketikan keyboard pengguna di `<canvas>` diteruskan langsung ke Chromium (`human_click`, `human_type`, `human_key`) tanpa dicatat ke log AI.
3. **Deterministic Sensitive Action Guardrail**
   - Sebelum klik dieksekusi, `inspect_element_sensitivity()` memeriksa atribut `data-sensitive` dan pola tombol berisiko (*bayar, checkout, transfer, hapus akun, ubah password, kirim email*).
   - Eksekusi dibekukan (`WAITING_APPROVAL`), tombol disorot merah pada layar live, dan agent menunggu keputusan **Setujui**, **Ambil Alih Manual**, atau **Tolak** dari pengguna.

---

## Cara Menjalankan di Lokal / VPS

```bash
# 1. Install dependencies Python
pip install -r requirements.txt

# 2. Download browser Chromium Playwright
python3 -m playwright install --with-deps chromium

# 3. Jalankan server Cloud Web App
python3 main.py
```

Buka `http://localhost:8000` di browser.

### Menghubungkan ke AI Vision Penuh (Opsional)
Secara bawaan aplikasi sudah dilengkapi **Smart Autopilot** sehingga bisa langsung diuji tanpa API key. Untuk mengaktifkan penalaran bebas di semua website, klik tombol **⚙️ AI Model** di kanan atas UI (atau atur `.env`) dan masukkan API Key salah satu provider berikut:
- **Google Gemini** (`gemini-2.5-flash` / `gemini-2.5-pro`)
- **OpenAI** (`gpt-4o`)
- **Anthropic Claude** (`claude-sonnet-4-20250514`)
- **OpenRouter / Ollama / Custom Endpoint**

---

## Roadmap Menuju Skala Produksi (Multi-User SaaS)

Jika ingin menjadikan MVP ini produk publik untuk ribuan pengguna:
1. **Isolasi Browser per User**: Jalankan satu container Docker / microVM (misal *E2B*, *Fly Machines*, atau *Steel / Browserbase*) untuk setiap sesi aktif user agar profil browser tidak bercampur.
2. **Enkripsi State Profil Browser**: Simpan folder `user_data_dir` atau `context.storage_state()` yang terenkripsi per `user_id` di Object Storage (S3/R2) supaya user tidak perlu login ulang saat kembali esok hari.
3. **Audit Log**: Catat histori setiap persetujuan tindakan sensitif untuk transparansi pengguna.
