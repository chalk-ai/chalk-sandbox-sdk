#!/bin/bash
mkdir -p /logs/verifier
# word_count is written by a RUN step, so it also proves the COPY happened before that RUN.
if [ "$(cat /app/upper.txt 2>/dev/null)" = "CHALK SANDBOXES RUN HARBOR TASKS" ] && [ "$(cat /opt/assets/word_count)" = "5" ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
