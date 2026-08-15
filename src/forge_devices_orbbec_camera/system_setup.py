"""Read-only device preflight and explicit privileged udev initialization."""

from __future__ import annotations

import ctypes.util
import grp
import hashlib
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

_RULE_NAME = "99-obsensor-libusb.rules"
_RULE_DESTINATION = Path("/etc/udev/rules.d") / _RULE_NAME
_VIDEO_GROUP = "video"
_ORBBEC_VENDOR_ID = "2bc5"
_RULE_SEARCH_PATHS = (
    Path("/etc/udev/rules.d"),
    Path("/run/udev/rules.d"),
    Path("/usr/lib/udev/rules.d"),
    Path("/lib/udev/rules.d"),
)
_PKEXEC_CANDIDATES = (Path("/usr/bin/pkexec"), Path("/bin/pkexec"))
_USERMOD_CANDIDATES = (Path("/usr/sbin/usermod"), Path("/sbin/usermod"))
_UDEVADM_CANDIDATES = (Path("/usr/bin/udevadm"), Path("/bin/udevadm"))
_SOURCE_INSTALL_COMMAND = "sudo bash scripts/install_permissions.sh"


@dataclass(frozen=True, slots=True)
class DeviceSetupStatus:
    platform_supported: bool
    libusb_found: bool
    rule_installed: bool
    rule_matches: bool
    rule_secure: bool
    rule_security_issue: str | None
    rule_expected_sha256: str
    rule_installed_sha256: str | None
    user: str
    running_as_root: bool
    video_group_exists: bool
    user_in_video_group: bool
    video_group_active: bool
    device_nodes: tuple[str, ...]
    accessible_device_nodes: tuple[str, ...]
    conflicting_rule_files: tuple[str, ...]
    unreadable_rule_files: tuple[str, ...]

    @property
    def inaccessible_device_nodes(self) -> tuple[str, ...]:
        accessible = set(self.accessible_device_nodes)
        return tuple(path for path in self.device_nodes if path not in accessible)

    @property
    def runtime_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not self.platform_supported:
            issues.append("仅支持 Linux")
        if not self.libusb_found:
            issues.append("未找到 libusb-1.0 运行时")
        if not self.rule_installed:
            issues.append(f"未安装 {_RULE_DESTINATION}")
        elif not self.rule_matches:
            issues.append(f"已安装的 {_RULE_DESTINATION} 与当前二进制不一致")
        if self.rule_installed and not self.rule_secure:
            detail = f": {self.rule_security_issue}" if self.rule_security_issue else ""
            issues.append(f"已安装的 udev 规则路径或权限不安全{detail}")
        if self.conflicting_rule_files:
            issues.append("检测到其他引用 Orbbec vendor 2bc5 的 udev 规则")
        if not self.running_as_root:
            if not self.video_group_exists:
                issues.append("系统不存在 video 用户组")
            elif not self.user_in_video_group:
                issues.append(f"用户 {self.user} 尚未加入 video 组")
            elif not self.video_group_active and not self.accessible_device_nodes:
                issues.append("video 用户组尚未在当前会话生效")
        if self.inaccessible_device_nodes:
            issues.append("当前进程不能读写 Orbbec USB 节点")
        return tuple(issues)



def _bundled_rule() -> resources.abc.Traversable:
    return resources.files("forge_devices_orbbec_camera").joinpath("resources", _RULE_NAME)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _effective_rule_lines(data: bytes) -> tuple[str, ...] | None:
    """Return active udev directives, ignoring comments and blank lines."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return tuple(
        line
        for raw_line in text.splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError):
        return ""


def _orbbec_device_nodes(
    *,
    sys_usb_root: Path = Path("/sys/bus/usb/devices"),
    dev_usb_root: Path = Path("/dev/bus/usb"),
) -> tuple[Path, ...]:
    nodes: set[Path] = set()
    try:
        candidates = tuple(sys_usb_root.iterdir())
    except OSError:
        return ()
    for candidate in candidates:
        if _read_text(candidate / "idVendor") != _ORBBEC_VENDOR_ID:
            continue
        bus_text = _read_text(candidate / "busnum")
        device_text = _read_text(candidate / "devnum")
        try:
            bus_number = int(bus_text)
            device_number = int(device_text)
        except ValueError:
            continue
        nodes.add(dev_usb_root / f"{bus_number:03d}" / f"{device_number:03d}")
    return tuple(sorted(nodes))


def _group_status(uid: int) -> tuple[str, bool, bool, bool]:
    user = pwd.getpwuid(uid)
    if uid == 0:
        return user.pw_name, True, True, True
    try:
        video_group = grp.getgrnam(_VIDEO_GROUP)
    except KeyError:
        return user.pw_name, False, False, False
    configured = user.pw_gid == video_group.gr_gid or user.pw_name in video_group.gr_mem
    active = video_group.gr_gid == os.getgid() or video_group.gr_gid in os.getgroups()
    return user.pw_name, True, configured, active


def _scan_rule_files(
    rule_destination: Path,
    rule_search_paths: tuple[Path, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    conflicts: set[str] = set()
    unreadable: set[str] = set()
    seen_candidates: set[Path] = set()
    destination = rule_destination.resolve(strict=False)
    for directory in rule_search_paths:
        try:
            candidates = tuple(
                candidate for candidate in directory.iterdir() if candidate.suffix == ".rules"
            )
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.add(str(directory))
            continue
        for candidate in candidates:
            resolved_candidate = candidate.resolve(strict=False)
            if resolved_candidate == destination or resolved_candidate in seen_candidates:
                continue
            seen_candidates.add(resolved_candidate)
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                unreadable.add(str(candidate))
                continue
            if _ORBBEC_VENDOR_ID in content:
                conflicts.add(str(candidate))
    return tuple(sorted(conflicts)), tuple(sorted(unreadable))


def _conflicting_rules(
    rule_destination: Path,
    rule_search_paths: tuple[Path, ...],
) -> tuple[str, ...]:
    conflicts, _ = _scan_rule_files(rule_destination, rule_search_paths)
    return conflicts


def _rule_security_issue(destination: Path) -> str | None:
    try:
        _validate_rule_destination(destination)
    except RuntimeError as exc:
        return str(exc)
    return None


def inspect_device_setup(
    *,
    rule_destination: Path = _RULE_DESTINATION,
    sys_usb_root: Path = Path("/sys/bus/usb/devices"),
    dev_usb_root: Path = Path("/dev/bus/usb"),
    rule_search_paths: tuple[Path, ...] = _RULE_SEARCH_PATHS,
) -> DeviceSetupStatus:
    expected_rule = _bundled_rule().read_bytes()
    expected_sha256 = _sha256(expected_rule)
    try:
        installed_rule = rule_destination.read_bytes()
    except OSError:
        installed_rule = None
    security_issue = _rule_security_issue(rule_destination)
    conflicts, unreadable = _scan_rule_files(rule_destination, rule_search_paths)
    nodes = _orbbec_device_nodes(sys_usb_root=sys_usb_root, dev_usb_root=dev_usb_root)
    accessible = tuple(
        path for path in nodes if os.access(path, os.R_OK | os.W_OK, effective_ids=True)
    )
    user, group_exists, configured, active = _group_status(os.getuid())
    return DeviceSetupStatus(
        platform_supported=sys.platform.startswith("linux"),
        libusb_found=ctypes.util.find_library("usb-1.0") is not None,
        rule_installed=installed_rule is not None,
        rule_matches=(
            installed_rule is not None
            and _effective_rule_lines(installed_rule) == _effective_rule_lines(expected_rule)
        ),
        rule_secure=installed_rule is not None and security_issue is None,
        rule_security_issue=security_issue,
        rule_expected_sha256=expected_sha256,
        rule_installed_sha256=None if installed_rule is None else _sha256(installed_rule),
        user=user,
        running_as_root=os.geteuid() == 0,
        video_group_exists=group_exists,
        user_in_video_group=configured,
        video_group_active=active,
        device_nodes=tuple(str(path) for path in nodes),
        accessible_device_nodes=tuple(str(path) for path in accessible),
        conflicting_rule_files=conflicts,
        unreadable_rule_files=unreadable,
    )


def _print_status(status: DeviceSetupStatus) -> None:
    checks = (
        ("platform", status.platform_supported, sys.platform),
        ("libusb", status.libusb_found, "libusb-1.0"),
        (
            "udev rule",
            status.rule_matches,
            str(_RULE_DESTINATION) if status.rule_installed else "未安装",
        ),
        (
            "udev rule security",
            status.rule_secure,
            status.rule_security_issue or "root-owned, mode-safe, no symlink",
        ),
        ("udev conflicts", not status.conflicting_rule_files, "无冲突规则"),
        ("video group", status.running_as_root or status.video_group_exists, _VIDEO_GROUP),
        ("user configured", status.running_as_root or status.user_in_video_group, status.user),
        ("group active", status.running_as_root or status.video_group_active, "重新登录后生效"),
    )
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    for path in status.conflicting_rule_files:
        print(f"[FAIL] conflicting udev rule: {path}")
    for path in status.unreadable_rule_files:
        print(f"[WARN] unreadable udev rule (root helper will re-check): {path}")
    if not status.device_nodes:
        print("[WARN] device: 未检测到 Orbbec USB 设备")
    else:
        accessible = set(status.accessible_device_nodes)
        for path in status.device_nodes:
            print(f"[{'OK' if path in accessible else 'FAIL'}] device access: {path}")
    for issue in status.runtime_issues:
        print(f"[ERROR] {issue}")


def _print_actions(status: DeviceSetupStatus) -> None:
    if not status.libusb_found:
        print(
            "[ACTION] 安装 libusb 运行时，例如 Debian/Ubuntu: "
            "sudo apt install libusb-1.0-0；Fedora: sudo dnf install libusb1。"
        )
    if status.rule_installed and not status.rule_secure:
        print("[ACTION] 请由管理员修复或移除不安全的目标 udev 规则，再重新运行 init-device。")
    elif status.conflicting_rule_files:
        print("[ACTION] 请由管理员检查以上冲突/不可读规则；init-device 不会自动删除它们。")
    elif not status.rule_matches or (
        not status.running_as_root and not status.user_in_video_group
    ):
        print("[ACTION] 运行: orbbec-camera init-device")
    if status.user_in_video_group and not status.video_group_active:
        print("[ACTION] 用户组已配置；请注销并重新登录，或重启系统。")
    if status.inaccessible_device_nodes:
        print("[ACTION] 重新插拔设备，并确认没有其他进程独占相机。")




def runtime_preflight() -> bool:
    status = inspect_device_setup()
    if not status.runtime_issues:
        return True
    _print_status(status)
    _print_actions(status)
    print("设备运行环境未就绪；请按上述 ACTION 处理。", file=sys.stderr)
    return False


def _system_subprocess_env() -> dict[str, str]:
    """Remove PyInstaller/user loader overrides before starting system tools."""
    environment = os.environ.copy()
    original_library_path = environment.pop("LD_LIBRARY_PATH_ORIG", None)
    if original_library_path:
        environment["LD_LIBRARY_PATH"] = original_library_path
    else:
        environment.pop("LD_LIBRARY_PATH", None)
    environment.pop("LD_PRELOAD", None)
    environment.pop("LD_AUDIT", None)
    return environment


def _caller_uid_from_pkexec() -> int:
    raw = os.environ.get("PKEXEC_UID")
    if raw is None:
        raise RuntimeError("缺少 PKEXEC_UID；privileged helper 只能由 pkexec 调用")
    try:
        uid = int(raw, 10)
    except ValueError as exc:
        raise RuntimeError("PKEXEC_UID 非法") from exc
    if uid <= 0:
        raise RuntimeError("PKEXEC_UID 必须是普通用户 UID")
    try:
        pwd.getpwuid(uid)
    except KeyError as exc:
        raise RuntimeError(f"PKEXEC_UID 对应的用户不存在: {uid}") from exc
    return uid


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _validate_secure_path(
    path: Path,
    *,
    expected_type: Literal["file", "directory"],
    require_executable: bool = False,
) -> Path:
    normalized = _absolute_without_symlink_resolution(path)
    try:
        metadata = os.lstat(normalized)
    except OSError as exc:
        raise RuntimeError(f"安全路径不存在或不可访问: {normalized}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"安全路径不得是符号链接: {normalized}")
    if expected_type == "file" and not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"安全路径不是普通文件: {normalized}")
    if expected_type == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"安全路径不是目录: {normalized}")
    if metadata.st_uid != 0:
        raise RuntimeError(f"安全路径必须属于 root: {normalized}")
    if metadata.st_mode & 0o022:
        raise RuntimeError(f"安全路径不得允许 group/world 写入: {normalized}")
    if require_executable and not metadata.st_mode & 0o111:
        raise RuntimeError(f"安全路径不可执行: {normalized}")
    return normalized


def _validate_trusted_executable(path: Path) -> Path:
    executable = _validate_secure_path(
        path,
        expected_type="file",
        require_executable=True,
    )
    parent = executable.parent
    while True:
        _validate_secure_path(parent, expected_type="directory")
        if parent == parent.parent:
            break
        parent = parent.parent
    return executable


def _trusted_system_executable(name: str, candidates: tuple[Path, ...]) -> Path:
    errors: list[str] = []
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            return _validate_trusted_executable(candidate)
        except RuntimeError as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError(f"未找到可信的 {name}: {'; '.join(errors)}")
    raise RuntimeError(f"未找到系统命令 {name}")


def _trusted_frozen_executable() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "源码/editable 模式禁止自动提权；请显式运行: " + _SOURCE_INSTALL_COMMAND
        )
    return _validate_trusted_executable(Path(sys.executable))


def _validate_rule_destination(destination: Path = _RULE_DESTINATION) -> None:
    normalized = _absolute_without_symlink_resolution(destination)
    parent = normalized.parent
    chain: list[Path] = []
    current = parent
    while True:
        chain.append(current)
        if current == current.parent:
            break
        current = current.parent
    for directory in reversed(chain):
        _validate_secure_path(directory, expected_type="directory")
    try:
        metadata = os.lstat(normalized)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"无法检查目标 udev 规则: {normalized}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"目标 udev 规则不得是符号链接: {normalized}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"目标 udev 规则不是普通文件: {normalized}")
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError(f"目标 udev 规则必须由 root 拥有且不可被 group/world 写入: {normalized}")


class RuleInstallError(RuntimeError):
    def __init__(self, message: str, *, rule_replaced: bool) -> None:
        super().__init__(message)
        self.rule_replaced = rule_replaced


def _install_rule(rule_data: bytes) -> None:
    _validate_rule_destination()
    parent = _RULE_DESTINATION.parent
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{_RULE_NAME}.",
        dir=parent,
    )
    temporary_path = Path(temporary_name)
    rule_replaced = False
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(rule_data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.chown(temporary_path, 0, 0)
        os.replace(temporary_path, _RULE_DESTINATION)
        rule_replaced = True
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise RuleInstallError(
            f"安装 udev 规则失败: {exc}",
            rule_replaced=rule_replaced,
        ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def _resolve_caller(uid: int) -> tuple[pwd.struct_passwd, grp.struct_group]:
    try:
        user = pwd.getpwuid(uid)
    except KeyError as exc:
        raise RuntimeError(f"调用用户不存在: {uid}") from exc
    try:
        video_group = grp.getgrnam(_VIDEO_GROUP)
    except KeyError as exc:
        raise RuntimeError("系统不存在 video 用户组") from exc
    return user, video_group


def _ensure_video_group_member(
    user: pwd.struct_passwd,
    video_group: grp.struct_group,
    usermod: Path,
) -> bool:
    if user.pw_gid == video_group.gr_gid or user.pw_name in video_group.gr_mem:
        return False
    subprocess.run(
        [str(usermod), "-aG", _VIDEO_GROUP, user.pw_name],
        check=True,
        env=_system_subprocess_env(),
    )
    return True


def _rule_needs_install(rule_data: bytes) -> bool:
    try:
        return _RULE_DESTINATION.read_bytes() != rule_data
    except OSError:
        return True


def _run_privileged_init() -> int:
    if os.geteuid() != 0:
        print("init-device privileged helper 必须由 pkexec 以 root 运行。", file=sys.stderr)
        return 1

    completed_steps: list[str] = []
    try:
        _trusted_frozen_executable()
        caller_uid = _caller_uid_from_pkexec()
        user, video_group = _resolve_caller(caller_uid)
        _validate_rule_destination()
        conflicts, unreadable = _scan_rule_files(_RULE_DESTINATION, _RULE_SEARCH_PATHS)
        if conflicts:
            raise RuntimeError("检测到冲突 udev 规则: " + ", ".join(conflicts))
        if unreadable:
            raise RuntimeError("无法安全检查 udev 规则: " + ", ".join(unreadable))
        rule_data = _bundled_rule().read_bytes()
        needs_rule = _rule_needs_install(rule_data)
        needs_membership = not (
            user.pw_gid == video_group.gr_gid or user.pw_name in video_group.gr_mem
        )
        usermod = (
            _trusted_system_executable("usermod", _USERMOD_CANDIDATES)
            if needs_membership
            else None
        )
        udevadm = (
            _trusted_system_executable("udevadm", _UDEVADM_CANDIDATES)
            if needs_rule
            else None
        )

        if needs_rule:
            try:
                _install_rule(rule_data)
            except RuleInstallError as exc:
                if exc.rule_replaced:
                    completed_steps.append(f"已替换 {_RULE_DESTINATION}，但目录 fsync 失败")
                raise
            completed_steps.append(f"已安装 {_RULE_DESTINATION}")

        membership_changed = False
        if needs_membership:
            assert usermod is not None
            membership_changed = _ensure_video_group_member(user, video_group, usermod)
            if membership_changed:
                completed_steps.append(f"已将 {user.pw_name} 加入 {_VIDEO_GROUP} 组")

        if needs_rule:
            assert udevadm is not None
            subprocess.run(
                [str(udevadm), "control", "--reload-rules"],
                check=True,
                env=_system_subprocess_env(),
            )
            completed_steps.append("已重新加载 udev 规则")
            subprocess.run(
                [str(udevadm), "trigger"],
                check=True,
                env=_system_subprocess_env(),
            )
            completed_steps.append("已触发 udev 设备更新")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"init-device 失败: {exc}", file=sys.stderr)
        if completed_steps:
            print("部分修改已经完成：", file=sys.stderr)
            for step in completed_steps:
                print(f"  - {step}", file=sys.stderr)
            print("请根据以上错误处理；必要时由管理员重新加载 udev 规则。", file=sys.stderr)
        return 1

    for step in completed_steps:
        print(step)
    if not completed_steps:
        print("Orbbec 系统配置已就绪，无需修改。")
        return 0
    if not membership_changed:
        print(f"用户 {user.pw_name} 已属于 {_VIDEO_GROUP} 组。")
    if needs_rule:
        print("请重新插拔 Orbbec 以应用新的 udev 规则。")
    if membership_changed:
        print("请注销后重新登录以刷新 video 用户组。")
    return 0


def _pkexec_command() -> list[str]:
    executable = _trusted_frozen_executable()
    pkexec = _trusted_system_executable("pkexec", _PKEXEC_CANDIDATES)
    return [str(pkexec), str(executable), "init-device", "--privileged"]


def run_init_device(*, privileged: bool = False) -> int:
    if privileged:
        return _run_privileged_init()
    if not sys.platform.startswith("linux"):
        print("init-device 仅支持 Linux。", file=sys.stderr)
        return 1
    if os.geteuid() == 0:
        print(
            "请不要直接以 root 运行 init-device；请由需要使用相机的普通用户运行。",
            file=sys.stderr,
        )
        return 1

    status = inspect_device_setup()
    if not status.libusb_found:
        _print_status(status)
        _print_actions(status)
        return 1
    if status.rule_installed and not status.rule_secure:
        _print_status(status)
        print("请由管理员先修复或移除不安全的目标 udev 规则。", file=sys.stderr)
        return 1
    if status.conflicting_rule_files:
        _print_status(status)
        print("请由管理员先处理冲突 udev 规则；init-device 不会自动删除它们。", file=sys.stderr)
        return 1

    needs_rule = not status.rule_matches or not status.rule_secure
    needs_group = not status.user_in_video_group
    if not needs_rule and not needs_group:
        if status.runtime_issues:
            _print_status(status)
            _print_actions(status)
            return 1
        print("Orbbec 设备环境已就绪，无需提权。")
        return 0

    print("init-device 将执行以下固定系统修改：")
    if needs_rule:
        print(f"  - 安装 {_RULE_DESTINATION}")
    if needs_group:
        print(f"  - 将用户 {status.user} 加入 {_VIDEO_GROUP} 组")
    print("即将通过 PolicyKit 请求管理员授权。")
    try:
        command = _pkexec_command()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return subprocess.run(
        command,
        check=False,
        env=_system_subprocess_env(),
    ).returncode
