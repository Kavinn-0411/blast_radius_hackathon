#!/usr/bin/env bash
# Same as `make sample` (no make on Windows): build, run under scorer limits, score.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=${IMAGE:-blast-radius-entry}
HERE=$(pwd -W 2>/dev/null || pwd)
export MSYS_NO_PATHCONV=1
SET=${SET:-sample}  # any dir with commits/ + answers.json (tests.json always from sample/)
if [ ! -f "$SET/tests.json" ]; then cp sample/tests.json "$SET/tests.json"; fi

docker build --platform linux/amd64 --tag "$IMAGE" .
start=$(date +%s.%N)
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$HERE/$SET/commits:/data/commits:ro" \
  --volume "$HERE/sample/tests.json:/data/tests.json:ro" \
  "$IMAGE" sh -c 'python /app/main.py; echo "CPU_USEC $(grep usage_usec /sys/fs/cgroup/cpu.stat)" >&2' > output.jsonl 2> stderr.log
end=$(date +%s.%N)
echo "wall: $(python -c "print(round($end-$start,1))") s" >&2
grep CPU_USEC stderr.log | awk '{print "container CPU (s):", $3/1e6}'
python score.py "$SET" "$SET/answers.json" output.jsonl
python tools/breakdown.py "$SET"
