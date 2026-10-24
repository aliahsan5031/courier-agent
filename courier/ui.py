"""Desktop window (tkinter, ships with Python). UNTESTED here: this sandbox has no display or tkinter.

Features: chat log, streaming answer, tool-activity panel (resizable split), attach file (button or drag-and-drop
if `pip install tkinterdnd2`), copy last answer, export chat to Markdown, regenerate, read last answer aloud,
dark/light toggle, always-on-top, STOP button, approval dialog, push-to-talk.
Not built: system tray, global shortcut, voice animation.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path


def export_markdown(turns, path):
    """turns: [(who, text)] -> Markdown file (pure function, unit-tested)."""
    lines = ["# Courier chat export", ""]
    for who, text in turns:
        lines += [f"**{who}:**", "", text, ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def attach_prompt(paths, text):
    """Turn attached file paths into an instruction the agent can act on (pure, unit-tested)."""
    names = [str(p) for p in paths]
    note = "Attached files (read them with read_document or read_file):\n" + "\n".join(f"- {n}" for n in names)
    return f"{note}\n\n{text}".strip() if names else text


def run(make_agent):
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root, dnd = TkinterDnD.Tk(), True
    except Exception:
        root, dnd = tk.Tk(), False
    root.title("Courier")
    root.geometry("760x680")
    q: queue.Queue = queue.Queue()
    st = {"dark": True, "turns": [], "attached": [], "last_user": "", "busy": False, "stream_open": False}

    def approve(cmd):
        ev, ans = threading.Event(), {"ok": False}
        q.put(("ask", cmd, ev, ans))
        ev.wait(120)
        return ans["ok"]

    agent = make_agent(approve)
    pane = tk.PanedWindow(root, orient="vertical", sashrelief="raised")
    pane.pack(fill="both", expand=True, padx=6, pady=6)
    log = scrolledtext.ScrolledText(pane, wrap="word", state="disabled")
    activity = scrolledtext.ScrolledText(pane, wrap="word", height=6, state="disabled")
    pane.add(log, stretch="always")
    pane.add(activity, stretch="never")
    entry = tk.Entry(root)
    entry.pack(fill="x", padx=6)
    chip = tk.Label(root, anchor="w", text="")
    chip.pack(fill="x", padx=6)
    bar = tk.Frame(root)
    bar.pack(fill="x", padx=6, pady=6)

    def put(widget, text):
        widget.configure(state="normal"); widget.insert("end", text); widget.see("end"); widget.configure(state="disabled")

    def theme():
        bg, fg = ("#1e1e24", "#e8e8ee") if st["dark"] else ("#fafafa", "#202020")
        for w in (root, bar, chip):
            w.configure(bg=bg)
        chip.configure(fg=fg)
        for w in (log, activity, entry):
            w.configure(bg=bg, fg=fg, insertbackground=fg)

    def say(who, text):
        st["turns"].append((who, text))
        put(log, f"{who}: {text}\n\n")

    def show_attached():
        chip.configure(text=("Attached: " + ", ".join(Path(p).name for p in st["attached"])) if st["attached"] else "")

    def attach(paths=None):
        paths = paths or filedialog.askopenfilenames(title="Attach files")
        for p in paths:
            if p not in st["attached"]:
                st["attached"].append(p)
        show_attached()

    def on_token(t):
        q.put(("token", t))

    agent.on_token = on_token
    agent.on_event = lambda kind, *a: q.put(("act", f"{kind}: {a[0] if a else ''} {str(a[1])[:80] if len(a) > 1 else ''}"))

    def work(text):
        st["busy"] = True
        try:
            out = agent.run(text)
            q.put(("final", out))
        except Exception as e:
            q.put(("final", f"error: {e}"))

    def send(_=None, text=None):
        if st["busy"]:
            return
        text = text if text is not None else entry.get().strip()
        if not text and not st["attached"]:
            return
        full = attach_prompt(st["attached"], text)
        entry.delete(0, "end")
        st["last_user"] = full
        say("you", full)
        st["attached"] = []
        show_attached()
        threading.Thread(target=work, args=(full,), daemon=True).start()

    def regenerate():
        if st["last_user"] and not st["busy"]:
            send(text=st["last_user"] + "\n\n(Please try again and give a different, improved answer.)")

    def last_answer():
        return next((t for w, t in reversed(st["turns"]) if w == "courier"), "")

    def copy_last():
        root.clipboard_clear(); root.clipboard_append(last_answer())

    def export():
        p = filedialog.asksaveasfilename(defaultextension=".md", filetypes=[("Markdown", "*.md")])
        if p:
            export_markdown(st["turns"], p); say("system", f"chat exported to {p}")

    def read_aloud():
        txt = last_answer()[:1500]
        if txt:
            def go():
                try:
                    from . import voice
                    voice.speak(txt)
                except Exception as e:
                    q.put(("say", "voice", str(e)))
            threading.Thread(target=go, daemon=True).start()

    def stop():
        agent.ctx.stop.set(); say("system", "emergency stop requested")

    def talk():
        try:
            from . import voice
            text = voice.listen()
            entry.delete(0, "end"); entry.insert(0, text)  # user can correct before sending
        except Exception as e:
            say("voice", str(e))

    top = tk.BooleanVar()
    for label, cmd, kw in (("Send", send, {}), ("Stop", stop, {"fg": "red"}), ("Attach", attach, {}), ("Regenerate", regenerate, {}),
                           ("Copy", copy_last, {}), ("Export", export, {}), ("Read aloud", read_aloud, {}), ("Talk", talk, {}),
                           ("Theme", lambda: (st.update(dark=not st["dark"]), theme()), {})):
        tk.Button(bar, text=label, command=cmd, **kw).pack(side="left", padx=2)
    tk.Checkbutton(bar, text="On top", variable=top, command=lambda: root.attributes("-topmost", top.get())).pack(side="left")
    entry.bind("<Return>", send)
    if dnd:
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", lambda e: attach(list(root.tk.splitlist(e.data))))
    else:
        chip.configure(text="(drag-and-drop needs: pip install tkinterdnd2; use the Attach button)")

    def pump():
        while not q.empty():
            item = q.get()
            kind = item[0]
            if kind == "say":
                say(item[1], item[2])
            elif kind == "act":
                put(activity, item[1] + "\n")
            elif kind == "token":
                if not st["stream_open"]:
                    put(log, "courier: "); st["stream_open"] = True
                put(log, item[1])
            elif kind == "final":
                if st["stream_open"]:
                    put(log, "\n\n"); st["stream_open"] = False
                    st["turns"].append(("courier", item[1]))
                else:
                    say("courier", item[1])
                st["busy"] = False
            else:
                _, cmd, ev, ans = item
                ans["ok"] = messagebox.askyesno("Approve action", f"Allow this?\n\n{cmd}")
                ev.set()
        root.after(150, pump)

    theme(); pump(); root.mainloop()
