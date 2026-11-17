#!/bin/bash
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
cd -- "$(dirname -- "$0")" || exit 1
echo 'Sherlock 本地账号查询'
echo '输入用户名（不是姓名或网址）；同名账号不代表属于同一个人。'
read -r -p '用户名: ' username
if [ -n "$username" ]; then
  ./query.sh "$username"
  status=$?
  if [ "$status" -eq 0 ]; then
    open results
  else
    echo "查询未完成，退出码: $status。请检查上面的错误以及 Docker Desktop。"
  fi
fi
read -r -p '按回车关闭窗口…' unused
