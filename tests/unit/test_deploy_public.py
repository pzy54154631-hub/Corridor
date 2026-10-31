"""The public deployment files match RW-8 items 15-20 and fail closed.

The static tests read the files as the target machine would. The script tests run the real
scripts under bash with every system command replaced by a stub that writes to one log, so
the order of the steps and the failure paths are checked without root, a network or Caddy.
The Caddyfile itself can only be validated on the target (install.sh runs ``caddy validate``).
"""

import configparser
import hashlib
import importlib.util
import io
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from corridor.ui.live_runtime import SpendLedger
from corridor.web import ledger_setup
from corridor.web.access_setup import draw_code
from corridor.web.public import BUDGET_CNY, JOURNAL_PATH, Kdf, PublicConfig, Rate, public_caps

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/public"
HOST = "43-154-247-126.sslip.io"
SCRIPTS = ("check_release.sh", "enable_live.sh", "install.sh", "open_public.sh", "rollback.sh")
GATE = "corridor-gate.caddy"


def read(name):
    return (DEPLOY / name).read_text(encoding="utf-8")


def caddy_lines():
    return [line.strip() for line in read("Caddyfile").splitlines() if not line.startswith("#")]


def active_caddy():
    return [line for line in caddy_lines() if line and not line.startswith("#")]


def unit():
    parser = configparser.RawConfigParser(strict=False, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(read("corridor-web.service"))
    return parser


def environment():
    pairs = {}
    for line in read("web-public.env.example").splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            pairs[name] = value
    return pairs


def entry_arguments():
    """The unit's ExecStart as the entry's parsed namespace."""
    words = shlex.split(unit()["Service"]["ExecStart"])
    assert words[:3] == ["/opt/corridor/current/.venv/bin/python", "-m", "corridor.web"]
    values = dict(zip(words[3::2], words[4::2], strict=True))
    return values, SimpleNamespace(
        public_host=None,
        max_sessions=int(values["--max-sessions"]),
        idle_seconds=float(values["--idle-seconds"]),
        evict_idle_after=float(values["--evict-idle-after"]),
        per_ip_sessions=int(values["--per-ip-sessions"]),
        per_ip_new_sessions=values["--per-ip-new-sessions"],
    )


def live_environment():
    kdf = Kdf(salt="0" * 32)
    return {
        **environment(),
        "CORRIDOR_LIVE": "1",
        "CORRIDOR_WEB_ACCESS_KDF": kdf.text(),
        "LIVE_ACCESS_CODE": kdf.verifier(draw_code()),
    }


# --- Caddyfile ------------------------------------------------------------------------------


def test_the_caddyfile_names_its_target_version_and_only_the_public_site():
    assert "# 目标版本：Caddy v2.11.7" in read("Caddyfile")
    assert f"{HOST} {{" in active_caddy()
    blocks = [
        line
        for line in read("Caddyfile").splitlines()
        if line.endswith("{") and not line.startswith(("\t", "#"))
    ]
    assert blocks == ["{", f"{HOST} {{"]
    assert "CADDY_VERSION=2.11.7\n" in read("install.sh")


@pytest.mark.parametrize(
    "line",
    [
        "-Server",
        "header_up -X-Real-Ip",
        "max_size 128KB",
        "read_header 10s",
        "read_body 30s",
        "idle 2m",
        "protocols h1 h2",
        "reverse_proxy 127.0.0.1:8772 {",
    ],
)
def test_the_caddyfile_has(line):
    assert line in active_caddy()


@pytest.mark.parametrize(
    "word", ["encode", "trusted_proxies", "tls", "http://", "basicauth", "file_server"]
)
def test_the_caddyfile_does_not_use(word):
    assert not any(line.split()[0].startswith(word) for line in active_caddy())


def test_the_site_imports_the_gate_by_exact_name_so_a_lost_file_stops_caddy():
    lines = active_caddy()
    site = lines.index(f"{HOST} {{")
    # A plain name, no glob: Caddy refuses to load when an exact import is missing.
    assert lines[site:].count(f"import {GATE}") == 1
    assert lines.index(f"import {GATE}") < lines.index("reverse_proxy 127.0.0.1:8772 {")


def test_the_shipped_gate_lets_only_the_loopback_through():
    active = [line.strip() for line in read(GATE).splitlines() if not line.startswith("#")]
    assert active == [
        "@outside {",
        "not remote_ip 127.0.0.1/8 ::1",
        "}",
        "respond @outside 503",
    ]


def test_the_caddyfile_writes_no_access_log_and_drops_the_loggers_that_carry_the_uri():
    lines = active_caddy()
    logs = [line for line in lines if line.split()[0] == "log"]
    assert logs == ["log default {"]
    assert lines.index("log default {") < lines.index(f"{HOST} {{")
    excluded = next(line for line in lines if line.startswith("exclude "))
    assert {"http.log.error", "http.handlers.reverse_proxy"} <= set(excluded.split()[1:])


# --- systemd unit ---------------------------------------------------------------------------


def test_the_unit_runs_unprivileged_under_a_strict_sandbox():
    service = unit()["Service"]
    assert service["User"] == "corridor"
    assert service["Group"] == "corridor"
    assert service["ProtectSystem"] == "strict"
    assert service["EnvironmentFile"] == "/etc/corridor/web-public.env"
    assert service["Restart"] == "always"
    assert service["MemoryMax"] == "6G"
    assert service["StateDirectoryMode"] == "0700"
    assert service["NoNewPrivileges"] == "yes"


def test_there_is_no_health_restart_of_any_kind():
    text = read("corridor-web.service")
    assert "Watchdog" not in text
    assert "ExecStartPost" not in text
    assert not list(DEPLOY.glob("*.timer"))
    assert not any("health" in path.name for path in DEPLOY.iterdir())


def test_the_entry_arguments_and_the_template_give_the_ruled_capacity():
    values, args = entry_arguments()
    assert (values["--host"], values["--port"]) == ("127.0.0.1", "8772")
    config = PublicConfig.from_args(args, environment())
    assert config.host == HOST
    assert (config.max_sessions, config.idle_seconds, config.evict_idle_after) == (128, 3600, 1800)
    assert config.per_ip_sessions == 16
    assert config.rates["session"] == Rate(30, 20.0)
    assert config.deployments == {"deepseek-flash"}


# --- environment template -------------------------------------------------------------------


def test_the_template_has_no_trailing_comment_and_no_secret():
    for line in read("web-public.env.example").splitlines():
        assert line.startswith("#") or re.fullmatch(r"[A-Z_]+=[^\s#]+", line), line
    pairs = environment()
    assert "LIVE_ACCESS_CODE" not in pairs
    assert "CORRIDOR_WEB_ACCESS_KDF" not in pairs
    assert not [name for name in pairs if name.startswith("LLM_")]


def test_the_template_starts_scripted_without_any_secret():
    pairs = environment()
    assert pairs["CORRIDOR_LIVE"] != "1"
    config = PublicConfig.from_args(entry_arguments()[1], pairs)
    assert config.kdf is None


def test_the_template_keeps_one_journal_directory_and_the_ruled_caps():
    pairs = environment()
    assert pairs["LIVE_JOURNAL_DIR"] == "/var/lib/corridor/journal"
    assert pairs["LIVE_SESSION_CALLS"] == "20"
    assert pairs["LIVE_BUDGET_CNY"] == "100"
    assert "CORRIDOR_WEB_JOURNAL_BASE" not in pairs
    assert Path(pairs["LIVE_JOURNAL_DIR"]) == JOURNAL_PATH


def test_a_live_site_pointed_at_another_directory_does_not_start():
    pairs = {**live_environment(), "LIVE_JOURNAL_DIR": "/var/lib/corridor/journal2"}
    with pytest.raises(ValueError, match="fixed journal directory"):
        PublicConfig.from_args(entry_arguments()[1], pairs)


def test_the_template_with_the_verifier_pair_starts_live():
    config = PublicConfig.from_args(entry_arguments()[1], live_environment())
    assert config.kdf is not None
    from corridor.web.public import live_configuration

    assert public_caps(live_configuration(live_environment())) is None


def test_the_template_switched_on_without_the_pair_is_refused():
    pairs = {**environment(), "CORRIDOR_LIVE": "1"}
    with pytest.raises(ValueError, match="CORRIDOR_WEB_ACCESS_KDF"):
        PublicConfig.from_args(entry_arguments()[1], pairs)


# --- scripts: static ------------------------------------------------------------------------


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_scripts_stop_on_any_error_and_never_source_the_environment_file(name):
    text = read(name)
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail\n" in text
    lines = [line.strip() for line in text.splitlines() if not line.strip().startswith("#")]
    assert not [line for line in lines if re.match(r"(source|\.)\s", line)]


def test_the_installer_verifies_the_archive_before_any_extraction():
    text = read("install.sh")
    main = text[text.index("main() {") :]
    order = [
        main.index(step)
        for step in (
            "verify_archive",
            "prepare_system",
            "configure_firewall",
            "build_release",
            "install_configuration",
            "deploy_behind_gate",
        )
    ]
    assert order == sorted(order)
    assert text.index("verify_archive() {") < text.index("tar -")


def test_the_installer_pins_its_tools_and_downloads_no_script():
    text = read("install.sh")
    assert "UV_VERSION=0.13.1\n" in text
    assert re.search(r"^UV_SHA256=[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^CADDY_DEB_SHA512=[0-9a-f]{128}$", text, re.M)
    assert "--frozen" in text
    assert "| sh" not in text
    assert "| bash" not in text


def test_the_firewall_opens_three_tcp_ports_only():
    text = read("install.sh")
    assert re.findall(r"ufw allow (\S+)", text) == ["22/tcp", "80/tcp", "443/tcp"]
    assert "ufw default deny incoming" in text


def test_no_file_offers_a_weaker_fallback():
    for path in DEPLOY.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "curl -k" not in text
        assert "--insecure" not in text
        assert "iptables" not in text


def test_the_readme_names_the_three_chores_and_says_scripted_needs_none():
    text = read("README.md")
    for phrase in (
        "`.env.deepseek`",
        "access_setup",
        "保存口令",
        "三件事在这个模式下都不需要做",
        "按单次调用上限预留推算的累计上限",
        "不等于提供商账单硬上限",
    ):
        assert phrase in text


def test_the_readme_says_a_failed_acceptance_takes_the_site_offline():
    text = read("README.md")
    assert "**任何一项不通过，就让站点下线，不带问题开放或继续开放：**" in text
    assert "sudo systemctl disable --now corridor-web" in text
    assert "`install.sh` 不对公网开放" in text
    assert "open_public.sh" in text
    assert "已在运行的服务不受影响" not in text


def test_the_readme_states_the_public_sites_boundaries():
    """RW-9 item 6, and what the R8 review asked to be written down."""
    text = read("README.md")
    for phrase in (
        "口令会进入终端回显",
        "不要把它的输出重定向到文件",
        "共享 NAT",
        "同时持有 16 个会话",
        "手机轮询每秒补 12 个",
        "页面和静态文件每秒补 60 个（突发 1200）",
        "在途请求 12 个",
        "慢请求体",
        "打开一次页面约 50 个请求",
        "溢出桶",
        "配对令牌是持有者凭证",
        "按创建来源占位",
        "只有 11 位以上的号码、邮箱、验证码和密钥换成代号",
        "整张合成通讯录",
        "服务账号只在构建期间能写这些目录",
        "持续操作会排队",
        "第 20 轮单次约 10 秒",
        "变慢但不拒绝",
    ):
        assert phrase in text, phrase


def test_the_readme_says_which_script_rolls_forward_again():
    text = read("README.md")
    rollback = text.split("## 回滚")[1].split("## 失败时的行为")[0]
    assert "旧 release 的回滚脚本切不回新版" in rollback
    assert (
        "sudo bash /opt/corridor/releases/<新编号>/deploy/public/rollback.sh <新编号>" in rollback
    )


def test_the_readme_says_the_install_may_start_anywhere():
    assert "可以在任何目录运行" in read("README.md")


def test_the_readme_describes_a_lost_ledger_and_its_reconciliation():
    text = read("README.md")
    for phrase in (
        "/etc/corridor/ledger-initialized",
        "ledger_setup --journal-dir /var/lib/corridor/journal --spent-cny",
        "评审结束后删除 journal 也会删除预算历史",
    ):
        assert phrase in text


# --- scripts: under bash --------------------------------------------------------------------


def find_bash():
    candidates = []
    git = shutil.which("git")
    if os.name == "nt" and git:
        for parent in Path(git).resolve().parents[:3]:
            candidates += [parent / "bin/bash.exe", parent / "usr/bin/bash.exe"]
    found = shutil.which("bash")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            reply = subprocess.run(
                [str(candidate), "-c", "echo $BASH_VERSION; command -v sha256sum tar timeout"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if reply.returncode == 0 and len(reply.stdout.split()) == 4:
            return str(candidate)
    return None


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no usable bash")


@needs_bash
@pytest.mark.parametrize("name", SCRIPTS)
def test_the_scripts_parse(name):
    subprocess.run([BASH, "-n", "-"], input=read(name).encode(), check=True, timeout=30)


STUB = """#!/bin/sh
printf '%s\\n' "{name} $*" >>"$STUB_LOG"
{body}
"""
# A directory holding LOCKED_OUT stands for a 0750 home such as /home/ubuntu: root works there,
# but the service account cannot enter it, and find then exits 1 even with nothing to report.
LOCKED_OUT = ".service-cannot-enter"
RUNUSER = f"""while [ "$1" != "--" ]; do shift; done; shift
if [ -e "./{LOCKED_OUT}" ]; then
  echo "find: Failed to restore initial working directory: Permission denied" >&2
  exit 1
fi
case "$1" in
  find) [ -z "${{WRITABLE:-}}" ] || echo "$2"; exit 0 ;;
esac
exec "$@\""""
INSTALL = """n=$#; directory=0
while [ "$n" -gt 0 ]; do
  a=$1; shift; n=$((n-1))
  case "$a" in
    -d) directory=1 ;;
    -m|-o|-g) shift; n=$((n-1)) ;;
    *) set -- "$@" "$a" ;;
  esac
done
if [ "$directory" = 1 ]; then mkdir -p "$@"; else mkdir -p "$(dirname "$2")"; cp "$1" "$2"; fi"""
CURL = """out=""; previous=""
for a in "$@"; do
  [ "$previous" = "-o" ] && out=$a
  [ "$previous" = "-D" ] && : >"$a"
  previous=$a
done
case "$*" in
  *uv-x86_64*) cp "$STUB_DATA/uv.tar.gz" "$out" ;;
  *caddy_*) : >"$out" ;;
  *":443:10.0.0.2"*)
    if [ -n "${BROKEN_GATE:-}" ]; then printf 502
    elif [ -s "$CORRIDOR_ROOT/etc/caddy/corridor-gate.caddy" ]; then printf 503
    elif [ -n "${FAIL_OPEN:-}" ]; then printf 502
    else printf 200
    fi ;;
  *"--resolve"*"http://"*) printf '308 https://%s/' "$PUBLIC_HOST" ;;
  *"/phone/"*)
    printf '%s\\n' "$*" >>"$STUB_DATA/phone.txt"
    case "$*" in *http_code*) printf 502 ;; esac ;;
  *)
    if [ -n "${FAIL_TLS:-}" ]; then printf 000
    elif [ -e "$STUB_DATA/web-up" ]; then printf 200
    else printf 502
    fi ;;
esac"""
SYSTEMCTL = """case "$*" in
  "restart corridor-web.service")
    [ -z "${FAIL_RESTART:-}" ] || exit 1
    : >"$STUB_DATA/web-up" ;;
  "start corridor-web.service") : >"$STUB_DATA/web-up" ;;
  "stop corridor-web.service") [ -n "${FAIL_STOP:-}" ] || rm -f "$STUB_DATA/web-up" ;;
  "is-active --quiet corridor-web.service") [ -e "$STUB_DATA/web-up" ] ;;
  "reload caddy.service") [ -z "${FAIL_RELOAD:-}" ] || exit 1 ;;
esac"""
JOURNAL = '[ -z "${LEAK:-}" ] || cat "$STUB_DATA/phone.txt"'
UFW = """case "$1" in status)
printf '22/tcp ALLOW Anywhere\\n80/tcp ALLOW Anywhere\\n443/tcp ALLOW Anywhere\\n'
[ -z "${EXTRA_PORT:-}" ] || printf '8772/tcp ALLOW Anywhere\\n' ;;
esac"""
CHECK_RELEASE = """#!/usr/bin/env bash
printf 'check_release %s\\n' "$1" >>"$STUB_LOG"
if [ "$1" = capability ] && [ -n "${FAIL_CAPABILITY:-}" ]; then exit 1; fi
if [ "$1" = serving ] && [ -n "${FAIL_SERVING:-}" ]; then exit 1; fi
"""


class Stage:
    """A root directory for the scripts, a bin of stubs and the one log they share."""

    def __init__(self, base):
        self.base = base
        self.root = base / "root"
        self.bin = base / "bin"
        self.data = base / "data"
        self.log = base / "stub.log"
        for directory in (self.root, self.bin, self.data):
            directory.mkdir()
        self.log.write_text("", encoding="utf-8")
        real = subprocess.run(
            [BASH, "-c", "command -v sha256sum"], capture_output=True, text=True, check=True
        ).stdout.strip()
        uv_digest = re.search(r"^UV_SHA256=(\S+)", read("install.sh"), re.M).group(1)
        caddy_digest = re.search(r"^CADDY_DEB_SHA512=(\S+)", read("install.sh"), re.M).group(1)
        stubs = {
            "id": 'case "$1" in -u) echo 0 ;; esac',
            "runuser": RUNUSER,
            "install": INSTALL,
            "curl": CURL,
            "journalctl": JOURNAL,
            "ufw": UFW,
            "sleep": "",
            "chown": "",
            "ln": "",
            "mv": 'case "$*" in *current*) ;; *) exec /usr/bin/mv "$@" ;; esac',
            "apt-get": "",
            "dpkg": "",
            "useradd": "",
            "systemctl": SYSTEMCTL,
            "hostname": "echo '10.0.0.2 '",
            "caddy": '[ -z "${BAD_CADDYFILE:-}" ] || case "$1" in validate) exit 1 ;; esac',
            "sha512sum": f'echo "{caddy_digest}  $1"',
            "sha256sum": (
                f'case "$1" in *uv.tar.gz) echo "{uv_digest}  $1" ;; *) exec "{real}" "$@" ;; esac'
            ),
        }
        for name, body in stubs.items():
            path = self.bin / name
            path.write_text(STUB.format(name=name, body=body), encoding="utf-8", newline="\n")
            path.chmod(0o755)

    def run(self, script, *args, cwd=None, **env):
        # Git for Windows' launcher puts its own directories first, so the stubs go in front
        # from inside bash, where the path is written the way bash splits it.
        stubs = re.sub(r"^([A-Za-z]):", lambda m: "/" + m[1].lower(), self.path(self.bin))
        return subprocess.run(
            [BASH, "-c", 'PATH="$STUB_BIN:$PATH" exec bash "$@"', "bash", script, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env={
                **os.environ,
                "STUB_BIN": stubs,
                "CORRIDOR_ROOT": self.path(self.root),
                "STUB_LOG": self.path(self.log),
                "STUB_DATA": self.path(self.data),
                "PUBLIC_HOST": HOST,
                "CORRIDOR_TLS_ATTEMPTS": "2",
                **env,
            },
        )

    @staticmethod
    def path(path):
        return Path(path).as_posix()

    def steps(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def called(self, prefix):
        return any(line.startswith(prefix) for line in self.steps())

    def index(self, prefix):
        return next(i for i, line in enumerate(self.steps()) if line.startswith(prefix))

    def probe(self):
        """Where the installer looked through the gate from a non-loopback address."""
        return next(i for i, line in enumerate(self.steps()) if ":443:10.0.0.2" in line)

    def service_running(self):
        (self.data / "web-up").write_text("", encoding="utf-8")

    def assert_offline(self):
        steps = self.steps()
        assert not (self.data / "web-up").exists()
        disabled = len(steps) - 1 - steps[::-1].index("systemctl disable corridor-web.service")
        assert "systemctl stop corridor-web.service" in steps[disabled:]
        # Nothing brings the service back, now or at the next boot.
        for verb in ("start", "restart", "enable"):
            assert f"systemctl {verb} corridor-web.service" not in steps[disabled:]


def tar_bytes(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def release_files():
    files = {
        f"deploy/public/{name}": (DEPLOY / name).read_bytes()
        for name in ("Caddyfile", GATE, "corridor-web.service", "journald-corridor.conf")
    }
    files["deploy/public/web-public.env.example"] = (DEPLOY / "web-public.env.example").read_bytes()
    files["deploy/public/check_release.sh"] = CHECK_RELEASE.encode()
    files["pyproject.toml"] = b"[project]\n"
    files["uv.lock"] = b"version = 1\n"
    files["src/corridor/web/public.py"] = b""
    return files


@pytest.fixture
def stage(tmp_path):
    made = Stage(tmp_path)
    uv = (
        b'#!/bin/sh\nprintf \'uv %s\\n\' "$*" >>"$STUB_LOG"\n'
        b'case "$*" in sync*) [ -z "${FAIL_UV:-}" ] || exit 1 ;; esac\n'
    )
    (made.data / "uv.tar.gz").write_bytes(tar_bytes({"uv-x86_64-unknown-linux-gnu/uv": uv}))
    return made


def archive_in(stage, files=None):
    content = tar_bytes(release_files() if files is None else files)
    path = stage.base / "corridor-public.tar.gz"
    path.write_bytes(content)
    return stage.path(path), hashlib.sha256(content).hexdigest()


def install(stage, archive, digest, **env):
    return stage.run(
        stage.path(DEPLOY / "install.sh"), "--archive", archive, "--sha256", digest, **env
    )


def wrong_digest(digest):
    return ("0" if digest[0] != "0" else "1") + digest[1:]


@needs_bash
def test_the_service_account_writes_only_while_the_release_is_built(stage):
    """RW-9 item 6: ``releases`` never belongs to the service account, and the release, the
    managed Python and the uv cache go back to root before anything runs from them."""
    archive, digest = archive_in(stage)
    assert install(stage, archive, digest).returncode == 0
    steps = stage.steps()
    root = stage.path(stage.root)
    trees = f"{root}/opt/corridor/releases {root}/opt/corridor/python {root}/opt/corridor/.cache"
    created = [line for line in steps if line.startswith("install -d") and "-o corridor" in line]
    assert [line.split()[-1] for line in created if "/opt/corridor" in line] == [
        f"{root}/opt/corridor/releases/{digest[:12]}"
    ]
    opened = stage.index("chown -R corridor:corridor")
    assert steps[opened].endswith(f"{root}/opt/corridor/python {root}/opt/corridor/.cache")
    sealed = steps.index(f"chown -R root:root {trees}")
    measured = next(i for i, line in enumerate(steps) if "-writable" in line)
    assert (
        opened
        < stage.index("uv python install")
        < stage.index("uv sync --frozen")
        < sealed
        < measured
        < stage.index("check_release capability")
    )
    assert steps[measured].startswith("runuser -u corridor -- find -H ")
    assert f"{root}/opt/corridor/bin" in steps[measured]


@needs_bash
def test_a_failed_build_still_takes_the_write_access_back(stage):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, digest, FAIL_UV="1")
    assert reply.returncode != 0
    assert stage.called("chown -R root:root")
    assert not stage.called("check_release capability")
    stage.assert_offline()


@needs_bash
def test_a_tree_the_service_can_still_write_stops_the_install(stage):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, digest, WRITABLE="1")
    assert reply.returncode != 0
    assert "服务账号还能写" in reply.stderr
    assert not stage.called("check_release capability")
    assert not stage.called("ln -sfn")
    stage.assert_offline()


@needs_bash
def test_a_reused_release_gives_the_service_no_write_access(stage):
    archive, digest = archive_in(stage)
    assert install(stage, archive, digest).returncode == 0
    stage.log.write_text("", encoding="utf-8")
    assert install(stage, archive, digest).returncode == 0
    assert not stage.called("chown -R corridor")
    assert any("-writable" in line for line in stage.steps())


def locked_home(stage):
    """The README's command from a home the service account cannot enter, archive beside it."""
    archive, digest = archive_in(stage)
    home = stage.base / "home"
    home.mkdir()
    (home / LOCKED_OUT).write_text("", encoding="utf-8")
    shutil.move(archive, home / "corridor-public.tar.gz")
    return home, digest


def install_from(stage, home, digest, **env):
    return stage.run(
        stage.path(DEPLOY / "install.sh"),
        "--archive",
        "corridor-public.tar.gz",
        "--sha256",
        digest,
        cwd=home,
        **env,
    )


@needs_bash
def test_the_install_works_from_a_home_the_service_cannot_enter(stage):
    home, digest = locked_home(stage)
    reply = install_from(stage, home, digest)
    assert reply.returncode == 0, reply.stderr
    assert (stage.root / "opt/corridor/releases" / digest[:12] / "pyproject.toml").exists()
    assert stage.called("runuser -u corridor -- find -H ")
    assert stage.called("runuser -u caddy -- env ")
    assert (stage.data / "web-up").exists()


@needs_bash
def test_a_reinstall_from_that_home_measures_the_seal_again(stage):
    home, digest = locked_home(stage)
    assert install_from(stage, home, digest).returncode == 0
    stage.log.write_text("", encoding="utf-8")
    assert install_from(stage, home, digest).returncode == 0
    assert stage.called("runuser -u corridor -- find -H ")


@needs_bash
def test_leaving_that_home_does_not_excuse_a_tree_the_service_can_write(stage):
    home, digest = locked_home(stage)
    reply = install_from(stage, home, digest, WRITABLE="1")
    assert reply.returncode != 0
    assert "服务账号还能写" in reply.stderr
    assert not stage.called("ln -sfn")
    stage.assert_offline()


@needs_bash
def test_a_missing_archive_is_named_from_where_the_operator_stands(stage):
    home, digest = locked_home(stage)
    (home / "corridor-public.tar.gz").unlink()
    reply = install_from(stage, home, digest)
    assert reply.returncode != 0
    assert "找不到归档：corridor-public.tar.gz" in reply.stderr


@needs_bash
def test_a_wrong_digest_extracts_nothing_and_changes_only_the_service(stage):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, wrong_digest(digest))
    assert reply.returncode != 0
    assert "sha256 不符" in reply.stderr
    assert "保持下线" in reply.stderr
    assert {step.split()[0] for step in stage.steps()} == {"id", "sha256sum", "systemctl"}
    assert not list(stage.root.iterdir())
    stage.assert_offline()


@needs_bash
def test_a_wrong_digest_takes_a_running_service_offline(stage):
    archive, digest = archive_in(stage)
    stage.service_running()
    reply = install(stage, archive, wrong_digest(digest))
    assert reply.returncode != 0
    assert "保持下线" in reply.stderr
    stage.assert_offline()


@needs_bash
def test_a_service_that_will_not_stop_is_reported_and_never_called_offline(stage):
    archive, digest = archive_in(stage)
    stage.service_running()
    reply = install(stage, archive, wrong_digest(digest), FAIL_STOP="1")
    assert reply.returncode != 0
    assert "站点可能仍在线" in reply.stderr
    assert "保持下线" not in reply.stderr


@needs_bash
@pytest.mark.parametrize("digest", ["", "abc", "A" * 64, "g" * 64])
def test_a_malformed_digest_is_refused_first(stage, digest):
    archive, _ = archive_in(stage)
    assert install(stage, archive, digest).returncode != 0
    assert stage.steps() == []


@needs_bash
def test_a_scripted_install_needs_no_key_and_ends_behind_the_closed_gate(stage):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, digest)
    assert reply.returncode == 0, reply.stderr
    assert "门禁仍然关闭" in reply.stdout
    assert "open_public.sh" in reply.stdout
    assert "已开放" not in reply.stdout
    env_file = stage.root / "etc/corridor/web-public.env"
    assert env_file.read_bytes() == (DEPLOY / "web-public.env.example").read_bytes()
    assert not (stage.root / "etc/corridor/live/.env.deepseek").exists()
    assert (stage.root / "var/lib/corridor/journal").is_dir()
    release = stage.root / "opt/corridor/releases" / digest[:12]
    assert (release / ".release-ok").read_text(encoding="utf-8").strip() == digest
    assert (stage.root / "etc/caddy/Caddyfile").read_bytes() == (DEPLOY / "Caddyfile").read_bytes()
    assert (stage.root / "etc/caddy" / GATE).read_bytes() == (DEPLOY / GATE).read_bytes()
    order = [
        stage.index(prefix)
        for prefix in (
            "apt-get update",
            "dpkg --force-confold",
            "ufw --force enable",
            "uv python install 3.11",
            "uv sync --frozen --no-dev",
            "check_release capability",
            "caddy validate",
            "systemctl stop corridor-web",
            "systemctl reload caddy",
            "ln -sfn",
            "mv -Tf",
            "systemctl enable corridor-web",
            "systemctl start corridor-web",
            "check_release serving",
            "journalctl",
        )
    ]
    assert order == sorted(order)
    assert stage.steps()[-1].startswith("journalctl")


@needs_bash
def test_the_gate_is_closed_and_proved_before_the_new_release_starts(stage):
    archive, digest = archive_in(stage)
    # The site was open and serving an older release when the install began.
    stage.service_running()
    caddy = stage.root / "etc/caddy"
    caddy.mkdir(parents=True)
    (caddy / GATE).write_text("", encoding="utf-8")
    assert install(stage, archive, digest).returncode == 0
    assert stage.index("systemctl stop corridor-web") < stage.index("systemctl reload caddy")
    assert stage.index("systemctl reload caddy") < stage.probe()
    assert stage.probe() < stage.index("ln -sfn")
    assert stage.probe() < stage.index("systemctl start corridor-web")


@needs_bash
def test_a_gate_that_does_not_answer_503_never_starts_the_new_release(stage):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, digest, BROKEN_GATE="1")
    assert reply.returncode != 0
    assert "门禁没有生效" in reply.stderr
    assert "保持下线" in reply.stderr
    assert not stage.called("ln ")
    assert not stage.called("systemctl start corridor-web")
    stage.assert_offline()


@needs_bash
def test_a_caddy_that_refuses_the_gate_never_starts_the_new_release(stage):
    archive, digest = archive_in(stage)
    stage.service_running()
    reply = install(stage, archive, digest, FAIL_RELOAD="1")
    assert reply.returncode != 0
    assert not stage.called("ln ")
    assert not stage.called("systemctl start corridor-web")
    stage.assert_offline()


@needs_bash
def test_a_second_install_keeps_the_environment_file(stage):
    archive, digest = archive_in(stage)
    assert install(stage, archive, digest).returncode == 0
    env_file = stage.root / "etc/corridor/web-public.env"
    env_file.write_text("CORRIDOR_WEB_PUBLIC_HOST=" + HOST + "\nKEEP=1\n", encoding="utf-8")
    assert install(stage, archive, digest).returncode == 0
    assert "KEEP=1" in env_file.read_text(encoding="utf-8")


@needs_bash
@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"../outside.txt": b"x"}, "绝对路径"),
        ({"/etc/passwd": b"x"}, "绝对路径"),
        ({"README.md": b"x"}, "归档缺少"),
    ],
)
def test_an_unsafe_or_incomplete_archive_never_goes_online(stage, files, message):
    archive, digest = archive_in(stage, files)
    reply = install(stage, archive, digest)
    assert reply.returncode != 0
    assert message in reply.stderr
    assert not stage.called("ln ")
    stage.assert_offline()


@needs_bash
def test_a_link_in_the_archive_is_refused(stage):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    path = stage.base / "corridor-public.tar.gz"
    path.write_bytes(buffer.getvalue())
    reply = install(stage, stage.path(path), hashlib.sha256(buffer.getvalue()).hexdigest())
    assert reply.returncode != 0
    assert "链接或特殊文件" in reply.stderr
    stage.assert_offline()


@needs_bash
def test_a_release_without_the_abilities_is_not_switched_to(stage):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, digest, FAIL_CAPABILITY="1")
    assert reply.returncode != 0
    assert not stage.called("ln ")
    assert not (stage.root / "opt/corridor/releases" / digest[:12] / ".release-ok").exists()
    stage.assert_offline()


@needs_bash
def test_a_caddyfile_the_target_refuses_starts_nothing_and_stays_offline(stage):
    archive, digest = archive_in(stage)
    stage.service_running()
    reply = install(stage, archive, digest, BAD_CADDYFILE="1")
    assert reply.returncode != 0
    assert not stage.called("systemctl reload caddy")
    assert not stage.called("ln ")
    stage.assert_offline()


@needs_bash
@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FAIL_TLS": "1"}, "不降级"),
        ({"FAIL_SERVING": "1"}, "保持下线"),
        ({"LEAK": "1"}, "手机配对路径"),
    ],
)
def test_a_failure_after_the_switch_stays_offline(stage, env, message):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, digest, **env)
    assert reply.returncode != 0
    assert message in reply.stderr
    assert "保持下线" in reply.stderr
    stage.assert_offline()
    assert sum(line.startswith("ln ") for line in stage.steps()) <= 1


@needs_bash
def test_a_firewall_with_another_open_port_stops_the_install(stage):
    archive, digest = archive_in(stage)
    reply = install(stage, archive, digest, EXTRA_PORT="1")
    assert reply.returncode != 0
    assert "8772/tcp" in reply.stderr
    stage.assert_offline()


def installed_release(stage, identifier, *, marker=True):
    release = stage.root / "opt/corridor/releases" / identifier
    (release / ".venv/bin").mkdir(parents=True)
    if marker:
        (release / ".release-ok").write_text("x\n", encoding="utf-8")
    env_file = stage.root / "etc/corridor/web-public.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_bytes((DEPLOY / "web-public.env.example").read_bytes())
    return release


def rollback_scripts(stage):
    directory = stage.base / "scripts"
    directory.mkdir()
    shutil.copy(DEPLOY / "rollback.sh", directory)
    (directory / "check_release.sh").write_text(CHECK_RELEASE, encoding="utf-8", newline="\n")
    return stage.path(directory / "rollback.sh")


@needs_bash
def test_rollback_refuses_a_release_without_the_abilities_and_touches_nothing(stage):
    installed_release(stage, "a" * 12)
    reply = stage.run(rollback_scripts(stage), "a" * 12, FAIL_CAPABILITY="1")
    assert reply.returncode != 0
    assert "不具备公开模式能力" in reply.stderr
    assert not stage.called("systemctl")
    assert not stage.called("ln ")


@needs_bash
def test_rollback_refuses_an_unfinished_release(stage):
    installed_release(stage, "a" * 12, marker=False)
    reply = stage.run(rollback_scripts(stage), "a" * 12)
    assert reply.returncode != 0
    assert "没有构建完成" in reply.stderr
    assert not stage.called("check_release")


@needs_bash
@pytest.mark.parametrize("identifier", ["../x", "abc", "A" * 12])
def test_rollback_refuses_a_malformed_name(stage, identifier):
    assert stage.run(rollback_scripts(stage), identifier).returncode != 0
    assert stage.steps() == ["id -u"]


@needs_bash
def test_rollback_switches_and_checks(stage):
    installed_release(stage, "a" * 12)
    reply = stage.run(rollback_scripts(stage), "a" * 12)
    assert reply.returncode == 0, reply.stderr
    assert stage.index("check_release capability") < stage.index("ln -sfn")
    assert stage.index("mv -Tf") < stage.index("systemctl enable corridor-web")
    assert stage.index("systemctl enable corridor-web") < stage.index(
        "systemctl start corridor-web"
    )
    assert stage.steps()[-1] == "check_release serving"


@needs_bash
def test_a_rollback_that_fails_to_serve_stays_offline(stage):
    installed_release(stage, "a" * 12)
    reply = stage.run(rollback_scripts(stage), "a" * 12, FAIL_SERVING="1")
    assert reply.returncode != 0
    assert "保持下线" in reply.stderr
    stage.assert_offline()
    assert sum(line.startswith("ln ") for line in stage.steps()) == 1


def release_shim(stage, *, body=None):
    """A release whose interpreter is this repository's, as a script the bash side can run."""
    release = stage.base / "release"
    (release / ".venv/bin").mkdir(parents=True)
    source = (ROOT / "src").as_posix()
    interpreter = Path(sys.executable).as_posix()
    body = body or (
        f'PYTHONPATH="{source}" SYSTEMROOT="{os.environ.get("SYSTEMROOT", "")}" '
        f'exec "{interpreter}" "$@"'
    )
    path = release / ".venv/bin/python"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return stage.path(release)


def check_release(stage, *args, **env):
    return stage.run(stage.path(DEPLOY / "check_release.sh"), *args, **env)


@needs_bash
def test_this_tree_has_the_public_abilities(stage):
    reply = check_release(stage, "capability", release_shim(stage))
    assert reply.returncode == 0, reply.stderr


@needs_bash
@pytest.mark.parametrize(
    "body", ["exit 0", "exit 1", 'case "$1" in -m) exit 0 ;; esac'], ids=["old", "broken", "lax"]
)
def test_a_release_that_starts_when_it_should_refuse_lacks_the_abilities(stage, body):
    reply = check_release(stage, "capability", release_shim(stage, body=body))
    assert reply.returncode != 0
    assert "check_release:" in reply.stderr


@needs_bash
def test_a_release_without_an_interpreter_lacks_the_abilities(stage):
    (stage.base / "empty").mkdir()
    reply = check_release(stage, "capability", stage.path(stage.base / "empty"))
    assert reply.returncode != 0


def test_the_capability_probe_passes_on_this_tree():
    spec = importlib.util.spec_from_file_location("deploy_capability", DEPLOY / "capability.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.missing() == []


def test_the_capability_probe_refuses_a_site_that_could_start_or_move_its_ledger(monkeypatch):
    spec = importlib.util.spec_from_file_location("deploy_capability", DEPLOY / "capability.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr("corridor.web.public.JOURNAL_PATH", Path("/var/lib/corridor/other"))
    monkeypatch.setattr(SpendLedger, "__init__", lambda self, directory: None)
    assert module.missing() == ["fixed journal directory", "no ledger started by the site"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def public_service(tmp_path):
    """The real entry point, started as the template starts it: no key, no verifier."""
    port = free_port()
    values, _ = entry_arguments()
    words = [
        sys.executable,
        "-m",
        "corridor.web",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        *[
            item
            for name, value in values.items()
            if name not in ("--host", "--port")
            for item in (name, value)
        ],
    ]
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("CORRIDOR_", "LIVE_", "LLM_"))
    }
    env.update(environment())
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        words, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        yield port
    finally:
        process.terminate()
        process.wait(timeout=30)


@needs_bash
@pytest.mark.skipif(shutil.which("curl") is None, reason="no curl")
def test_the_template_serves_in_public_mode_without_any_secret(tmp_path, public_service):
    base = tmp_path / "plain"
    base.mkdir()
    stage = Stage(base)
    for name in ("curl", "sleep", "id"):
        (stage.bin / name).unlink()
    reply = check_release(
        stage,
        "serving",
        HOST,
        CORRIDOR_PORT=str(public_service),
        CORRIDOR_PROBE_SECONDS="30",
    )
    assert reply.returncode == 0, reply.stderr


@needs_bash
@pytest.mark.skipif(shutil.which("curl") is None, reason="no curl")
def test_the_serving_check_fails_when_nothing_listens(stage):
    (stage.bin / "curl").unlink()
    reply = check_release(
        stage, "serving", HOST, CORRIDOR_PORT=str(free_port()), CORRIDOR_PROBE_SECONDS="1"
    )
    assert reply.returncode != 0


def enable_stage(stage, *, key=True):
    env_file = stage.root / "etc/corridor/web-public.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_bytes((DEPLOY / "web-public.env.example").read_bytes())
    live = stage.root / "etc/corridor/live"
    live.mkdir()
    if key:
        (live / ".env.deepseek").write_text("LLM_API_KEY=sk-test-only-key\n", encoding="utf-8")
    current = stage.root / "opt/corridor/current"
    (current / ".venv/bin").mkdir(parents=True)
    source = (ROOT / "src").as_posix()
    shim = current / ".venv/bin/python"
    shim.write_text(
        f'#!/bin/sh\nPYTHONPATH="{source}" SYSTEMROOT="{os.environ.get("SYSTEMROOT", "")}" '
        f'exec "{Path(sys.executable).as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    shim.chmod(0o755)
    directory = stage.base / "scripts"
    directory.mkdir()
    shutil.copy(DEPLOY / "enable_live.sh", directory)
    (directory / "check_release.sh").write_text(CHECK_RELEASE, encoding="utf-8", newline="\n")
    return env_file, stage.path(directory / "enable_live.sh")


@needs_bash
def test_enable_live_needs_the_key_file_first(stage):
    env_file, script = enable_stage(stage, key=False)
    before = env_file.read_bytes()
    reply = stage.run(script)
    assert reply.returncode != 0
    assert ".env.deepseek" in reply.stderr
    assert env_file.read_bytes() == before
    assert not stage.called("systemctl")


@needs_bash
def test_enable_live_writes_the_pair_and_shows_the_code_once(stage):
    env_file, script = enable_stage(stage)
    first = stage.run(script)
    assert first.returncode == 0, first.stderr
    text = env_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines.count("CORRIDOR_LIVE=1") == 1
    assert "CORRIDOR_LIVE=0" not in lines
    code = first.stderr.strip().splitlines()[-1]
    assert code not in text
    assert code not in first.stdout
    assert "sk-test-only-key" not in first.stdout + first.stderr + text
    pairs = dict(line.split("=", 1) for line in lines if not line.startswith("#"))
    config = PublicConfig.from_args(entry_arguments()[1], pairs)
    assert config.kdf.verifier(code) == pairs["LIVE_ACCESS_CODE"]
    assert stage.index("systemctl restart corridor-web") < stage.index("check_release serving")
    second = stage.run(script)
    assert second.returncode == 0, second.stderr
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("LIVE_ACCESS_CODE=") for line in lines) == 1
    assert sum(line.startswith("CORRIDOR_WEB_ACCESS_KDF=") for line in lines) == 1
    assert sum(line.startswith("CORRIDOR_LIVE=") for line in lines) == 1


@needs_bash
def test_enable_live_stays_offline_when_the_service_does_not_serve(stage):
    _, script = enable_stage(stage)
    reply = stage.run(script, FAIL_SERVING="1")
    assert reply.returncode != 0
    assert "保持下线" in reply.stderr
    stage.assert_offline()


@needs_bash
def test_enable_live_stays_offline_when_the_restart_itself_fails(stage):
    env_file, script = enable_stage(stage)
    stage.service_running()
    reply = stage.run(script, FAIL_RESTART="1")
    assert reply.returncode != 0
    assert "保持下线" in reply.stderr
    assert not stage.called("check_release serving")
    stage.assert_offline()
    # The switch is already written: the process would start with it, so it may not run.
    assert "CORRIDOR_LIVE=1" in env_file.read_text(encoding="utf-8").splitlines()


def ledger_paths(stage):
    return stage.root / "var/lib/corridor/journal", stage.root / "etc/corridor/ledger-initialized"


@needs_bash
def test_enable_live_starts_the_ledger_once_and_leaves_the_mark(stage):
    env_file, script = enable_stage(stage)
    journal, marker = ledger_paths(stage)
    assert not journal.exists() and not marker.exists()
    assert stage.run(script).returncode == 0
    assert marker.exists()
    ledger = SpendLedger(journal, create=False)
    assert ledger.remaining("CNY", BUDGET_CNY) == BUDGET_CNY
    settle = ledger.reserve("CNY", "deepseek-flash", 1, 100)
    settle(1)
    reply = stage.run(script)
    assert reply.returncode == 0, reply.stderr
    assert ledger.remaining("CNY", BUDGET_CNY) == 99


@needs_bash
def test_enable_live_refuses_a_lost_ledger_and_changes_nothing(stage):
    env_file, script = enable_stage(stage)
    journal, _ = ledger_paths(stage)
    assert stage.run(script).returncode == 0
    (journal / SpendLedger.FILE).unlink()
    before = env_file.read_bytes()
    steps = len(stage.steps())
    reply = stage.run(script)
    assert reply.returncode != 0
    assert "台账丢失" in reply.stderr
    assert env_file.read_bytes() == before
    assert not (journal / SpendLedger.FILE).exists()
    assert not [step for step in stage.steps()[steps:] if step.startswith("systemctl")]


@needs_bash
def test_enable_live_refuses_a_ruined_ledger_and_keeps_it_for_the_reconciliation(stage):
    env_file, script = enable_stage(stage)
    journal, _ = ledger_paths(stage)
    assert stage.run(script).returncode == 0
    (journal / SpendLedger.FILE).write_bytes(b"not a database, not at all, not even close")
    before = env_file.read_bytes()
    reply = stage.run(script)
    assert reply.returncode != 0
    assert "台账无法读取" in reply.stderr
    assert env_file.read_bytes() == before
    assert (journal / SpendLedger.FILE).read_bytes().startswith(b"not a database")


@needs_bash
def test_a_ledger_restored_after_the_reconciliation_opens_again(stage):
    env_file, script = enable_stage(stage)
    journal, _ = ledger_paths(stage)
    assert stage.run(script).returncode == 0
    (journal / SpendLedger.FILE).unlink()
    assert (
        ledger_setup.main([
            "--journal-dir",
            str(journal),
            "--spent-cny",
            "37.5",
        ])
        == 0
    )
    reply = stage.run(script)
    assert reply.returncode == 0, reply.stderr
    assert SpendLedger(journal, create=False).remaining("CNY", BUDGET_CNY) == 62.5


def open_scripts(stage):
    directory = stage.base / "scripts"
    directory.mkdir()
    shutil.copy(DEPLOY / "open_public.sh", directory)
    (directory / "check_release.sh").write_text(CHECK_RELEASE, encoding="utf-8", newline="\n")
    env_file = stage.root / "etc/corridor/web-public.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_bytes((DEPLOY / "web-public.env.example").read_bytes())
    gate = stage.root / "etc/caddy" / GATE
    gate.parent.mkdir(parents=True)
    gate.write_bytes((DEPLOY / GATE).read_bytes())
    stage.service_running()
    return gate, stage.path(directory / "open_public.sh")


@needs_bash
def test_open_public_clears_the_gate_and_proves_the_site_answers(stage):
    gate, script = open_scripts(stage)
    reply = stage.run(script)
    assert reply.returncode == 0, reply.stderr
    assert "已开放" in reply.stdout
    assert gate.read_bytes() == b""
    assert stage.index("check_release serving") < stage.index("systemctl reload caddy")
    assert stage.index("systemctl reload caddy") < stage.probe()
    assert not stage.called("systemctl stop")


@needs_bash
def test_open_public_refuses_before_an_install(stage):
    gate, script = open_scripts(stage)
    gate.unlink()
    reply = stage.run(script)
    assert reply.returncode != 0
    assert "先运行 install.sh" in reply.stderr
    assert not stage.called("systemctl")


@needs_bash
@pytest.mark.parametrize("env", [{"FAIL_SERVING": "1"}, {"FAIL_RELOAD": "1"}, {"FAIL_OPEN": "1"}])
def test_a_failed_opening_stays_offline(stage, env):
    _, script = open_scripts(stage)
    reply = stage.run(script, **env)
    assert reply.returncode != 0
    assert "保持下线" in reply.stderr
    stage.assert_offline()


@needs_bash
def test_a_serving_check_that_fails_before_opening_keeps_the_gate(stage):
    gate, script = open_scripts(stage)
    assert stage.run(script, FAIL_SERVING="1").returncode != 0
    assert gate.read_bytes() == (DEPLOY / GATE).read_bytes()
    assert not stage.called("systemctl reload caddy")


# --- the ledger tool ------------------------------------------------------------------------


def test_the_ledger_tool_starts_a_ledger_once_and_carries_the_reconciled_total(tmp_path):
    journal = tmp_path / "journal"
    assert ledger_setup.main(["--journal-dir", str(journal), "--check"]) == 1
    assert not (journal / SpendLedger.FILE).exists()
    assert ledger_setup.main(["--journal-dir", str(journal), "--spent-cny", "37.5"]) == 0
    assert ledger_setup.main(["--journal-dir", str(journal), "--check"]) == 0
    ledger = SpendLedger(journal, create=False)
    assert ledger.remaining("CNY", BUDGET_CNY) == 62.5
    # An existing file is never replaced, whatever the amount.
    assert ledger_setup.main(["--journal-dir", str(journal), "--spent-cny", "0"]) == 1
    assert ledger.remaining("CNY", BUDGET_CNY) == 62.5


@pytest.mark.parametrize("value", ["", "-1", "1e3", "1/3", "abc", "1.1234567", "١"])
def test_the_ledger_tool_refuses_an_amount_that_is_not_plain_yuan(tmp_path, value):
    journal = tmp_path / "journal"
    with pytest.raises(SystemExit) as refused:
        ledger_setup.main(["--journal-dir", str(journal), "--spent-cny", value])
    assert refused.value.code == 2
    assert not journal.exists()


def test_the_ledger_tool_needs_an_explicit_action(tmp_path):
    with pytest.raises(SystemExit) as refused:
        ledger_setup.main(["--journal-dir", str(tmp_path / "journal")])
    assert refused.value.code == 2
