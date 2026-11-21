"""Tkinter chat surface for the local Persian composer.
The conversation log is display-only but selectable: typing and pasting into it
are blocked by key guards, while mouse selection, Ctrl+C and Ctrl+A stay free.
Every line starts with an RTL mark so Tk's bidi engine keeps a stable direction
and list numbers use Persian digits to avoid weak-character reordering.
Drill mode samples topics that already have mined frames and advances
automatically after each rating, sharing one memory with the normal chat.
The teach button ingests chosen text files in a worker thread and rebuilds.
Messages that intent classifies as an action are routed to intent.run instead
of the composer: approval is collected on the Tk thread, execution happens in a
worker with its own connections, and the command's own output is displayed.
"""
from __future__ import annotations

import queue
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from agent.errors import AgentError

from . import compose, corpus, execute, frames, intent, lexicon, usage

FONT = ("Tahoma", 11)
FONT_BOLD = ("Tahoma", 11, "bold")
RLM = "\u200f"
_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
PACKAGE_PARENT = Path(__file__).resolve().parent.parent
ACTION_TIMEOUT = 120.0
OUTPUT_LIMIT = 4000

REASONS = {
    "corpus-too-small": "هنوز متن کافی نخوانده‌ام.",
    "no-known-content-word": "واژهٔ اصلی را در آموخته‌هایم پیدا نکردم.",
    "no-frames-for-topic": "برای این موضوع اسکلت کافی ندارم.",
    "only-known-sentences": "فقط جملهٔ تکراری خود پیکره پیدا شد؛ جملهٔ نو نساختم.",
    "all-excluded": "همهٔ نامزدهای این دور قبلاً رد شده‌اند؛ متن دیگری بده.",
}


def fa_digits(value: int) -> str:
    return "".join(_FA_DIGITS[int(ch)] for ch in str(value))


class _Approval:
    """intent.run asks its notifier; the human answer was already collected on
    the Tk thread, so the worker never has to touch a widget."""

    def __init__(self, approved: bool) -> None:
        self._approved = bool(approved)

    def approval(self, kind: str, summary: str, payload: dict) -> bool:
        return self._approved


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.offer_id: int | None = None
        self.candidates: list[dict] = []
        self.rejected: set[str] = set()
        self.last_text = ""
        self.drill_active = False
        self.drill_used: set[str] = set()
        self.drill_tally = {"rated": 0, "rejected": 0, "skipped": 0, "empty": 0}
        self.teach_busy = False
        self.action_busy = False
        self.events: queue.Queue = queue.Queue()
        self._open()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(120, self._poll_events)

    def _open(self) -> None:
        self.cdb = corpus.connect()
        self.udb = usage.connect()
        self.ldb = lexicon.connect()
        self.fdb = frames.connect()
        self.tdb = compose.connect()
        self.idb = intent.prepare()

    def _build(self) -> None:
        style = ttk.Style(self.root)
        style.configure("TButton", font=FONT)
        style.configure("TLabel", font=FONT)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.status = tk.StringVar(value="آماده")
        top = ttk.Frame(self.root, padding=(10, 6))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, textvariable=self.status).pack(side="right")
        ttk.Button(top, text="پاک کردن", command=self.clear).pack(side="left")
        ttk.Button(top, text="دریل", command=self.toggle_drill).pack(side="left")
        self.teach_btn = ttk.Button(top, text="آموزش", command=self.teach)
        self.teach_btn.pack(side="left")
        self.log = tk.Text(
            self.root,
            wrap="word",
            state="normal",
            font=FONT,
            relief="flat",
            padx=10,
            pady=8,
            insertwidth=0,
            undo=False,
        )
        self.log.grid(row=1, column=0, sticky="nsew")
        self.log.tag_configure("user", justify="right", font=FONT_BOLD)
        self.log.tag_configure("assistant", justify="right")
        self.log.tag_configure("system", justify="right", foreground="#5f6368")
        self.log.tag_configure("candidate", justify="right", foreground="#0b57d0")
        self._lock_log()
        bottom = ttk.Frame(self.root, padding=(10, 8))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.entry = tk.Entry(
            bottom,
            font=FONT,
            justify="right",
            relief="solid",
            bd=1,
        )
        self.entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.entry.bind("<Return>", self.send)
        self.entry.focus_set()
        ttk.Button(bottom, text="بگو", command=self.send).grid(row=0, column=1)
        self.choice_box = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        self.choice_box.grid(row=3, column=0, sticky="ew")
        self._try_rtl()
        self._log("سیستم: آماده‌ام. دستور اجرایی باشد، اجرا می‌شود؛ "
                  "گفت‌وگو باشد، جمله می‌سازم.", "system")

    def _lock_log(self) -> None:
        self.log.bind("<Key>", self._guard_key)
        for seq in ("<Control-v>", "<Control-V>", "<Control-x>", "<Control-X>",
                    "<BackSpace>", "<Delete>"):
            self.log.bind(seq, lambda event: "break")
        self.log.bind("<Control-a>", self._select_all)

    def _guard_key(self, event: tk.Event) -> str | None:
        if event.state & 0x4:
            return None
        if event.keysym in ("Up", "Down", "Left", "Right", "Home", "End",
                            "Prior", "Next", "Shift_L", "Shift_R",
                            "Control_L", "Control_R", "Alt_L", "Alt_R"):
            return None
        return "break"

    def _select_all(self, event: tk.Event | None = None) -> str:
        self.log.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _try_rtl(self) -> None:
        for widget in (getattr(self, "log", None), getattr(self, "entry", None)):
            if widget is None:
                continue
            try:
                widget.configure(direction="rtl")
            except tk.TclError:
                continue

    def _log(self, text: str, tag: str) -> None:
        self.log.insert("end", RLM + str(text) + "\n", tag)
        self.log.see("end")

    def _clear_choices(self) -> None:
        for child in self.choice_box.winfo_children():
            child.destroy()
        self.offer_id = None
        self.candidates = []

    def clear(self) -> None:
        self._clear_choices()
        self.log.delete("1.0", "end")
        self._log("سیستم: آماده‌ام.", "system")
        self.status.set("آماده")

    def teach(self) -> None:
        if self.teach_busy or self.action_busy:
            return
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="انتخاب فایل‌های آموزشی",
            filetypes=[("متن فارسی", "*.txt"), ("همه فایل‌ها", "*.*")],
        )
        if not paths:
            return
        self.teach_busy = True
        self.teach_btn.state(["disabled"])
        self._log(f"سیستم: آموزش شروع شد؛ {fa_digits(len(paths))} فایل.",
                  "system")
        self.status.set("آموزش در جریان...")
        threading.Thread(target=self._teach_worker, args=(list(paths),),
                         daemon=True).start()

    def _run_module(self, module: str, *args: str) -> int:
        done = subprocess.Popen(
            [sys.executable, "-u", "-m", module, *args],
            cwd=str(PACKAGE_PARENT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        if done.stdout is not None:
            for line in done.stdout:
                if line.strip():
                    self.events.put({"kind": "log",
                                     "text": "  " + line.strip(),
                                     "tag": "system"})
        return int(done.wait())

    def _teach_worker(self, paths: list[str]) -> None:
        try:
            self.events.put({"kind": "log",
                             "text": "سیستم: تکه‌کردن و افزودن فایل‌ها...",
                             "tag": "system"})
            self._run_module("farsi.teach", *paths)
        except Exception as exc:
            self.events.put({"kind": "log",
                             "text": f"سیستم: خطای آموزش: {exc}",
                             "tag": "system"})
        finally:
            self.events.put({"kind": "done"})

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event.get("kind")
                if kind == "log":
                    self._log(event["text"], event.get("tag", "system"))
                elif kind == "done":
                    self.teach_busy = False
                    self.teach_btn.state(["!disabled"])
                    lexicon._df_snapshot.cache_clear()
                    self.status.set("آماده")
                    self._log("سیستم: آموزش تمام شد. حالا امتحان کن.",
                              "system")
                elif kind == "action_done":
                    self.action_busy = False
                    self._action_report(str(event.get("status") or ""),
                                        event.get("code"))
        except queue.Empty:
            pass
        self.root.after(120, self._poll_events)

    def _action_report(self, status: str, code) -> None:
        if status == "ok":
            self._log("سیستم: دستور اجرا شد.", "system")
            self.status.set("انجام شد")
        elif status == "denied":
            self._log("سیستم: اجرا رد شد.", "system")
            self.status.set("رد شد")
        elif status == "not-runnable":
            self._log("سیستم: این پیام دستور اجرایی نبود.", "system")
            self.status.set("آماده")
        else:
            self._log(f"سیستم: اجرا با خطا پایان یافت (کد {code}).", "system")
            self.status.set("خطا در اجرا")

    def _run_argv(self, argv: list) -> int:
        """Runner handed to intent.run: keeps the project's own argv gate and
        child environment, but captures output so the user can read it."""
        try:
            clean = execute.check_argv([str(part) for part in argv])
        except (AgentError, ValueError, OSError) as exc:
            self.events.put({"kind": "log",
                             "text": "سیستم: دستور مجاز نیست: " + str(exc),
                             "tag": "system"})
            return 1
        try:
            done = subprocess.run(
                clean, env=execute.child_env(),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=ACTION_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            self.events.put({"kind": "log",
                             "text": "سیستم: زمان اجرا طولانی شد؛ متوقف شد.",
                             "tag": "system"})
            return 1
        for stream in (done.stdout, done.stderr):
            body = (stream or "").strip()
            if body:
                self.events.put({"kind": "log",
                                 "text": body[:OUTPUT_LIMIT],
                                 "tag": "assistant"})
        return int(done.returncode)

    def _start_action(self, text: str, decision: dict) -> None:
        argv = [str(part) for part in decision["argv"]]
        shown = " ".join(argv[1:]) or argv[0]
        self._log("سیستم: این دستور است، نه گفت‌وگو → " + shown, "system")
        approved = messagebox.askyesno(
            "اجرای دستور",
            "این دستور اجرا شود؟\n" + shown,
            parent=self.root,
        )
        if not approved:
            self._log("سیستم: اجرا لغو شد.", "system")
            self.status.set("لغو شد")
            return
        self.action_busy = True
        self.status.set("در حال اجرا...")
        threading.Thread(target=self._action_worker, args=(text,),
                         daemon=True).start()

    def _action_worker(self, text: str) -> None:
        idb = None
        udb = None
        try:
            idb = intent.prepare()
            udb = usage.connect()
            report = intent.run(text, idb, udb, _Approval(True),
                                runner=self._run_argv)
            self.events.put({"kind": "action_done",
                             "status": str(report.get("status") or ""),
                             "code": report.get("exit_code")})
        except Exception as exc:
            self.events.put({"kind": "log",
                             "text": "سیستم: خطای اجرا: " + str(exc),
                             "tag": "system"})
            self.events.put({"kind": "action_done", "status": "error",
                             "code": None})
        finally:
            for db in (udb, idb):
                if db is not None:
                    try:
                        db.close()
                    except sqlite3.Error:
                        pass

    def send(self, event: tk.Event | None = None) -> None:
        if self.teach_busy or self.action_busy:
            self._log("سیستم: صبر کن کار قبلی تمام شود.", "system")
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.last_text = text
        self._log("شما: " + text, "user")
        self.status.set("در حال بررسی...")
        self.root.update_idletasks()
        try:
            decision = intent.classify(text, self.idb, self.udb)
        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            decision = {}
            self._log("سیستم: تشخیص قصد ناموفق: " + str(exc), "system")
        if decision.get("kind") == "action" and decision.get("argv"):
            self._start_action(text, decision)
            return
        try:
            report = compose.compose(
                self.tdb, self.cdb, self.udb, self.ldb, self.fdb, text,
                exclude=self.rejected,
            )
        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            self.status.set("خطا")
            self._log("سیستم: " + str(exc), "system")
            return
        self._show_report(report)

    def _show_report(self, report: dict) -> None:
        self._clear_choices()
        candidates = report.get("candidates") or []
        self.candidates = candidates
        self.offer_id = report.get("offer_id")
        topic = report.get("topic") or "-"
        if not candidates:
            reason_key = str(report.get("reason") or "")
            reason = REASONS.get(reason_key, "پاسخ مناسبی پیدا نشد.")
            self._log(f"سیستم: {reason} (موضوع: {topic})", "system")
            self.status.set("جوابی نبود")
            return
        if report.get("auto"):
            chosen = candidates[0]
            self._log("سیستم: " + str(chosen.get("text") or ""), "assistant")
            self.status.set("پاسخ خودکار؛ اگر خواستی بازخورد بده")
            self._auto_buttons()
            if self.drill_active:
                self._drill_buttons()
            return
        self._log("سیستم: چند پیشنهاد دارم:", "system")
        for index, item in enumerate(candidates, start=1):
            self._log(f"{fa_digits(index)}. {item.get('text') or ''}",
                      "candidate")
        self.status.set("یکی را انتخاب کن، یا هر دو را رد کن")
        self._choice_buttons()
        if self.drill_active:
            self._drill_buttons()

    def _choice_buttons(self) -> None:
        for index in range(1, len(self.candidates) + 1):
            button = ttk.Button(
                self.choice_box,
                text=f"گزینه {fa_digits(index)}",
                command=lambda position=index: self.rate_choice(position),
            )
            button.pack(side="right", padx=4)
        reject = ttk.Button(
            self.choice_box,
            text="رد هر دو",
            command=self.reject_both,
        )
        reject.pack(side="right", padx=4)

    def _auto_buttons(self) -> None:
        good = ttk.Button(
            self.choice_box,
            text="خوب بود",
            command=lambda: self.rate_score(5.0),
        )
        bad = ttk.Button(
            self.choice_box,
            text="بد بود",
            command=lambda: self.rate_score(1.0),
        )
        good.pack(side="right", padx=4)
        bad.pack(side="right", padx=4)

    def _drill_buttons(self) -> None:
        ttk.Button(self.choice_box, text="بعدی",
                   command=self._drill_skip).pack(side="right", padx=4)
        ttk.Button(self.choice_box, text="پایان دریل",
                   command=self._end_drill).pack(side="right", padx=4)

    def toggle_drill(self) -> None:
        if self.teach_busy or self.action_busy:
            self._log("سیستم: صبر کن کار قبلی تمام شود.", "system")
            return
        if self.drill_active:
            self._end_drill()
            return
        self.drill_active = True
        self.drill_used.clear()
        self.drill_tally = {"rated": 0, "rejected": 0, "skipped": 0, "empty": 0}
        self._log("سیستم: حالت دریل روشن شد؛ هر امتیاز بلافاصله دور بعد را می‌آورد.",
                  "system")
        self._drill_next_round()

    def _end_drill(self) -> None:
        self.drill_active = False
        self._clear_choices()
        self._log(f"سیستم: دریل پایان یافت. خلاصه: {self.drill_tally}", "system")
        self.status.set("آماده")

    def _pick_topic(self) -> str | None:
        rows = self.fdb.execute(
            "SELECT DISTINCT term FROM frame_slots "
            "ORDER BY RANDOM() LIMIT 40"
        ).fetchall()
        for row in rows:
            term = str(row["term"])
            if term in self.drill_used:
                continue
            if lexicon.word_weight(self.ldb, term) <= 0.1:
                continue
            return term
        return None

    def _drill_next_round(self) -> None:
        if not self.drill_active:
            return
        topic = self._pick_topic()
        if topic is None:
            self._log("سیستم: موضوع تازه‌ای نماند؛ دریل تمام شد.", "system")
            self._end_drill()
            return
        self.drill_used.add(topic)
        self._clear_choices()
        self._log("--- دور دریل | موضوع: "
                  + lexicon.surface_for(self.ldb, topic) + " ---", "system")
        self.status.set("دریل: یک دکمه بزن")
        try:
            report = compose.compose(
                self.tdb, self.cdb, self.udb, self.ldb, self.fdb,
                f"یه جمله درباره {topic} بگو", exclude=self.rejected,
            )
        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            self.status.set("خطا")
            self._log("سیستم: " + str(exc), "system")
            self._end_drill()
            return
        candidates = report.get("candidates") or []
        if not candidates:
            self.drill_tally["empty"] += 1
            self._log("نامزدی نبود: " + (report.get("reason") or "-"), "system")
            self.root.after(400, self._drill_next_round)
            return
        self._show_report(report)

    def _drill_skip(self) -> None:
        self.drill_tally["skipped"] += 1
        self._clear_choices()
        self._drill_next_round()

    def reject_both(self) -> None:
        if self.offer_id is None or not self.candidates or not self.last_text:
            return
        scores = {int(item["position"]): 1.0 for item in self.candidates}
        try:
            compose.rate(
                self.tdb, self.fdb, self.ldb, self.offer_id, scores=scores
            )
        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            self.status.set("خطا")
            self._log("سیستم: ثبت نشد: " + str(exc), "system")
            return
        for item in self.candidates:
            key = str(item.get("fkey") or "")
            if key:
                self.rejected.add(key)
        self._log("سیستم: هر دو رد شدند؛ امتیازشان کم شد.", "system")
        self._clear_choices()
        if self.drill_active:
            self.drill_tally["rejected"] += 1
            self._drill_next_round()
            return
        self.status.set("در حال جست‌وجوی جواب دیگر...")
        self.root.update_idletasks()
        try:
            report = compose.compose(
                self.tdb, self.cdb, self.udb, self.ldb, self.fdb,
                self.last_text, exclude=self.rejected,
            )
        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            self.status.set("خطا")
            self._log("سیستم: " + str(exc), "system")
            return
        self._show_report(report)

    def rate_choice(self, position: int) -> None:
        if self.offer_id is None:
            return
        try:
            compose.rate(
                self.tdb, self.fdb, self.ldb, self.offer_id, choice=position
            )
        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            self.status.set("خطا")
            self._log("سیستم: ثبت نشد: " + str(exc), "system")
            return
        self._log("سیستم: انتخاب ثبت شد.", "system")
        self._clear_choices()
        if self.drill_active:
            self.drill_tally["rated"] += 1
            self._drill_next_round()
            return
        self.status.set("ثبت شد")

    def rate_score(self, score: float) -> None:
        if self.offer_id is None:
            return
        try:
            compose.rate(
                self.tdb, self.fdb, self.ldb, self.offer_id, scores={1: score}
            )
        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            self.status.set("خطا")
            self._log("سیستم: ثبت نشد: " + str(exc), "system")
            return
        self._log("سیستم: بازخورد ثبت شد.", "system")
        self._clear_choices()
        if self.drill_active:
            self.drill_tally["rated"] += 1
            self._drill_next_round()
            return
        self.status.set("ثبت شد")

    def _close(self) -> None:
        if self.teach_busy or self.action_busy:
            self._log("سیستم: کاری در جریان است؛ صبر کن تمام شود.", "system")
            return
        for db in (self.tdb, self.fdb, self.ldb, self.udb, self.cdb, self.idb):
            try:
                db.close()
            except sqlite3.Error:
                pass
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    root.title("دستیار فارسی")
    root.geometry("780x540")
    root.minsize(560, 380)
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())