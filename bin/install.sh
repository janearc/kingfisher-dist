#!/bin/sh
# install.sh -- put kingfisher's control verbs where the enclave expects them.
#
# every service here leaves a control script in the operator's init
# directory that accepts start, stop, status and so on,
# and that works whether or not Claude is running. gaggle's bin/gaggle is the
# model: the script lives in the repo, and the installer LINKS it -- not
# copies -- so there is one file to edit and the repo's history is its history.
# hygiene.py's init column reads pass when the link exists.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
for LINKDIR in "$HOME/.claude/init.claude" "$HOME/.local/bin"; do
  if [ -d "$LINKDIR" ]; then
    ln -sfn "$REPO/bin/kingfisher" "$LINKDIR/kingfisher"
    echo "linked $LINKDIR/kingfisher -> $REPO/bin/kingfisher"
  fi
done
