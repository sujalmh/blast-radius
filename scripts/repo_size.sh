#!/usr/bin/env bash
# Repo-size guardrail for the Blast Radius submission.
#
#   - sums the bytes of every git-tracked file, i.e. what the scorer actually
#     clones (the 20 MB limit is on the repository, not on untracked local
#     build noise), and fails above 18 MB so there is 2 MB of headroom left
#   - prints the ten largest tracked files, which is where the bytes go
#   - asserts the buildathon session token is not tracked: it must never be
#     committed or shipped inside an image
#
# Usage: bash scripts/repo_size.sh        (or: make size)

set -u

LIMIT_BYTES=$((18 * 1024 * 1024))
status=0

if ! command -v git >/dev/null 2>&1; then
	echo "FAIL git is not installed; cannot list the tracked tree" >&2
	exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	echo "FAIL not inside a git work tree" >&2
	exit 1
fi

total=0
count=0
listing=""
while IFS= read -r -d '' path; do
	[ -f "$path" ] || continue
	bytes=$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')
	[ -n "$bytes" ] || bytes=0
	total=$((total + bytes))
	count=$((count + 1))
	listing="${listing}${bytes} ${path}"$'\n'
done < <(git ls-files -z)

human=$(awk -v b="$total" 'BEGIN { printf "%.2f", b / 1048576 }')

echo "tracked files: ${count}"
echo "tracked bytes: ${total} (${human} MB)"
echo "largest tracked files:"
printf '%s' "$listing" | sort -rn | head -n 10 | while IFS= read -r line; do
	[ -n "$line" ] || continue
	bytes=${line%% *}
	path=${line#* }
	printf '  %10s  %s\n' "$bytes" "$path"
done

if [ "$total" -gt "$LIMIT_BYTES" ]; then
	echo "FAIL tracked tree ${total} bytes > ${LIMIT_BYTES} bytes (18 MB); trim maps/ or sources"
	status=1
else
	echo "PASS tracked tree ${total} bytes <= ${LIMIT_BYTES} bytes (18 MB)"
fi

if git ls-files --error-unmatch .buildathon_session >/dev/null 2>&1; then
	echo "FAIL .buildathon_session is tracked; untrack it and keep it in .gitignore"
	status=1
else
	echo "PASS .buildathon_session is not tracked"
fi

exit "$status"
