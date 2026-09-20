# Deployment HERO ke VPS Ubuntu 24.04 + Integrasi Nextcloud

Panduan ini untuk memindahkan **seluruh proyek HERO** (bukan cuma datanya) ke
VPS Tencent Cloud Anda yang sudah menjalankan Nextcloud di Ubuntu 24.04, dan
membuat knowledge base + hasil export otomatis muncul di Nextcloud.

> Semua perintah di bawah harus **Anda jalankan sendiri** — dari terminal Mac
> untuk langkah transfer, lalu lewat SSH di VPS untuk sisanya. Sesi ini tidak
> punya akses jaringan ke VPS Anda.

## Arsitektur

```
┌─────────────────────────── VPS Tencent (Ubuntu 24.04) ───────────────────────────┐
│                                                                                    │
│  /opt/hero/                          Nextcloud (native / snap / Docker — apa      │
│  ├── hero/            kode Python    pun cara instalnya, tidak disentuh)          │
│  ├── config/sources.yaml                                                          │
│  ├── .venv/                                                                       │
│  └── data/                                                                        │
│       ├── knowledge_base/   ──rclone sync (WebDAV)──▶  Files/HERO-KnowledgeBase   │
│       ├── export/           ──rclone sync (WebDAV)──▶  Files/HERO-Exports         │
│       ├── hero_catalog.db          (state internal — TIDAK disinkron)             │
│       └── staging/, logs/          (sementara — TIDAK disinkron)                  │
│                                                                                    │
│  systemd timer (harian, 02:30) → run-pipeline.sh                                  │
│    discover → harvest → kb-restructure → inventory-export → rclone sync           │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Kenapa rclone + WebDAV, bukan menaruh file langsung di folder data Nextcloud?**
Karena cara ini jalan **sama persis** apapun cara Nextcloud Anda terpasang —
native (apt/LAMP), Nextcloud Snap, atau Docker/AIO (paket instan yang umum
ditawarkan marketplace VPS). Kalau ditaruh langsung di direktori data
Nextcloud, harus tahu persis lokasi filesystem-nya dan repot mengurus
kepemilikan file (`www-data` vs proses HERO), dan kalau Nextcloud-nya Docker,
lokasi itu bahkan tidak terlihat dari luar container sama sekali. rclone
bicara ke Nextcloud lewat WebDAV — protokol HTTP standar yang sama dipakai
aplikasi desktop/mobile Nextcloud — jadi tidak peduli instalasinya seperti apa.

Database katalog (`hero_catalog.db`) sengaja **tidak** disinkron: itu state
internal SQLite (ada file `-wal`/`-shm` yang berubah tiap detik saat pipeline
jalan), bukan sesuatu yang perlu dilihat manusia di Nextcloud.

---

## Prasyarat

- [ ] Alamat IP atau domain VPS, akses SSH (root atau user dengan sudo)
- [ ] Nextcloud sudah bisa diakses lewat browser (`https://nextcloud-anda.com` atau `https://IP-VPS`)
- [ ] Akun Nextcloud yang akan menampung data HERO (bisa akun Anda sendiri)
- [ ] Di Mac Anda: proyek ini ada di `/Users/mirzafathirr/Documents/Cool Yeah/TC/Capstone`

---

## Langkah 1 — Pindahkan kode ke VPS

Dari **terminal Mac** (bukan di VPS):

```bash
# Ganti USER dan IP_VPS sesuai VPS Anda
export VPS="USER@IP_VPS"

# Siapkan direktori tujuan (sekali saja)
ssh "$VPS" "sudo mkdir -p /opt/hero && sudo chown \$USER:\$USER /opt/hero"

# Salin kode — .venv dan data/ SENGAJA tidak ikut (venv dibuat ulang di VPS,
# data akan terisi sendiri dari hasil scraping di server)
rsync -avz --progress \
  --exclude='.venv' --exclude='data' --exclude='__pycache__' \
  --exclude='.pytest_cache' --exclude='.DS_Store' \
  "/Users/mirzafathirr/Documents/Cool Yeah/TC/Capstone/" \
  "$VPS:/opt/hero/"
```

Kalau nanti ada perubahan kode di Mac, ulangi perintah `rsync` yang sama —
aman dijalankan berkali-kali (hanya mengirim yang berubah).

> **Alternatif via git** (lebih rapi untuk update rutin): `git init` proyek
> ini di Mac, push ke repo privat (GitHub/GitLab), lalu di VPS cukup
> `git clone` sekali dan `git pull` tiap update. `.gitignore` sudah disiapkan
> agar `.venv/` dan `data/` tidak ikut ter-commit.

---

## Langkah 2 — Instalasi di VPS

SSH masuk ke VPS, lalu:

```bash
ssh "$VPS"
cd /opt/hero
sudo bash deploy/install.sh
```

Skrip ini otomatis:
1. Install paket sistem: `python3`, `poppler-utils`, `tesseract-ocr` (+ bahasa Indonesia & Inggris), `rclone`
2. Membuat user sistem `hero` (khusus menjalankan HERO, tidak bisa login — prinsip least-privilege, terpisah dari akun Anda)
3. Membuat virtual environment Python + install HERO
4. Memasang jadwal otomatis (`systemd timer`, harian jam 02:30)

Verifikasi:
```bash
sudo -u hero /opt/hero/.venv/bin/hero version
# Harus muncul: OCR: tesseract siap — bahasa dipakai: ind, eng
```

---

## Langkah 3 — Konfigurasi sumber

`config/sources.yaml` **tidak perlu diubah** untuk deployment ini — path
penyimpanan (`storage:`) tetap relatif ke `/opt/hero` (folder kerja systemd
service sudah diset ke situ). Yang perlu Anda putuskan hanya cakupan
scraping: buka `/opt/hero/config/sources.yaml` dan sesuaikan `sektor`/`jenis`
di bagian JDIH OJK kalau tidak mau menarik seluruh 120 kombinasi sekaligus.

---

## Langkah 4 — Hubungkan ke Nextcloud (rclone)

### 4a. Buat App Password di Nextcloud (jangan pakai password akun utama)

Di browser: **Nextcloud → foto profil (kanan atas) → Settings → Security →
Devices & sessions → "Create new app password"**. Beri nama `hero-vps`,
klik Create. **Salin token yang muncul** — hanya ditampilkan sekali.

App Password ini bisa dicabut kapan saja dari halaman yang sama tanpa
mengganti password akun Anda — jauh lebih aman daripada menaruh password
asli di server.

### 4b. Konfigurasi rclone di VPS, sebagai user `hero`

```bash
sudo -u hero -H rclone config
```

Ikuti wizard interaktifnya dengan jawaban berikut:

| Prompt | Isi |
|---|---|
| `n) New remote` | `n` |
| `name>` | `nextcloud` |
| `Storage>` | cari dan pilih **`webdav`** |
| `url>` | `https://nextcloud-anda.com/remote.php/dav/files/USERNAME/` (ganti domain & USERNAME — perhatikan **garis miring di akhir**) |
| `vendor>` | `nextcloud` |
| `user>` | username Nextcloud Anda |
| `pass>` | pilih `y`, lalu tempel **App Password** dari langkah 4a (bukan password login) |
| `bearer_token>` | kosongkan (Enter) |
| `Edit advanced config?` | `n` |
| `Keep this remote?` | `y` |
| `q) Quit config` | `q` |

Uji koneksinya:
```bash
sudo -u hero -H rclone lsd nextcloud:
# Harus tampil daftar folder Nextcloud Anda tanpa error
```

Kalau muncul error 401 — App Password salah tempel atau username salah.
Kalau 404 — cek lagi URL-nya (harus persis path `remote.php/dav/files/...`).

### 4c. Buat folder tujuan di Nextcloud (sekali saja)

```bash
sudo -u hero -H rclone mkdir nextcloud:HERO-KnowledgeBase
sudo -u hero -H rclone mkdir nextcloud:HERO-Exports
```

Kedua folder ini sekarang akan muncul di aplikasi Nextcloud Anda (web,
desktop sync client, aplikasi mobile) — dan otomatis terisi setiap pipeline
jalan.

---

## Langkah 5 — Uji manual sebelum mengandalkan jadwal otomatis

```bash
# Discovery kecil dulu (satu kombinasi sektor/jenis, cepat) untuk memastikan
# semua bagian nyambung sebelum menjalankan yang penuh (bisa 30+ menit)
sudo -u hero /opt/hero/.venv/bin/hero discover --sektor 01 --jenis 06
sudo -u hero /opt/hero/.venv/bin/hero inventory

# Jalankan satu putaran pipeline penuh secara manual
sudo -u hero /opt/hero/deploy/run-pipeline.sh
```

Setelah selesai, cek di Nextcloud (web atau aplikasi) — folder
`HERO-KnowledgeBase` dan `HERO-Exports` harus sudah berisi file.

---

## Langkah 6 — Jadwal otomatis

Sudah aktif otomatis dari `install.sh`. Untuk memeriksa atau mengubah:

```bash
# Status jadwal & kapan giliran berikutnya
systemctl list-timers hero-pipeline.timer

# Jalankan sekarang juga, di luar jadwal (untuk uji coba)
sudo systemctl start hero-pipeline.service

# Log real-time
journalctl -u hero-pipeline.service -f

# Log putaran-putaran sebelumnya (juga tersimpan sebagai file biasa)
ls -la /opt/hero/data/logs/
```

Ubah jam jadwal: edit `OnCalendar=` di
`/etc/systemd/system/hero-pipeline.timer`, lalu:
```bash
sudo systemctl daemon-reload
sudo systemctl restart hero-pipeline.timer
```

Ubah berapa dokumen yang diunduh per putaran (default 100 per sumber): edit
`HARVEST_LIMIT` di baris `Environment=` yang bisa Anda tambahkan ke
`hero-pipeline.service`, atau langsung di `deploy/run-pipeline.sh`.

---

## Operasional

### Update kode

```bash
# Dari Mac:
rsync -avz --exclude='.venv' --exclude='data' --exclude='__pycache__' \
  "/Users/mirzafathirr/Documents/Cool Yeah/TC/Capstone/" "$VPS:/opt/hero/"

# Di VPS:
sudo -u hero /opt/hero/.venv/bin/pip install -q -e /opt/hero
sudo systemctl restart hero-pipeline.timer   # opsional, timer tidak perlu direstart kecuali file .timer berubah
```

### Backup katalog

`hero_catalog.db` adalah satu-satunya state yang benar-benar penting dan
tidak tersinkron ke mana pun — cadangkan berkala:

```bash
sudo -u hero sqlite3 /opt/hero/data/hero_catalog.db ".backup /opt/hero/data/hero_catalog.db.bak"
# lalu tarik file .bak itu ke Mac, atau ikutkan ke rclone sync terpisah:
sudo -u hero -H rclone copy /opt/hero/data/hero_catalog.db.bak nextcloud:HERO-Backup
```

### Melihat status knowledge base dari VPS

```bash
sudo -u hero /opt/hero/.venv/bin/hero stats
sudo -u hero /opt/hero/.venv/bin/hero inventory
```

---

## Keamanan

- **App Password, bukan password akun** untuk rclone — bisa dicabut sendiri tanpa ganti password utama kalau VPS bermasalah.
- Config rclone (`~hero/.config/rclone/rclone.conf`) berisi token itu dalam bentuk ter-obfuscate (bukan plaintext, tapi juga bukan enkripsi kuat) — pastikan permission-nya `600` dan hanya bisa dibaca user `hero`:
  ```bash
  sudo chmod 600 /opt/hero/.config/rclone/rclone.conf
  ```
- User sistem `hero` sengaja **tanpa shell login** (`--shell /usr/sbin/nologin`) — hanya bisa dieksekusi lewat `sudo -u hero`, tidak bisa dipakai SSH langsung.
- Firewall dasar (kalau belum ada `ufw` aktif):
  ```bash
  sudo ufw allow OpenSSH
  sudo ufw allow 80,443/tcp   # untuk Nextcloud
  sudo ufw enable
  ```
- HERO sendiri tidak membuka port apapun — dia klien HTTP keluar (scraping) + WebDAV keluar (rclone), tidak menerima koneksi masuk sama sekali.

---

## Troubleshooting

| Gejala | Kemungkinan penyebab & solusi |
|---|---|
| `hero: command not found` | Anda memanggil `hero` langsung — harus lewat venv: `/opt/hero/.venv/bin/hero` |
| `rclone lsd nextcloud:` → `401 Unauthorized` | App Password salah/sudah dicabut — buat baru (langkah 4a), `rclone config` ulang untuk remote `nextcloud` |
| `rclone lsd nextcloud:` → `404` | URL WebDAV salah — pastikan formatnya `https://domain/remote.php/dav/files/USERNAME/` persis, termasuk garis miring di akhir |
| Folder di Nextcloud tidak terisi setelah pipeline jalan | Cek `journalctl -u hero-pipeline.service -n 100` — cari baris `[sync-kb]` atau `[sync-export]`, lihat pesan errornya |
| `Permission denied` saat `install.sh` bikin venv | Pastikan Anda pakai `sudo bash deploy/install.sh` (bukan dijalankan sebagai user biasa) |
| Pipeline lambat / VPS terasa berat saat jam 02:30 | Turunkan `HARVEST_LIMIT` di `run-pipeline.sh`, atau geser `OnCalendar` di file `.timer` ke jam yang lebih sepi |
| Nextcloud terasa lambat saat file besar disinkron | Wajar untuk sinkron pertama kali (2.876+ halaman/59 PDF); putaran berikutnya jauh lebih cepat karena `rclone sync` hanya mengirim yang berubah |

---

## Ringkasan perintah — dari nol sampai jalan

```bash
# === Di Mac ===
export VPS="user@ip-vps-anda"
ssh "$VPS" "sudo mkdir -p /opt/hero && sudo chown \$USER:\$USER /opt/hero"
rsync -avz --exclude='.venv' --exclude='data' --exclude='__pycache__' --exclude='.pytest_cache' \
  "/Users/mirzafathirr/Documents/Cool Yeah/TC/Capstone/" "$VPS:/opt/hero/"

# === Di VPS (ssh "$VPS") ===
cd /opt/hero
sudo bash deploy/install.sh
sudo -u hero -H rclone config            # ikuti tabel langkah 4b
sudo -u hero -H rclone mkdir nextcloud:HERO-KnowledgeBase
sudo -u hero -H rclone mkdir nextcloud:HERO-Exports
sudo -u hero /opt/hero/deploy/run-pipeline.sh    # uji manual sekali
systemctl list-timers hero-pipeline.timer         # pastikan jadwal aktif
```
