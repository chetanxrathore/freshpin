#!/bin/sh
# Claude Code hook entrypoint: run check_written_file.py with the first Python >= 3.9
# that starts. Without one the hook stays silent (exit 0) so it never blocks the agent.
# No external commands (dirname...) here: a hook may run with a minimal PATH.
case $0 in */*) dir=${0%/*} ;; *) dir=. ;; esac
dir=$(CDPATH='' cd -- "$dir" && pwd)
for py in python3 /usr/bin/python3 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
  if command -v "$py" >/dev/null 2>&1 &&
    "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    exec "$py" "$dir/check_written_file.py"
  fi
done
exit 0
