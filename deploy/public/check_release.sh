#!/usr/bin/env bash
# Corridor 发布版本检查。两种用法：
#   check_release.sh capability <release 目录>  目标版本是否具备公开模式能力
#   check_release.sh serving <公开域名>         本机上游是否真的在公开模式下服务
# 检查失败退出码非 0。这里只读代码和本机回环端口，不读环境文件、凭据和台账。
set -euo pipefail

PORT=${CORRIDOR_PORT:-8772}
STARTUP_SECONDS=${CORRIDOR_STARTUP_SECONDS:-60}
PROBE_SECONDS=${CORRIDOR_PROBE_SECONDS:-60}
PROBE_HOST=probe.example.test
HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
WORK=""

die() {
	printf 'check_release: %s\n' "$*" >&2
	exit 1
}

cleanup() {
	[ -z "$WORK" ] || rm -rf -- "$WORK"
}

as_service() {
	if [ "$(id -u)" = 0 ]; then
		runuser -u corridor -- "$@"
	else
		"$@"
	fi
}

# 入口在给定环境下必须以退出码 2 拒绝启动。旧版本不认识公开模式，会照常启动并等到超时，
# 退出码就不是 2。这里用干净环境，不继承服务配置。
refuses_to_start() {
	local release=$1 status=0
	shift
	(
		cd "$release"
		as_service env -i "PATH=$PATH" HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 "$@" \
			timeout "$STARTUP_SECONDS" "$release/.venv/bin/python" -m corridor.web \
			--host 127.0.0.1 --port 0
	) >/dev/null 2>&1 || status=$?
	[ "$status" -eq 2 ]
}

capability() {
	local release=${1:-}
	[ -n "$release" ] || die "缺少 release 目录"
	release=$(cd "$release" && pwd)
	[ -x "$release/.venv/bin/python" ] || die "$release 没有可用的解释器"
	(
		cd "$release"
		as_service env -i "PATH=$PATH" HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 \
			"$release/.venv/bin/python" "$HERE/capability.py"
	) || die "$release 缺少公开模式能力"
	refuses_to_start "$release" "CORRIDOR_WEB_PUBLIC_HOST=$PROBE_HOST" CORRIDOR_LIVE=1 \
		|| die "$release 在缺验证子时没有拒绝启动"
	refuses_to_start "$release" "CORRIDOR_WEB_PUBLIC_HOST=$PROBE_HOST" \
		CORRIDOR_WEB_PUBLIC_DEPLOYMENTS=openai-gpt-6.1-sol \
		|| die "$release 在部署集合不合规时没有拒绝启动"
}

serving() {
	local host=${1:-} base="http://127.0.0.1:$PORT" code="" waited=0
	[ -n "$host" ] || die "缺少公开域名"
	while [ "$waited" -lt "$PROBE_SECONDS" ]; do
		code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Host: $host" "$base/" || true)
		[ "$code" = 200 ] && break
		waited=$((waited + 1))
		sleep 1
	done
	[ "$code" = 200 ] || die "公开 Host 没有得到 200（最后状态 $code）"
	code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$base/" || true)
	[ "$code" = 403 ] || die "回环 Host 应得 403，实际 $code：进程不在公开模式"
	WORK=$(mktemp -d)
	trap cleanup EXIT
	code=$(curl -s --max-time 30 -D "$WORK/headers" -o "$WORK/body" -w '%{http_code}' -X POST \
		-H "Host: $host" -H "Origin: https://$host" -H 'Content-Type: application/json' \
		-H 'X-Corridor-Web: 1' -H 'X-Forwarded-For: 127.0.0.1' --data '{}' \
		"$base/api/session" || true)
	[ "$code" = 200 ] || die "新建会话应得 200，实际 $code"
	tr -d '\r' <"$WORK/headers" >"$WORK/plain"
	grep -i '^set-cookie: __Host-' "$WORK/plain" >"$WORK/cookie" || die "响应没有 __Host- Cookie"
	grep -qi '; *secure' "$WORK/cookie" || die "Cookie 没有 Secure"
	grep -qi '; *path=/\(;\|$\)' "$WORK/cookie" || die "Cookie 的 Path 不是 /"
	if grep -qi '; *domain=' "$WORK/cookie"; then
		die "Cookie 带了 Domain"
	fi
	grep -q '"public_banner"' "$WORK/body" || die "首次响应没有演示环境横幅"
}

case "${1:-}" in
capability) capability "${2:-}" ;;
serving) serving "${2:-}" ;;
*) die "用法：check_release.sh capability <release 目录> | serving <公开域名>" ;;
esac
