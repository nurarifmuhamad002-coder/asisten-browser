# Akun WaroengProxy Aktif

## Akun BARU (dipakai)
- Kode akun bot   : WX-JMJRY5U
- Username proxy  : WXJMJRY5U          <-- TANPA tanda hubung
- Password proxy  : 16 karakter hex (JANGAN ditulis di file ini)
- Deposit         : Rp10.000 masuk 16 Nov 2026 11.21 WIB
- Kuota           : 1 GB dibeli 16 Nov 2026
- STATUS          : ✅ PROXY JALAN — IP Indonesia terbukti

### Bukti uji nyata (16 Nov 2026)
    username tanpa -cc-ID  -> IP India (New Delhi) — negara acak
    -cc-ID rotating        -> timeout / TLS reset (tidak stabil)
    -cc-ID-s-label-ttl-N   -> IP 140.213.99.38 Bandung, XL Axiata  ✅
    -cc-ID-ct-jakarta      -> 502 (terlalu sempit)
    -cc-ID-isp-telkomsel   -> 502 (terlalu sempit)

    allindonesia.imigrasi.go.id -> HTTP 200 "All Indonesia"   (dulu 403)
    evisa.imigrasi.go.id        -> HTTP 200 "Official eVisa"  (dulu 403)

## Akun LAMA (tinggal Rp10.000 nyangkut, token hilang)
- Kode akun bot   : WX-LL44U4Q
- Username proxy  : WXLL44U4Q
- Deposit         : Rp10.000 (9 Nov 2026, DP-J6SRLWMZ)
- Status          : tidak bisa diakses, token akses tidak bisa dipulihkan

## Ambil password proxy (2 cara)
1. Lewat bot Telegram kita (disarankan):
     /wxkey wxk_live_xxxxxxxx      (API key dari dashboard > Akses API)
     /wxpasang                     (password diambil otomatis, proxy langsung nyala)

2. Manual dengan curl:
     curl https://waroengproxy.com/api/v1/proxy -H "X-Api-Key: wxk_live_xxxx"

## Aturan username (PENTING)
WaroengProxy memakai '-' sebagai pemisah parameter:
    WXJMJRY5U-cc-ID-s-bot01-ttl-3600
Kode akun yang ditampilkan bot (WX-JMJRY5U) HARUS dibuang tanda hubungnya.
Kalau tidak, gateway membalas: 400 "Format username tidak valid".

## Endpoint
- Host/port : proxy.waroengproxy.com:8000
- Protokol  : http, https, socks5 (port sama)
