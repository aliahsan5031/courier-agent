"""Desktop control: keyboard, mouse, windows, clipboard. OPTIONAL and OFF by default (config desktop_enabled).

Needs `pip install pyautogui pyperclip` (+ `pygetwindow` on Windows). UNTESTED on a real screen: the tests use a
fake backend to prove the approval, emergency-stop and action-limit gating, not the real mouse movement.
"""
from __future__ import annotations

import platform
import shutil
import subprocess


class DesktopError(Exception):
    pass


class PyAutoGuiBackend:
    def __init__(self):
        try:
            import pyautogui
        except ImportError:
            raise DesktopError("pyautogui is not installed: pip install pyautogui")
        pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort
        pyautogui.PAUSE = 0.15
        self.g = pyautogui

    def type_text(self, text):
        self.g.write(text, interval=0.02)

    def hotkey(self, *keys):
        self.g.hotkey(*keys)

    def click(self, x, y, button="left", clicks=1):
        self.g.click(x=x, y=y, button=button, clicks=clicks)

    def scroll(self, amount):
        self.g.scroll(amount)

    def screenshot(self, path):
        self.g.screenshot(path)

    def list_windows(self):
        try:
            import pygetwindow as gw
            return [{"title": w.title, "x": w.left, "y": w.top, "w": w.width, "h": w.height} for w in gw.getAllWindows() if w.title.strip()]
        except ImportError:
            pass
        if shutil.which("wmctrl"):
            out = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=10).stdout
            rows = [l.split(None, 7) for l in out.splitlines()]
            return [{"title": r[7], "x": int(r[2]), "y": int(r[3]), "w": int(r[4]), "h": int(r[5])} for r in rows if len(r) == 8]
        raise DesktopError("window control needs pygetwindow (Windows) or wmctrl (Linux)")

    def window_action(self, title, action, x=None, y=None, w=None, h=None):
        try:
            import pygetwindow as gw
        except ImportError:
            if shutil.which("wmctrl") and action in ("focus", "close"):
                subprocess.run(["wmctrl", "-a" if action == "focus" else "-c", title], timeout=10, check=True)
                return
            raise DesktopError("window control needs pygetwindow (Windows)")
        wins = [win for win in gw.getWindowsWithTitle(title)]
        if not wins:
            raise DesktopError(f"no window matching {title!r}")
        win = wins[0]
        if action == "focus":
            win.activate()
        elif action == "minimize":
            win.minimize()
        elif action == "maximize":
            win.maximize()
        elif action == "restore":
            win.restore()
        elif action == "close":
            win.close()
        elif action == "move":
            win.moveTo(x, y)
            if w and h:
                win.resizeTo(w, h)
        else:
            raise DesktopError("action: focus|minimize|maximize|restore|close|move")


def get_clipboard() -> str:
    try:
        import pyperclip
        return pyperclip.paste()
    except ImportError:
        pass
    sysname = platform.system()
    cmds = {"Windows": ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], "Darwin": ["pbpaste"]}
    cmd = cmds.get(sysname) or next((c for c in (["wl-paste"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-b", "-o"]) if shutil.which(c[0])), None)
    if not cmd:
        raise DesktopError("no clipboard tool found (pip install pyperclip)")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def set_clipboard(text: str):
    try:
        import pyperclip
        pyperclip.copy(text)
        return
    except ImportError:
        pass
    sysname = platform.system()
    cmds = {"Windows": ["clip"], "Darwin": ["pbcopy"]}
    cmd = cmds.get(sysname) or next((c for c in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]) if shutil.which(c[0])), None)
    if not cmd:
        raise DesktopError("no clipboard tool found (pip install pyperclip)")
    subprocess.run(cmd, input=text, text=True, timeout=10, check=True)
