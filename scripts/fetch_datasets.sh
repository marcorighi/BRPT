#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${ROOT}/data"
mkdir -p "${DATA_DIR}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/fetch_datasets.sh c21
  scripts/fetch_datasets.sh psps
  scripts/fetch_datasets.sh all

Warning: the PSPS download is very large (the CECM page reports about 884 MB
compressed). Dataset files are intentionally excluded from Git.
USAGE
}

download_c21() {
  local url='https://zenodo.org/records/13755236/files/c21.gz?download=1'
  local out="${DATA_DIR}/c21.gz"
  echo "Downloading C21 -> ${out}"
  curl -fL --retry 3 --continue-at - -o "${out}" "${url}"
  echo "Published Zenodo MD5: 4dd51363fe05cbd54ba109aab86a66fd"
  md5sum "${out}" || true
}

download_psps() {
  local url='https://www.cecm.sfu.ca/Pseudoprimes/psps-below-2-to-64.txt.bz2'
  local out="${DATA_DIR}/psps.bz2"
  echo "Downloading PSPS -> ${out}"
  curl -fL --retry 3 --continue-at - -o "${out}" "${url}"
}

case "${1:-}" in
  c21)  download_c21 ;;
  psps) download_psps ;;
  all)  download_c21; download_psps ;;
  *)    usage; exit 2 ;;
esac
