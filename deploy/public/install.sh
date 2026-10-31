#!/usr/bin/env bash
# Corridor 公开模式安装脚本。目标机：Ubuntu 22.04，x86_64，用 root 运行。
#
#   sudo bash install.sh --archive <导出树.tar.gz> --sha256 <64 位十六进制>
#
# sha256 由发布方在本地算出，经独立渠道交给操作者，不从服务器上取。
# 这个脚本要在解压之前运行，所以只有它需要单独上传；其余部署文件都来自校验通过的归档。
#
# 可以在任何目录运行，--archive 可以写相对路径。
#
# 失败语义：
#   参数检查通过之后的任何失败，包括 sha256 不符，都停掉服务并保持下线，机器重启也不会
#   自己回来。sha256 不符时不解压，也不改动别的东西。不回退到旧版本，不降级为自签或明文。
#
# 这个脚本不对公网开放。它先关门禁，再启动服务，自动检查都在门禁之内做；
# 结束时外部访问仍然得 503。人工验收通过之后，由 open_public.sh 开放。
# 不需要模型密钥：模板是脚本规划配置，密钥和口令都不是安装的前提。
# 环境文件只由 systemd 读取，这里绝不 source 它，值里的 $ 对 shell 不安全。
set -euo pipefail
umask 022

UV_VERSION=0.13.1
UV_URL=https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz
UV_SHA256=eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220
CADDY_VERSION=2.11.7
CADDY_URL=https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION/caddy_${CADDY_VERSION}_linux_amd64.deb
CADDY_DEB_SHA512=47e8351c2317b427af14a103e763ca1118a3d2396a88b4c0669cdec9c4a68a957690194e2423a1633f53135741c33a41bdac2b55515b7d0f7adc8b733add50d9
PYTHON_VERSION=3.11
TLS_ATTEMPTS=${CORRIDOR_TLS_ATTEMPTS:-36}

# CORRIDOR_ROOT 只给测试用，真机上为空。
ROOT=${CORRIDOR_ROOT:-}
PREFIX=$ROOT/opt/corridor
ETC_DIR=$ROOT/etc/corridor
ENV_FILE=$ETC_DIR/web-public.env
LIVE_DIR=$ETC_DIR/live
STATE_DIR=$ROOT/var/lib/corridor
CADDYFILE=$ROOT/etc/caddy/Caddyfile
GATE_FILE=$ROOT/etc/caddy/corridor-gate.caddy
UNIT_FILE=$ROOT/etc/systemd/system/corridor-web.service
JOURNALD_FILE=$ROOT/etc/systemd/journald.conf.d/corridor.conf
MARKER=.release-ok

ARCHIVE=""
EXPECTED=""
WORK=""
RELEASE=""
PUBLIC_HOST=""
GUARDED=0
BUILDING=0

log() {
	printf 'install: %s\n' "$*"
}

die() {
	printf 'install: %s\n' "$*" >&2
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
	if [ "$BUILDING" -eq 1 ]; then
		seal_build || true
	fi
	[ -z "$WORK" ] || rm -rf -- "$WORK"
	if [ "$status" -ne 0 ] && [ "$GUARDED" -eq 1 ]; then
		take_offline install 安装失败
	fi
	exit "$status"
}

digest() {
	"$1" "$2" | cut -d' ' -f1
}

env_value() {
	sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

as_service() {
	(cd "$RELEASE" && runuser -u corridor -- env \
		"HOME=$PREFIX/.cache" "UV_CACHE_DIR=$PREFIX/.cache/uv" \
		"UV_PYTHON_INSTALL_DIR=$PREFIX/python" UV_NO_PROGRESS=1 "$@")
}

parse_args() {
	while [ $# -gt 0 ]; do
		case "$1" in
		--archive)
			[ $# -ge 2 ] || die "--archive 缺少路径"
			ARCHIVE=$2
			shift 2
			;;
		--sha256)
			[ $# -ge 2 ] || die "--sha256 缺少摘要"
			EXPECTED=$2
			shift 2
			;;
		*) die "未知参数：$1" ;;
		esac
	done
	[ -n "$ARCHIVE" ] || die "用法：install.sh --archive <导出树.tar.gz> --sha256 <摘要>"
	[ -f "$ARCHIVE" ] || die "找不到归档：$ARCHIVE"
	[[ "$EXPECTED" =~ ^[0-9a-f]{64}$ ]] || die "--sha256 必须是 64 位小写十六进制"
	ARCHIVE=$(readlink -f -- "$ARCHIVE")
}

require_target() {
	[ "$(id -u)" = 0 ] || die "必须用 root 运行"
	[ "$(uname -m)" = x86_64 ] || die "只支持 x86_64"
}

# 校验的是复制到 root 专用目录里的那一份，之后解压的也是这一份。
verify_archive() {
	WORK=$(mktemp -d)
	cp -- "$ARCHIVE" "$WORK/archive.tar.gz"
	local actual
	actual=$(digest sha256sum "$WORK/archive.tar.gz")
	[ "$actual" = "$EXPECTED" ] || die "sha256 不符，已拒绝：没有解压"
	log "归档 sha256 校验通过"
}

install_caddy() {
	local current
	current=$(caddy version 2>/dev/null || true)
	case "$current" in
	"v$CADDY_VERSION" | "v$CADDY_VERSION "*) return 0 ;;
	esac
	curl -fsSL --proto '=https' --tlsv1.2 -o "$WORK/caddy.deb" "$CADDY_URL"
	[ "$(digest sha512sum "$WORK/caddy.deb")" = "$CADDY_DEB_SHA512" ] || die "Caddy 安装包校验失败"
	dpkg --force-confold -i "$WORK/caddy.deb"
}

install_uv() {
	local current=""
	if [ -x "$PREFIX/bin/uv" ]; then
		current=$("$PREFIX/bin/uv" --version | cut -d' ' -f2)
	fi
	[ "$current" != "$UV_VERSION" ] || return 0
	curl -fsSL --proto '=https' --tlsv1.2 -o "$WORK/uv.tar.gz" "$UV_URL"
	[ "$(digest sha256sum "$WORK/uv.tar.gz")" = "$UV_SHA256" ] || die "uv 校验失败"
	mkdir -p "$WORK/uv"
	tar -xzf "$WORK/uv.tar.gz" -C "$WORK/uv" --strip-components=1
	install -m 0755 -o root -g root "$WORK/uv/uv" "$PREFIX/bin/uv"
}

prepare_system() {
	export DEBIAN_FRONTEND=noninteractive
	apt-get update
	apt-get install -y curl ca-certificates ufw
	id corridor >/dev/null 2>&1 \
		|| useradd --system --user-group --no-create-home --home-dir "$PREFIX" \
			--shell /usr/sbin/nologin corridor
	install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/bin" "$PREFIX/releases" \
		"$PREFIX/python" "$PREFIX/.cache"
	install -d -m 0750 -o root -g corridor "$ETC_DIR" "$LIVE_DIR"
	# 台账与 journal 在 release 之外，升级和回滚都不动它们。
	install -d -m 0700 -o corridor -g corridor "$STATE_DIR" "$STATE_DIR/journal"
	install_caddy
	install_uv
}

configure_firewall() {
	ufw default deny incoming
	ufw default allow outgoing
	ufw allow 22/tcp
	ufw allow 80/tcp
	ufw allow 443/tcp
	ufw --force enable
	ufw status >"$WORK/ufw.txt"
	local extra
	extra=$(awk '/ALLOW/ { print $1 }' "$WORK/ufw.txt" | grep -vxE '(22|80|443)/tcp' || true)
	[ -z "$extra" ] || die "防火墙还放行了其他端口，请先删除：$extra"
}

# 名单先落到文件再检查，避免管道提前结束和 pipefail 互相干扰。
check_archive_listing() {
	tar -tzf "$WORK/archive.tar.gz" >"$WORK/names.txt"
	tar -tzvf "$WORK/archive.tar.gz" >"$WORK/types.txt"
	if grep -Eq '^/|(^|/)\.\.(/|$)' "$WORK/names.txt"; then
		die "归档含绝对路径或 ..，已拒绝"
	fi
	if grep -qv '^[-d]' "$WORK/types.txt"; then
		die "归档含链接或特殊文件，已拒绝"
	fi
}

# 服务账号只在构建期间能写：本次 release 的目录、托管 Python 和 uv 缓存。构建一结束就全部
# 收回 root，运行中的服务账号改不了代码，也换不了解释器。
open_build() {
	BUILDING=1
	install -d -m 0755 -o corridor -g corridor "$RELEASE"
	chown -R corridor:corridor "$PREFIX/python" "$PREFIX/.cache"
}

seal_build() {
	chown -R root:root "$PREFIX/releases" "$PREFIX/python" "$PREFIX/.cache"
	chmod -R a+rX,go-w "$PREFIX/releases" "$PREFIX/python" "$PREFIX/.cache"
	BUILDING=0
}

# 实测，不靠上面两条命令的退出码：用服务账号自己找它能写的东西，找到一个就算失败。
check_sealed() {
	local found
	found=$(runuser -u corridor -- find -H "$PREFIX/releases" "$PREFIX/python" "$PREFIX/.cache" \
		"$PREFIX/bin" ! -type l -writable -print -quit)
	[ -z "$found" ] || die "服务账号还能写 $found，没有上线"
}

build_release() {
	RELEASE=$PREFIX/releases/${EXPECTED:0:12}
	if [ -f "$RELEASE/$MARKER" ] && [ "$(cat "$RELEASE/$MARKER")" = "$EXPECTED" ]; then
		log "release ${EXPECTED:0:12} 已构建，复用"
		check_sealed
		bash "$RELEASE/deploy/public/check_release.sh" capability "$RELEASE"
		return 0
	fi
	check_archive_listing
	rm -rf -- "$RELEASE"
	open_build
	# 用服务账号解压，归档里的属主和权限都不生效。
	(cd "$RELEASE" && runuser -u corridor -- tar -xzf - --no-same-owner --no-same-permissions) \
		<"$WORK/archive.tar.gz"
	local name
	for name in pyproject.toml uv.lock src/corridor/web/public.py deploy/public/check_release.sh; do
		[ -f "$RELEASE/$name" ] || die "归档缺少 $name"
	done
	as_service "$PREFIX/bin/uv" python install "$PYTHON_VERSION"
	as_service "$PREFIX/bin/uv" sync --frozen --no-dev --managed-python --python "$PYTHON_VERSION"
	seal_build
	check_sealed
	bash "$RELEASE/deploy/public/check_release.sh" capability "$RELEASE"
	printf '%s\n' "$EXPECTED" >"$RELEASE/$MARKER"
}

install_configuration() {
	local source=$RELEASE/deploy/public
	[ -e "$ENV_FILE" ] || install -m 0600 -o root -g root "$source/web-public.env.example" "$ENV_FILE"
	chown root:root "$ENV_FILE"
	chmod 0600 "$ENV_FILE"
	if [ -e "$LIVE_DIR/.env.deepseek" ]; then
		chown root:corridor "$LIVE_DIR/.env.deepseek"
		chmod 0640 "$LIVE_DIR/.env.deepseek"
	fi
	PUBLIC_HOST=$(env_value CORRIDOR_WEB_PUBLIC_HOST)
	[ -n "$PUBLIC_HOST" ] || die "环境文件没有 CORRIDOR_WEB_PUBLIC_HOST"
	grep -qxF "$PUBLIC_HOST {" "$source/Caddyfile" || die "Caddyfile 的站点名与环境文件不一致"
	runuser -u caddy -- env "HOME=$ROOT/var/lib/caddy" \
		caddy validate --adapter caddyfile --config "$source/Caddyfile" \
		|| die "Caddyfile 未通过目标版本校验，没有上线"
	install -m 0644 -o root -g root "$source/corridor-web.service" "$UNIT_FILE"
	install -d -m 0755 -o root -g root "$(dirname "$JOURNALD_FILE")"
	install -m 0644 -o root -g root "$source/journald-corridor.conf" "$JOURNALD_FILE"
	systemctl daemon-reload
	systemctl restart systemd-journald.service
}

switch_current() {
	ln -sfn "$1" "$PREFIX/current.new"
	mv -Tf "$PREFIX/current.new" "$PREFIX/current"
}

# 直接连本机，不依赖服务器回连自己的公网地址。curl 不带 -k，证书必须真实有效。
public_curl() {
	curl -sS --max-time 10 --resolve "$PUBLIC_HOST:443:127.0.0.1" \
		--resolve "$PUBLIC_HOST:80:127.0.0.1" "$@"
}

# 上游还没启动时，有效证书下回环地址得 502。这里只等证书，失败不降级。
wait_for_certificate() {
	local attempt=0 code=""
	while [ "$attempt" -lt "$TLS_ATTEMPTS" ]; do
		code=$(public_curl -o /dev/null -w '%{http_code}' "https://$PUBLIC_HOST/" || true)
		[ "$code" = 502 ] && return 0
		attempt=$((attempt + 1))
		sleep 5
	done
	die "HTTPS 没有用有效证书应答，不降级：请看 journalctl -u caddy"
}

# 门禁要实测，不靠配置文本：从本机的非回环地址连 443，Caddy 看到的对端就不是回环。
# 这一步在上游启动之前做，门禁不生效时业务一刻也没有对外。
check_gate() {
	local address code
	address=$(hostname -I | awk '{ print $1 }')
	[ -n "$address" ] || die "找不到本机的非回环地址，无法实测门禁"
	code=$(curl -sS --max-time 10 --resolve "$PUBLIC_HOST:443:$address" -o /dev/null \
		-w '%{http_code}' "https://$PUBLIC_HOST/" || true)
	[ "$code" = 503 ] || die "门禁没有生效：非回环地址得到 $code，应得 503"
}

verify_public() {
	local code redirect expected
	code=$(public_curl -D "$WORK/headers" -o /dev/null -w '%{http_code}' \
		"https://$PUBLIC_HOST/" || true)
	[ "$code" = 200 ] || die "HTTPS 没有用有效证书返回 200，不降级：请看 journalctl -u caddy"
	tr -d '\r' <"$WORK/headers" >"$WORK/plain"
	if grep -qi '^server:' "$WORK/plain"; then
		die "响应里还有 Server 头"
	fi
	redirect=$(public_curl -o /dev/null -w '%{http_code} %{redirect_url}' "http://$PUBLIC_HOST/" || true)
	expected="https://$PUBLIC_HOST/"
	case "$redirect" in
	"301 $expected" | "308 $expected") ;;
	*) die "HTTP 没有跳转到 HTTPS：$redirect" ;;
	esac
}

# 配对路径不进日志的实测：上游在线和下线时各发一次带金丝雀的手机页请求，
# 再查 Caddy 与进程的 journal。任何一处出现金丝雀都算失败。
check_log_canary() {
	local since canary code
	since=$(date '+%Y-%m-%d %H:%M:%S')
	canary=$(od -An -N11 -tx1 /dev/urandom | tr -d ' \n')
	public_curl -o /dev/null "https://$PUBLIC_HOST/phone/$canary" || true
	systemctl stop corridor-web.service
	code=$(public_curl -o /dev/null -w '%{http_code}' "https://$PUBLIC_HOST/phone/$canary" || true)
	[ "$code" = 502 ] || die "上游下线时应得 502，实际 $code"
	systemctl start corridor-web.service
	bash "$RELEASE/deploy/public/check_release.sh" serving "$PUBLIC_HOST"
	sleep 2
	journalctl --no-pager --since "$since" -u caddy.service -u corridor-web.service >"$WORK/journal.txt"
	if grep -qF "$canary" "$WORK/journal.txt"; then
		die "journal 里出现了手机配对路径，不能上线"
	fi
}

close_gate() {
	install -m 0644 -o root -g root "$RELEASE/deploy/public/corridor-gate.caddy" "$GATE_FILE"
	install -m 0644 -o root -g root "$RELEASE/deploy/public/Caddyfile" "$CADDYFILE"
	systemctl enable caddy.service
	systemctl start caddy.service
	systemctl reload caddy.service
}

# 顺序是这里的全部意义：先停旧服务，再关门禁，门禁实测生效之后才启动新版本。
deploy_behind_gate() {
	systemctl stop corridor-web.service || true
	close_gate
	wait_for_certificate
	check_gate
	switch_current "$RELEASE"
	systemctl enable corridor-web.service
	systemctl start corridor-web.service
	bash "$RELEASE/deploy/public/check_release.sh" serving "$PUBLIC_HOST"
	verify_public
	check_log_canary
}

main() {
	parse_args "$@"
	# 操作者常在 0750 的主目录里运行。服务账号进不去那里，find 这类命令会因此退出非 0，
	# 所以归档路径解析之后就离开，不留在调用者的目录里。
	cd /
	require_target
	trap on_exit EXIT
	GUARDED=1
	verify_archive
	prepare_system
	configure_firewall
	build_release
	install_configuration
	deploy_behind_gate
	GUARDED=0
	log "自动检查通过，门禁仍然关闭，外部访问得 503（release ${EXPECTED:0:12}，模型开关 $(env_value CORRIDOR_LIVE)）"
	log "人工验收通过后运行 open_public.sh；验收不通过就运行 systemctl disable --now corridor-web"
}

main "$@"
