"""Pure rendering and validation tests for Mímir search systemd units."""
from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from tools.artifact_search.service_units import main, render


RELEASE = "a" * 40
HOME = "/home/search_user"


class ServiceUnitsTests(unittest.TestCase):
    def test_publisher_units_bind_only_validated_paths_and_cadence(self) -> None:
        units = render("publisher", HOME, RELEASE)
        self.assertEqual(set(units), {
            "mimir-search-publish.service", "mimir-search-publish.timer",
        })
        service = units["mimir-search-publish.service"]
        self.assertIn(f"ExecStart=/usr/bin/python3 {HOME}/.local/share/mimir-search/releases/{RELEASE}/search.pyz ops publish --config {HOME}/.config/mimir-search/publisher.json\n", service)
        self.assertNotIn("EnvironmentFile=", service)
        self.assert_common_hardening(service, RELEASE)
        self.assertIn("TimeoutStartSec=120\n", service)
        timer = units["mimir-search-publish.timer"]
        self.assertIn("OnBootSec=30\n", timer)
        self.assertIn("OnUnitActiveSec=5min\n", timer)
        self.assertIn("AccuracySec=5\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)

    def test_reader_units_bind_runtime_refresh_env_and_cadence(self) -> None:
        units = render("reader", HOME, RELEASE)
        self.assertEqual(set(units), {
            "mimir-search.service", "mimir-search-refresh.service",
            "mimir-search-refresh.timer",
        })
        runtime = units["mimir-search.service"]
        refresh = units["mimir-search-refresh.service"]
        self.assertIn(f"ExecStart=/usr/bin/python3 {HOME}/.local/share/mimir-search/releases/{RELEASE}/search.pyz serve --state {HOME}/.local/state/mimir-search --handoff {HOME}/.config/mimir-search/handoff.json --port 8791\n", runtime)
        self.assertIn("After=mimir-search-refresh.service\n", runtime)
        self.assertIn("Requires=mimir-search-refresh.service\n", runtime)
        self.assertIn(f"EnvironmentFile={HOME}/.config/mimir-search/runtime.env\n", runtime)
        self.assertIn("Restart=on-failure\n", runtime)
        self.assertIn("RestartSec=10\n", runtime)
        self.assertIn("TimeoutStopSec=20\n", runtime)
        self.assertIn("WantedBy=default.target\n", runtime)
        self.assertIn(f"ops refresh --config {HOME}/.config/mimir-search/reader.json\n", refresh)
        self.assertIn(f"EnvironmentFile={HOME}/.config/mimir-search/runtime.env\n", refresh)
        self.assertIn("TimeoutStartSec=120\n", refresh)
        timer = units["mimir-search-refresh.timer"]
        self.assertIn("OnUnitActiveSec=60s\n", timer)
        self.assertIn("OnBootSec=30\n", timer)
        self.assertIn("AccuracySec=5\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)
        for filename in ("mimir-search.service", "mimir-search-refresh.service"):
            self.assert_common_hardening(units[filename], RELEASE)

    def test_render_rejects_role_path_and_release_injection(self) -> None:
        for role in ("admin", "reader;touch /tmp/pwned", "publisher\nExecStart=/bin/sh"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                render(role, HOME, RELEASE)
        with self.assertRaises(ValueError):
            render(None, HOME, RELEASE)
        for home in (
            "relative/home", "/home/../tmp", "/home/user/..", "/home/user;touch",
            "/home/user\nExecStart=/bin/sh", "/home/üser", "/home/user with spaces",
            "/home/user%n", "/home/user$()",
        ):
            with self.subTest(home=home), self.assertRaises(ValueError):
                render("reader", home, RELEASE)
        for release in ("A" * 40, "a" * 39, "a" * 41, "a" * 39 + ";", "../" + "a" * 37):
            with self.subTest(release=release), self.assertRaises(ValueError):
                render("publisher", HOME, release)
        self.assertIn("ExecStart=/usr/bin/python3 /.local/share/", render("publisher", "/", RELEASE)["mimir-search-publish.service"])

    def test_no_unrequested_write_paths_or_system_protection_directives(self) -> None:
        for units in (render("publisher", HOME, RELEASE), render("reader", HOME, RELEASE)):
            for content in units.values():
                self.assertNotRegex(content, r"(?m)^(?:ProtectSystem|ReadWritePaths)=")
                self.assertNotRegex(content, r"(?m)^EnvironmentFile=.*publisher")
                self.assertNotRegex(content, r"(?m)^(?:ExecStart|Environment|EnvironmentFile)=.*(?:;|\$\(|`)")

    def test_cli_creates_exclusively_in_a_fresh_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "new" / "units"
            args = ["--role", "publisher", "--home", HOME, "--release", RELEASE,
                    "--output-directory", str(output)]
            self.assertEqual(main(args), 0)
            self.assertEqual({p.name for p in output.iterdir()}, {
                "mimir-search-publish.service", "mimir-search-publish.timer",
            })
            original = (output / "mimir-search-publish.service").read_text()
            self.assertEqual(main(args), 1)
            self.assertEqual((output / "mimir-search-publish.service").read_text(), original)

    @staticmethod
    def assert_common_hardening(content: str, release: str) -> None:
        for directive in (
            "UMask=0077", "NoNewPrivileges=true", "PrivateTmp=true", "MemoryMax=1G",
            "TasksMax=16", "Nice=10", f"Environment=MIMIR_SEARCH_RELEASE={release}",
        ):
            assert directive + "\n" in content, directive


if __name__ == "__main__":
    unittest.main()
