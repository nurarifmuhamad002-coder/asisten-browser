# Akun WaroengProxy Aktif

## Akun BARU (dipakai)
- Kode akun bot   : WX-JMJRY5U
- Username proxy  : WXJMJRY5U          <-- TANPA tanda hubung
- Password proxy  : (ambil via API, lihat bawah)
- Deposit         : Rp10.000 masuk 16 Nov 2026 11.21 WIB
- Kuota          : 0 GB  -> WAJIB beli GB dulu

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
