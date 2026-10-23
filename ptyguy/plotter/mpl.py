"""Live matplotlib view of the reconstruction jobs.

    python -m ptyguy watch examples/launcher.yaml

Keys:  left/right  older / newer job (stops following)
       f           follow the newest running job again
       w           toggle phase display: offset removed (linear) / wrapped (+-pi)
Artists are created once and updated in place (set_data), so redraws are cheap.
"""
import time

import numpy as np

from .model import JobReader, display_phase, list_jobs, pick_follow


class Watcher:
    def __init__(self, analysis_root, poll_s=1.0, plt=None):
        if plt is None:
            import matplotlib.pyplot as plt
        self.plt = plt
        self.root = analysis_root
        self.poll_s = poll_s
        self.follow = True
        self.wrapped = False
        self.jobs = []
        self.reader = None

        self.fig = plt.figure(figsize=(15, 5.2))
        gs = self.fig.add_gridspec(1, 4, width_ratios=[1, 1, 1, 1.15], left=0.03, right=0.98,
                                   bottom=0.12, top=0.78, wspace=0.25)
        self.ax = [self.fig.add_subplot(gs[0, i]) for i in range(4)]
        blank = np.zeros((2, 2))
        self.im_phase = self.ax[0].imshow(blank, cmap="RdBu_r")
        self.cb_phase = self.fig.colorbar(self.im_phase, ax=self.ax[0], fraction=0.046, pad=0.02)
        self.im_amp = self.ax[1].imshow(blank, cmap="gray")
        self.im_probe = self.ax[2].imshow(blank, cmap="magma")
        for a, title in zip(self.ax[:3], ["object phase", "object |amplitude|", "probe |amplitude|"]):
            a.set_title(title, fontsize=10)
            a.set_xticks([])
            a.set_yticks([])
        (self.err_line,) = self.ax[3].semilogy([1], [1], lw=1.5)
        self.ax[3].set_title("error", fontsize=10)
        self.ax[3].set_xlabel("iteration")
        self.ax[3].grid(alpha=0.3, which="both")
        self.header = self.fig.text(0.03, 0.93, "", fontsize=12, weight="bold")
        self.notes = self.fig.text(0.03, 0.87, "", fontsize=9, color="#b35900")
        self.footer = self.fig.text(0.03, 0.03, "", fontsize=9, color="#555555")
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    # ------------------------------------------------------------------
    def refresh(self):
        """Poll the folders and update the figure. Called by the timer."""
        self.jobs = list_jobs(self.root)
        if self.follow:
            target = pick_follow(self.jobs)
            if target is not None and (self.reader is None or
                                       self.reader.summary.name != target.name):
                self.reader = JobReader(target)
        if self.reader is None:
            self.header.set_text("waiting for jobs in %s" % self.root)
            self._footer()
            self.fig.canvas.draw_idle()
            return None

        view = self.reader.read()
        self._draw(view)
        self._footer()
        self.fig.canvas.draw_idle()
        return view

    def _draw(self, view):
        st, pr = view.status, view.progress or {}
        parts = ["scan %s" % view.scan_number, st.get("recipe", "?"),
                 st.get("state", "?").upper()]
        if pr.get("iteration") is not None:
            total = pr.get("iterations")
            parts.append("iteration %s%s" % (pr["iteration"], "/%s" % total if total else ""))
        started, ended = st.get("started"), st.get("ended")
        if started:
            parts.append("%.0f s" % ((ended or time.time()) - started))
        if view.arrays is not None:
            parts.append(view.arrays.source)
        self.header.set_text("  ·  ".join(parts))
        self.notes.set_text(" | ".join(view.notes)[:250])

        a = view.arrays
        if a is None:
            return
        phase, lim = display_phase(a.obj, wrapped=self.wrapped)
        self.im_phase.set_data(phase)
        self.im_phase.set_cmap("twilight" if self.wrapped else "RdBu_r")
        self.im_phase.set_clim(-lim, lim)
        self.im_phase.set_extent((0, phase.shape[1], phase.shape[0], 0))
        self.ax[0].set_title("object phase (%s)" % ("wrapped, ±π" if self.wrapped
                                                    else "offset removed"), fontsize=10)
        amp = np.abs(a.obj)
        self.im_amp.set_data(amp)
        self.im_amp.set_clim(*np.percentile(amp, [0.5, 99.5]))
        self.im_amp.set_extent((0, amp.shape[1], amp.shape[0], 0))
        if a.probe is not None:
            p = np.abs(a.probe if a.probe.ndim == 2 else a.probe[0])   # first mode
            self.im_probe.set_data(p)
            self.im_probe.set_clim(0, p.max() or 1)
            self.im_probe.set_extent((0, p.shape[1], p.shape[0], 0))
        if len(a.error):
            it = np.arange(1, len(a.error) + 1)
            self.err_line.set_data(it, a.error)
            self.ax[3].set_xlim(0.5, max(len(a.error), 2) + 0.5)
            pos = a.error[a.error > 0]
            if len(pos):
                self.ax[3].set_ylim(pos.min() / 1.5, pos.max() * 1.5)

    def _footer(self):
        names = []
        current = self.reader.summary.name if self.reader else None
        for j in self.jobs[:8]:
            label = "%s %s" % (j.name.split("_")[1] if "_" in j.name else j.name, j.state)
            names.append("[%s]" % label if j.name == current else label)
        mode = "following newest" if self.follow else "pinned (f: follow)"
        self.footer.set_text("jobs (←/→): " + "   ".join(names) + "      %s · w: phase mode" % mode)

    # ------------------------------------------------------------------
    def _on_key(self, event):
        if event.key in ("left", "right") and self.jobs:
            names = [j.name for j in self.jobs]
            i = names.index(self.reader.summary.name) if self.reader and \
                self.reader.summary.name in names else 0
            i = min(i + 1, len(names) - 1) if event.key == "left" else max(i - 1, 0)
            self.reader = JobReader(self.jobs[i])     # left = older (list is newest first)
            self.follow = False
        elif event.key == "f":
            self.follow = True
        elif event.key == "w":
            self.wrapped = not self.wrapped
        else:
            return
        self.refresh()

    def run(self):
        timer = self.fig.canvas.new_timer(interval=int(self.poll_s * 1000))
        timer.add_callback(self.refresh)
        self.refresh()
        timer.start()
        self.plt.show()
