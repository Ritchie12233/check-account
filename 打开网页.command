#!/bin/bash
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
cd -- "$(dirname -- "$0")" || exit 1
mkdir -p web-data
if ! docker info >/dev/null 2>&1; then
  open -a Docker
  echo '正在等待 Docker Desktop 启动…'
  for ((i=0; i<60; i++)); do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
fi
if docker compose up -d web; then
  open 'http://127.0.0.1:18765'
else
  echo '启动失败，请查看上面的错误。'
  read -r -p '按回车关闭…' unused
fi
