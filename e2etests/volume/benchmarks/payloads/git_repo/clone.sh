#!/bin/sh

set -eu

url=$1
revision=$2
dest=$3
repository=/tmp/repository

# Keep these commands in step with chalkcompute/_git.py.
git clone --depth=1 --filter=blob:none --no-checkout --no-tags -- "$url" "$repository"
git -C "$repository" fetch --depth=1 --no-tags origin -- "$revision"
git --git-dir="$repository/.git" --work-tree="$dest" checkout --force FETCH_HEAD
cp -a "$repository/.git" "$dest/.git"
