"""Render narrowly scoped systemd units for the Mímir search runtime."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


_HOME = re.compile(r"/[A-Za-z0-9_./-]*\Z")
_RELEASE = re.compile(r"[0-9a-f]{40}\Z")


def _validate(role: str, home: str, release: str) -> str:
    if not isinstance(role, str) or role not in {"publisher", "reader"}:
        raise ValueError("invalid_role")
    if not isinstance(home, str) or not home or not _HOME.fullmatch(home):
        raise ValueError("invalid_home")
    if ".." in home.split("/"):
        raise ValueError("invalid_home")
    if not isinstance(release, str) or not _RELEASE.fullmatch(release):
        raise ValueError("invalid_release")
    return home.rstrip("/") or "/"


def _unit(*sections: tuple[str, tuple[str, ...]]) -> str:
    return "".join("[" + name + "]\n" + "".join(line + "\n" for line in lines) + "\n"
                   for name, lines in sections)


def _service(description: str, command: str, *, release: str,
             timeout: int | None = None, environment_file: str | tuple[str, ...] | None = None,
             restart: bool = False, runtime_dependency: bool = False) -> str:
    unit = [f"Description={description}"]
    service: list[str] = []
    if runtime_dependency:
        unit.extend(("After=mimir-search-refresh.service", "Requires=mimir-search-refresh.service"))
    service.extend(("Type=simple" if restart else "Type=oneshot",
                    "UMask=0077", "NoNewPrivileges=true", "PrivateTmp=true",
                    "MemoryMax=1G", "TasksMax=16", "Nice=10",
                    f"Environment=MIMIR_SEARCH_RELEASE={release}"))
    files = (environment_file,) if isinstance(environment_file, str) else environment_file or ()
    service.extend(f"EnvironmentFile={path}" for path in files)
    service.append(f"ExecStart={command}")
    if timeout is not None:
        service.append(f"TimeoutStartSec={timeout}")
    if restart:
        service.extend(("Restart=on-failure", "RestartSec=10", "TimeoutStopSec=20"))
    install = ("WantedBy=default.target",) if restart else ()
    sections: list[tuple[str, tuple[str, ...]]] = [("Unit", tuple(unit)), ("Service", tuple(service))]
    if install:
        sections.append(("Install", install))
    return _unit(*sections)


def _path(home: str, *parts: str) -> str:
    prefix = "" if home == "/" else home
    return prefix + "/" + "/".join(parts)


def _timer(description: str, service: str, cadence: str) -> str:
    return _unit(("Unit", (f"Description={description}",)),
                 ("Timer", ("OnBootSec=30", f"OnUnitActiveSec={cadence}",
                            "AccuracySec=5", "Persistent=true", f"Unit={service}")),
                 ("Install", ("WantedBy=timers.target",)))


def render(role: str, home: str, release: str) -> dict[str, str]:
    """Return the exact unit filenames and contents for a validated role."""
    home = _validate(role, home, release)
    config_dir = _path(home, ".config", "mimir-search")
    package = _path(home, ".local", "share", "mimir-search", "releases", release, "search.pyz")
    state = _path(home, ".local", "state", "mimir-search")
    release_env = release
    if role == "publisher":
        service = "mimir-search-publish.service"
        return {
            service: _service(
            "Publish the current Mímir artifact snapshot",
                f"/usr/bin/python3 {package} ops publish --config {config_dir}/publisher.json",
                release=release_env, timeout=120,
            ),
            "mimir-search-publish.timer": _timer(
                "Publish the current Mímir artifact snapshot", service, "5min"),
        }

    refresh_service = "mimir-search-refresh.service"
    runtime_service = "mimir-search.service"
    check_service = "mimir-search-semantic-check.service"
    return {
        runtime_service: _service(
            "Mímir owner-only artifact search",
            f"/usr/bin/python3 {package} serve --state {state} "
            f"--handoff {config_dir}/handoff.json --port 8791",
            release=release_env, environment_file=f"{config_dir}/runtime.env",
            restart=True, runtime_dependency=True,
        ),
        refresh_service: _service(
            "Refresh the Mímir artifact search snapshot",
            f"/usr/bin/python3 {package} ops refresh --config {config_dir}/reader.json",
            release=release_env, timeout=120,
            environment_file=f"{config_dir}/runtime.env",
        ),
        "mimir-search-refresh.timer": _timer(
            "Refresh the Mímir artifact search snapshot", refresh_service, "60s"),
        # Gateway revision drift must be visible within minutes. The optional
        # leading "-" file holds only the Heimdall hub URL and fleet token.
        check_service: _service(
            "Check Mímir semantic search state and gateway revision drift",
            f"/usr/bin/python3 {package} ops semantic-check --port 8791",
            release=release_env, timeout=60,
            environment_file=(f"{config_dir}/runtime.env", f"-{config_dir}/heimdall.env"),
        ),
        "mimir-search-semantic-check.timer": _timer(
            "Check Mímir semantic search state and gateway revision drift", check_service, "5min"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("publisher", "reader"))
    parser.add_argument("--home", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        units = render(args.role, args.home, args.release)
        args.output_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        for filename, content in units.items():
            with (args.output_directory / filename).open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
    except (OSError, ValueError) as error:
        print(f"service unit rendering failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
