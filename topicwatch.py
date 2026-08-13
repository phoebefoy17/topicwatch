# .github/workflows/daily-topicwatch.yml
#
# Runs topicwatch every morning on GitHub's servers and commits the report
# back to the repo. Free on public repos; well inside the free minutes on
# private ones (this job takes ~5-15 min/day).
#
# Why this over cron on your laptop: a closed lid means no run, and gaps in
# the corpus are the one thing that actually degrades this tool — the "rising
# term" scores compare against a trailing baseline, so missing days quietly
# distort every future report.
#
# Setup:
#   1. Make a repo with topicwatch.py, sources.json, and this file
#      at .github/workflows/daily-topicwatch.yml
#   2. Settings > Actions > General > Workflow permissions
#      -> "Read and write permissions"  (needed to commit the db + report)
#   3. Actions tab > Daily topicwatch > Run workflow (test it once manually)

name: Daily topicwatch

on:
  schedule:
    # 15:30 UTC = 5:30am Hawaii (HST is UTC-10, no daylight saving).
    # GitHub's scheduler is best-effort and often runs late under load;
    # avoid the top of the hour, which is the most congested slot.
    - cron: "30 15 * * *"
  workflow_dispatch:        # lets you trigger it by hand from the Actions tab

permissions:
  contents: write

concurrency:
  group: topicwatch         # never let two runs race on the sqlite file
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 60

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Fetch and build report
        env:
          TOPICWATCH_DELAY: "1.5"
          TOPICWATCH_MAX_NEW: "60"
        run: python topicwatch.py run

      - name: Commit corpus and report
        run: |
          git config user.name  "topicwatch-bot"
          git config user.email "actions@github.com"
          # `git add reports/` errors with "pathspec did not match" if the
          # directory does not exist yet, which is the case on a first run
          # where every feed failed. -A avoids that.
          git add -A
          git diff --staged --quiet || git commit -m "topicwatch $(date +%F)"
          git push

      # Optional: also drop the report where you'll actually read it.
      # Set SLACK_WEBHOOK in Settings > Secrets and variables > Actions.
      # - name: Post to Slack
      #   if: env.SLACK_WEBHOOK != ''
      #   env:
      #     SLACK_WEBHOOK: ${{ secrets.SLACK_WEBHOOK }}
      #   run: |
      #     REPORT="reports/$(date +%F)-topicwatch.md"
      #     python - <<'PY'
      #     import json, os, urllib.request
      #     path = f"reports/{os.popen('date +%F').read().strip()}-topicwatch.md"
      #     body = open(path).read()[:3500]
      #     req = urllib.request.Request(
      #         os.environ["SLACK_WEBHOOK"],
      #         data=json.dumps({"text": body}).encode(),
      #         headers={"Content-Type": "application/json"})
      #     urllib.request.urlopen(req)
      #     PY
