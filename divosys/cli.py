"""The divo-sys command line. Runs from a checkout (`python divo.py ...`) and from inside divo-sys.exe."""
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser

from . import __version__, launch

USAGE = """divo-sys {version}

  setup               one-step install: puts divo-sys.exe in your programs folder, finds your speaker, starts with
                      Windows, adds the Claude Code hooks and MCP server and a desktop icon
                      (options: --no-app --no-hooks --no-mcp --no-autostart --no-shortcut)
  uninstall           undo everything setup did
  gui                 open the control panel (starts the service if needed)
  shortcut            put a divo-sys icon on your desktop (also: shortcut startmenu | shortcut remove)
  install             only build and install divo-sys.exe
  build-installer     build divo-sys-setup.exe, the file to give to other people (option: --out FOLDER)
  discover            list paired Divoom speakers and their Bluetooth addresses
  start | stop        run / stop the background service
  daemon              run the service in the foreground
  demo                try the panel without a speaker (a pretend one on port 47833)
  face <mood>         show a face
  text <message>      scroll a message
  bright <0-100>      set the screen brightness
  status              what is connected and showing
  hook                for Claude Code hooks (reads the event on stdin)
  mcp                 the MCP server (stdio)
  version             print the version
"""


def _say(message, error=False):
    """Print, or pop a message box when there is no console (the desktop icon and the app run without one)."""
    if sys.stdout is None and os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "divo-sys", 0x10 if error else 0x40)
    else:
        print(message)


def _message_box(text, style=0x40):
    import ctypes
    return ctypes.windll.user32.MessageBoxW(0, text, "divo-sys", style)


def interactive(ask=None, show=None, open_panel=None):
    """What happens when divo-sys.exe is double-clicked: it has no console, so it talks in dialogs.
    The installed app opens the panel; a downloaded setup file offers to install (or update) it."""
    ask = ask or (lambda text: _message_box(text, 0x24) == 6)  # Yes / No
    show = show or _message_box
    open_panel = open_panel or (lambda: main(["gui"]))
    here = launch.archive_path()
    if launch.installed() and here and here.resolve() == launch.install_exe().resolve():
        open_panel()
        return 0
    verb = "Update" if launch.installed() else "Install"
    if not ask(f"{verb} divo-sys {__version__}?\n\nIt starts with Windows and runs in the background. It adds the Claude "
               "Code hooks, an MCP server and a desktop icon. You can remove it any time from Settings, Apps."):
        return 0
    from .setup_cmd import setup
    lines = []
    code = setup(say=lines.append)
    show("\n".join(lines))
    if code == 0:
        open_panel()
    return code


def main(argv):
    if not argv:
        if sys.stdout is None and os.name == "nt" and launch.packed():  # double-clicked: no console to print to
            return interactive()
        print(USAGE.format(version=__version__))
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "daemon":
        from .daemon import run
        run()
    elif cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
    elif cmd == "hook":
        from .hook import run
        run(rest[0] if rest else None)
    elif cmd in ("version", "--version", "-V"):
        print(__version__)
    elif cmd == "setup":
        from .setup_cmd import setup
        return setup(app="--no-app" not in rest, hooks="--no-hooks" not in rest, mcp="--no-mcp" not in rest,
                     autostart="--no-autostart" not in rest, shortcut="--no-shortcut" not in rest)
    elif cmd == "install":
        from . import appinstall
        appinstall.install()
        print("installed. `divo-sys.exe setup` (or `python divo.py setup`) finishes the setup.")
    elif cmd == "build-installer":
        from . import appinstall
        out = rest[rest.index("--out") + 1] if "--out" in rest and rest.index("--out") + 1 < len(rest) else "dist"
        path = appinstall.build_installer(os.path.join(out, "divo-sys-setup.exe"))
        print(f"built {path} ({os.path.getsize(path) // 1024} KB). It needs Python 3.9+ on the PATH of the PC it runs on.")
    elif cmd == "uninstall":
        from .setup_cmd import uninstall
        lines = []
        code = uninstall(say=lines.append)
        _say("\n".join(lines))
        return code
    elif cmd == "shortcut":
        from . import setup_cmd
        which = rest[0] if rest else "desktop"
        try:
            if which == "remove":
                print(f"removed {sum(setup_cmd.remove_shortcut(w) for w in ('desktop', 'startmenu'))} shortcut(s)")
            else:
                print("created", setup_cmd.create_shortcut(which))
        except (RuntimeError, ValueError, OSError) as e:
            print("could not create the shortcut:", e)
            return 1
    elif cmd == "discover":
        from .discover import find_speakers
        found = find_speakers()
        if not found:
            print("No Divoom speaker found. Pair it in Windows Bluetooth settings first.")
            return 1
        for d in found:
            print(f"{d['name']}  {d['address']}")
    elif cmd == "demo":
        # a throw-away instance with a pretend speaker, its own settings folder and its own port
        env = dict(os.environ, DIVO_DEMO="1", DIVO_DATA=tempfile.mkdtemp(prefix="divo-demo-"), DIVO_PORT="47833")
        proc = subprocess.Popen([*launch.tool_command(), "daemon"], env=env)
        time.sleep(2)
        webbrowser.open("http://127.0.0.1:47833/")
        print("Demo running on http://127.0.0.1:47833/ (no speaker needed). Press Ctrl+C to close it.")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
    elif cmd == "start":
        from .client import ensure_daemon
        print("running" if ensure_daemon() else "could not start")
    elif cmd == "stop":
        from .client import stop_daemon
        print("stopped" if stop_daemon() else "not running")
    elif cmd == "gui":
        from .client import ensure_daemon
        from .config import HOST, PORT
        if not ensure_daemon():
            _say("divo-sys could not start its background service. See daemon.log in the divo-sys data folder.", True)
            return 1
        webbrowser.open(f"http://{HOST}:{PORT}/")
    elif cmd == "face" and rest:
        from . import faces
        if rest[0] not in faces.MOODS:
            print(f"unknown mood {rest[0]!r}. Available: {', '.join(faces.MOODS)}")
            return 1
        from .client import send
        print(send({"cmd": "face", "mood": rest[0]}, "mcp"))
    elif cmd == "text" and rest:
        from .client import send
        print(send({"cmd": "text", "text": " ".join(rest)}, "mcp"))
    elif cmd == "bright" and rest:
        from .client import send, admin_token
        try:
            value = int(rest[0])
        except ValueError:
            print("usage: bright <0-100>")
            return 1
        print(json.dumps(send({"cmd": "brightness", "value": value}, "gui", token=admin_token())))
    elif cmd == "status":
        from .client import get
        s = get("/api/state?peek=1")
        print(json.dumps({k: s[k] for k in ("connected", "address", "pinned", "brightness")}
                         | {"showing": s["current"]["label"]}, indent=2))
    else:
        print(USAGE.format(version=__version__))
        return 1
    return 0
