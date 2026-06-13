"""
app.py — PFD Ground Station: a desktop tool to build, deploy and publish the
flight data the PFD/AHRS consumes.

One card per data product (nav data, airspace, airports, obstacles, terrain,
water).  Pick the source files, BUILD the cache into a workspace, DEPLOY it into
a connected checkout's device data/ dirs, and (for nav data) PUBLISH it to the
GitHub release the on-device NAV DATA screen downloads from.

Run in dev:   python3 -m ground_station.app
Packaged:     see ground_station/README.md (PyInstaller one-file build).
"""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import paths
from . import datasets as ds_mod
from . import publish as pub_mod

_STATE_PATH = os.path.expanduser("~/.pfd_ground_station.json")


class GroundStation:
    def __init__(self, root):
        self.root = root
        root.title("PFD Ground Station")
        root.geometry("760x820")
        self.q = queue.Queue()
        self.busy = False
        self.state = self._load_state()
        self.input_vars = {}      # (dataset_key, input_key) -> tk.StringVar
        self.status_lbls = {}     # dataset_key -> ttk.Label
        self.action_btns = []     # all build/deploy/publish buttons (to disable)
        self.dev_vars = {}        # device_name -> tk.BooleanVar

        self._build_ui()
        self._refresh_all_status()
        self.root.after(100, self._drain)

    # ── UI construction ────────────────────────────────────────────────────────
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        hdr = ttk.Frame(self.root, padding=(12, 10))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="PFD Ground Station",
                  font=("TkDefaultFont", 15, "bold")).pack(side="left")
        ttk.Label(hdr, text=f"workspace: {paths.WORKSPACE}",
                  foreground="#666").pack(side="right")

        # Deploy targets + GitHub token.
        cfg = ttk.LabelFrame(self.root, text="Targets", padding=8)
        cfg.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(cfg, text="Deploy to:").grid(row=0, column=0, sticky="w")
        col = 1
        for dev in paths.device_data_dirs():
            var = tk.BooleanVar(value=self.state.get("deploy", {}).get(dev, True))
            self.dev_vars[dev] = var
            ttk.Checkbutton(cfg, text=dev, variable=var).grid(
                row=0, column=col, sticky="w", padx=4)
            col += 1
        ttk.Label(cfg, text="GitHub token:").grid(row=1, column=0, sticky="w",
                                                  pady=(6, 0))
        self.token_var = tk.StringVar(value=self.state.get("token", ""))
        ttk.Entry(cfg, textvariable=self.token_var, show="•", width=48).grid(
            row=1, column=1, columnspan=col, sticky="we", pady=(6, 0))
        ttk.Label(cfg, text="(repo / Contents:write scope — only needed to publish)",
                  foreground="#888").grid(row=2, column=1, columnspan=col,
                                          sticky="w")
        cfg.columnconfigure(col, weight=1)

        # Scrollable list of dataset cards.
        wrap = ttk.Frame(self.root)
        wrap.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(wrap, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        cards = ttk.Frame(canvas)
        cards.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=cards, anchor="nw", width=720)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for d in ds_mod.DATASETS:
            self._card(cards, d)

        # Log pane.
        logf = ttk.LabelFrame(self.root, text="Log", padding=4)
        logf.pack(fill="both", expand=False, padx=12, pady=8)
        self.log = tk.Text(logf, height=10, wrap="word", state="disabled",
                           bg="#0d1117", fg="#c9d1d9", font=("TkFixedFont", 9))
        lsb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    def _card(self, parent, d):
        f = ttk.LabelFrame(parent, text=d.label, padding=8)
        f.pack(fill="x", pady=6)
        ttk.Label(f, text=d.blurb, foreground="#555").grid(
            row=0, column=0, columnspan=3, sticky="w")
        status = ttk.Label(f, text="…", foreground="#0a7", font=("TkDefaultFont", 9, "bold"))
        status.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 4))
        self.status_lbls[d.key] = status

        r = 2
        for inp in d.inputs:
            var = tk.StringVar(value=self.state.get("inputs", {}).get(
                f"{d.key}.{inp.key}", ""))
            self.input_vars[(d.key, inp.key)] = var
            lbl = inp.label + ("" if not inp.optional else "  (optional)")
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky="w")
            ttk.Entry(f, textvariable=var, width=44).grid(
                row=r, column=1, sticky="we", padx=4)
            if inp.kind in ("dir", "file"):
                ttk.Button(f, text="Browse…",
                           command=lambda i=inp, v=var: self._browse(i, v)).grid(
                    row=r, column=2)
            if inp.hint:
                r += 1
                ttk.Label(f, text=inp.hint, foreground="#999").grid(
                    row=r, column=1, sticky="w", padx=4)
            r += 1
        f.columnconfigure(1, weight=1)

        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        b_build = ttk.Button(btns, text="Build",
                             command=lambda x=d: self._build(x))
        b_build.pack(side="left")
        b_deploy = ttk.Button(btns, text="Deploy",
                              command=lambda x=d: self._deploy(x))
        b_deploy.pack(side="left", padx=4)
        self.action_btns += [b_build, b_deploy]
        if d.publish:
            b_pub = ttk.Button(btns, text="Publish →GitHub",
                               command=lambda x=d: self._publish(x))
            b_pub.pack(side="left", padx=4)
            self.action_btns.append(b_pub)

    # ── pickers ─────────────────────────────────────────────────────────────────
    def _browse(self, inp, var):
        init = var.get() or os.path.expanduser("~")
        init = init if os.path.exists(init) else os.path.expanduser("~")
        if inp.kind == "dir":
            path = filedialog.askdirectory(initialdir=init, title=inp.label)
        else:
            path = filedialog.askopenfilename(
                initialdir=init, title=inp.label,
                filetypes=(inp.patterns or [("All files", "*")]))
        if path:
            var.set(path)

    # ── worker plumbing ─────────────────────────────────────────────────────────
    def _logline(self, s):
        self.q.put(("log", s))

    def _run(self, title, fn):
        if self.busy:
            return
        self.busy = True
        for b in self.action_btns:
            b.configure(state="disabled")
        self._logline(f"━━ {title} ━━")

        def worker():
            try:
                fn(self._logline)
                self.q.put(("done", None))
            except Exception as exc:                # surface, keep app alive
                self.q.put(("log", f"ERROR: {exc}"))
                self.q.put(("done", None))

        threading.Thread(target=worker, daemon=True, name="GSWorker").start()

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "done":
                    self.busy = False
                    for b in self.action_btns:
                        b.configure(state="normal")
                    self._refresh_all_status()
                    self._save_state()
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    # ── actions ─────────────────────────────────────────────────────────────────
    def _gather_inputs(self, d):
        out = {}
        for inp in d.inputs:
            val = self.input_vars[(d.key, inp.key)].get().strip()
            if val:
                out[inp.key] = val
        return out

    def _build(self, d):
        inputs = self._gather_inputs(d)
        self._run(f"Build {d.label}", lambda log: d.build(inputs, log))

    def _deploy(self, d):
        targets = {dev: path for dev, path in paths.device_data_dirs().items()
                   if self.dev_vars.get(dev) and self.dev_vars[dev].get()}
        if not targets:
            messagebox.showwarning("Deploy", "No deploy targets selected.")
            return

        def do(log):
            for dev, path in targets.items():
                d.deploy(path, log)
        self._run(f"Deploy {d.label}", do)

    def _publish(self, d):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("Publish", "Enter a GitHub token first.")
            return
        files = d.product_paths()
        if not files:
            messagebox.showwarning("Publish", f"Build {d.label} first.")
            return
        p = d.publish

        def do(log):
            pub_mod.publish(files, p["owner"], p["repo"], p["tag"], token, log)
        self._run(f"Publish {d.label}", do)

    # ── status + state ──────────────────────────────────────────────────────────
    def _refresh_all_status(self):
        for d in ds_mod.DATASETS:
            try:
                txt = d.status()
            except Exception as exc:
                txt = f"status error: {exc}"
            lbl = self.status_lbls.get(d.key)
            if lbl:
                built = d.is_built()
                lbl.configure(text=txt,
                              foreground="#0a7" if built else "#b80")

    def _load_state(self):
        try:
            with open(_STATE_PATH) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_state(self):
        state = {
            "token": self.token_var.get(),
            "deploy": {dev: var.get() for dev, var in self.dev_vars.items()},
            "inputs": {f"{k[0]}.{k[1]}": v.get()
                       for k, v in self.input_vars.items() if v.get()},
        }
        try:
            with open(_STATE_PATH, "w") as fh:
                json.dump(state, fh, indent=2)
        except Exception:
            pass


def main():
    root = tk.Tk()
    GroundStation(root)
    root.mainloop()


if __name__ == "__main__":
    main()
