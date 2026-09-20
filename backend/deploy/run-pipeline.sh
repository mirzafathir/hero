#!/usr/bin/env bash
# HERO — satu putaran pipeline lengkap: discover -> harvest bertahap ->
# restructure -> export. Dipanggil oleh hero-pipeline.service (lihat timer-nya
# untuk jadwal), tapi juga aman dijalankan manual untuk uji coba:
#   sudo -u hero /opt/hero/deploy/run-pipeline.sh
set -uo pipefail

HERO_HOME="/opt/hero"
BIN="${HERO_HOME}/.venv/bin/hero"
LOG_DIR="${HERO_HOME}/data/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="${LOG_DIR}/pipeline-${STAMP}.log"

# Berapa dokumen baru yang diunduh per situs pada tiap putaran. Angka kecil
# dengan jadwal harian => beban ke server sumber tetap ringan dan merata,
# bukan lonjakan besar sekali seminggu.
HARVEST_LIMIT="${HERO_HARVEST_LIMIT:-100}"

mkdir -p "${LOG_DIR}"
exec > >(tee -a "${LOG}") 2>&1

echo "===== HERO pipeline mulai: $(date -Is) ====="

run_step() {
  local name="$1"; shift
  echo "--- [${name}] $*"
  if "$@"; then
    echo "--- [${name}] selesai"
  else
    echo "--- [${name}] GAGAL (exit $?) — lanjut ke langkah berikutnya"
  fi
}

# 1) Daftar ulang setiap rekaman + baca halaman detail yang belum pernah
#    dibaca. `hero discover` sudah mencocokkan status hukum ke JDIH secara
#    otomatis di akhir (reconcile_status) — tidak perlu langkah terpisah.
run_step discover "${BIN}" discover

# 2) Unduh dokumen baru, bertahap per sumber (bukan --all, supaya satu putaran
#    tidak menyedot ratusan dokumen sekaligus).
run_step harvest-jdih       "${BIN}" harvest --source jdih-ojk       --limit "${HARVEST_LIMIT}"
run_step harvest-regulasi   "${BIN}" harvest --source ojk-regulasi   --limit "${HARVEST_LIMIT}"
run_step harvest-rancangan  "${BIN}" harvest --source ojk-rancangan  --limit "${HARVEST_LIMIT}"

# 3) Rapikan ulang KB (idempoten — memindahkan file yang status/kategorinya berubah).
run_step restructure "${BIN}" kb-restructure

# 4) Ekspor tabel penuh (CSV/XLSX/JSON/HTML) ke data/export/.
run_step export "${BIN}" inventory-export

# 5) Sinkron ke Nextcloud lewat WebDAV (rclone), kalau remote "nextcloud:"
#    sudah dikonfigurasi (deploy/README.md bagian 4). Kalau belum, langkah
#    ini dilewati dengan pesan yang jelas — pipeline lain tetap jalan.
if rclone listremotes 2>/dev/null | grep -qx "nextcloud:"; then
  # --create-empty-src-dirs supaya struktur folder kosong pun ikut tampak;
  # sync (bukan copy) supaya file yang dipindah kb-restructure (langkah 3)
  # juga terhapus dari lokasi lamanya di Nextcloud, bukan menumpuk duplikat.
  run_step sync-kb      rclone sync "${HERO_HOME}/data/knowledge_base" nextcloud:HERO-KnowledgeBase --create-empty-src-dirs
  run_step sync-export  rclone sync "${HERO_HOME}/data/export"         nextcloud:HERO-Exports
else
  echo "--- [sync] remote rclone 'nextcloud:' belum dikonfigurasi — dilewati (lihat deploy/README.md bagian 4)"
fi

echo "===== HERO pipeline selesai: $(date -Is) ====="

# Simpan log 30 hari terakhir saja.
find "${LOG_DIR}" -name 'pipeline-*.log' -mtime +30 -delete
