#!/bin/sh
set -eu

cd "$(dirname "$0")"
exec "${ELRC_PYTHON:-.venv/bin/python}" -m PyInstaller \
    --noconfirm --windowed --specpath build \
    --name 'ELRC 录像下载器' \
    --osx-bundle-identifier local.elrc.video-downloader \
    --add-data "$PWD/LICENSE:." \
    --add-data "$PWD/DISCLAIMER.md:." \
    --recursive-copy-metadata playwright \
    --recursive-copy-metadata requests \
    elrc_downloader.py
