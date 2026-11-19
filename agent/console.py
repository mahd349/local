"""Agent Console: a thin tkinter client over the local bridge.

Runs agent subcommands as subprocesses and streams their output live, shows
engine status, and lets the user grant/deny pending approvals. It never
duplicates the engine; it only observes and dispatches.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from urllib.request import Request, urlopen

from .bridge import ensure_token
from .config import cfg, child_env, get_logger
from .notify import STOP_PATH

log = get_logger("console")
CREATE_NO_WINDOW = 0x08000000

COMMANDS = [
    ("مرورگر", "باز کردن سایت", ["browse", "--url", "{arg}", "--seconds", "120"]),
    ("مرورگر", "دستور takeover", ["takeover", "--command", "{arg}"]),
    ("مرورگر", "عنوان صفحه", ["takeover", "--command", "title"]),
    ("مرورگر", "آدرس صفحه", ["takeover", "--command", "url"]),
    ("مرورگر", "اسکرین‌شات", ["takeover", "--command", "shot console"]),
    ("مرورگر", "PDF صفحه", ["takeover", "--command", "pdf D:/Work/agent-state/reports/console.pdf"]),
    ("مأموریت", "اجرای مأموریت", ["run", "{arg}"]),
    ("مأموریت", "زمان‌بندی", ["watch", "{arg}"]),
    ("یادگیری", "ضبط ماکرو", ["learn", "--url", "{arg}", "--name", "console_macro"]),
    ("یادگیری", "فهرست ماکروها", ["macros"]),
    ("یادگیری", "بازپخش ماکرو", ["replay", "{arg}"]),
    ("هوش", "پرسش از هوش", ["ai", "ask", "--task", "{arg}", "--url", "{chat}"]),
    ("هوش", "probe سلکتورها", ["ai", "probe", "--url", "{chat}"]),
    ("فایل", "جست‌وجو", ["index", "search", "{arg}"]),
    ("فایل", "ایندکس D:\\Python", ["index", "build", "--root", "D:\\Python"]),
    ("فایل", "اجرای کد", ["code", "run", "{arg}"]),
    ("سیستم", "وضعیت محیط", ["doctor"]),
]


BRAIN_COMMANDS = [
    ("دانش پایتون", "اسکن D:/Python", ["@brain", "ingest", "scan", "--root", "D:/Python"]),
    ("دانش پایتون", "اسکن مسیر دلخواه", ["@brain", "ingest", "scan", "--root", "{arg}"]),
    ("دانش پایتون", "پردازش پوشهٔ ورودی", ["@brain", "ingest", "inbox"]),
    ("دانش پایتون", "آمار دانش", ["@brain", "ingest", "stats"]),
    ("ساخت برنامه", "کارت‌های قابلیت", ["@brain", "capability", "list"]),
    ("ساخت برنامه", "نقشهٔ درخواست", ["@brain", "planner", "{arg}"]),
    ("ساخت برنامه", "ساخت برنامه", ["@brain", "planner", "{arg}", "--build"]),
    ("ساخت برنامه", "افزودن به فایل (خشک)", ["@brain", "integrate", "{arg1}", "{arg2}", "--dry-run"]),
    ("ساخت برنامه", "افزودن به فایل", ["@brain", "integrate", "{arg1}", "{arg2}"]),
    ("تست دانش", "تست هسته", ["@brain", "knowtest"]),
    ("تست دانش", "تست اعتبارسنجی", ["@brain", "verifytest"]),
    ("تست دانش", "تست نقشه", ["@brain", "plantest"]),
    ("تست دانش", "تست ادغام", ["@brain", "integratetest"]),
]


FARSI_COMMANDS = [
    ("فارسی", "یادگیری روشن", ["@farsi", "on"]),
    ("فارسی", "یادگیری خاموش", ["@farsi", "off"]),
    ("فارسی", "آمار پیکره", ["@farsi", "stats"]),
    ("فارسی", "این متن را یاد بگیر", ["@farsi", "add", "--text", "{arg}"]),
    ("فارسی", "خواندن فایل یا آرشیو", ["@farsi", "add-file", "{arg}"]),
    ("آمار زبان", "ساخت آمار هم‌نشینی", ["@farsi.usage", "build"]),
    ("آمار زبان", "عبارت‌های کشف‌شده", ["@farsi.usage", "phrases",
                                    "--min-count", "2", "--min-npmi", "0.2"]),
    ("آمار زبان", "واژه‌های نزدیک", ["@farsi.usage", "neighbors", "{arg}"]),
    ("دستیار", "فهم درخواست", ["@farsi.intent", "say", "{arg}"]),
    ("دستیار", "پاسخ محلی", ["@farsi.nlg", "say", "{arg}"]),
    ("دستیار", "آموزش نمونهٔ جدید", ["@farsi.intent", "teach", "{arg1}", "{arg2}"]),
    ("پیشنهاد", "پیشنهاد داخل چت", ["@farsi.watch", "run", "--seconds", "900"]),
    ("پیشنهاد", "پیشنهاد روشن", ["@farsi.watch", "on"]),
    ("پیشنهاد", "پیشنهاد خاموش", ["@farsi.watch", "off"]),
]


def _python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


class Console:
    def __init__(self):
        self.token = ensure_token()
        self.base = f"http://{cfg('bridge_host')}:{cfg('bridge_port')}"
        self.q: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.last_event = 0
        self.approval_ids: list[int] = []
        self._build_ui()
        self._autostart()
        self.root.after(250, self._drain)
        self.root.after(1500, self._poll)

    # ---------- bridge ----------
    def _http(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.base + path, data=body, method=method)
        request.add_header("X-Agent-Token", self.token)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def _autostart(self) -> None:
        health = self._http("GET", "/health")
        if not health.get("ok"):
            self._log("پل خاموش بود؛ روشن می‌کنم…")
            subprocess.Popen([_python(), "-m", "agent", "bridge"],
                             creationflags=CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=child_env())
            for _ in range(20):
                threading.Event().wait(0.3)
                if self._http("GET", "/health").get("ok"):
                    break
        health = self._http("GET", "/health")
        if health.get("ok") and not health.get("engine"):
            self._log("موتور خاموش بود؛ روشن می‌کنم…")
            subprocess.Popen([_python(), "-m", "agent", "daemon"],
                             creationflags=CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=child_env())

    # ---------- ui ----------
    def _build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title("کنسول ایجنت")
        self.root.geometry("1360x720")
        status = tk.Frame(self.root)
        status.pack(fill="x", padx=8, pady=4)
        self.lbl_bridge = tk.Label(status, text="پل: ؟")
        self.lbl_bridge.pack(side="left", padx=6)
        self.lbl_engine = tk.Label(status, text="موتور: ؟")
        self.lbl_engine.pack(side="left", padx=6)
        self.lbl_stop = tk.Label(status, text="توقف: ؟")
        self.lbl_stop.pack(side="left", padx=6)
        tk.Button(status, text="توقف اضطراری", command=self._stop).pack(side="right", padx=4)
        tk.Button(status, text="رفع توقف", command=self._resume).pack(side="right", padx=4)
        tools = tk.Frame(self.root)
        tools.pack(fill="x", padx=8)
        tk.Button(tools, text="کپی همهٔ لاگ", command=self._copy_log).pack(side="left", padx=2)
        tk.Button(tools, text="ذخیرهٔ لاگ", command=self._save_log).pack(side="left", padx=2)
        tk.Button(tools, text="پاک کردن لاگ", command=self._clear_log).pack(side="left", padx=2)
        tk.Button(tools, text="چسباندن در آرگومان", command=self._paste_entry).pack(side="left", padx=2)
        argtools = tk.Frame(self.root)
        argtools.pack(fill="x", padx=8)
        tk.Button(argtools, text="چسباندن آرگومان",
                  command=self._paste_arg).pack(side="left", padx=2)
        tk.Button(argtools, text="خواندن آرگومان از فایل",
                  command=self._load_arg).pack(side="left", padx=2)
        tk.Button(argtools, text="ویرایش آرگومان در Notepad",
                  command=self._edit_arg).pack(side="left", padx=2)
        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=8)
        self._button_column(body, COMMANDS)
        self._button_column(body, BRAIN_COMMANDS)
        self._button_column(body, FARSI_COMMANDS)
        self.log = tk.Text(body, state="disabled", wrap="word")
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.log.bind("<Button-3>", self._log_menu)
        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=6)
        tk.Label(bottom, text="آرگومان:").pack(side="left")
        self.entry = tk.Entry(bottom)
        self.entry.pack(side="left", fill="x", expand=True, padx=6)
        self.entry.bind("<Return>", lambda _e: self._run_current())
        self.entry.bind("<Button-3>", self._arg_menu)
        self.entry.bind("<Control-v>", self._paste_arg)
        self.entry.bind("<Control-v>", self._paste_entry)
        tk.Button(bottom, text="اجرا", command=self._run_current).pack(side="left")
        self.current = COMMANDS[0][2]
        approvals = tk.Frame(self.root)
        approvals.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(approvals, text="تأییدهای در انتظار:").pack(side="left")
        self.listbox = tk.Listbox(approvals, height=3)
        self.listbox.pack(side="left", fill="x", expand=True, padx=6)
        tk.Button(approvals, text="تأیید", command=lambda: self._decide(True)).pack(side="left", padx=2)
        tk.Button(approvals, text="رد", command=lambda: self._decide(False)).pack(side="left", padx=2)

    def _button_column(self, parent, commands) -> None:
        frame = tk.Frame(parent)
        frame.pack(side="left", fill="y")
        group = None
        for category, label, template in commands:
            if category != group:
                tk.Label(frame, text="— " + category, anchor="w").pack(fill="x", pady=(6, 0))
                group = category
            tk.Button(frame, text=label, width=22, anchor="w",
                      command=lambda t=template: self._run(t)).pack(fill="x", pady=1)

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))
        self._log("لاگ در کلیپ‌بورد کپی شد.")

    def _save_log(self) -> None:
        import time
        from .config import REPORT_DIR, assert_writable
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = assert_writable(REPORT_DIR / f"console_{int(time.time())}.txt")
        target.write_text(self.log.get("1.0", "end-1c"), encoding="utf-8")
        self._log("لاگ ذخیره شد: " + str(target))

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _paste_entry(self) -> str:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self._log("کلیپ‌بورد خالی است.")
            return "break"
        self.entry.delete(0, "end")
        self.entry.insert(0, str(text).strip())
        return "break"

    def _log_menu(self, event) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="کپی انتخاب",
                         command=lambda: self.root.event_generate("<<Copy>>"))
        menu.add_command(label="کپی همه", command=self._copy_log)
        menu.add_command(label="ذخیرهٔ لاگ", command=self._save_log)
        menu.tk_popup(event.x_root, event.y_root)

    def _arg_path(self):
        from .config import STATE_DIR, assert_writable
        return assert_writable(STATE_DIR / "argument.txt")

    def _set_arg(self, text: str) -> None:
        # An Entry is single-line; collapse breaks so nothing is silently lost.
        self.entry.delete(0, "end")
        self.entry.insert(0, " ".join(str(text).split()))

    def _paste_arg(self, _event=None) -> str:
        text = ""
        for getter in (lambda: self.root.clipboard_get(),
                       lambda: self.root.selection_get(selection="CLIPBOARD")):
            try:
                text = str(getter() or "")
            except tk.TclError:
                continue
            if text:
                break
        if not text:
            self._log("کلیپ‌بورد متن ندارد؛ از «خواندن آرگومان از فایل» استفاده کن.")
            return "break"
        self._set_arg(text)
        self._log("آرگومان از کلیپ‌بورد گذاشته شد.")
        return "break"

    def _load_arg(self) -> None:
        path = self._arg_path()
        if not path.exists():
            path.write_text("", encoding="utf-8")
        self._set_arg(path.read_text(encoding="utf-8-sig", errors="replace"))
        self._log("آرگومان از فایل خوانده شد: " + str(path))

    def _edit_arg(self) -> None:
        path = self._arg_path()
        if not path.exists():
            path.write_text("", encoding="utf-8")
        subprocess.Popen(["notepad.exe", str(path)])
        self._log("در Notepad بنویس و ذخیره کن، بعد «خواندن آرگومان از فایل» را بزن.")

    def _arg_menu(self, event) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="چسباندن", command=self._paste_arg)
        menu.add_command(label="خواندن از فایل", command=self._load_arg)
        menu.add_command(label="پاک کردن", command=lambda: self.entry.delete(0, "end"))
        menu.tk_popup(event.x_root, event.y_root)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- execution ----------
    def _run_current(self) -> None:
        self._run(self.current)

    def _run(self, template: list[str]) -> None:
        self.current = template
        if self.proc is not None and self.proc.poll() is None:
            self._log("یک دستور هنوز در حال اجراست؛ صبر کن تمام شود.")
            return
        arg = self.entry.get().strip()
        if "{arg}" in template and not arg:
            self._log("این دستور به آرگومان نیاز دارد (کادر پایین).")
            return
        if "{arg2}" in template and "|" not in arg:
            self._log("این دستور دو آرگومان می‌خواهد: مسیر|درخواست")
            return
        parts = [item.strip() for item in arg.split("|")]
        pieces = list(template)
        module = "agent"
        if pieces and pieces[0] == "@brain":
            module = "brain"
            pieces = pieces[1:]
        elif pieces and pieces[0].startswith("@farsi"):
            module = pieces[0][1:]
            pieces = pieces[1:]
        argv = [_python(), "-m", module]
        for piece in pieces:
            if piece == "{arg}":
                argv.append(arg)
            elif piece == "{arg1}":
                argv.append(parts[0])
            elif piece == "{arg2}":
                argv.append(parts[1] if len(parts) > 1 else "")
            elif piece == "{chat}":
                argv.append(cfg("chat_url"))
            else:
                argv.append(piece)
        self._log("» " + " ".join(argv[2:]))
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=child_env(),
            creationflags=CREATE_NO_WINDOW, text=True,
            encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.q.put(("log", line.rstrip()))
        proc.wait()
        self.q.put(("log", f"[پایان با کد {proc.returncode}]"))

    def _stop(self) -> None:
        STOP_PATH.write_text("stop", encoding="utf-8")
        self._log("توقف اضطراری فعال شد.")

    def _resume(self) -> None:
        STOP_PATH.unlink(missing_ok=True)
        self._log("توقف برداشته شد.")

    def _decide(self, granted: bool) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        approval_id = self.approval_ids[selection[0]]
        result = self._http("POST", f"/approvals/{approval_id}/decision", {"granted": granted})
        self._log(f"تأیید #{approval_id}: {'اجازه' if granted else 'رد'} → {result.get('ok')}")

    # ---------- polling ----------
    def _poll(self) -> None:
        threading.Thread(target=self._fetch, daemon=True).start()
        self.root.after(1500, self._poll)

    def _fetch(self) -> None:
        health = self._http("GET", "/health")
        self.q.put(("status", health))
        events = self._http("GET", f"/events?since={self.last_event}")
        for event in events.get("events", []):
            self.last_event = max(self.last_event, event["id"])
            self.q.put(("log", f"[{event['kind']}] {json.dumps(event['payload'], ensure_ascii=False)[:160]}"))
        approvals = self._http("GET", "/approvals")
        self.q.put(("approvals", approvals.get("approvals", [])))

    def _drain(self) -> None:
        while not self.q.empty():
            kind, payload = self.q.get()
            if kind == "log":
                self._log(payload)
            elif kind == "status":
                ok = payload.get("ok")
                self.lbl_bridge.configure(text="پل: " + ("بالا" if ok else "خاموش"))
                self.lbl_engine.configure(text="موتور: " + ("بالا" if payload.get("engine") else "خاموش"))
                self.lbl_stop.configure(text="توقف: " + ("فعال" if STOP_PATH.exists() else "عادی"))
            elif kind == "approvals":
                self.approval_ids = [item["id"] for item in payload]
                self.listbox.delete(0, "end")
                for item in payload:
                    self.listbox.insert("end", f"#{item['id']} {item['summary'][:70]}")
        self.root.after(250, self._drain)


def main() -> int:
    Console().root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())