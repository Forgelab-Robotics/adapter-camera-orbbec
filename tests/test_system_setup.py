from __future__ import annotations

import os
import pwd
import grp
import stat
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from forge_devices_orbbec_camera import system_setup
from forge_devices_orbbec_camera.system_setup import DeviceSetupStatus


def _status(**overrides: object) -> DeviceSetupStatus:
    values: dict[str, object] = {
        "platform_supported": True,
        "libusb_found": True,
        "rule_installed": True,
        "rule_matches": True,
        "rule_secure": True,
        "rule_security_issue": None,
        "rule_expected_sha256": "expected",
        "rule_installed_sha256": "expected",
        "user": "operator",
        "running_as_root": False,
        "video_group_exists": True,
        "user_in_video_group": True,
        "video_group_active": True,
        "device_nodes": ("/dev/bus/usb/001/002",),
        "accessible_device_nodes": ("/dev/bus/usb/001/002",),
        "conflicting_rule_files": (),
        "unreadable_rule_files": (),
    }
    values.update(overrides)
    return DeviceSetupStatus(**values)  # type: ignore[arg-type]


def _passwd(name: str = "operator", uid: int = 1000, gid: int = 1000) -> pwd.struct_passwd:
    return pwd.struct_passwd((name, "x", uid, gid, "", f"/home/{name}", "/bin/sh"))


def _group(members: list[str] | None = None, gid: int = 44) -> grp.struct_group:
    return grp.struct_group(("video", "x", gid, [] if members is None else members))


def _stat(mode: int, *, uid: int = 0) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


def test_inspect_matches_bundled_rule_and_discovers_accessible_orbbec_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = system_setup._bundled_rule().read_bytes()
    installed_rule = tmp_path / "rule"
    installed_rule.write_bytes(
        b"\n".join(
            line for line in rule.splitlines() if not line.lstrip().startswith(b"#")
        )
    )
    sys_root = tmp_path / "sys"
    device = sys_root / "1-2"
    device.mkdir(parents=True)
    (device / "idVendor").write_text("2bc5\n", encoding="ascii")
    (device / "busnum").write_text("1\n", encoding="ascii")
    (device / "devnum").write_text("2\n", encoding="ascii")
    dev_root = tmp_path / "dev"
    node = dev_root / "001" / "002"
    node.parent.mkdir(parents=True)
    node.touch()

    monkeypatch.setattr(system_setup.ctypes.util, "find_library", lambda _: "libusb.so")
    monkeypatch.setattr(system_setup, "_group_status", lambda _: ("operator", True, True, True))
    monkeypatch.setattr(system_setup.os, "access", lambda path, mode, **kwargs: path == node)
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(system_setup, "_rule_security_issue", lambda path: None)

    status = system_setup.inspect_device_setup(
        rule_destination=installed_rule,
        sys_usb_root=sys_root,
        dev_usb_root=dev_root,
        rule_search_paths=(),
    )

    assert status.rule_matches
    assert status.device_nodes == (str(node),)
    assert status.accessible_device_nodes == (str(node),)
    assert status.runtime_issues == ()


def test_runtime_issues_report_stale_rule_and_inaccessible_device() -> None:
    status = _status(
        rule_matches=False,
        rule_installed_sha256="stale",
        accessible_device_nodes=(),
    )
    assert any("不一致" in issue for issue in status.runtime_issues)
    assert any("不能读写" in issue for issue in status.runtime_issues)


def test_headless_readiness_still_requires_video_membership() -> None:
    status = _status(
        user_in_video_group=False,
        video_group_active=False,
        device_nodes=(),
        accessible_device_nodes=(),
    )
    assert any("尚未加入 video" in issue for issue in status.runtime_issues)


def test_root_readiness_does_not_require_runtime_user_group() -> None:
    status = _status(
        running_as_root=True,
        video_group_exists=False,
        user_in_video_group=False,
        video_group_active=False,
        device_nodes=(),
        accessible_device_nodes=(),
    )
    assert status.runtime_issues == ()


def test_rule_search_paths_cover_non_usr_merged_linux() -> None:
    assert Path("/lib/udev/rules.d") in system_setup._RULE_SEARCH_PATHS


def test_conflicting_rule_detection_excludes_canonical_rule(tmp_path: Path) -> None:
    destination = tmp_path / "etc" / system_setup._RULE_NAME
    destination.parent.mkdir()
    destination.write_text('ATTR{idVendor}=="2bc5"\n', encoding="ascii")
    vendor_dir = tmp_path / "usr"
    vendor_dir.mkdir()
    conflict = vendor_dir / "60-orbbec.rules"
    conflict.write_text('ATTR{idVendor}=="2BC5"\n', encoding="ascii")
    irrelevant = vendor_dir / "70-other.rules"
    irrelevant.write_text('ATTR{idVendor}=="1234"\n', encoding="ascii")

    assert system_setup._conflicting_rules(destination, (destination.parent, vendor_dir)) == (
        str(conflict),
    )


def test_rule_scan_reports_unreadable_rule_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "99-canonical.rules"
    destination.write_text("canonical", encoding="ascii")
    candidate = tmp_path / "60-unknown.rules"
    candidate.write_text("unknown", encoding="ascii")
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == candidate:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert system_setup._scan_rule_files(destination, (tmp_path,)) == (
        (),
        (str(candidate),),
    )


def test_runtime_preflight_gives_libusb_specific_guidance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(system_setup, "inspect_device_setup", lambda: _status(libusb_found=False))
    run = mock.Mock()
    monkeypatch.setattr(system_setup.subprocess, "run", run)

    assert not system_setup.runtime_preflight()

    output = capsys.readouterr()
    assert "apt install libusb-1.0-0" in output.out
    run.assert_not_called()


def test_init_device_does_not_request_pkexec_when_already_configured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(system_setup, "inspect_device_setup", lambda: _status())
    run = mock.Mock()
    monkeypatch.setattr(system_setup.subprocess, "run", run)

    assert system_setup.run_init_device() == 0
    assert "设备环境已就绪" in capsys.readouterr().out
    run.assert_not_called()


def test_init_device_reports_missing_libusb_without_pkexec(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(system_setup, "inspect_device_setup", lambda: _status(libusb_found=False))
    run = mock.Mock()
    monkeypatch.setattr(system_setup.subprocess, "run", run)

    assert system_setup.run_init_device() == 1
    assert "apt install libusb-1.0-0" in capsys.readouterr().out
    run.assert_not_called()


def test_init_device_requests_fixed_privileged_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        system_setup,
        "inspect_device_setup",
        lambda: _status(rule_installed=False, rule_matches=False),
    )
    monkeypatch.setattr(
        system_setup,
        "_pkexec_command",
        lambda: ["/usr/bin/pkexec", "/app/orbbec_camera", "init-device", "--privileged"],
    )
    completed = mock.Mock(returncode=0)
    run = mock.Mock(return_value=completed)
    monkeypatch.setattr(system_setup.subprocess, "run", run)

    assert system_setup.run_init_device() == 0
    run.assert_called_once_with(
        ["/usr/bin/pkexec", "/app/orbbec_camera", "init-device", "--privileged"],
        check=False,
        env=system_setup._system_subprocess_env(),
    )


def test_matching_but_insecure_rule_requires_manual_admin_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        system_setup,
        "inspect_device_setup",
        lambda: _status(
            rule_secure=False,
            rule_security_issue="目标 udev 规则不得是符号链接",
        ),
    )
    run = mock.Mock()
    monkeypatch.setattr(system_setup.subprocess, "run", run)

    assert system_setup.run_init_device() == 1
    run.assert_not_called()


def test_source_mode_refuses_automatic_pkexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_setup.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError, match="源码/editable.*install_permissions"):
        system_setup._pkexec_command()


def test_untrusted_frozen_path_refuses_automatic_pkexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(system_setup.sys, "executable", "/home/operator/orbbec_camera")
    monkeypatch.setattr(
        system_setup,
        "_validate_trusted_executable",
        mock.Mock(side_effect=RuntimeError("必须属于 root")),
    )
    with pytest.raises(RuntimeError, match="必须属于 root"):
        system_setup._pkexec_command()


def test_trusted_frozen_path_builds_pkexec_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/opt/forge/bin/orbbec_camera")
    monkeypatch.setattr(system_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(system_setup.sys, "executable", str(executable))
    monkeypatch.setattr(system_setup, "_validate_trusted_executable", lambda path: executable)
    monkeypatch.setattr(
        system_setup,
        "_trusted_system_executable",
        lambda name, candidates: Path("/usr/bin/pkexec"),
    )

    assert system_setup._pkexec_command() == [
        "/usr/bin/pkexec",
        str(executable),
        "init-device",
        "--privileged",
    ]


def test_trusted_executable_requires_root_owned_nonwritable_path_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/opt/forge/bin/orbbec_camera")
    metadata = {
        executable: _stat(stat.S_IFREG | 0o755),
        executable.parent: _stat(stat.S_IFDIR | 0o755),
        executable.parent.parent: _stat(stat.S_IFDIR | 0o755),
        Path("/opt"): _stat(stat.S_IFDIR | 0o755),
        Path("/"): _stat(stat.S_IFDIR | 0o755),
    }
    monkeypatch.setattr(system_setup.os, "lstat", lambda path: metadata[Path(path)])

    assert system_setup._validate_trusted_executable(executable) == executable

    metadata[executable.parent.parent] = _stat(stat.S_IFDIR | 0o775)
    with pytest.raises(RuntimeError, match="group/world 写入"):
        system_setup._validate_trusted_executable(executable)


def test_system_subprocess_env_restores_loader_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123:/user/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/user/lib")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/injected.so")
    monkeypatch.setenv("LD_AUDIT", "/tmp/audit.so")

    environment = system_setup._system_subprocess_env()

    assert environment["LD_LIBRARY_PATH"] == "/user/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in environment
    assert "LD_PRELOAD" not in environment
    assert "LD_AUDIT" not in environment


def test_caller_uid_requires_valid_nonroot_pkexec_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PKEXEC_UID", raising=False)
    with pytest.raises(RuntimeError, match="缺少 PKEXEC_UID"):
        system_setup._caller_uid_from_pkexec()

    for value in ("invalid", "0", "-1"):
        monkeypatch.setenv("PKEXEC_UID", value)
        with pytest.raises(RuntimeError, match="PKEXEC_UID"):
            system_setup._caller_uid_from_pkexec()

    monkeypatch.setenv("PKEXEC_UID", "1000")
    monkeypatch.setattr(system_setup.pwd, "getpwuid", lambda uid: _passwd(uid=uid))
    assert system_setup._caller_uid_from_pkexec() == 1000


def test_validate_rule_destination_rejects_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = Path("/etc/udev/rules.d/99-test.rules")
    monkeypatch.setattr(system_setup, "_validate_secure_path", lambda *args, **kwargs: Path(args[0]))
    monkeypatch.setattr(
        system_setup.os,
        "lstat",
        lambda path: _stat(stat.S_IFLNK | 0o777),
    )

    with pytest.raises(RuntimeError, match="符号链接"):
        system_setup._validate_rule_destination(destination)


def test_secure_directory_rejects_unsafe_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system_setup.os,
        "lstat",
        lambda path: _stat(stat.S_IFDIR | 0o777),
    )
    with pytest.raises(RuntimeError, match="group/world 写入"):
        system_setup._validate_secure_path(Path("/etc/udev"), expected_type="directory")


def test_install_rule_is_atomic_and_mode_0644(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "rules" / "99.rules"
    destination.parent.mkdir()
    monkeypatch.setattr(system_setup, "_RULE_DESTINATION", destination)
    monkeypatch.setattr(system_setup, "_validate_rule_destination", lambda: None)
    monkeypatch.setattr(system_setup.os, "chown", lambda *args: None)

    system_setup._install_rule(b"fixed rule\n")

    assert destination.read_bytes() == b"fixed rule\n"
    assert os.stat(destination).st_mode & 0o777 == 0o644


def test_install_rule_reports_replace_before_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "rules" / "99.rules"
    destination.parent.mkdir()
    monkeypatch.setattr(system_setup, "_RULE_DESTINATION", destination)
    monkeypatch.setattr(system_setup, "_validate_rule_destination", lambda: None)
    monkeypatch.setattr(system_setup.os, "chown", lambda *args: None)
    fsync = mock.Mock(side_effect=[None, OSError("directory fsync failed")])
    monkeypatch.setattr(system_setup.os, "fsync", fsync)

    with pytest.raises(system_setup.RuleInstallError) as error:
        system_setup._install_rule(b"fixed rule\n")

    assert error.value.rule_replaced
    assert destination.read_bytes() == b"fixed rule\n"


def test_privileged_helper_refuses_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 1000)
    assert system_setup.run_init_device(privileged=True) == 1


def test_privileged_helper_does_not_rewrite_matching_rule_for_group_only_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _passwd()
    video_group = _group()
    install_rule = mock.Mock()
    trusted_command = mock.Mock(return_value=Path("/usr/sbin/usermod"))
    run = mock.Mock()
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(system_setup, "_trusted_frozen_executable", lambda: Path("/opt/orbbec"))
    monkeypatch.setattr(system_setup, "_caller_uid_from_pkexec", lambda: 1000)
    monkeypatch.setattr(system_setup, "_resolve_caller", lambda uid: (user, video_group))
    monkeypatch.setattr(system_setup, "_trusted_system_executable", trusted_command)
    monkeypatch.setattr(system_setup, "_validate_rule_destination", lambda: None)
    monkeypatch.setattr(system_setup, "_scan_rule_files", lambda *args: ((), ()))
    monkeypatch.setattr(system_setup, "_rule_needs_install", lambda data: False)
    monkeypatch.setattr(system_setup, "_install_rule", install_rule)
    monkeypatch.setattr(system_setup, "_ensure_video_group_member", lambda *args: True)
    monkeypatch.setattr(system_setup.subprocess, "run", run)

    assert system_setup._run_privileged_init() == 0

    install_rule.assert_not_called()
    run.assert_not_called()
    assert trusted_command.call_args_list == [
        mock.call("usermod", system_setup._USERMOD_CANDIDATES),
    ]


def test_privileged_helper_reports_partial_changes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user = _passwd()
    video_group = _group()
    monkeypatch.setattr(system_setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(system_setup, "_trusted_frozen_executable", lambda: Path("/opt/orbbec"))
    monkeypatch.setattr(system_setup, "_caller_uid_from_pkexec", lambda: 1000)
    monkeypatch.setattr(system_setup, "_resolve_caller", lambda uid: (user, video_group))
    monkeypatch.setattr(
        system_setup,
        "_trusted_system_executable",
        lambda name, candidates: Path(f"/usr/bin/{name}"),
    )
    monkeypatch.setattr(system_setup, "_validate_rule_destination", lambda: None)
    monkeypatch.setattr(system_setup, "_scan_rule_files", lambda *args: ((), ()))
    monkeypatch.setattr(system_setup, "_rule_needs_install", lambda data: True)
    monkeypatch.setattr(system_setup, "_install_rule", lambda data: None)
    monkeypatch.setattr(system_setup, "_ensure_video_group_member", lambda *args: True)
    monkeypatch.setattr(
        system_setup.subprocess,
        "run",
        mock.Mock(side_effect=subprocess.CalledProcessError(1, "udevadm")),
    )

    assert system_setup._run_privileged_init() == 1

    error = capsys.readouterr().err
    assert "部分修改已经完成" in error
    assert "已安装" in error
    assert "已将 operator 加入 video" in error
