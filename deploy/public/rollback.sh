#!/usr/bin/env bash
# Corridor 回滚脚本：只回滚到具备公开模式能力的 release。
#
#   sudo bash /opt/corridor/current/deploy/public/rollback.sh          列出已安装的 release
#   sudo bash /opt/corridor/current/deploy/public/rollback.sh <编号>    切换到这个 release
#
# 目标不合格时什么都不改，服务保持原样。切换之后启动失败就停掉服务并保持下线，机器重启
# 也不会自己回来，不再自动换到别的版本。台账、journal 和环境文件都在 release 之外，
# 回滚不动它们，预算不会因此重置。回滚不动门禁。
set -euo pipefail
umask 022

ROOT=${CORRIDOR_ROOT:-}
PREFIX=$ROOT/opt/corridor
ENV_FILE=$ROOT/etc/corridor/web-public.env
MARKER=.release-ok
HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
SWITCHED=0

die() {
	printf 'rollback: %s\n' "$*" >&2
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
	if [ "$status" -ne 0 ] && [ "$SWITCHED" -eq 1 ]; then
		take_offline rollback 回滚后没有通过检查
	fi
	exit "$status"
}

[ "$(id -u)" = 0 ] || die "必须用 root 运行"

if [ $# -eq 0 ]; then
	ls -1 "$PREFIX/releases"
	exit 0
fi
[ $# -eq 1 ] || die "用法：rollback.sh [release 编号]"
[[ "$1" =~ ^[0-9a-f]{12}$ ]] || die "release 编号是 12 位十六进制"

target=$PREFIX/releases/$1
[ -d "$target" ] || die "没有这个 release：$1"
[ -f "$target/$MARKER" ] || die "$1 没有构建完成，拒绝回滚"
bash "$HERE/check_release.sh" capability "$target" \
	|| die "$1 不具备公开模式能力，拒绝回滚；服务保持原样"
host=$(sed -n 's/^CORRIDOR_WEB_PUBLIC_HOST=//p' "$ENV_FILE" | tail -n 1)
[ -n "$host" ] || die "环境文件没有 CORRIDOR_WEB_PUBLIC_HOST"

trap on_exit EXIT
SWITCHED=1
systemctl stop corridor-web.service || true
ln -sfn "$target" "$PREFIX/current.new"
mv -Tf "$PREFIX/current.new" "$PREFIX/current"
systemctl enable corridor-web.service
systemctl start corridor-web.service
bash "$HERE/check_release.sh" serving "$host"
SWITCHED=0
printf 'rollback: 已切换到 %s\n' "$1"
