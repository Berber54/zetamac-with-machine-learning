"""
Zetamac Trainer — an adaptive mental-math drill in the style of zetamac.com.

Run with:
    python zetamac.py

Dependency (documented in README.md):
    pip install customtkinter

Game rules (fixed):
    Addition        a + b           a, b in [2, 100]
    Subtraction     a - b           a, b in [2, 100], shown larger - smaller
    Multiplication  a x b           a in [1, 12], b in [2, 100]
    Division        dividend / d    d in [1, 12], dividend in [2, 100], exact
All answers are non-negative integers.  Typing the correct answer advances
immediately — no Enter needed.  Sessions are timed (default 120 s) or endless.

Adaptivity:
    Every solve time is attributed to each meaningful number in the problem,
    keyed by (operation, number) — for division the divisor and the quotient,
    for everything else both displayed operands.  An exponential moving
    average per (operation, number) is blended toward a prior for low sample
    counts, and both the operation and each individual number of the next
    problem are sampled with weight  WEIGHT_BASELINE + slowness ** WEIGHT_EXPONENT,
    so slow numbers and slow operations appear more often while everything
    keeps a non-zero chance of showing up.

Tunable constants (see below):
    DEFAULT_TIME    prior solve time (s) assumed for unseen (op, number) pairs
    EMA_ALPHA       smoothing factor of the per-number EMA (higher = reacts faster)
    PRIOR_STRENGTH  pseudo-samples of the prior blended into each EMA
    WEIGHT_BASELINE additive weight floor — keeps every number/op in rotation
    WEIGHT_EXPONENT exponent > 1 — makes genuinely slow items appear much more

High scores:
    The best score (problems solved) is kept per session length and shown on
    the main screen and in the stats window.  Only timed sessions whose timer
    ran out count — stopping early or playing endless never sets a record.

Files (created next to this script):
    zetamac_stats.json   per-(operation, number) statistics and high scores
    zetamac_log.csv      one row per solved problem
Old-format files from the previous version are renamed to *_old.* on first run.
"""

import csv
import json
import math
import os
import random
import time
from dataclasses import dataclass

import tkinter as tk
import customtkinter as ctk

# --- files -------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(HERE, "zetamac_stats.json")
LOG_FILE = os.path.join(HERE, "zetamac_log.csv")

STATS_VERSION = 2
LOG_HEADER = ["timestamp", "operation", "left", "right", "answer", "seconds"]

OPS = ["add", "sub", "mul", "div"]
OP_LABELS = {"add": "Addition", "sub": "Subtraction",
             "mul": "Multiplication", "div": "Division"}
OP_SYMBOLS = {"add": "+", "sub": "−", "mul": "×", "div": "÷"}

# --- adaptivity tunables -----------------------------------------------------
DEFAULT_TIME = 3.0      # assumed solve time (s) for a number with no data yet
EMA_ALPHA = 0.3         # smoothing factor of the solve-time EMA
PRIOR_STRENGTH = 3.0    # pseudo-samples of DEFAULT_TIME blended into each EMA
WEIGHT_BASELINE = 0.4   # weight floor so everything keeps appearing
WEIGHT_EXPONENT = 1.5   # >1 amplifies slow numbers/operations

# --- session defaults --------------------------------------------------------
DEFAULT_DURATION = 120  # seconds


# ==============================================================================
# Problems
# ==============================================================================
@dataclass(frozen=True)
class Problem:
    op: str
    left: int      # displayed left number (dividend for division)
    right: int     # displayed right number (divisor for division)
    answer: int
    tracked: tuple # numbers the solve time is attributed to

    @property
    def text(self):
        return f"{self.left} {OP_SYMBOLS[self.op]} {self.right}"


# ==============================================================================
# Per-(operation, number) statistics
# ==============================================================================
def blank_stats():
    return {
        "version": STATS_VERSION,
        "ops": {op: {"count": 0, "sum": 0.0} for op in OPS},
        "numbers": {op: {} for op in OPS},
        # timed-session high scores: duration in seconds -> {score, date};
        # only sessions whose timer ran out count (not stopped early/endless)
        "best": {},
    }


def load_stats():
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return blank_stats()
    if not isinstance(data, dict) or data.get("version") != STATS_VERSION:
        _archive(STATS_FILE)  # old per-operation-only schema — start fresh
        return blank_stats()
    stats = blank_stats()
    for op in OPS:
        rec = data.get("ops", {}).get(op, {})
        stats["ops"][op]["count"] = int(rec.get("count", 0))
        stats["ops"][op]["sum"] = float(rec.get("sum", 0.0))
        for key, r in data.get("numbers", {}).get(op, {}).items():
            try:
                n = int(key)  # JSON keys are strings
            except ValueError:
                continue
            stats["numbers"][op][str(n)] = {
                "count": int(r.get("count", 0)),
                "sum": float(r.get("sum", 0.0)),
                "ema": None if r.get("ema") is None else float(r["ema"]),
            }
    for key, rec in data.get("best", {}).items():
        try:
            stats["best"][str(int(key))] = {"score": int(rec["score"]),
                                            "date": str(rec.get("date", ""))}
        except (TypeError, ValueError, KeyError):
            continue
    return stats


def save_stats(stats):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1)


def _archive(path):
    """Rename an incompatible old-format file to <name>_old.<ext>."""
    if not os.path.exists(path):
        return
    base, ext = os.path.splitext(path)
    target = f"{base}_old{ext}"
    n = 1
    while os.path.exists(target):
        target = f"{base}_old{n}{ext}"
        n += 1
    os.replace(path, target)


def record_solve(stats, problem, seconds):
    """Attribute one solve time to the operation and each tracked number."""
    op_rec = stats["ops"][problem.op]
    op_rec["count"] += 1
    op_rec["sum"] += seconds
    for n in problem.tracked:
        rec = stats["numbers"][problem.op].setdefault(
            str(n), {"count": 0, "sum": 0.0, "ema": None})
        rec["count"] += 1
        rec["sum"] += seconds
        rec["ema"] = seconds if rec["ema"] is None else \
            EMA_ALPHA * seconds + (1 - EMA_ALPHA) * rec["ema"]


def _blended_time(rec):
    """EMA blended toward the prior — hugs DEFAULT_TIME at low sample counts."""
    if not rec or rec["count"] <= 0 or rec.get("ema") is None:
        return DEFAULT_TIME
    trust = rec["count"] / (rec["count"] + PRIOR_STRENGTH)
    return trust * rec["ema"] + (1 - trust) * DEFAULT_TIME


def slowness(stats, op, number):
    return _blended_time(stats["numbers"][op].get(str(number)))


def _weight(slow):
    return WEIGHT_BASELINE + slow ** WEIGHT_EXPONENT


def weighted_number(stats, op, lo, hi):
    """Sample a number in [lo, hi], biased toward this op's slow numbers."""
    candidates = range(lo, hi + 1)
    weights = [_weight(slowness(stats, op, n)) for n in candidates]
    return random.choices(candidates, weights=weights)[0]


def op_slowness(stats, op):
    """Aggregate slowness of an operation: mean over its recorded numbers."""
    recs = stats["numbers"][op]
    if not recs:
        return DEFAULT_TIME
    vals = [_blended_time(r) for r in recs.values()]
    return sum(vals) / len(vals)


def op_weights(stats):
    return {op: _weight(op_slowness(stats, op)) for op in OPS}


def choose_op(stats):
    w = op_weights(stats)
    return random.choices(OPS, weights=[w[op] for op in OPS])[0]


def generate_problem(stats, op=None):
    """Generate the next problem with weighted operation and number choice."""
    if op is None:
        op = choose_op(stats)
    if op == "add":
        a = weighted_number(stats, "add", 2, 100)
        b = weighted_number(stats, "add", 2, 100)
        return Problem("add", a, b, a + b, (a, b))
    if op == "sub":
        a = weighted_number(stats, "sub", 2, 100)
        b = weighted_number(stats, "sub", 2, 100)
        hi, lo = max(a, b), min(a, b)   # larger - smaller, never negative
        return Problem("sub", hi, lo, hi - lo, (hi, lo))
    if op == "mul":
        a = weighted_number(stats, "mul", 1, 12)
        b = weighted_number(stats, "mul", 2, 100)
        return Problem("mul", a, b, a * b, (a, b))
    if op == "div":
        divisor = weighted_number(stats, "div", 1, 12)
        q_lo = math.ceil(2 / divisor)     # keep dividend >= 2
        q_hi = 100 // divisor             # keep dividend <= 100
        quotient = weighted_number(stats, "div", q_lo, q_hi)
        dividend = divisor * quotient
        return Problem("div", dividend, divisor, quotient, (divisor, quotient))
    raise ValueError(op)


# ==============================================================================
# CSV log
# ==============================================================================
def ensure_log_compatible():
    """Archive a log written by the old version (different columns)."""
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            header = f.readline().strip().split(",")
    except FileNotFoundError:
        return
    if header != LOG_HEADER:
        _archive(LOG_FILE)


def log_solve(problem, seconds):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(LOG_HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), problem.op,
                    problem.left, problem.right, problem.answer,
                    f"{seconds:.3f}"])


# ==============================================================================
# Theme (dark)
# ==============================================================================
COL_BG        = "#0d0d0d"   # window plane
COL_SURFACE   = "#1a1a19"   # cards / chart surface
COL_SURFACE_2 = "#242423"   # raised controls
COL_BORDER    = "#2c2c2a"
COL_TEXT      = "#ffffff"
COL_TEXT_2    = "#c3c2b7"
COL_MUTED     = "#898781"
COL_ACCENT    = "#3987e5"
COL_ACCENT_2  = "#256abf"   # hover
COL_CRITICAL  = "#d03b3b"   # timer in the final seconds

FONT = "Segoe UI"

# Sequential blue ramp (light -> dark).  On this dark surface the ramp is
# flipped: fast times recede into the surface (dark end), slow times pop
# bright (light end) so weak spots draw the eye.
SEQ_RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
            "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
            "#0d366b"]
HEAT_FAST_S = 1.0   # solve time mapped to the "fast" end of the ramp
HEAT_SLOW_S = 6.0   # solve time mapped to the "slow" end of the ramp
CELL_EMPTY = "#232322"      # numbers with no data yet


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(c)) for c in rgb)


def heat_color(seconds):
    """Map a solve time onto the (dark-flipped) sequential ramp."""
    t = (seconds - HEAT_FAST_S) / (HEAT_SLOW_S - HEAT_FAST_S)
    t = min(1.0, max(0.0, t))
    pos = (1.0 - t) * (len(SEQ_RAMP) - 1)   # slow -> index 0 (bright)
    i = int(pos)
    j = min(i + 1, len(SEQ_RAMP) - 1)
    frac = pos - i
    c1, c2 = _hex_to_rgb(SEQ_RAMP[i]), _hex_to_rgb(SEQ_RAMP[j])
    return _rgb_to_hex(tuple(a + (b - a) * frac for a, b in zip(c1, c2)))


def ink_for(fill):
    """Dark or light text so the cell label stays readable on its fill."""
    r, g, b = _hex_to_rgb(fill)
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#0b0b0b" if lum > 140 else "#ffffff"


def heat_domain(op):
    """Numbers that can appear under an operation (for the stats grid)."""
    return range(2, 101) if op in ("add", "sub") else range(1, 101)


def fmt_mmss(seconds):
    seconds = max(0, int(math.ceil(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ==============================================================================
# Stats window
# ==============================================================================
class StatsWindow(ctk.CTkToplevel):
    def __init__(self, master, stats):
        super().__init__(master, fg_color=COL_BG)
        self.stats = stats
        self.title("Zetamac Trainer — Stats")
        self.geometry("1040x780")
        self.minsize(860, 640)
        self._hover_cell = None

        pad = {"padx": 24}

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", pady=(20, 12), **pad)
        ctk.CTkLabel(head, text="Performance",
                     font=ctk.CTkFont(FONT, 26, "bold"),
                     text_color=COL_TEXT).pack(side="left")
        self.overall_label = ctk.CTkLabel(head, text="",
                                          font=ctk.CTkFont(FONT, 15),
                                          text_color=COL_TEXT_2)
        self.overall_label.pack(side="right")

        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.pack(fill="x", **pad)
        self.card_labels = {}
        for i, op in enumerate(OPS):
            card = ctk.CTkFrame(cards, fg_color=COL_SURFACE, corner_radius=12)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else 10, 0))
            cards.grid_columnconfigure(i, weight=1)
            ctk.CTkLabel(card, text=OP_LABELS[op],
                         font=ctk.CTkFont(FONT, 13, "bold"),
                         text_color=COL_MUTED).pack(anchor="w",
                                                    padx=16, pady=(12, 0))
            big = ctk.CTkLabel(card, text="—",
                               font=ctk.CTkFont(FONT, 26, "bold"),
                               text_color=COL_TEXT)
            big.pack(anchor="w", padx=16)
            small = ctk.CTkLabel(card, text="", font=ctk.CTkFont(FONT, 12),
                                 text_color=COL_TEXT_2)
            small.pack(anchor="w", padx=16, pady=(0, 12))
            self.card_labels[op] = (big, small)

        picker_row = ctk.CTkFrame(self, fg_color="transparent")
        picker_row.pack(fill="x", pady=(18, 10), **pad)
        self.op_picker = ctk.CTkSegmentedButton(
            picker_row, values=[OP_LABELS[op] for op in OPS],
            command=lambda _v: self.refresh(),
            font=ctk.CTkFont(FONT, 13),
            fg_color=COL_SURFACE, unselected_color=COL_SURFACE,
            unselected_hover_color=COL_SURFACE_2,
            selected_color=COL_ACCENT, selected_hover_color=COL_ACCENT_2)
        self.op_picker.set(OP_LABELS["add"])
        self.op_picker.pack(side="left")
        self.hover_label = ctk.CTkLabel(picker_row, text="",
                                        font=ctk.CTkFont(FONT, 13),
                                        text_color=COL_TEXT_2)
        self.hover_label.pack(side="right")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, pady=(0, 20), **pad)

        grid_card = ctk.CTkFrame(body, fg_color=COL_SURFACE, corner_radius=12)
        grid_card.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(grid_card, bg=COL_SURFACE,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=14, pady=14)
        self.canvas.bind("<Configure>", lambda _e: self._draw_grid())
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))

        side = ctk.CTkFrame(body, fg_color=COL_SURFACE, corner_radius=12,
                            width=250)
        side.pack(side="left", fill="y", padx=(14, 0))
        side.pack_propagate(False)
        ctk.CTkLabel(side, text="Slowest numbers",
                     font=ctk.CTkFont(FONT, 15, "bold"),
                     text_color=COL_TEXT).pack(anchor="w", padx=18,
                                               pady=(16, 6))
        self.slow_label = ctk.CTkLabel(side, text="", justify="left",
                                       font=ctk.CTkFont("Consolas", 14),
                                       text_color=COL_TEXT_2)
        self.slow_label.pack(anchor="nw", padx=18)

        self.refresh()

    # ---- data helpers ----
    def _selected_op(self):
        label = self.op_picker.get()
        return next(op for op in OPS if OP_LABELS[op] == label)

    def _avg(self, rec):
        return rec["sum"] / rec["count"] if rec and rec["count"] else None

    # ---- refresh everything ----
    def refresh(self):
        total = sum(self.stats["ops"][op]["count"] for op in OPS)
        total_t = sum(self.stats["ops"][op]["sum"] for op in OPS)
        line = (f"{total} problems solved  ·  {total_t / total:.2f}s average"
                if total else "no problems solved yet")
        bests = sorted(((int(d), rec) for d, rec in
                        self.stats["best"].items()), reverse=True)
        if bests:
            line += "\nbest score: " + "  ·  ".join(
                f"{rec['score']} in {fmt_mmss(d)}" for d, rec in bests[:4])
        self.overall_label.configure(text=line, justify="right")

        weights = op_weights(self.stats)
        wsum = sum(weights.values())
        for op in OPS:
            rec = self.stats["ops"][op]
            avg = self._avg(rec)
            big, small = self.card_labels[op]
            big.configure(text=f"{avg:.2f}s" if avg is not None else "—")
            small.configure(text=f"{rec['count']} solved  ·  appears "
                                 f"{100 * weights[op] / wsum:.0f}%")

        op = self._selected_op()
        rows = []
        for key, rec in self.stats["numbers"][op].items():
            avg = self._avg(rec)
            if avg is not None:
                # rank by blended slowness (what drives selection) so
                # single-sample outliers don't crowd out proven weak spots
                rows.append((_blended_time(rec), avg, int(key), rec["count"]))
        rows.sort(reverse=True)
        lines = [f"{n:>3}   {avg:5.2f}s  ×{cnt}"
                 for _blend, avg, n, cnt in rows[:14]]
        self.slow_label.configure(
            text="\n".join(lines) if lines else "no data yet —\nplay a round!")

        self._draw_grid()

    # ---- heatmap ----
    def _draw_grid(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 60 or h < 60:
            return
        op = self._selected_op()
        numbers = list(heat_domain(op))

        legend_h = 44
        cols = 10
        rows = 10
        gap = 2
        cell_w = (w - gap * (cols - 1)) / cols
        cell_h = (h - legend_h - gap * (rows - 1)) / rows
        font_size = max(9, min(15, int(min(cell_w, cell_h) * 0.30)))

        self._cells = {}
        for n in numbers:
            row, col = (n - 1) // 10, (n - 1) % 10  # decade rows: 1–10, 11–20…
            x0 = col * (cell_w + gap)
            y0 = row * (cell_h + gap)
            rec = self.stats["numbers"][op].get(str(n))
            avg = self._avg(rec)
            fill = heat_color(avg) if avg is not None else CELL_EMPTY
            ink = ink_for(fill) if avg is not None else COL_MUTED
            c.create_rectangle(x0, y0, x0 + cell_w, y0 + cell_h,
                               fill=fill, width=0)
            c.create_text(x0 + cell_w / 2, y0 + cell_h / 2, text=str(n),
                          fill=ink, font=(FONT, font_size, "bold"))
            self._cells[n] = (x0, y0, x0 + cell_w, y0 + cell_h, avg,
                              rec["count"] if rec else 0)

        # legend: fast (recedes) -> slow (bright)
        ly = h - legend_h + 18
        lx, lw = 0, 170
        steps = 40
        for i in range(steps):
            t = HEAT_FAST_S + (HEAT_SLOW_S - HEAT_FAST_S) * i / (steps - 1)
            c.create_rectangle(lx + lw * i / steps, ly,
                               lx + lw * (i + 1) / steps, ly + 10,
                               fill=heat_color(t), width=0)
        c.create_text(lx, ly - 9, anchor="w", fill=COL_MUTED,
                      text=f"fast  ≤{HEAT_FAST_S:.0f}s", font=(FONT, 10))
        c.create_text(lx + lw, ly - 9, anchor="e", fill=COL_MUTED,
                      text=f"≥{HEAT_SLOW_S:.0f}s  slow", font=(FONT, 10))
        c.create_text(w, ly + 5, anchor="e", fill=COL_MUTED,
                      text="grey = not seen yet  ·  hover a cell for details",
                      font=(FONT, 10))

    def _on_hover(self, event):
        hit = None
        for n, (x0, y0, x1, y1, _avg, _cnt) in getattr(self, "_cells",
                                                       {}).items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                hit = n
                break
        self._set_hover(hit)

    def _set_hover(self, n):
        if n == self._hover_cell:
            return
        self.canvas.delete("hover")
        self._hover_cell = n
        if n is None:
            self.hover_label.configure(text="")
            return
        x0, y0, x1, y1, avg, cnt = self._cells[n]
        self.canvas.create_rectangle(x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                                     outline=COL_TEXT, width=2, tags="hover")
        op = self._selected_op()
        if avg is None:
            info = f"{n}  ·  not seen yet under {OP_LABELS[op].lower()}"
        else:
            info = (f"{n}  ·  {OP_LABELS[op].lower()}  ·  avg {avg:.2f}s "
                    f"over {cnt} solves")
        self.hover_label.configure(text=info)


# ==============================================================================
# Main app
# ==============================================================================
class ZetamacApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=COL_BG)
        self.stats = load_stats()
        ensure_log_compatible()

        self.running = False
        self.session_end = None        # monotonic deadline, None = endless
        self.session_start = None
        self.session_duration = None   # seconds, None = endless
        self.problem = None
        self.problem_start = None
        self.correct_count = 0
        self.session_time_sum = 0.0
        self.stats_window = None
        self._tick_job = None

        self.title("Zetamac Trainer")
        self.geometry("900x640")
        self.minsize(760, 560)

        self._build()
        self._show_idle()
        self.duration_var.trace_add("write", self._update_best_label)
        self._update_best_label()

        self.bind("<Return>", self._on_return)
        self.bind("<Escape>", lambda _e: self.stop() if self.running else None)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ----
    def _build(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(22, 0))

        ctk.CTkLabel(head, text="Zetamac Trainer",
                     font=ctk.CTkFont(FONT, 18, "bold"),
                     text_color=COL_TEXT_2).pack(side="left")

        self.start_btn = ctk.CTkButton(
            head, text="Start", width=110, height=36, corner_radius=18,
            font=ctk.CTkFont(FONT, 14, "bold"),
            fg_color=COL_ACCENT, hover_color=COL_ACCENT_2,
            command=self.start)
        self.start_btn.pack(side="right")

        self.stats_btn = ctk.CTkButton(
            head, text="Stats", width=84, height=36, corner_radius=18,
            font=ctk.CTkFont(FONT, 14),
            fg_color=COL_SURFACE_2, hover_color=COL_BORDER,
            text_color=COL_TEXT_2, command=self.open_stats)
        self.stats_btn.pack(side="right", padx=(0, 10))

        self.endless_var = tk.BooleanVar(value=False)
        self.endless_switch = ctk.CTkSwitch(
            head, text="Endless", variable=self.endless_var,
            font=ctk.CTkFont(FONT, 13), text_color=COL_TEXT_2,
            progress_color=COL_ACCENT, command=self._on_endless_toggle)
        self.endless_switch.pack(side="right", padx=(0, 18))

        self.duration_var = tk.StringVar(value=str(DEFAULT_DURATION))
        self.duration_entry = ctk.CTkEntry(
            head, width=64, height=32, corner_radius=10, justify="center",
            textvariable=self.duration_var, font=ctk.CTkFont(FONT, 13),
            fg_color=COL_SURFACE, border_color=COL_BORDER,
            text_color=COL_TEXT)
        self.duration_entry.pack(side="right", padx=(0, 6))
        ctk.CTkLabel(head, text="seconds", font=ctk.CTkFont(FONT, 13),
                     text_color=COL_MUTED).pack(side="right", padx=(0, 6))

        # bottom bar: one stats shortcut per operation
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="bottom", fill="x", pady=(0, 20))
        group = ctk.CTkFrame(bar, fg_color="transparent")
        group.pack()
        ctk.CTkLabel(group, text="Stats by operation",
                     font=ctk.CTkFont(FONT, 13),
                     text_color=COL_MUTED).pack(side="left", padx=(0, 10))
        for op in OPS:
            ctk.CTkButton(group, text=OP_SYMBOLS[op], width=44, height=32,
                          corner_radius=16,
                          font=ctk.CTkFont(FONT, 16, "bold"),
                          fg_color=COL_SURFACE_2, hover_color=COL_BORDER,
                          text_color=COL_TEXT_2,
                          command=lambda o=op: self.open_stats(o)).pack(
                side="left", padx=4)

        center = ctk.CTkFrame(self, fg_color="transparent")
        center.pack(fill="both", expand=True)
        inner = ctk.CTkFrame(center, fg_color="transparent")
        inner.place(relx=0.5, rely=0.46, anchor="center")

        self.timer_label = ctk.CTkLabel(inner, text="",
                                        font=ctk.CTkFont(FONT, 30, "bold"),
                                        text_color=COL_MUTED)
        self.timer_label.pack(pady=(0, 6))

        self.problem_label = ctk.CTkLabel(
            inner, text="", font=ctk.CTkFont(FONT, 104, "bold"),
            text_color=COL_TEXT)
        self.problem_label.pack(pady=(0, 26))

        self.answer_var = tk.StringVar()
        self.answer_var.trace_add("write", self._on_type)
        self.entry = ctk.CTkEntry(
            inner, width=260, height=72, corner_radius=16, justify="center",
            textvariable=self.answer_var, font=ctk.CTkFont(FONT, 36, "bold"),
            fg_color=COL_SURFACE, border_color=COL_BORDER, border_width=2,
            text_color=COL_TEXT)
        self.entry.pack()

        self.score_label = ctk.CTkLabel(inner, text="",
                                        font=ctk.CTkFont(FONT, 15),
                                        text_color=COL_MUTED)
        self.score_label.pack(pady=(18, 0))

        self.best_label = ctk.CTkLabel(inner, text="",
                                       font=ctk.CTkFont(FONT, 14),
                                       text_color=COL_MUTED)
        self.best_label.pack(pady=(4, 0))

    # ---- idle / finished screens ----
    def _show_idle(self):
        self.timer_label.configure(text="", text_color=COL_MUTED)
        self.problem_label.configure(text="Ready", text_color=COL_MUTED)
        self.entry.configure(state="disabled", border_color=COL_BORDER)
        self.score_label.configure(
            text="Press Start (or hit Enter) — answers advance automatically",
            text_color=COL_MUTED)

    def _show_finished(self, new_best=False):
        avg = (self.session_time_sum / self.correct_count
               if self.correct_count else 0.0)
        self.problem_label.configure(text=f"{self.correct_count} solved",
                                     text_color=COL_ACCENT)
        if new_best:
            self.score_label.configure(
                text=f"new personal best!  ·  {avg:.2f}s per problem",
                text_color=COL_ACCENT)
        else:
            self.score_label.configure(
                text=(f"session over  ·  {avg:.2f}s per problem"
                      if self.correct_count else "session over"),
                text_color=COL_MUTED)
        self.timer_label.configure(text="0:00" if self.session_end else "",
                                   text_color=COL_MUTED)

    def _update_best_label(self, *_):
        """Show the high score for the currently selected session length."""
        if self.endless_var.get():
            self.best_label.configure(text="endless mode  ·  no high score")
            return
        try:
            dur = max(5, int(round(float(self.duration_var.get()))))
        except ValueError:
            dur = DEFAULT_DURATION
        rec = self.stats["best"].get(str(dur))
        if rec:
            when = f"  ·  {rec['date']}" if rec.get("date") else ""
            text = f"best {fmt_mmss(dur)} score: {rec['score']}{when}"
        else:
            text = f"best {fmt_mmss(dur)} score: —"
        self.best_label.configure(text=text)

    # ---- session control ----
    def _on_endless_toggle(self):
        state = "disabled" if self.endless_var.get() else "normal"
        self.duration_entry.configure(state=state)
        self._update_best_label()

    def _on_return(self, _event):
        if not self.running:
            self.start()

    def start(self):
        self.correct_count = 0
        self.session_time_sum = 0.0
        self.running = True
        self.session_start = time.monotonic()
        if self.endless_var.get():
            self.session_end = None
            self.session_duration = None
        else:
            try:
                dur = max(5.0, float(self.duration_var.get()))
            except ValueError:
                dur = float(DEFAULT_DURATION)
                self.duration_var.set(str(DEFAULT_DURATION))
            self.session_end = self.session_start + dur
            self.session_duration = dur
        self.start_btn.configure(text="Stop", fg_color=COL_SURFACE_2,
                                 hover_color=COL_BORDER,
                                 text_color=COL_TEXT_2, command=self.stop)
        self.entry.configure(state="normal", border_color=COL_ACCENT)
        self.score_label.configure(text="0 solved", text_color=COL_MUTED)
        self._next_problem()
        self._tick()

    def stop(self, natural=False):
        self.running = False
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        # a high score requires a full timed session, not an early stop
        new_best = False
        if natural and self.session_duration is not None \
                and self.correct_count > 0:
            key = str(int(round(self.session_duration)))
            rec = self.stats["best"].get(key)
            if rec is None or self.correct_count > rec["score"]:
                self.stats["best"][key] = {"score": self.correct_count,
                                           "date": time.strftime("%Y-%m-%d")}
                new_best = True
        save_stats(self.stats)
        self.start_btn.configure(text="Start", fg_color=COL_ACCENT,
                                 hover_color=COL_ACCENT_2,
                                 text_color=COL_TEXT, command=self.start)
        self.answer_var.set("")
        self.entry.configure(state="disabled", border_color=COL_BORDER)
        self._show_finished(new_best)
        self._update_best_label()
        self._refresh_stats_window()

    # ---- problem flow ----
    def _next_problem(self):
        self.problem = generate_problem(self.stats)
        self.problem_label.configure(text=f"{self.problem.text} =",
                                     text_color=COL_TEXT)
        self.answer_var.set("")
        self.entry.delete(0, "end")  # var sync can miss a clear from a trace
        self.problem_start = time.monotonic()
        self.entry.focus_set()

    def _on_type(self, *_):
        """Auto-advance the instant the typed value matches the answer."""
        if not self.running or self.problem is None:
            return
        txt = self.answer_var.get().strip()
        if not txt:
            return
        try:
            val = int(txt)
        except ValueError:
            return
        if val == self.problem.answer:
            self._accept()

    def _accept(self):
        elapsed = time.monotonic() - self.problem_start
        record_solve(self.stats, self.problem, elapsed)
        log_solve(self.problem, elapsed)
        save_stats(self.stats)
        self.correct_count += 1
        self.session_time_sum += elapsed
        self.score_label.configure(
            text=f"{self.correct_count} solved  ·  last {elapsed:.1f}s")
        self._refresh_stats_window()
        self._next_problem()

    # ---- timer ----
    def _tick(self):
        if not self.running:
            return
        now = time.monotonic()
        if self.session_end is not None:
            remaining = self.session_end - now
            if remaining <= 0:
                self.stop(natural=True)
                return
            color = COL_CRITICAL if remaining <= 10 else COL_TEXT_2
            self.timer_label.configure(text=fmt_mmss(remaining),
                                       text_color=color)
        else:
            self.timer_label.configure(
                text=f"∞  {fmt_mmss(now - self.session_start)}",
                text_color=COL_TEXT_2)
        self._tick_job = self.after(100, self._tick)

    # ---- stats window ----
    def open_stats(self, op=None):
        if self.stats_window is None or not self.stats_window.winfo_exists():
            self.stats_window = StatsWindow(self, self.stats)
        if op is not None:
            self.stats_window.op_picker.set(OP_LABELS[op])
        self.stats_window.refresh()
        self.stats_window.focus()
        self.stats_window.lift()

    def _refresh_stats_window(self):
        if self.stats_window is not None and self.stats_window.winfo_exists():
            self.stats_window.refresh()

    def _on_close(self):
        save_stats(self.stats)
        self.destroy()


def main():
    ctk.set_appearance_mode("dark")
    app = ZetamacApp()
    app.mainloop()


if __name__ == "__main__":
    main()
