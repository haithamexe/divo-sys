import contextlib
import http.client
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from divosys import appinstall, cli, config, launch, settings, setup_cmd  # noqa: E402


def parse_block(data, offset=0):
    """Walk one version-resource node and check its lengths and alignment. Returns (key, value, children, length)."""
    length, value_len, kind = struct.unpack_from("<HHH", data, offset)
    pos, key = offset + 6, ""
    while data[pos:pos + 2] != b"\0\0":
        key += data[pos:pos + 2].decode("utf-16-le")
        pos += 2
    pos += 2
    pos += -pos % 4
    value_size = value_len * 2 if kind == 1 else value_len
    value = data[pos:pos + value_size]
    pos += value_size
    children = []
    end = offset + length
    while pos < end:
        pos += -pos % 4
        if pos >= end:
            break
        child = parse_block(data, pos)
        children.append(child[:3])
        pos += child[3]
    assert pos == end, f"block {key!r} says it is {length} bytes but its parts add up to {pos - offset}"
    assert offset % 4 == 0
    return key, value, children, length


class VersionResource(unittest.TestCase):
    STRINGS = {"CompanyName": "divo-sys", "FileDescription": "divo-sys", "ProductName": "divo-sys",
               "FileVersion": "0.2.0", "LegalCopyright": "MIT License"}

    def test_structure_is_consistent(self):
        data = appinstall.version_resource("0.2.0", self.STRINGS)
        key, value, children, length = parse_block(data)
        self.assertEqual(key, "VS_VERSION_INFO")
        self.assertEqual(length, len(data))
        sig, struc, ms, ls = struct.unpack_from("<4I", value)
        self.assertEqual(sig, 0xFEEF04BD)
        self.assertEqual((ms, ls), ((0 << 16) | 2, (0 << 16) | 0))  # 0.2.0.0
        self.assertEqual([c[0] for c in children], ["StringFileInfo", "VarFileInfo"])

    def test_strings_and_translation_round_trip(self):
        data = appinstall.version_resource("1.2.3", self.STRINGS)
        _, value, children, _ = parse_block(data)
        table = children[0][2][0]
        self.assertEqual(table[0], "040904B0")
        found = {k: v.decode("utf-16-le").rstrip("\0") for k, v, _ in table[2]}
        self.assertEqual(found, self.STRINGS)
        translation = children[1][2][0]
        self.assertEqual(translation[0], "Translation")
        self.assertEqual(struct.unpack("<HH", translation[1]), (0x0409, 0x04B0))
        self.assertEqual(struct.unpack_from("<2I", value, 8), (1 << 16 | 2, 3 << 16 | 0))

    def test_shebang_quotes_paths_with_spaces(self):
        self.assertEqual(appinstall.shebang("C:/Py/pythonw.exe"), b"#!C:/Py/pythonw.exe\n")
        self.assertEqual(appinstall.shebang("C:/Program Files/Py/pythonw.exe"), b'#!"C:/Program Files/Py/pythonw.exe"\n')

    def test_program_zip_has_the_whole_package_and_a_main(self):
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(appinstall.program_zip())).namelist()
        self.assertIn("__main__.py", names)
        for needed in ("divosys/cli.py", "divosys/engine.py", "divosys/web/index.html", "divosys/__init__.py"):
            self.assertIn(needed, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])


class Where(unittest.TestCase):
    def test_paths_follow_local_app_data(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/Profiles/someone/AppData/Local"}):
            self.assertEqual(launch.install_exe(), Path("C:/Profiles/someone/AppData/Local/Programs/divo-sys/divo-sys.exe"))
            self.assertEqual(launch.app_data_dir(), Path("C:/Profiles/someone/AppData/Local/divo-sys"))

    def test_service_and_tool_commands(self):
        with mock.patch.object(launch, "installed", return_value=False):
            service = launch.service_command()
            self.assertEqual(service[-1], "daemon")
            self.assertTrue(service[-2].endswith(("divo.py", ".exe")))
        with mock.patch.object(launch, "installed", return_value=True), \
                mock.patch.object(launch, "install_exe", return_value=Path("C:/A B/divo-sys.exe")):
            self.assertEqual(launch.service_command(), [str(Path("C:/A B/divo-sys.exe")), "daemon"])
            tool = launch.tool_command()
            self.assertEqual(tool[1], str(Path("C:/A B/divo-sys.exe")))
            self.assertEqual(Path(tool[0]).name.lower(), "python.exe" if os.name == "nt" else Path(tool[0]).name.lower())
            self.assertTrue(launch.quote(launch.service_command()).startswith('"'))

    def test_a_checkout_is_not_packed(self):
        self.assertFalse(launch.packed())
        self.assertIsNone(launch.archive_path())

    def test_hooks_recognise_the_app_form(self):
        for cmd in ('"C:/Python/python.exe" "C:/Profiles/u/AppData/Local/Programs/divo-sys/divo-sys.exe" hook',
                    'python /x/divo-sys.exe hook', '"C:/Py/python.exe" "D:/a b/divo.py" hook'):
            self.assertTrue(setup_cmd._is_ours({"hooks": [{"command": cmd}]}), cmd)
        self.assertFalse(setup_cmd._is_ours({"hooks": [{"command": 'python "C:/other/tool.exe" hook'}]}))

    def test_hook_and_mcp_commands_use_the_entry(self):
        with mock.patch.object(launch, "installed", return_value=True), \
                mock.patch.object(launch, "install_exe", return_value=Path("C:/A B/divo-sys.exe")):
            self.assertTrue(setup_cmd.hook_command().endswith('divo-sys.exe" hook'))
            self.assertEqual(setup_cmd.mcp_args()[1:], [str(Path("C:/A B/divo-sys.exe")), "mcp"])


class DataFolder(unittest.TestCase):
    def test_environment_override_wins(self):
        with mock.patch.dict(os.environ, {"DIVO_DATA": "C:/somewhere"}):
            self.assertEqual(config._data_dir(), Path("C:/somewhere"))

    @unittest.skipUnless(os.name == "nt", "Windows keeps its data in the user's app data folder")
    def test_windows_uses_the_app_data_folder(self):
        env = {k: v for k, v in os.environ.items() if k != "DIVO_DATA"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config._data_dir(), launch.app_data_dir())

    def test_settings_are_picked_up_from_the_old_place_once(self):
        legacy, fresh = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (legacy / "settings.json").write_text(json.dumps({"brightness": 33, "claude": {"enabled": False}}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "DIVO_DATA"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(settings, "DATA_DIR", fresh), \
                mock.patch.object(settings, "LEGACY_DATA_DIR", legacy):
            first = settings.Settings()
            self.assertEqual(first.snapshot()["brightness"], 33)
            self.assertFalse(first.snapshot()["claude"]["enabled"])
            first.patch({"brightness": 44})  # saving writes to the new place
            self.assertTrue((fresh / "settings.json").is_file())
            self.assertEqual(settings.Settings().snapshot()["brightness"], 44)  # and the new place wins from now on
            self.assertEqual(json.loads((legacy / "settings.json").read_text(encoding="utf-8"))["brightness"], 33)


class MigrateSettings(unittest.TestCase):
    def setUp(self):
        self.checkout, self.appdata = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (self.checkout / "data").mkdir()
        self.old = self.checkout / "data" / "settings.json"
        self.new = self.appdata / "divo-sys" / "settings.json"
        self.patches = [mock.patch.object(launch, "CHECKOUT", self.checkout),
                        mock.patch.object(launch, "app_data_dir", return_value=self.appdata / "divo-sys")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_carries_the_old_choices_over_once(self):
        self.old.write_text('{"claude": {"override_pin": true}}', encoding="utf-8")
        self.assertTrue(appinstall.migrate_settings(lambda *_: None))
        self.assertEqual(json.loads(self.new.read_text(encoding="utf-8")), {"claude": {"override_pin": True}})
        self.old.write_text('{"claude": {"override_pin": false}}', encoding="utf-8")
        self.assertFalse(appinstall.migrate_settings(lambda *_: None))  # never overwrites what is already there
        self.assertTrue(json.loads(self.new.read_text(encoding="utf-8"))["claude"]["override_pin"])

    def test_nothing_to_carry(self):
        self.assertFalse(appinstall.migrate_settings(lambda *_: None))
        self.assertFalse(self.new.exists())


class CommandLine(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def test_bright_sends_the_cli_source(self):
        calls = []

        def fake_send(req, source="mcp", token=None):
            calls.append((req, source, token))
            return {"ok": True}

        with mock.patch("divosys.client.send", fake_send), mock.patch("divosys.client.admin_token", lambda: "T"):
            code, out = self.run_cli("bright", "40")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [({"cmd": "brightness", "value": 40}, "gui", "T")])

    def test_bright_rejects_nonsense(self):
        self.assertEqual(self.run_cli("bright", "lots")[0], 1)
        self.assertEqual(self.run_cli("bright")[0], 1)

    def test_face_lists_moods_when_unknown(self):
        code, out = self.run_cli("face", "no-such-mood")
        self.assertEqual(code, 1)
        self.assertIn("Available:", out)
        self.assertIn("idle", out)

    def test_version_and_usage(self):
        from divosys import __version__
        self.assertEqual(self.run_cli("version"), (0, __version__ + "\n"))
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        for word in ("setup", "uninstall", "shortcut", "demo", "hook", "mcp"):
            self.assertIn(word, text)
        self.assertEqual(self.run_cli("no-such-command")[0], 1)


def stub_available():
    try:
        return os.name == "nt" and appinstall.find_stub().is_file()
    except RuntimeError:
        return False


@unittest.skipUnless(stub_available(), "needs Windows and pip's launcher stub")
class RealApp(unittest.TestCase):
    """Builds a real divo-sys.exe and checks what Windows sees, and that it runs with no window."""

    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="divo-app-test-"))
        cls.exe = appinstall.build_exe(cls.work / "divo-sys.exe")

    @staticmethod
    def powershell(command):
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=60).stdout.strip()

    def test_windows_shows_its_name_and_publisher(self):
        out = self.powershell(f"$v=(Get-Item '{self.exe}').VersionInfo; $v.FileDescription; $v.CompanyName; "
                              "$v.ProductName; $v.OriginalFilename").splitlines()
        self.assertEqual(out, ["divo-sys", "divo-sys", "divo-sys", "divo-sys.exe"])

    def test_the_icon_is_ours_not_the_stubs(self):
        out = self.powershell(
            "Add-Type -AssemblyName System.Drawing; "
            f"$b=[System.Drawing.Icon]::ExtractAssociatedIcon('{self.exe}').ToBitmap(); "
            "$p=$b.GetPixel([int]($b.Width/2),[int]($b.Height/2)); $c=$b.GetPixel(0,0); "
            "'{0},{1},{2},{3}' -f $p.R,$p.G,$p.B,$c.A")
        self.assertEqual(out, "255,96,24,0")  # Claude orange in the middle, transparent in the corner

    def test_the_program_runs_from_inside_the_file(self):
        from divosys import __version__
        done = subprocess.run([str(launch.console_python()), str(self.exe), "version"], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.stdout.strip(), __version__)

    def test_the_launcher_starts_the_service_with_no_window(self):
        s = __import__("socket").socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env = dict(os.environ, DIVO_DEMO="1", DIVO_DATA=str(self.work / "data"), DIVO_PORT=str(port))
        proc = subprocess.Popen([str(self.exe), "daemon"], env=env, creationflags=0x08000000)
        try:
            up = False
            for _ in range(40):
                time.sleep(0.5)
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    c.request("GET", "/api/ping", headers={"X-Divo": "1"})
                    up = c.getresponse().status == 200
                except OSError:
                    continue
                if up:
                    break
            self.assertTrue(up, "the service did not answer")
            windows = self.powershell(f"(Get-Process | Where-Object {{ $_.MainWindowHandle -ne 0 -and "
                                      f"$_.Path -like '*{self.work.name}*' }} | Measure-Object).Count")
            self.assertEqual(windows, "0")
            token = (self.work / "data" / ".token").read_text(encoding="utf-8").strip()
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request("POST", "/api/quit", "{}", {"X-Divo": "1", "X-Divo-Token": token, "Content-Type": "application/json"})
            c.getresponse().read()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
