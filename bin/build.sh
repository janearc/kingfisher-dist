#!/bin/sh
# build.sh -- the image, from a commit git can name.
#
#   bin/build.sh              build kingfisher:dev from HEAD and import into the target
#   bin/build.sh --no-import  build only
#
# WHY A SCRIPT AND NOT TWO LINES IN THE README. The image carries the short sha
# of the commit it was built from, as KINGFISHER_COMMIT, and every daemon's
# /health reports it. That is only true if the build passes it -- a bare
# `docker build` produces an image that says "unknown", which hygiene.py
# correctly fails. Putting the argument here means there is one build path and
# it cannot be forgotten. It also refuses a dirty tree: an image labelled with a
# commit must contain exactly that commit, or the label is a lie.
set -eu
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "build.sh: the tree has uncommitted changes; the image would claim a commit it does not match" >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

sha=$(git rev-parse --short HEAD)
echo "building kingfisher:dev from $sha"
docker build --build-arg "KINGFISHER_COMMIT=$sha" -t kingfisher:dev .
# the same image under its own name: the overlay's ${COMMIT} resolves to this
docker tag kingfisher:dev "kingfisher:$sha"

if [ "${1:-}" != "--no-import" ]; then
  # the middle command the README calls not optional: the k3d node runs its own
  # containerd and cannot see the host daemon's image
  # the cluster the image lands in is the target's, never a name typed here and
  # never a default: roll.sh exports TARGET; a hand run says it or skips the import
  [ -n "${TARGET:-}" ] || { echo "build.sh: set TARGET=<env> to import, or pass --no-import" >&2; exit 2; }
  k3d image import kingfisher:dev "kingfisher:$sha" -c "$TARGET"
fi
echo "kingfisher:dev = kingfisher:$sha = $sha"
