#!/usr/bin/env bash
# 打开真模型：准备台账，运行 access_setup，写入验证子对，打开开关，重启服务。
#
#   sudo bash /opt/corridor/current/deploy/public/enable_live.sh
#
# 前提：/etc/corridor/live/.env.deepseek 已经放好。这个脚本不打印、不复制模型密钥。
# 口令由 access_setup 随机生成，只在终端显示一次，不进任何文件。再次运行就换一个新口令。
# 重启之后的任何失败，包括 restart 命令自身失败，都停掉服务并保持下线。
set -euo pipefail
umask 077

ROOT=${CORRIDOR_ROOT:-}
PREFIX=$ROOT/opt/corridor
ETC_DIR=$ROOT/etc/corridor
ENV_FILE=$ETC_DIR/web-public.env
KEY_FILE=$ETC_DIR/live/.env.deepseek
JOURNAL_DIR=$ROOT/var/lib/corridor/journal
LEDGER=$JOURNAL_DIR/live-ledger.sqlite3
LEDGER_MARKER=$ETC_DIR/ledger-initialized
HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
NEXT=""
GUARDED=0

die() {
	printf 'enable_live: %s\n' "$*" >&2
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
	[ -z "$NEXT" ] || rm -f -- "$NEXT"
	if [ "$status" -ne 0 ] && [ "$GUARDED" -eq 1 ]; then
		take_offline enable_live 重启后没有通过检查
	fi
	exit "$status"
}

service_python() {
	(cd "$PREFIX/current" && runuser -u corridor -- env HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 \
		"$PREFIX/current/.venv/bin/python" "$@")
}

# 网页进程从不创建台账，这里是第一次创建的唯一入口。标记放在 /etc/corridor，和台账分开：
# 标记在而台账不在，说明台账丢了，要人工核账，这里绝不重新开始计预算。
prepare_ledger() {
	if [ -e "$LEDGER" ]; then
		service_python -m corridor.web.ledger_setup --journal-dir "$JOURNAL_DIR" --check \
			|| die "台账无法读取，真模型没有打开：先按 README 人工核账"
	elif [ -e "$LEDGER_MARKER" ]; then
		die "台账丢失，真模型没有打开：先按 README 人工核账"
	else
		service_python -m corridor.web.ledger_setup --journal-dir "$JOURNAL_DIR" --spent-cny 0
	fi
	touch "$LEDGER_MARKER"
}

[ "$(id -u)" = 0 ] || die "必须用 root 运行"
[ -f "$ENV_FILE" ] || die "先运行 install.sh"
[ -s "$KEY_FILE" ] || die "先把模型密钥放到 $KEY_FILE"
chown root:corridor "$KEY_FILE"
chmod 0640 "$KEY_FILE"
host=$(sed -n 's/^CORRIDOR_WEB_PUBLIC_HOST=//p' "$ENV_FILE" | tail -n 1)
[ -n "$host" ] || die "环境文件没有 CORRIDOR_WEB_PUBLIC_HOST"

trap on_exit EXIT
prepare_ledger
NEXT=$(mktemp "$ETC_DIR/.web-public.env.XXXXXX")
grep -vE '^(CORRIDOR_LIVE|CORRIDOR_WEB_ACCESS_KDF|LIVE_ACCESS_CODE)=' "$ENV_FILE" >"$NEXT" || true
printf 'CORRIDOR_LIVE=1\n' >>"$NEXT"
# 标准输出只有两行配置，写进文件；口令在标准错误，只出现在终端。
service_python -m corridor.web.access_setup >>"$NEXT"
chown root:root "$NEXT"
chmod 0600 "$NEXT"
mv -f "$NEXT" "$ENV_FILE"
NEXT=""
GUARDED=1
systemctl enable corridor-web.service
systemctl restart corridor-web.service
bash "$HERE/check_release.sh" serving "$host"
GUARDED=0
printf 'enable_live: 已打开。把上面显示的口令保存好，它不会再出现。\n'
