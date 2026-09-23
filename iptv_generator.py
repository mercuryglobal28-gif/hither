name: Update IPTV Playlist

on:
  schedule:
    - cron: '0 */6 * * *'        # كل 6 ساعات
  workflow_dispatch:              # تشغيل يدوي
  push:
    paths:
      - 'source_playlist.m3u'     # عند رفع قائمة جديدة

permissions:
  contents: write

concurrency:
  group: iptv-update
  cancel-in-progress: false

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 60

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run updater
        env:
          INPUT_PLAYLIST:  source_playlist.m3u
          OUTPUT_PLAYLIST: playlist.m3u
          CACHE_FILE:      working_links.json
          MAX_WORKERS:     '60'
          CHECK_TIMEOUT:   '6'
        run: python iptv_generator.py

      - name: Show summary
        run: |
          echo "=== playlist.m3u ==="
          grep -c '^#EXTINF' playlist.m3u || true
          echo ""
          echo "=== Report ==="
          head -50 update_report.txt || true

      - name: Commit & push
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add playlist.m3u working_links.json update_report.txt
          if git diff --staged --quiet; then
            echo "لا تغييرات."
          else
            CNT=$(grep -c '^#EXTINF' playlist.m3u || echo 0)
            git commit -m "🤖 تحديث القائمة ($CNT قناة) — $(date -u +'%Y-%m-%d %H:%M UTC')"
            git push
          fi
