name: Sync F1 Fantasy Data

on:
  schedule:
    - cron: '0 10 * * 2'  # Every Tuesday 10:00 AM UTC
  workflow_dispatch:        # Manual trigger from Actions tab

jobs:
  build:
    runs-on: ubuntu-latest

    permissions:
      contents: write       # Required to commit history.json back to repo

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install requests

      - name: Run Sync Script
        env:
          F1_EMAIL:    ${{ secrets.F1_EMAIL }}
          F1_PASSWORD: ${{ secrets.F1_PASSWORD }}
        run: python fetch_f1_data.py

      - name: Commit and Push history.json
        uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "chore: auto-sync F1 Fantasy data [skip ci]"
          file_pattern: history.json
