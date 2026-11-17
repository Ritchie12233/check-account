#!/bin/bash
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
cd -- "$(dirname -- "$0")"
if [ "$#" -eq 0 ]; then
  echo '用法: ./query.sh 用户名 [--site GitHub] [其他 Sherlock 参数]'
  exit 1
fi
case "$1" in
  -*|*/*|*\\*|.|..) echo '请输入用户名，不要输入网址或路径。'; exit 1 ;;
esac
mkdir -p results
exec docker compose run --rm -T sherlock --txt --csv --print-found "$@"
