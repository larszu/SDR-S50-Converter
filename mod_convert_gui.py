#!/usr/bin/env python3
# SDR-S50 Converter — convert Panasonic/JVC camcorder .MOD/.TOD files
# Copyright (C) 2026  larszu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
SDR-S50 Converter — a small Tkinter GUI for turning Panasonic/JVC camcorder
.MOD / .TOD files into modern, deinterlaced, square-pixel MP4s, or into a
lossless archive (.mkv or .mp4).

It is a thin front-end over mod_convert_core: pick files or a folder, press
"Analyse selected" to see what you actually have, choose a mode and quality,
hit Convert. Conversions run on a background thread so the window stays
responsive, with per-file and overall progress bars and a log.

Run:  python3 mod_convert_gui.py
Requires: Python 3.9+ with tkinter (bundled with the python.org / Homebrew
builds on macOS). ffmpeg + ffprobe are bundled into the released app; when
running from source they are taken from PATH (or SDRS50_FFMPEG_DIR).
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import mod_convert_core as core

APP_TITLE = "SDR-S50 Converter"

MODE_LABELS = {
    core.MODE_MP4: "MP4 · 50p (deinterlace, field-doubled) — recommended",
    core.MODE_MP4_25P: "MP4 · 25p (deinterlace, woven) — smaller",
    core.MODE_ARCHIVE: "Archive · lossless remux (streams untouched)",
}
ASPECT_LABELS = {
    core.ASPECT_AUTO: "Auto (from file)",
    "16:9": "Force 16:9",
    "4:3": "Force 4:3",
}
FIELD_LABELS = {
    core.FIELD_AUTO: "Auto — detect from file (recommended)",
    core.FIELD_TFF: "Top field first (TFF)",
    core.FIELD_BFF: "Bottom field first (BFF)",
}
CONTAINER_LABELS = {
    core.CONTAINER_MKV: "MKV — keeps MPEG-2 + MP2 natively (safest)",
    core.CONTAINER_MP4: "MP4 — also lossless; MP2 audio may not play in QuickTime",
}


class ConverterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(APP_TITLE)
        root.minsize(720, 560)

        # State ------------------------------------------------------------- #
        self.sources: list[str] = []
        self.mode_var = tk.StringVar(value=core.MODE_MP4)
        self.crf_var = tk.IntVar(value=20)
        self.aspect_var = tk.StringVar(value=core.ASPECT_AUTO)
        self.field_var = tk.StringVar(value=core.FIELD_AUTO)
        self.container_var = tk.StringVar(value=core.CONTAINER_MKV)
        self.outdir_var = tk.StringVar(value="")   # empty = alongside source
        self.overwrite_var = tk.BooleanVar(value=False)
        self.recursive_var = tk.BooleanVar(value=True)

        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._events: "queue.Queue[tuple]" = queue.Queue()

        self._build_ui()
        self._on_mode_change()      # apply the enable/disable rules for the default mode
        self._check_ffmpeg()
        self.root.after(100, self._drain_events)

    # ------------------------------------------------------------------ UI #
    def _build_ui(self) -> None:
        pad = dict(padx=8, pady=4)
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # --- Source list ------------------------------------------------- #
        src_frame = ttk.LabelFrame(main, text="Source files (.MOD / .TOD)")
        src_frame.grid(row=0, column=0, sticky="nsew", **pad)
        src_frame.columnconfigure(0, weight=1)
        src_frame.rowconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(src_frame, height=8, activestyle="none",
                                  selectmode="extended")
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(src_frame, orient="vertical",
                           command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=6)
        self.listbox.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(src_frame)
        btns.grid(row=0, column=2, sticky="ns", padx=6, pady=6)
        ttk.Button(btns, text="Add files…", command=self._add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Add folder…", command=self._add_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove", command=self._remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear", command=self._clear).pack(fill="x", pady=2)
        ttk.Checkbutton(btns, text="Recurse folders",
                        variable=self.recursive_var).pack(anchor="w", pady=(10, 0))

        # --- Options ----------------------------------------------------- #
        opt = ttk.LabelFrame(main, text="Options")
        opt.grid(row=1, column=0, sticky="ew", **pad)
        opt.columnconfigure(1, weight=1)

        ttk.Label(opt, text="Mode:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        mode_box = ttk.Combobox(opt, state="readonly", textvariable=self.mode_var,
                                values=[core.MODE_MP4, core.MODE_MP4_25P, core.MODE_ARCHIVE])
        mode_box.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        mode_box.configure(width=48)
        # Show friendly labels while keeping the raw value in the variable.
        mode_box["values"] = list(MODE_LABELS.values())
        mode_box.set(MODE_LABELS[core.MODE_MP4])
        self._mode_box = mode_box
        mode_box.bind("<<ComboboxSelected>>", self._on_mode_change)

        ttk.Label(opt, text="Quality (CRF):").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        q_frame = ttk.Frame(opt)
        q_frame.grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        q_frame.columnconfigure(0, weight=1)
        self.crf_scale = ttk.Scale(q_frame, from_=16, to=28, orient="horizontal",
                                   command=self._on_crf_change)
        self.crf_scale.grid(row=0, column=0, sticky="ew")
        self.crf_label = ttk.Label(q_frame, text="20  (lower = better/larger)", width=26)
        self.crf_label.grid(row=0, column=1, padx=(8, 0))
        # Erst set(), wenn crf_label existiert — set() feuert sofort _on_crf_change,
        # das auf self.crf_label zugreift (sonst AttributeError beim Start).
        self.crf_scale.set(20)

        ttk.Label(opt, text="Field order:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        fld = ttk.Combobox(opt, state="readonly", width=48,
                           values=list(FIELD_LABELS.values()))
        fld.set(FIELD_LABELS[core.FIELD_AUTO])
        fld.grid(row=2, column=1, sticky="ew", padx=6, pady=4)
        self._field_box = fld
        fld.bind("<<ComboboxSelected>>", self._on_field_change)

        ttk.Label(opt, text="Archive as:").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        cnt = ttk.Combobox(opt, state="readonly", width=48,
                           values=list(CONTAINER_LABELS.values()))
        cnt.set(CONTAINER_LABELS[core.CONTAINER_MKV])
        cnt.grid(row=3, column=1, sticky="ew", padx=6, pady=4)
        self._container_box = cnt
        cnt.bind("<<ComboboxSelected>>", self._on_container_change)

        ttk.Label(opt, text="Aspect:").grid(row=4, column=0, sticky="w", padx=6, pady=4)
        asp = ttk.Combobox(opt, state="readonly",
                           values=list(ASPECT_LABELS.values()))
        asp.set(ASPECT_LABELS[core.ASPECT_AUTO])
        asp.grid(row=4, column=1, sticky="w", padx=6, pady=4)
        self._aspect_box = asp
        asp.bind("<<ComboboxSelected>>", self._on_aspect_change)

        ttk.Label(opt, text="Output:").grid(row=5, column=0, sticky="w", padx=6, pady=4)
        out_frame = ttk.Frame(opt)
        out_frame.grid(row=5, column=1, sticky="ew", padx=6, pady=4)
        out_frame.columnconfigure(0, weight=1)
        self.out_entry = ttk.Entry(out_frame, textvariable=self.outdir_var)
        self.out_entry.grid(row=0, column=0, sticky="ew")
        self.out_entry.insert(0, "")
        ttk.Button(out_frame, text="Choose…", command=self._choose_outdir).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(out_frame, text="Beside source", command=lambda: self.outdir_var.set("")).grid(row=0, column=2, padx=(6, 0))

        ttk.Checkbutton(opt, text="Overwrite existing output files",
                        variable=self.overwrite_var).grid(row=6, column=1, sticky="w", padx=6, pady=(2, 6))

        # --- Progress + actions ----------------------------------------- #
        prog = ttk.Frame(main)
        prog.grid(row=2, column=0, sticky="ew", **pad)
        prog.columnconfigure(0, weight=1)

        self.file_progress = ttk.Progressbar(prog, mode="determinate", maximum=1.0)
        self.file_progress.grid(row=0, column=0, sticky="ew")
        self.overall_progress = ttk.Progressbar(prog, mode="determinate", maximum=1.0)
        self.overall_progress.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.status_label = ttk.Label(prog, text="Ready.")
        self.status_label.grid(row=2, column=0, sticky="w", pady=(4, 0))

        action = ttk.Frame(main)
        action.grid(row=3, column=0, sticky="ew", **pad)
        self.convert_btn = ttk.Button(action, text="Convert", command=self._start)
        self.convert_btn.pack(side="left")
        self.cancel_btn = ttk.Button(action, text="Cancel", command=self._request_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.analyze_btn = ttk.Button(action, text="Analyse selected",
                                      command=self._analyze_selected)
        self.analyze_btn.pack(side="left", padx=(8, 0))

        # --- Log --------------------------------------------------------- #
        log_frame = ttk.LabelFrame(main, text="Analysis / Log")
        log_frame.grid(row=4, column=0, sticky="nsew", **pad)
        main.rowconfigure(4, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=8, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns", pady=6)
        self.log.configure(yscrollcommand=lsb.set)

    # -------------------------------------------------------- UI callbacks #
    def _on_mode_change(self, *_evt) -> None:
        label = self._mode_box.get()
        for value, text in MODE_LABELS.items():
            if text == label:
                self.mode_var.set(value)
                break
        # In archive mode nothing is re-encoded, so quality/field order/aspect
        # are meaningless — only the container choice applies.
        archive = self.mode_var.get() == core.MODE_ARCHIVE
        self.crf_scale.configure(state="disabled" if archive else "normal")
        self._field_box.configure(state="disabled" if archive else "readonly")
        self._aspect_box.configure(state="disabled" if archive else "readonly")
        self._container_box.configure(state="readonly" if archive else "disabled")

    def _on_crf_change(self, _val: str) -> None:
        crf = int(round(float(self.crf_scale.get())))
        self.crf_var.set(crf)
        hint = {16: "near-lossless", 20: "≈ original size", 22: "smaller", 28: "low"}
        note = hint.get(crf, "")
        self.crf_label.configure(text=f"{crf}  ({note})" if note else str(crf))

    def _on_aspect_change(self, *_evt) -> None:
        label = self._aspect_box.get()
        for value, text in ASPECT_LABELS.items():
            if text == label:
                self.aspect_var.set(value)
                break

    def _on_field_change(self, *_evt) -> None:
        label = self._field_box.get()
        for value, text in FIELD_LABELS.items():
            if text == label:
                self.field_var.set(value)
                break

    def _on_container_change(self, *_evt) -> None:
        label = self._container_box.get()
        for value, text in CONTAINER_LABELS.items():
            if text == label:
                self.container_var.set(value)
                break

    def _analyze_selected(self) -> None:
        """Probe the selected files (or all of them) and print the details."""
        indices = self.listbox.curselection() or range(len(self.sources))
        targets = [self.sources[i] for i in indices]
        if not targets:
            messagebox.showinfo(APP_TITLE, "Add some .MOD / .TOD files first.")
            return
        if core.which_ffmpeg("ffprobe") is None:
            messagebox.showerror(APP_TITLE, "ffprobe not found.")
            return

        self.analyze_btn.configure(state="disabled")

        def work() -> None:
            for path in targets[:20]:      # keep the log readable
                self._post("log", core.describe(path))
                self._post("log", "")
            self._post("analysis_done", None)

        threading.Thread(target=work, daemon=True).start()

    def _choose_outdir(self) -> None:
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir_var.set(d)

    def _add_files(self) -> None:
        files = filedialog.askopenfilenames(
            title="Select .MOD / .TOD files",
            filetypes=[("Camcorder video", "*.MOD *.mod *.TOD *.tod"), ("All files", "*.*")],
        )
        self._ingest(list(files))

    def _add_folder(self) -> None:
        d = filedialog.askdirectory(title="Select a folder of .MOD / .TOD files")
        if d:
            self._ingest([d])

    def _ingest(self, paths: list[str]) -> None:
        found = core.discover_sources(paths, recursive=self.recursive_var.get())
        added = 0
        existing = set(self.sources)
        for p in found:
            if p not in existing:
                self.sources.append(p)
                self.listbox.insert("end", self._short(p))
                existing.add(p)
                added += 1
        if added:
            self._log(f"Added {added} file(s). {len(self.sources)} queued.")
        elif paths:
            self._log("No new .MOD / .TOD files found.")

    def _short(self, path: str) -> str:
        home = os.path.expanduser("~")
        return "~" + path[len(home):] if path.startswith(home) else path

    def _remove_selected(self) -> None:
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)
            del self.sources[idx]

    def _clear(self) -> None:
        self.listbox.delete(0, "end")
        self.sources.clear()

    # ---------------------------------------------------------- lifecycle #
    def _check_ffmpeg(self) -> None:
        missing = [t for t in ("ffmpeg", "ffprobe") if core.which_ffmpeg(t) is None]
        if missing:
            self._log("WARNING: " + ", ".join(missing) + " not found on PATH. "
                      "Install ffmpeg (e.g. `brew install ffmpeg`) before converting.")
            self.status_label.configure(text="ffmpeg not found — install it to convert.")

    def _current_options(self) -> core.ConvertOptions:
        return core.ConvertOptions(
            mode=self.mode_var.get(),
            crf=self.crf_var.get(),
            aspect=self.aspect_var.get(),
            output_dir=self.outdir_var.get().strip() or None,
            overwrite=self.overwrite_var.get(),
            archive_container=self.container_var.get(),
            field_order=self.field_var.get(),
        )

    def _start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        if not self.sources:
            messagebox.showinfo(APP_TITLE, "Add some .MOD / .TOD files first.")
            return
        if any(core.which_ffmpeg(t) is None for t in ("ffmpeg", "ffprobe")):
            messagebox.showerror(APP_TITLE, "ffmpeg / ffprobe not found on PATH.")
            return

        opts = self._current_options()
        sources = list(self.sources)
        self._cancel.clear()
        self.convert_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.overall_progress.configure(value=0)
        self.file_progress.configure(value=0)

        self._worker = threading.Thread(
            target=self._run_batch, args=(sources, opts), daemon=True)
        self._worker.start()

    def _request_cancel(self) -> None:
        self._cancel.set()
        self.status_label.configure(text="Cancelling…")

    # ------------------------------------------------------ worker thread #
    def _run_batch(self, sources: list[str], opts: core.ConvertOptions) -> None:
        total = len(sources)
        ok = fail = 0
        for i, src in enumerate(sources):
            if self._cancel.is_set():
                self._post("log", f"Cancelled after {i} file(s).")
                break
            self._post("status", f"[{i + 1}/{total}] {os.path.basename(src)}")
            self._post("log", f"[{i + 1}/{total}] {os.path.basename(src)}")
            self._post("file_progress", 0.0)

            def prog(frac: float, base=i, n=total) -> None:
                self._post("file_progress", frac)
                self._post("overall_progress", (base + frac) / n)

            result = core.convert_file(
                src, opts,
                on_progress=prog,
                on_log=lambda m: self._post("log", m),
                should_cancel=self._cancel.is_set,
            )
            if result.ok:
                ok += 1
                self._post("log", f"  ✓ {result.message}")
            else:
                fail += 1
                self._post("log", f"  ✗ {result.message}")
            self._post("overall_progress", (i + 1) / total)

        self._post("done", (ok, fail))

    # ---------------------------------------------------- event pump (UI) #
    def _post(self, kind: str, payload) -> None:
        self._events.put((kind, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self.status_label.configure(text=payload)
                elif kind == "file_progress":
                    self.file_progress.configure(value=payload)
                elif kind == "overall_progress":
                    self.overall_progress.configure(value=payload)
                elif kind == "analysis_done":
                    self.analyze_btn.configure(state="normal")
                elif kind == "done":
                    self._on_done(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _on_done(self, ok: int, fail: int) -> None:
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.file_progress.configure(value=0)
        msg = f"Done — {ok} converted"
        if fail:
            msg += f", {fail} failed/skipped"
        self.status_label.configure(text=msg + ".")
        self._log(msg + ".")

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    root = tk.Tk()
    try:
        # Slightly nicer defaults where the platform theme allows it.
        style = ttk.Style(root)
        if "aqua" in style.theme_names():
            style.theme_use("aqua")
    except tk.TclError:
        pass
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
