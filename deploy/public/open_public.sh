#!/usr/bin/env bash
# 开放公网：验收通过之后才运行。清空门禁文件，重新加载 Caddy，再实测非回环地址能到业务。
#
#   sudo bash /opt/corridor/current/deploy/public/open_public.sh
#
# 任何失败都停掉服务并保持下线，机器重启也不会自己回来。
set -euo pipefail
umask 022

ROOT=${CORRIDOR_ROOT:-}
ENV_FILE=$ROOT/etc/corridor/web-public.env
GATE_FILE=$ROOT/etc/caddy/corridor-gate.caddy
HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
GUARDED=0

die() {
	printf 'open_public: %s\n' "$*" >&2
	exit 1
}

# 下线要经得起重启：disable 之后，机器重启也不会把服务带回来。
take_offline() {
	systemctl disable corridor-web.service >/dev/null 2>&1 || true
	systemctl stop corridor-web.service >/dev/null 2>&1 || true
	if systemctl is-active --quiet corridor-web.service; then
		printf '%s: 服务没有停下来，站点可能仍在线：请立刻手工运行 systemctl stop corridor-web\n' "$1" >&2
	else
		printf '%s: %s，服务已停止并保持下线，没有回退到旧版本。\n' "$1" "$2" >&2
	fi
}

on_exit() {
	local status=$?
	if [ "$status" -ne 0 ] && [ "$GUARDED" -eq 1 ]; then
		take_offline open_public 开放失败
	fi
	exit "$status"
}

[ "$(id -u)" = 0 ] || die "必须用 root 运行"
[ -f "$GATE_FILE" ] || die "先运行 install.sh"
host=$(sed -n 's/^CORRIDOR_WEB_PUBLIC_HOST=//p' "$ENV_FILE" | tail -n 1)
[ -n "$host" ] || die "环境文件没有 CORRIDOR_WEB_PUBLIC_HOST"

trap on_exit EXIT
GUARDED=1
bash "$HERE/check_release.sh" serving "$host"
: >"$GATE_FILE"
systemctl reload caddy.service
address=$(hostname -I | awk '{ print $1 }')
[ -n "$address" ] || die "找不到本机的非回环地址，无法实测开放结果"
code=$(curl -sS --max-time 10 --resolve "$host:443:$address" -o /dev/null \
	-w '%{http_code}' "https://$host/" || true)
[ "$code" = 200 ] || die "开放之后非回环地址得到 $code，应得 200"
GUARDED=0
printf 'open_public: 已开放：https://%s/ 。现在从外部再跑一次浏览器旅程，不通过就下线。\n' "$host"
