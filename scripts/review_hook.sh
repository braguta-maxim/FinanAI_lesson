#!/usr/bin/env bash
# Stop hook: ask a nested Claude to review changes and write planning/REVIEW.md.
# FINALLY_REVIEW_RUNNING stops the nested session's own Stop hook from recursing.
[ -n "$FINALLY_REVIEW_RUNNING" ] && exit 0
export FINALLY_REVIEW_RUNNING=1
cd "$(dirname "$0")/.." || exit 1
claude -p "Review changes since last commit and write results to a file named planning/REVIEW.md" \
  --permission-mode acceptEdits --disable-slash-commands
