"""
Japanese Hover Translator -- dashboard app.

A single light-mode window that ties everything together:

  * runs the hover-translation engine (see hover_translate.py) in the
    background while the window is open,
  * shows the overlay popup near the cursor when you dwell on Japanese text,
  * lets you browse and study words you've saved, and
  * lets you choose the pin/save/toggle hotkeys.

Everything lives in one process. Closing this window stops the dwell worker
and the global hotkey listener, so the hover overlay only runs while the
dashboard is open -- there is no leftover background service.

Run:
    python dashboard_app.py
"""

import queue
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from pynput import keyboard

import hover_translate as ht
from spaced_repetition import (
    ReviewState,
    format_db_datetime,
    parse_db_datetime,
    schedule_review,
    stage_label,
    utc_now,
)

# --- Light theme palette ----------------------------------------------------

BG = "#f5f7fb"            # app canvas
SIDEBAR_BG = "#111827"    # navigation rail
SIDEBAR_ACTIVE = "#263449"
SIDEBAR_HOVER = "#1f2937"
SIDEBAR_TEXT = "#d1d5db"
TEXT = "#172033"
MUTED = "#667085"
FAINT = "#98a2b3"
ACCENT = "#4f46e5"
ACCENT_HOVER = "#4338ca"
ACCENT_SOFT = "#eef2ff"
BORDER = "#e4e7ec"
CARD = "#ffffff"
GOOD = "#12b76a"
GOOD_SOFT_BG = "#ecfdf3"
GOOD_SOFT_FG = "#027a48"
DANGER = "#d92d20"
DANGER_SOFT_BG = "#fef3f2"
CHIP_BG = "#f2f4f7"
CHIP_FG = "#344054"

JP_TRANSLATION_COLOR = "#b45309"
JP_DICT_COLOR = "#047857"

UI_FONT = ("Segoe UI", 10)
UI_BOLD = ("Segoe UI Semibold", 10)
UI_SMALL = ("Segoe UI", 9)
H1 = ("Segoe UI Semibold", 24)
H2 = ("Segoe UI Semibold", 13)
JP_FONT = ("Yu Gothic UI", 18)
JP_BIG = ("Yu Gothic UI", 30)
CHIP_FONT = ("Segoe UI Semibold", 9)

# Modifier keys are ignored while recording a hotkey: a bare modifier isn't a
# useful single-key hotkey, and — more importantly — the selection feature
# simulates Ctrl+C, whose synthetic Ctrl press would otherwise be captured as
# the "recorded" key. (The engine is also paused during recording; this is a
# belt-and-suspenders guard.)
MODIFIER_KEYS = set()

SEARCH_PLACEHOLDER_TEXT = "Search word or meaning…"
for _name in (
    "ctrl", "ctrl_l", "ctrl_r", "shift", "shift_l", "shift_r",
    "alt", "alt_l", "alt_r", "alt_gr", "cmd", "cmd_l", "cmd_r",
):
    _k = getattr(keyboard.Key, _name, None)
    if _k is not None:
        MODIFIER_KEYS.add(_k)


class DashboardApp:
    """The main window: navigation sidebar, three pages (Overview / Saved
    words / Settings), and the plumbing that ties the background hover
    engine to the Tk UI.

    Three threads are alive while this app runs:
      * the Tk main thread (this class's methods, unless noted otherwise),
      * `self.dwell_thread`, running HoverTranslator.dwell_watch_loop (polls
        the cursor, does OCR/selection capture, and queues translation jobs),
      * `self.listener`, a pynput global keyboard hook for the hotkeys.

    Tkinter widgets may only be touched from the Tk main thread -- touching
    them from the other two produces corrupted/blank renders (learned the
    hard way earlier in this project's history). The two background threads
    never call Tk methods directly; they instead put small tuples/objects
    onto `self.ui_queue`, which `_poll_queue` drains on the Tk main thread via
    `root.after`. See `_poll_queue` and `_on_key_press` for the two producers.
    """

    def __init__(self):
        """Load persisted config, start the hover engine and its background
        threads, build the window, and land on the Overview page. Raises
        ht.OcrSetupError / ht.TranslationSetupError (from HoverTranslator's
        constructor) if neither OCR backend nor the offline translation model
        can be made to work -- see the top-level __main__ guard below for how
        that's surfaced to the user."""
        ht.init_study_db()
        self.config = ht.load_config()
        self.hotkeys = {
            action: ht.str_to_key(self.config["hotkeys"][action])
            for action in ("toggle", "pin", "save")
        }
        self._recording_action = None  # set while capturing a new hotkey

        self.ui_queue: queue.Queue = queue.Queue()
        self.translator = ht.HoverTranslator(self.ui_queue)

        # One connection, used only on the Tk main thread (the engine never
        # touches the DB; saves are marshalled here through the queue).
        self.conn = sqlite3.connect(ht.STUDY_DB_PATH)

        self.root = tk.Tk()
        self.root.title("Japanese Hover Translator")
        self.root.configure(bg=BG)
        self.root.geometry("1120x720")
        self.root.minsize(980, 640)
        self.root.report_callback_exception = self._report_callback_exception

        self.overlay = ht.OverlayWindow(self.root)
        self._sync_overlay_labels()

        self._init_style()

        # dynamic label vars
        self.status_text_var = tk.StringVar()
        self.status_sub_var = tk.StringVar()
        self.sidebar_status_var = tk.StringVar()
        self.sidebar_hint_var = tk.StringVar()
        self.toggle_btn_var = tk.StringVar()
        self.stats_total_var = tk.StringVar(value="0")
        self.stats_due_var = tk.StringVar(value="0")
        self.saved_count_var = tk.StringVar(value="")
        self.settings_status_var = tk.StringVar(value="")

        # page + nav registries
        self.pages = {}
        self.nav_buttons = {}
        self.hotkey_key_labels = {}
        self.hotkey_change_buttons = {}

        self._build_layout()

        # background workers
        self.dwell_thread = threading.Thread(
            target=self.translator.dwell_watch_loop,
            name="hover-dwell-worker",
            daemon=True,
        )
        self.dwell_thread.start()
        self.listener = keyboard.Listener(on_press=self._on_key_press)
        self.listener.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._poll_queue)

        self.show_page("home")
        self._update_status()
        self._update_stats()
        self._refresh_hotkey_summary()

    # ------------------------------------------------------------------ style

    def _init_style(self):
        """ttk widgets (Treeview, Scrollbar, Progressbar) don't take plain
        bg=/fg= kwargs like tk widgets do -- their look is set once here via
        ttk.Style so they match the rest of the light theme."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
            rowheight=40, borderwidth=0, font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading", background="#f9fafb", foreground="#475467",
            borderwidth=0, relief="flat", font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT_SOFT)],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Vertical.TScrollbar", background="#d0d5dd", troughcolor="#f2f4f7",
            borderwidth=0, arrowcolor=MUTED,
        )
        style.configure(
            "Review.Horizontal.TProgressbar", background=ACCENT,
            troughcolor="#e4e7ec", borderwidth=0, lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    # ---------------------------------------------------------------- widgets

    def _button(self, parent, text=None, command=None, kind="primary", textvariable=None):
        """Flat-styled tk.Button in one of four color variants, with a manual
        hover-color swap (tk.Button has no hover state of its own)."""
        palette = {
            "primary": (ACCENT, "#ffffff", ACCENT_HOVER),
            "neutral": ("#eef1f6", TEXT, "#e2e8f0"),
            "danger": (DANGER_SOFT_BG, DANGER, "#fecaca"),
            "good": (GOOD_SOFT_BG, GOOD_SOFT_FG, "#bbf7d0"),
        }
        bg, fg, hover = palette[kind]
        btn = tk.Button(
            parent, text=text, textvariable=textvariable, command=command,
            bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
            relief="flat", font=UI_BOLD, padx=16, pady=9, cursor="hand2",
            borderwidth=0, highlightthickness=0,
        )
        btn.bind("<Enter>", lambda e: btn.config(bg=hover))
        btn.bind("<Leave>", lambda e: btn.config(bg=bg))
        return btn

    def _card(self, parent):
        """White, thin-bordered container frame -- the basic building block
        of every page (status card, metric tiles, settings sections, etc.)."""
        return tk.Frame(
            parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1,
        )

    def _chip(self, parent, text):
        """Small rounded-looking label used for hotkey badges and the
        saved-word stage indicator (New/Learning/Review)."""
        return tk.Label(
            parent, text=text, bg=CHIP_BG, fg=CHIP_FG, font=CHIP_FONT,
            padx=10, pady=4,
        )

    # ----------------------------------------------------------------- layout

    def _build_layout(self):
        """One-time construction of the whole window: the dark sidebar (brand
        mark, nav links, running/paused status pill) plus the light content
        area that the three pages are built into and swapped within (see
        show_page). Called once from __init__; the pages themselves stay
        alive for the app's lifetime and are just pack/pack_forget'd."""
        sidebar = tk.Frame(self.root, bg=SIDEBAR_BG, width=232)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=SIDEBAR_BG)
        brand.pack(fill="x", padx=20, pady=(24, 30))
        mark = tk.Label(
            brand, text="日", bg=ACCENT, fg="#ffffff",
            font=("Yu Gothic UI", 15, "bold"), width=2, height=1,
        )
        mark.pack(side="left")
        brand_text = tk.Frame(brand, bg=SIDEBAR_BG)
        brand_text.pack(side="left", padx=(11, 0))
        tk.Label(
            brand_text, text="Japanese Hover", bg=SIDEBAR_BG, fg="#ffffff",
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w")
        tk.Label(
            brand_text, text="Translator", bg=SIDEBAR_BG, fg="#98a2b3",
            font=UI_SMALL,
        ).pack(anchor="w")

        tk.Label(
            sidebar, text="WORKSPACE", bg=SIDEBAR_BG, fg="#667085",
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=24, pady=(0, 8))

        for key, label, badge in (
            ("home", "Overview", "🏠"),
            ("saved", "Saved words", "📚"),
            ("settings", "Settings", "⚙"),
        ):
            btn = tk.Label(
                sidebar, text=f"  {badge}   {label}", bg=SIDEBAR_BG,
                fg=SIDEBAR_TEXT, font=UI_BOLD, anchor="w", padx=13, pady=11,
                cursor="hand2",
            )
            btn.pack(fill="x", padx=12, pady=2)
            btn.bind("<Button-1>", lambda _event, k=key: self.show_page(k))
            btn.bind("<Enter>", lambda _event, k=key: self._nav_hover(k, True))
            btn.bind("<Leave>", lambda _event, k=key: self._nav_hover(k, False))
            self.nav_buttons[key] = btn

        footer = tk.Frame(sidebar, bg="#0b1220")
        footer.pack(side="bottom", fill="x", padx=12, pady=12)
        status_row = tk.Frame(footer, bg="#0b1220")
        status_row.pack(fill="x", padx=12, pady=(11, 2))
        self.sidebar_dot = tk.Label(
            status_row, text="●", bg="#0b1220", font=("Segoe UI", 9)
        )
        self.sidebar_dot.pack(side="left")
        tk.Label(
            status_row, textvariable=self.sidebar_status_var, bg="#0b1220",
            fg="#e5e7eb", font=UI_SMALL,
        ).pack(side="left", padx=(7, 0))
        tk.Label(
            footer, textvariable=self.sidebar_hint_var, bg="#0b1220",
            fg="#667085", font=("Segoe UI", 8),
        ).pack(anchor="w", padx=12, pady=(0, 11))

        content = tk.Frame(self.root, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        self.page_container = tk.Frame(content, bg=BG)
        self.page_container.pack(fill="both", expand=True)

        self._build_home()
        self._build_saved()
        self._build_settings()

    def _nav_hover(self, key, entering):
        """<Enter>/<Leave> handler for one sidebar nav item; no-ops on the
        currently active page so it doesn't visually flicker under the mouse."""
        if self._active_page == key:
            return
        self.nav_buttons[key].config(
            bg=SIDEBAR_HOVER if entering else SIDEBAR_BG,
            fg="#ffffff" if entering else SIDEBAR_TEXT,
        )

    def show_page(self, key):
        """Switch the visible page to one of "home"/"saved"/"settings".

        All three page frames already exist (built once in _build_layout);
        this just pack/pack_forget's between them and restyles the sidebar's
        active-item highlight. Also the single choke point for leaving any
        in-progress state cleanly: cancels an active hotkey recording and
        unbinds study-mode keys no matter which page we're leaving or going
        to, so neither can leak into whatever page loads next.
        """
        self._cancel_recording()
        # Unbind study-mode keys regardless of the target page: leaving them
        # bound while navigating away from a study session mid-review means a
        # stray 1-4/Space press elsewhere would silently mutate the SM-2
        # schedule of whatever card was left on the stale study_queue.
        self._unbind_study_keys()
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self._active_page = key
        for nav_key, button in self.nav_buttons.items():
            button.config(
                bg=SIDEBAR_ACTIVE if nav_key == key else SIDEBAR_BG,
                fg="#ffffff" if nav_key == key else SIDEBAR_TEXT,
            )
        if key == "saved":
            self._exit_study()
            self._refresh_saved_list()

    def _page_header(self, parent, title, subtitle, eyebrow=None):
        """Shared page-title block (small caps "eyebrow" label, big title,
        muted subtitle) used at the top of all three pages."""
        if eyebrow:
            tk.Label(
                parent, text=eyebrow.upper(), bg=BG, fg=ACCENT,
                font=("Segoe UI Semibold", 8),
            ).pack(anchor="w", pady=(0, 5))
        tk.Label(parent, text=title, bg=BG, fg=TEXT, font=H1).pack(anchor="w")
        tk.Label(
            parent, text=subtitle, bg=BG, fg=MUTED, font=UI_FONT,
        ).pack(anchor="w", pady=(4, 16))

    def _build_home(self):
        """Build the Overview page: on/off status card, three metric tiles
        (saved words / due today / active OCR engine), a "how it works" list,
        and a live hotkey summary. Widgets that later methods update by
        variable (status_text_var, stats_total_var, ...) or by reference
        (home_dot, home_hotkey_rows) are created here and stashed on self."""
        page = tk.Frame(self.page_container, bg=BG)
        self.pages["home"] = page
        inner = tk.Frame(page, bg=BG)
        inner.pack(fill="both", expand=True, padx=38, pady=24)

        self._page_header(
            inner,
            "Overview",
            "Translate Japanese anywhere on your screen and turn useful words into study cards.",
            "Japanese learning workspace",
        )

        status = self._card(inner)
        status.pack(fill="x", pady=(0, 16))
        accent_bar = tk.Frame(status, bg=ACCENT, width=5)
        accent_bar.pack(side="left", fill="y")
        status_body = tk.Frame(status, bg=CARD)
        status_body.pack(side="left", fill="both", expand=True, padx=22, pady=15)
        status_title = tk.Frame(status_body, bg=CARD)
        status_title.pack(fill="x")
        self.home_dot = tk.Label(
            status_title, text="●", bg=CARD, font=("Segoe UI", 12)
        )
        self.home_dot.pack(side="left")
        tk.Label(
            status_title, textvariable=self.status_text_var, bg=CARD, fg=TEXT,
            font=("Segoe UI Semibold", 15),
        ).pack(side="left", padx=(9, 0))
        tk.Label(
            status_body, textvariable=self.status_sub_var, bg=CARD, fg=MUTED,
            font=UI_FONT, justify="left", anchor="w", wraplength=600,
        ).pack(anchor="w", pady=(7, 0))
        self.toggle_btn = self._button(
            status, textvariable=self.toggle_btn_var,
            command=self._toggle_enabled, kind="primary",
        )
        self.toggle_btn.pack(side="right", padx=22, pady=15)

        metrics = tk.Frame(inner, bg=BG)
        metrics.pack(fill="x", pady=(0, 16))
        for index, (label, value, caption) in enumerate((
            ("Saved words", self.stats_total_var, "In your personal library"),
            ("Due today", self.stats_due_var, "Ready for review"),
            ("OCR engine", self.translator.ocr_backend_display, "Screen text recognition"),
        )):
            card = self._card(metrics)
            card.pack(
                side="left", fill="both", expand=True,
                padx=(0 if index == 0 else 6, 0 if index == 2 else 6),
            )
            tk.Label(
                card, text=label.upper(), bg=CARD, fg=FAINT,
                font=("Segoe UI Semibold", 8),
            ).pack(anchor="w", padx=18, pady=(12, 4))
            # value is either a live tk.Variable (numbers that change while the
            # dashboard runs) or a plain string (the OCR backend, fixed once at
            # startup) -- either way it belongs in the same bold "value" slot
            # so all three tiles read consistently, with a static caption below.
            value_label = tk.Label(card, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 18))
            if isinstance(value, tk.Variable):
                value_label.config(textvariable=value)
            else:
                value_label.config(text=value)
            value_label.pack(anchor="w", padx=18)
            tk.Label(
                card, text=caption, bg=CARD, fg=MUTED, font=UI_SMALL,
            ).pack(anchor="w", padx=18, pady=(2, 12))

        columns = tk.Frame(inner, bg=BG)
        columns.pack(fill="both", expand=True)

        guide = self._card(columns)
        guide.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tk.Label(
            guide, text="How it works", bg=CARD, fg=TEXT, font=H2,
        ).pack(anchor="w", padx=20, pady=(13, 8))
        for number, title, text in (
            ("1", "Pause over Japanese", "Keep the pointer still over readable text."),
            ("2", "Read the translation", "Words use JMdict; phrases use translation."),
            ("3", "Pin and save", "Pin useful results, then add them to your library."),
        ):
            row = tk.Frame(guide, bg=CARD)
            row.pack(fill="x", padx=20, pady=3)
            tk.Label(
                row, text=number, bg=ACCENT_SOFT, fg=ACCENT,
                font=UI_BOLD, width=3, pady=3,
            ).pack(side="left")
            copy = tk.Frame(row, bg=CARD)
            copy.pack(side="left", padx=(12, 0), fill="x", expand=True)
            tk.Label(copy, text=title, bg=CARD, fg=TEXT, font=UI_BOLD).pack(anchor="w")
            tk.Label(copy, text=text, bg=CARD, fg=MUTED, font=UI_SMALL).pack(anchor="w")

        shortcuts = self._card(columns)
        shortcuts.pack(side="left", fill="both", expand=True, padx=(8, 0))
        top = tk.Frame(shortcuts, bg=CARD)
        top.pack(fill="x", padx=20, pady=(13, 6))
        tk.Label(top, text="Keyboard shortcuts", bg=CARD, fg=TEXT, font=H2).pack(side="left")
        settings_link = tk.Label(
            top, text="Edit", bg=CARD, fg=ACCENT, font=UI_BOLD, cursor="hand2"
        )
        settings_link.pack(side="right")
        settings_link.bind("<Button-1>", lambda _event: self.show_page("settings"))
        self.home_hotkey_rows = tk.Frame(shortcuts, bg=CARD)
        self.home_hotkey_rows.pack(fill="x", padx=20, pady=(3, 8))
        self._button(
            shortcuts, text="Open saved words", command=lambda: self.show_page("saved"),
            kind="neutral",
        ).pack(anchor="w", padx=20, pady=(4, 18))

    def _stat_row(self, parent, label, var):
        """One "label ..... value" row bound live to a tk.Variable. Currently
        unused directly by _build_home (which builds metric tiles instead)
        but kept as a shared helper for any future simple label/value row."""
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", padx=20, pady=4)
        tk.Label(row, text=label, bg=CARD, fg=MUTED, font=UI_FONT).pack(side="left")
        tk.Label(row, textvariable=var, bg=CARD, fg=TEXT, font=UI_BOLD).pack(side="right")

    def _refresh_hotkey_summary(self):
        """Rebuild the Overview page's hotkey list and the sidebar footer
        hint from the current config -- called on load and after every
        successful rebind/reset so both stay in sync with what's actually
        configured (see _handle_hotkey_recorded / _reset_hotkeys)."""
        for widget in self.home_hotkey_rows.winfo_children():
            widget.destroy()
        labels = {"toggle": "Toggle translator", "pin": "Pin popup", "save": "Save word"}
        for action in ("toggle", "pin", "save"):
            row = tk.Frame(self.home_hotkey_rows, bg=CARD)
            row.pack(fill="x", pady=3)
            tk.Label(
                row, text=labels[action], bg=CARD, fg=MUTED, font=UI_FONT,
            ).pack(side="left")
            self._chip(row, ht.key_display(self.config["hotkeys"][action])).pack(side="right")
        self.sidebar_hint_var.set(
            f"{ht.key_display(self.config['hotkeys']['toggle'])} toggles hover translation"
        )

    def _build_saved(self):
        """Build the Saved words page: header + "Review due cards" button,
        a search/filter toolbar, and a body that holds two mutually-exclusive
        views swapped in place -- the word list (_build_saved_list_view,
        built here) and the flashcard study view (_build_study_view, built
        lazily the first time _start_study runs)."""
        page = tk.Frame(self.page_container, bg=BG)
        self.pages["saved"] = page
        inner = tk.Frame(page, bg=BG)
        inner.pack(fill="both", expand=True, padx=38, pady=24)

        self.saved_top = tk.Frame(inner, bg=BG)
        self.saved_top.pack(fill="x")
        header = tk.Frame(self.saved_top, bg=BG)
        header.pack(fill="x")
        copy = tk.Frame(header, bg=BG)
        copy.pack(side="left")
        tk.Label(copy, text="Saved words", bg=BG, fg=TEXT, font=H1).pack(anchor="w")
        tk.Label(
            copy, text="Your personal vocabulary library and spaced-repetition queue.",
            bg=BG, fg=MUTED, font=UI_FONT,
        ).pack(anchor="w", pady=(4, 0))
        self._button(
            header, text="Review due cards", command=self._start_study, kind="primary",
        ).pack(side="right", anchor="e")

        toolbar = self._card(self.saved_top)
        toolbar.pack(fill="x", pady=(20, 12))
        search_group = tk.Frame(toolbar, bg=CARD)
        search_group.pack(side="left", fill="x", expand=True, padx=14, pady=11)
        tk.Label(
            search_group, text="Search", bg=CARD, fg=MUTED, font=UI_SMALL,
        ).pack(side="left", padx=(0, 8))
        self.saved_search_var = tk.StringVar()
        self.saved_search_entry = tk.Entry(
            search_group, textvariable=self.saved_search_var, bg="#f9fafb", fg=TEXT,
            insertbackground=TEXT, relief="flat", font=UI_FONT,
            highlightbackground=BORDER, highlightcolor=ACCENT, highlightthickness=1,
        )
        self.saved_search_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.saved_search_clear = tk.Label(
            search_group, text="✕", bg=CARD, fg=FAINT, font=UI_SMALL, cursor="hand2",
        )
        self.saved_search_clear.pack(side="left", padx=(6, 0))
        self.saved_search_clear.bind("<Button-1>", lambda _event: self._clear_search())
        # Plain Tk Entry has no built-in placeholder -- shown/hidden by hand via
        # focus events, and always excluded from the actual search filter (see
        # _refresh_saved_list) so it can never be searched for as literal text.
        self._search_showing_placeholder = True
        self.saved_search_var.set(SEARCH_PLACEHOLDER_TEXT)
        self.saved_search_entry.config(fg=FAINT)
        self.saved_search_entry.bind("<FocusIn>", self._focus_in_search)
        self.saved_search_entry.bind("<FocusOut>", self._focus_out_search)

        filters = tk.Frame(toolbar, bg=CARD)
        filters.pack(side="left", padx=(8, 14), pady=10)
        self.saved_filter_var = tk.StringVar(value="all")
        self.saved_filter_buttons = {}
        for key, label in (("all", "All words"), ("due", "Due now")):
            button = tk.Button(
                filters, text=label, command=lambda k=key: self._set_saved_filter(k),
                relief="flat", borderwidth=0, highlightthickness=0, cursor="hand2",
                font=UI_BOLD, padx=12, pady=7,
            )
            button.pack(side="left", padx=2)
            self.saved_filter_buttons[key] = button
        tk.Label(
            toolbar, textvariable=self.saved_count_var, bg=CARD, fg=MUTED, font=UI_SMALL,
        ).pack(side="right", padx=(0, 16))

        self.saved_body = tk.Frame(inner, bg=BG)
        self.saved_body.pack(fill="both", expand=True)
        self._build_saved_list_view()
        self.study_view = None
        self._set_saved_filter("all", refresh=False)
        self.saved_search_var.trace_add("write", lambda *_args: self._refresh_saved_list())

    def _set_saved_filter(self, key, refresh=True):
        """Switch the All words / Due now toggle and restyle both buttons to
        show which is active. refresh=False is used only during initial page
        build, before the word list/tree even exists yet to refresh."""
        self.saved_filter_var.set(key)
        for filter_key, button in self.saved_filter_buttons.items():
            active = filter_key == key
            button.config(
                bg=ACCENT if active else "#f2f4f7",
                fg="#ffffff" if active else "#475467",
                activebackground=ACCENT_HOVER if active else "#e4e7ec",
                activeforeground="#ffffff" if active else TEXT,
            )
        if refresh:
            self._refresh_saved_list()

    def _focus_in_search(self, _event=None):
        """Clear the placeholder text when the search box gains focus."""
        if self._search_showing_placeholder:
            self._search_showing_placeholder = False
            self.saved_search_var.set("")
            self.saved_search_entry.config(fg=TEXT)

    def _focus_out_search(self, _event=None):
        """Restore the placeholder text when the search box loses focus
        empty (never overwrites text the user actually typed)."""
        if not self.saved_search_var.get():
            self._search_showing_placeholder = True
            self.saved_search_var.set(SEARCH_PLACEHOLDER_TEXT)
            self.saved_search_entry.config(fg=FAINT)

    def _clear_search(self):
        """The "✕" button: empty the field and refocus it, so the user can
        start typing a new search immediately instead of the placeholder
        reappearing the instant focus leaves."""
        self._search_showing_placeholder = False
        self.saved_search_var.set("")
        self.saved_search_entry.config(fg=TEXT)
        self.saved_search_entry.focus_set()

    def _build_saved_list_view(self):
        """Build the word-list half of the Saved words page: a Treeview of
        saved words on the left, a fixed-width detail/actions panel on the
        right. Rebuilt fresh each time _build_saved runs (i.e. once, at
        startup) -- not torn down/rebuilt on every page visit."""
        self.list_view = tk.Frame(self.saved_body, bg=BG)
        self.list_view.pack(fill="both", expand=True)

        tree_wrap = tk.Frame(
            self.list_view, bg=CARD, highlightbackground=BORDER, highlightthickness=1,
        )
        tree_wrap.pack(side="left", fill="both", expand=True)
        columns = ("word", "translation", "due", "stage")
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings")
        self.tree.heading("word", text="WORD OR PHRASE")
        self.tree.heading("translation", text="MEANING")
        self.tree.heading("due", text="NEXT REVIEW")
        self.tree.heading("stage", text="STAGE")
        self.tree.column("word", width=140, minwidth=110)
        self.tree.column("translation", width=170, minwidth=130)
        self.tree.column("due", width=90, minwidth=82)
        self.tree.column("stage", width=75, minwidth=68, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_saved_select)
        scrollbar = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        detail = tk.Frame(
            self.list_view, bg=CARD, width=292,
            highlightbackground=BORDER, highlightthickness=1,
        )
        detail.pack(side="left", fill="y", padx=(12, 0))
        detail.pack_propagate(False)
        tk.Label(
            detail, text="WORD DETAILS", bg=CARD, fg=FAINT,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=20, pady=(20, 8))
        self.detail_word = tk.Label(
            detail, bg=CARD, fg=TEXT, font=("Yu Gothic UI", 20, "bold"),
            wraplength=250, justify="left",
        )
        self.detail_word.pack(anchor="w", padx=20)
        self.detail_translation = tk.Label(
            detail, bg=CARD, fg=JP_TRANSLATION_COLOR,
            font=("Segoe UI Semibold", 11), wraplength=250, justify="left",
        )
        self.detail_translation.pack(anchor="w", padx=20, pady=(5, 14))

        tags = tk.Frame(detail, bg=CARD)
        tags.pack(fill="x", padx=20)
        self.detail_stage = self._chip(tags, "")
        self.detail_stage.pack(side="left")
        self.detail_due = tk.Label(tags, bg=CARD, fg=MUTED, font=UI_SMALL)
        self.detail_due.pack(side="left", padx=(10, 0))

        tk.Frame(detail, bg=BORDER, height=1).pack(fill="x", padx=20, pady=16)
        tk.Label(
            detail, text="DICTIONARY FORM", bg=CARD, fg=FAINT,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=20)
        self.detail_dict = tk.Label(
            detail, bg=CARD, fg=JP_DICT_COLOR, font=UI_SMALL,
            wraplength=250, justify="left",
        )
        self.detail_dict.pack(anchor="w", padx=20, pady=(5, 14))
        self.detail_metrics = tk.Label(
            detail, bg=CARD, fg=MUTED, font=UI_SMALL, wraplength=250, justify="left",
        )
        self.detail_metrics.pack(anchor="w", padx=20)
        self.detail_saved = tk.Label(detail, bg=CARD, fg=FAINT, font=("Segoe UI", 8))
        self.detail_saved.pack(anchor="w", padx=20, pady=(6, 0))

        actions = tk.Frame(detail, bg=CARD)
        actions.pack(side="bottom", fill="x", padx=20, pady=20)
        self.learned_btn = self._button(
            actions, text="Reset progress", command=self._reset_schedule,
            kind="neutral",
        )
        self.learned_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._button(
            actions, text="Delete", command=self._delete_selected, kind="danger",
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.selected_id = None
        self._clear_detail()
        # Pack the fixed-width detail panel first so Treeview's requested
        # column width cannot squeeze it at smaller supported window sizes.
        tree_wrap.pack_forget()
        detail.pack_forget()
        detail.pack(side="right", fill="y", padx=(12, 0))
        tree_wrap.pack(side="left", fill="both", expand=True)

    def _clear_detail(self):
        """Reset the detail panel to its empty state -- shown on page load
        and whenever the previously-selected word disappears from the list
        (deleted, or filtered/searched out)."""
        self.detail_word.config(text="Select a word", fg=FAINT)
        self.detail_translation.config(text="Choose an item from the library to view its details.")
        self.detail_stage.config(text="No selection")
        self.detail_due.config(text="")
        self.detail_dict.config(text="—")
        self.detail_metrics.config(text="")
        self.detail_saved.config(text="")
        self.learned_btn.config(state="disabled")

    @staticmethod
    def _due_label(value):
        """Format a saved_words.due_at value (DB string or NULL) as a short
        human-relative string ("Due now" / "In 3h" / "Tomorrow" / "In 5 days"
        / an absolute date past ~2 weeks out). value=None/NULL means "never
        reviewed yet", which is also due now."""
        due_at = parse_db_datetime(value)
        if due_at is None:
            return "Due now"
        seconds = (due_at - utc_now()).total_seconds()
        if seconds <= 0:
            return "Due now"
        if seconds < 86400:
            hours = max(1, int((seconds + 3599) // 3600))
            return f"In {hours}h"
        if seconds < 2 * 86400:
            return "Tomorrow"
        days = max(2, round(seconds / 86400))
        if days < 14:
            return f"In {days} days"
        return due_at.astimezone().strftime("%Y-%m-%d")

    def _refresh_saved_list(self):
        """Rebuild the Treeview from the database using the current filter
        (all/due) and search text. Called after every mutation (save,
        delete, reset, review) and on every search keystroke (via the
        StringVar trace set up in _build_saved) -- simple full requery
        rather than incremental updates, which is fine at this data scale."""
        for row in self.tree.get_children():
            self.tree.delete(row)
        now_text = format_db_datetime(utc_now())
        conditions = []
        params = []
        if self.saved_filter_var.get() == "due":
            conditions.append("(due_at IS NULL OR due_at <= ?)")
            params.append(now_text)
        search = (
            "" if self._search_showing_placeholder
            else self.saved_search_var.get().strip()
        )
        if search:
            conditions.append("(surface_text LIKE ? OR translation LIKE ?)")
            like = f"%{search}%"
            params.extend((like, like))
        query = (
            "SELECT id, surface_text, translation, due_at, repetitions, review_count "
            "FROM saved_words"
        )
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += (
            " ORDER BY CASE WHEN due_at IS NULL OR due_at <= ? THEN 0 ELSE 1 END, "
            "COALESCE(due_at, ''), saved_at DESC"
        )
        params.append(now_text)
        shown = 0
        for row_id, word, translation, due_at, repetitions, review_count in self.conn.execute(
            query, tuple(params)
        ):
            self.tree.insert(
                "", "end", iid=str(row_id),
                values=(
                    word,
                    translation or "",
                    self._due_label(due_at),
                    stage_label(repetitions, review_count),
                ),
            )
            shown += 1
        total, due = self._counts()
        self.saved_count_var.set(f"{shown} shown  •  {due} due  •  {total} total")
        if self.selected_id is not None and not self.tree.exists(str(self.selected_id)):
            self.selected_id = None
            self._clear_detail()

    def _on_saved_select(self, _event):
        """Treeview <<TreeviewSelect>> handler: load the full row for the
        newly-selected word and populate the detail panel. Also called
        directly with _event=None after a reset/save to refresh the panel
        for whatever's still selected -- the event argument is unused."""
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = int(selection[0])
        row = self.conn.execute(
            "SELECT surface_text, translation, dict_forms, saved_at, repetitions,"
            " interval_days, ease_factor, due_at, review_count, lapses"
            " FROM saved_words WHERE id = ?",
            (self.selected_id,),
        ).fetchone()
        if not row:
            return
        (
            surface, translation, dictionary_forms, saved_at, repetitions,
            interval_days, ease_factor, due_at, review_count, lapses,
        ) = row
        self.detail_word.config(text=surface, fg=TEXT)
        self.detail_translation.config(text=translation or "No translation saved")
        self.detail_stage.config(text=stage_label(repetitions, review_count))
        self.detail_due.config(text=self._due_label(due_at))
        self.detail_dict.config(text=dictionary_forms or "No alternate form")
        self.detail_metrics.config(
            text=(
                f"Interval   {interval_days} days\n"
                f"Ease       {ease_factor:.2f}\n"
                f"Reviews    {review_count}\n"
                f"Lapses     {lapses}"
            )
        )
        self.detail_saved.config(text=f"Saved {saved_at}")
        self.learned_btn.config(state="normal")

    def _reset_schedule(self):
        """"Reset progress" button: wipe the selected word's SM-2 state back
        to a brand-new card (due immediately), after an explicit confirm
        since this discards real review history."""
        if self.selected_id is None:
            return
        word = self.detail_word.cget("text")
        if not messagebox.askyesno(
            "Reset review progress",
            f"Reset spaced-repetition progress for “{word}”? "
            "It will become due for review immediately.",
            parent=self.root,
        ):
            return
        self.conn.execute(
            """
            UPDATE saved_words
               SET learned = 0, repetitions = 0, interval_days = 0,
                   ease_factor = 2.5, due_at = ?, last_reviewed_at = NULL,
                   review_count = 0, lapses = 0
             WHERE id = ?
            """,
            (format_db_datetime(utc_now()), self.selected_id),
        )
        self.conn.commit()
        # keep selection so the detail panel refreshes with the reset state
        keep = self.selected_id
        self._refresh_saved_list()
        if self.tree.exists(str(keep)):
            self.tree.selection_set(str(keep))
            self._on_saved_select(None)
        self._update_stats()

    def _delete_selected(self):
        """"Delete" button: permanently remove the selected word after an
        explicit confirm (this is the one truly irreversible action in the
        app -- there's no undo or trash)."""
        if self.selected_id is None:
            return
        word = self.detail_word.cget("text")
        if not messagebox.askyesno(
            "Delete word",
            f"Delete “{word}” from your saved words? This cannot be undone.",
            parent=self.root,
        ):
            return
        self.conn.execute("DELETE FROM saved_words WHERE id = ?", (self.selected_id,))
        self.conn.commit()
        self.selected_id = None
        self._clear_detail()
        self._refresh_saved_list()
        self._update_stats()

    def _start_study(self):
        """"Review due cards" button: load every due-or-never-reviewed word
        (oldest-due first) into self.study_queue and switch to the flashcard
        view. If nothing is due, shows a status message (with when the next
        card will be) instead of entering an empty study session."""
        now_text = format_db_datetime(utc_now())
        rows = self.conn.execute(
            """
            SELECT id, surface_text, translation, dict_forms, repetitions,
                   interval_days, ease_factor, due_at, last_reviewed_at,
                   review_count, lapses
              FROM saved_words
             WHERE due_at IS NULL OR due_at <= ?
             ORDER BY COALESCE(due_at, ''), id
            """,
            (now_text,),
        ).fetchall()
        if not rows:
            next_due = self.conn.execute(
                "SELECT MIN(due_at) FROM saved_words WHERE due_at > ?", (now_text,)
            ).fetchone()[0]
            message = "You are all caught up"
            if next_due:
                message += f"  •  Next review {self._due_label(next_due).lower()}"
            self.saved_count_var.set(message)
            return
        self.study_queue = rows
        self.study_index = 0
        self.study_revealed = False
        self.saved_top.pack_forget()
        self.list_view.pack_forget()
        self._build_study_view()
        self.study_view.pack(fill="both", expand=True)
        self._show_card()

    def _build_study_view(self):
        """(Re)build the flashcard view: progress bar, the card itself (word,
        then a reveal-hidden translation/dictionary-form area), and the
        Again/Hard/Good/Easy rating buttons. Rebuilt fresh on every
        _start_study call (not reused across sessions) since the card count
        for the progress bar's maximum is only known once the queue is
        loaded. Binds the space/1-4 keyboard shortcuts here; _unbind_study_keys
        (called from show_page and _exit_study) is the only place that undoes
        that binding, so it must run on every path out of this view."""
        if self.study_view is not None:
            self.study_view.destroy()
        self.study_view = tk.Frame(self.saved_body, bg=BG)

        top = tk.Frame(self.study_view, bg=BG)
        top.pack(fill="x", pady=(0, 12))
        self.study_back = tk.Label(
            top, text="←  Exit review", bg=BG, fg=MUTED, font=UI_BOLD, cursor="hand2",
        )
        self.study_back.pack(side="left")
        self.study_back.bind("<Button-1>", lambda _event: self._exit_study())
        self.study_progress = tk.Label(top, bg=BG, fg=MUTED, font=UI_SMALL)
        self.study_progress.pack(side="right")

        self.study_progress_bar = ttk.Progressbar(
            self.study_view, mode="determinate", maximum=max(1, len(self.study_queue)),
            style="Review.Horizontal.TProgressbar",
        )
        self.study_progress_bar.pack(fill="x", pady=(0, 20))

        card = tk.Frame(
            self.study_view, bg=CARD, highlightbackground=BORDER, highlightthickness=1,
        )
        card.pack(fill="both", expand=True, padx=70, pady=(0, 12))
        tk.Label(
            card, text="RECALL THE MEANING", bg=CARD, fg=FAINT,
            font=("Segoe UI Semibold", 8),
        ).pack(pady=(32, 8))
        self.study_word = tk.Label(
            card, bg=CARD, fg=TEXT, font=JP_BIG, wraplength=650,
            justify="center", padx=35, pady=18,
        )
        self.study_word.pack()

        self.study_reveal = tk.Frame(card, bg=CARD)
        tk.Frame(self.study_reveal, bg=BORDER, height=1).pack(fill="x", padx=50, pady=(4, 18))
        self.study_translation = tk.Label(
            self.study_reveal, bg=CARD, fg=JP_TRANSLATION_COLOR,
            font=("Segoe UI Semibold", 15), wraplength=650, justify="center",
        )
        self.study_translation.pack(padx=30)
        self.study_dict = tk.Label(
            self.study_reveal, bg=CARD, fg=JP_DICT_COLOR, font=UI_SMALL,
            wraplength=650, justify="center",
        )
        self.study_dict.pack(padx=30, pady=(8, 28))

        self.study_controls = tk.Frame(self.study_view, bg=BG)
        self.study_controls.pack(pady=(5, 0))
        self.reveal_btn = self._button(
            self.study_controls, text="Reveal answer   Space",
            command=self._reveal_card, kind="primary",
        )
        self.reveal_btn.pack(side="left", padx=5)
        self.again_btn = self._button(
            self.study_controls, text="1  Again", command=lambda: self._answer("again"),
            kind="danger",
        )
        self.hard_btn = self._button(
            self.study_controls, text="2  Hard", command=lambda: self._answer("hard"),
            kind="neutral",
        )
        self.good_btn = self._button(
            self.study_controls, text="3  Good", command=lambda: self._answer("good"),
            kind="primary",
        )
        self.easy_btn = self._button(
            self.study_controls, text="4  Easy", command=lambda: self._answer("easy"),
            kind="good",
        )
        self.rating_buttons = [
            self.again_btn, self.hard_btn, self.good_btn, self.easy_btn,
        ]
        self.study_hint = tk.Label(
            self.study_view, text="Choose how well you remembered to schedule the next review.",
            bg=BG, fg=FAINT, font=UI_SMALL,
        )
        self.study_hint.pack(pady=(10, 0))

        self.root.bind("<space>", lambda _event: self._reveal_card())
        self.root.bind("<Key-1>", lambda _event: self._answer("again"))
        self.root.bind("<Key-2>", lambda _event: self._answer("hard"))
        self.root.bind("<Key-3>", lambda _event: self._answer("good"))
        self.root.bind("<Key-4>", lambda _event: self._answer("easy"))

    def _show_card(self):
        """Render study_queue[study_index] in its un-revealed state (word
        only, Reveal button, rating buttons disabled/hidden). Ends the
        session via _exit_study once the index runs past the end of the
        queue -- there's no "previous card" navigation, sessions are linear."""
        if self.study_index >= len(self.study_queue):
            self.saved_count_var.set("Review complete  •  Cards rescheduled automatically")
            self._exit_study()
            return
        _row_id, surface, _translation, _forms, *_schedule = self.study_queue[self.study_index]
        self.study_word.config(text=surface)
        self.study_reveal.pack_forget()
        self.study_revealed = False
        for button in self.rating_buttons:
            button.pack_forget()
        if not self.reveal_btn.winfo_manager():
            self.reveal_btn.pack(side="left", padx=5)
        self.reveal_btn.config(state="normal")
        self.study_progress.config(
            text=f"Card {self.study_index + 1} of {len(self.study_queue)}"
        )
        self.study_progress_bar["value"] = self.study_index + 1
        self.study_hint.config(text="Think of the meaning, then reveal the answer.")

    def _reveal_card(self):
        """Space bar / "Reveal answer" button: show the translation and
        dictionary form, swap the Reveal button for the four rating buttons.
        Guarded so a stray Space press elsewhere (or a second one before the
        next card loads) can't double-reveal or fire while the study view
        isn't even the visible page."""
        if self.study_view is None or not self.study_view.winfo_ismapped() or self.study_revealed:
            return
        _row_id, _surface, translation, forms, *_schedule = self.study_queue[self.study_index]
        self.study_translation.config(text=translation or "No translation saved")
        self.study_dict.config(text=forms or "")
        self.study_reveal.pack(fill="x")
        self.study_revealed = True
        self.reveal_btn.pack_forget()
        for button in self.rating_buttons:
            button.config(state="normal")
            button.pack(side="left", padx=4)
        self.study_hint.config(text="Rate your recall. Keyboard shortcuts 1–4 also work.")

    def _answer(self, rating):
        """1-4 keys / rating buttons: run the current card through the SM-2
        scheduler (spaced_repetition.schedule_review) for the given rating
        ("again"/"hard"/"good"/"easy"), persist the new schedule, and advance
        to the next card. Guarded against firing before the answer is
        revealed (rating an un-revealed card wouldn't have a meaningful
        recall signal behind it)."""
        if not self.study_revealed:
            return
        row = self.study_queue[self.study_index]
        (
            row_id, _surface, _translation, _dict_forms, repetitions,
            interval_days, ease_factor, due_at, last_reviewed_at,
            review_count, lapses,
        ) = row
        state = ReviewState(
            repetitions=repetitions,
            interval_days=interval_days,
            ease_factor=ease_factor,
            due_at=parse_db_datetime(due_at),
            last_reviewed_at=parse_db_datetime(last_reviewed_at),
            review_count=review_count,
            lapses=lapses,
        )
        result = schedule_review(state, rating)
        self.conn.execute(
            """
            UPDATE saved_words
               SET learned = ?, repetitions = ?, interval_days = ?,
                   ease_factor = ?, due_at = ?, last_reviewed_at = ?,
                   review_count = ?, lapses = ?
             WHERE id = ?
            """,
            (
                int(result.repetitions >= 2),
                result.repetitions,
                result.interval_days,
                result.ease_factor,
                format_db_datetime(result.due_at),
                format_db_datetime(result.last_reviewed_at),
                result.review_count,
                result.lapses,
                row_id,
            ),
        )
        self.conn.commit()
        self._update_stats()
        self.study_index += 1
        self._show_card()

    def _unbind_study_keys(self):
        """Undo the global key bindings _build_study_view sets up. Called
        both from _exit_study and unconditionally from show_page, since
        leaving the bindings active while navigating to another page would
        let a stray keypress mutate whatever card was left mid-session."""
        self.root.unbind("<space>")
        for key in ("<Key-1>", "<Key-2>", "<Key-3>", "<Key-4>"):
            self.root.unbind(key)

    def _exit_study(self):
        """Leave the flashcard view and return to the word list -- used both
        for a deliberate "Exit review" click and automatically once the
        queue is exhausted (see _show_card). Also doubles as the general
        "make sure the list view (not study view) is what's showing and
        up to date" call used by show_page when landing on this page."""
        self._unbind_study_keys()
        if self.study_view is not None and self.study_view.winfo_ismapped():
            self.study_view.pack_forget()
        if not self.saved_top.winfo_ismapped():
            self.saved_top.pack(fill="x", before=self.saved_body)
        if not self.list_view.winfo_ismapped():
            self.list_view.pack(fill="both", expand=True)
        self._refresh_saved_list()
        self._update_stats()

    def _build_settings(self):
        """Build the Settings page: hotkey rows with per-action "Change"
        buttons (see _start_recording), a hover-translation on/off toggle
        mirroring the Overview page's, and a read-only System status /
        Storage panel showing which backends are active and where local
        data lives."""
        page = tk.Frame(self.page_container, bg=BG)
        self.pages["settings"] = page
        inner = tk.Frame(page, bg=BG)
        inner.pack(fill="both", expand=True, padx=38, pady=24)
        self._page_header(
            inner,
            "Settings",
            "Personalize the controls and find technical details when you need them.",
            "Application preferences",
        )

        columns = tk.Frame(inner, bg=BG)
        columns.pack(fill="both", expand=True)
        left = tk.Frame(columns, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = tk.Frame(columns, bg=BG, width=320)
        right.pack(side="left", fill="y", padx=(8, 0))
        right.pack_propagate(False)

        shortcuts = self._card(left)
        shortcuts.pack(fill="x")
        tk.Label(shortcuts, text="Keyboard shortcuts", bg=CARD, fg=TEXT, font=H2).pack(
            anchor="w", padx=20, pady=(18, 3)
        )
        tk.Label(
            shortcuts, text="Function keys work best because they do not interrupt normal typing.",
            bg=CARD, fg=MUTED, font=UI_SMALL,
        ).pack(anchor="w", padx=20, pady=(0, 12))
        labels = {
            "toggle": ("Toggle translator", "Pause or resume hover translation."),
            "pin": ("Pin current popup", "Keep a translation visible while you study."),
            "save": ("Save pinned word", "Add the current result to Saved words."),
        }
        for index, action in enumerate(("toggle", "pin", "save")):
            title, description = labels[action]
            row = tk.Frame(shortcuts, bg=CARD)
            row.pack(fill="x", padx=20, pady=6)
            copy = tk.Frame(row, bg=CARD)
            copy.pack(side="left", fill="x", expand=True)
            tk.Label(copy, text=title, bg=CARD, fg=TEXT, font=UI_BOLD).pack(anchor="w")
            tk.Label(copy, text=description, bg=CARD, fg=MUTED, font=UI_SMALL).pack(anchor="w")
            change = self._button(
                row, text="Change", command=lambda a=action: self._start_recording(a),
                kind="neutral",
            )
            change.pack(side="right")
            chip = self._chip(row, ht.key_display(self.config["hotkeys"][action]))
            chip.pack(side="right", padx=(0, 10))
            self.hotkey_key_labels[action] = chip
            self.hotkey_change_buttons[action] = change
            if index < 2:
                tk.Frame(shortcuts, bg="#f2f4f7", height=1).pack(fill="x", padx=20)
        bottom = tk.Frame(shortcuts, bg=CARD)
        bottom.pack(fill="x", padx=20, pady=(9, 14))
        self._button(
            bottom, text="Restore default keys", command=self._reset_hotkeys, kind="neutral",
        ).pack(side="left")
        tk.Label(
            bottom, textvariable=self.settings_status_var, bg=CARD, fg=ACCENT,
            font=UI_SMALL, wraplength=340, justify="left",
        ).pack(side="left", padx=(14, 0))

        behavior = self._card(left)
        behavior.pack(fill="x", pady=(12, 0))
        tk.Label(behavior, text="Application behavior", bg=CARD, fg=TEXT, font=H2).pack(
            anchor="w", padx=20, pady=(18, 8)
        )
        row = tk.Frame(behavior, bg=CARD)
        row.pack(fill="x", padx=20, pady=(0, 18))
        copy = tk.Frame(row, bg=CARD)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(copy, text="Hover translation", bg=CARD, fg=TEXT, font=UI_BOLD).pack(anchor="w")
        tk.Label(
            copy, text="The translator only runs while this window is open.",
            bg=CARD, fg=MUTED, font=UI_SMALL,
        ).pack(anchor="w")
        self.settings_toggle = self._button(
            row, textvariable=self.toggle_btn_var, command=self._toggle_enabled, kind="neutral",
        )
        self.settings_toggle.pack(side="right")

        runtime = self._card(right)
        runtime.pack(fill="x")
        tk.Label(runtime, text="System status", bg=CARD, fg=TEXT, font=H2).pack(
            anchor="w", padx=20, pady=(18, 12)
        )
        for label, value in (
            ("OCR", self.translator.ocr_backend_display),
            ("Words", "Local JMdict"),
            ("Phrases", "Google + offline fallback"),
            ("Study", "SM-2 scheduling"),
        ):
            row = tk.Frame(runtime, bg=CARD)
            row.pack(fill="x", padx=20, pady=5)
            tk.Label(row, text=label, bg=CARD, fg=FAINT, font=UI_SMALL).pack(side="left")
            tk.Label(
                row, text=value, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 9),
            ).pack(side="right")
        tk.Frame(runtime, bg=CARD, height=8).pack()

        storage = self._card(right)
        storage.pack(fill="x", pady=(12, 0))
        tk.Label(storage, text="Storage & diagnostics", bg=CARD, fg=TEXT, font=H2).pack(
            anchor="w", padx=20, pady=(18, 6)
        )
        tk.Label(
            storage,
            text="Your settings, saved words, and translation cache stay on this computer.",
            bg=CARD, fg=MUTED, font=UI_SMALL, wraplength=235, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 12))
        for label, value in (
            ("Study database", "study_words.db"),
            ("Diagnostic log", "JapaneseHoverTranslator.log"),
        ):
            tk.Label(
                storage, text=label.upper(), bg=CARD, fg=FAINT,
                font=("Segoe UI Semibold", 8),
            ).pack(anchor="w", padx=20, pady=(5, 2))
            tk.Label(
                storage, text=value, bg="#f9fafb", fg=MUTED, font=("Segoe UI", 8),
                wraplength=235, justify="left", padx=8, pady=6,
            ).pack(anchor="w", fill="x", padx=20)
        tk.Frame(storage, bg=CARD, height=12).pack()

    def _refresh_settings_hotkeys(self):
        """Sync each hotkey row's chip (current key) and Change button label
        ("Change" vs "Press a key…") with self.config and self._recording_action."""
        for action in ("toggle", "pin", "save"):
            self.hotkey_key_labels[action].config(
                text=ht.key_display(self.config["hotkeys"][action])
            )
            self.hotkey_change_buttons[action].config(
                text="Press a key…" if self._recording_action == action else "Change"
            )

    def _start_recording(self, action):
        """"Change" button: begin capturing a new hotkey for one of
        toggle/pin/save. This is a small cross-thread state machine:
        _start_recording (here, main thread) sets self._recording_action;
        _on_key_press (pynput listener thread) sees it's set and forwards the
        next keypress through ui_queue instead of treating it as a normal
        hotkey; _handle_hotkey_recorded (main thread, via _poll_queue) is
        what actually applies it and clears the flag.

        Pause the engine so its selection-copy (synthetic Ctrl+C) can't be
        recorded as the new key. Only snapshot enabled-state on the first
        start, so switching Change targets mid-record doesn't lose it.
        """
        if self._recording_action is None:
            self._enabled_before_record = self.translator.enabled
            self.translator.enabled = False
        self._recording_action = action
        self._refresh_settings_hotkeys()
        self.settings_status_var.set(f"Press the key you want for “{action}” …  (Esc to cancel)")

    def _cancel_recording(self):
        """Abandon an in-progress hotkey recording without applying anything
        (called from show_page so navigating away mid-record can't leave the
        engine permanently paused with the next keystroke still being
        captured as a hotkey)."""
        if self._recording_action is not None:
            self._recording_action = None
            self.translator.enabled = getattr(self, "_enabled_before_record", True)
            self._refresh_settings_hotkeys()

    def _handle_hotkey_recorded(self, action, key):
        """Apply (or reject) a key captured while recording -- the main-thread
        half of the _start_recording state machine. Rejects: a stale event
        for an action no longer being recorded, a bare modifier key, Esc
        (cancels instead), a key with no stable string form, and a key
        already bound to a different action. Persists to config.json on
        success and rolls back in memory if the write fails."""
        if self._recording_action != action:
            return  # stale (a second keypress arrived after we already applied one)
        if key in MODIFIER_KEYS:
            return  # ignore bare modifiers; keep waiting for a real key
        self._recording_action = None
        self.translator.enabled = getattr(self, "_enabled_before_record", True)
        if key == keyboard.Key.esc:
            self.settings_status_var.set("Cancelled.")
            self._refresh_settings_hotkeys()
            return
        key_str = ht.key_to_str(key)
        if key_str is None:
            self.settings_status_var.set("That key can’t be used — try a function key.")
            self._refresh_settings_hotkeys()
            return
        for other in ("toggle", "pin", "save"):
            if other != action and self.config["hotkeys"][other] == key_str:
                self.settings_status_var.set(
                    f"{ht.key_display(key_str)} is already used for “{other}”. Pick another key."
                )
                self._refresh_settings_hotkeys()
                return
        previous_key_str = self.config["hotkeys"][action]
        previous_key = self.hotkeys[action]
        self.config["hotkeys"][action] = key_str
        self.hotkeys[action] = ht.str_to_key(key_str)
        if not ht.save_config(self.config):
            self.config["hotkeys"][action] = previous_key_str
            self.hotkeys[action] = previous_key
            self.settings_status_var.set(
                "Could not save that shortcut. Details were written to the log."
            )
            self._refresh_settings_hotkeys()
            return
        self._sync_overlay_labels()
        self._refresh_settings_hotkeys()
        self._refresh_hotkey_summary()
        self.settings_status_var.set(f"“{action}” set to {ht.key_display(key_str)}.")

    def _reset_hotkeys(self):
        """"Restore default keys" button: revert all three hotkeys to
        ht.DEFAULT_HOTKEYS (F9/F10/F11) and persist, with the same
        roll-back-on-write-failure behavior as _handle_hotkey_recorded."""
        self._recording_action = None
        previous_config = dict(self.config["hotkeys"])
        previous_hotkeys = dict(self.hotkeys)
        self.config["hotkeys"] = dict(ht.DEFAULT_HOTKEYS)
        self.hotkeys = {a: ht.str_to_key(self.config["hotkeys"][a]) for a in ("toggle", "pin", "save")}
        if not ht.save_config(self.config):
            self.config["hotkeys"] = previous_config
            self.hotkeys = previous_hotkeys
            self.settings_status_var.set(
                "Could not reset shortcuts. Details were written to the log."
            )
            self._refresh_settings_hotkeys()
            return
        self._sync_overlay_labels()
        self._refresh_settings_hotkeys()
        self._refresh_hotkey_summary()
        self.settings_status_var.set("Hotkeys reset to defaults (F9 / F10 / F11).")

    def _sync_overlay_labels(self):
        """Push the current pin/save key labels into the hover overlay
        popup (see hover_translate.OverlayWindow.set_hotkey_labels) so its
        "📌 Pinned — F10 save · F9 unpin"-style banner always matches
        whatever the user has actually bound, not the defaults."""
        self.overlay.set_hotkey_labels(
            ht.key_display(self.config["hotkeys"]["pin"]),
            ht.key_display(self.config["hotkeys"]["save"]),
        )

    # --------------------------------------------------------- engine control

    def _toggle_enabled(self):
        """Flip the engine on/off -- shared by the Overview "Turn off/on"
        button, the Settings page's mirrored toggle, and the toggle hotkey
        (via ui_queue, see _poll_queue)."""
        self._set_enabled(not self.translator.enabled)

    def _set_enabled(self, value):
        """Set HoverTranslator.enabled directly and refresh the UI to match.
        Hides any visible overlay popup when turning off, since a paused
        engine showing a stale popup would be confusing."""
        self.translator.enabled = value
        if not value:
            self.overlay.hide()
        self._update_status()

    def _update_status(self):
        """Repaint every on/off-dependent bit of UI: status card text and
        color, both toggle buttons' label/color/hover-color, and the two
        status dots (Overview card + sidebar). Called after every change to
        translator.enabled, from three different call sites."""
        on = self.translator.enabled
        self.status_text_var.set("Hover translation is ON" if on else "Hover translation is OFF")
        self.status_sub_var.set(
            "Hover over Japanese text anywhere on your screen.\n"
            f"OCR: {self.translator.ocr_backend_display}  ·  "
            f"Translation: {self.translator.translation_backend_display}"
            if on else "Paused — press the button (or your toggle key) to resume."
        )
        self.toggle_btn_var.set("Turn off" if on else "Turn on")
        normal = self._toggle_colors(on)
        hover = "#fecdca" if on else ACCENT_HOVER
        for button in (self.toggle_btn, getattr(self, "settings_toggle", None)):
            if button is None:
                continue
            button.config(**normal)
            button.bind("<Enter>", lambda event, color=hover: event.widget.config(bg=color))
            button.bind(
                "<Leave>",
                lambda event, colors=normal: event.widget.config(**colors),
            )
        self.home_dot.config(fg=GOOD if on else FAINT)
        self.sidebar_dot.config(fg=GOOD if on else FAINT)
        self.sidebar_status_var.set("Running" if on else "Paused")

    def _toggle_colors(self, on):
        """Color kwargs for the toggle button: red "Turn off" while running
        (it's the button you'd press to stop it), accent-colored "Turn on"
        while paused."""
        if on:
            return {"bg": DANGER_SOFT_BG, "fg": DANGER, "activebackground": "#fecaca"}
        return {"bg": ACCENT, "fg": "#ffffff", "activebackground": ACCENT_HOVER}

    def _counts(self):
        """(total saved words, due-now count) as a single indexed query --
        used by both the Overview stat tiles and the Saved words page's
        "shown / due / total" summary line."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN due_at IS NULL OR due_at <= ? "
            "THEN 1 ELSE 0 END), 0) FROM saved_words",
            (format_db_datetime(utc_now()),),
        ).fetchone()
        return int(row[0]), int(row[1])

    def _update_stats(self):
        """Refresh the Overview page's "Saved words" / "Due today" tiles."""
        total, due = self._counts()
        self.stats_total_var.set(str(total))
        self.stats_due_var.set(str(due))

    # ------------------------------------------------------------- event loop

    def _on_key_press(self, key):
        """pynput's global keyboard hook callback -- runs on the listener
        thread, NOT the Tk main thread. Never touch tkinter here; every
        outcome is a small tuple pushed onto ui_queue for _poll_queue (main
        thread) to act on. Handles two cases: a hotkey is being recorded
        (see _start_recording), or a normal toggle/pin/save keypress."""
        if self._recording_action is not None:
            # Capturing a new hotkey. Hand the key to the main thread and let
            # _handle_hotkey_recorded clear the recording flag -- clearing it
            # HERE would make that handler's stale-guard reject the key. Extra
            # keys pressed before poll runs enqueue too, but the handler applies
            # only the first (the rest fail the guard once the flag is cleared).
            self.ui_queue.put(("hotkey_recorded", self._recording_action, key))
            return
        if self._key_matches(key, "toggle"):
            self.ui_queue.put(("toggle_enabled",))
        elif self._key_matches(key, "pin"):
            self.ui_queue.put(("toggle_pin",))
        elif self._key_matches(key, "save"):
            self.ui_queue.put(("save_entry",))

    def _key_matches(self, key, action):
        """Whether `key` is the currently-configured hotkey for `action`
        ("toggle"/"pin"/"save"). False if that action has no valid binding."""
        target = self.hotkeys.get(action)
        return target is not None and key == target

    def _poll_queue(self):
        """The sole consumer of ui_queue, run every 50ms via root.after --
        this is where everything the two background threads (dwell worker,
        hotkey listener) hand off actually gets applied to Tk widgets. Also
        drives the overlay's own time-based auto-hide (overlay.tick()).
        Re-schedules itself, so once started in __init__ this effectively
        runs for the lifetime of the app."""
        try:
            while True:
                item = self.ui_queue.get_nowait()
                if isinstance(item, ht.TranslationJob):
                    if self.translator.enabled:
                        self.overlay.show(item)
                elif item[0] == "translation_ready":
                    self.overlay.update_translation(item[1], item[2])
                elif item[0] == "cursor_moved":
                    self.overlay.handle_cursor_moved(item[1], item[2])
                elif item[0] == "force_hide":
                    self.overlay.hide()
                elif item[0] == "toggle_enabled":
                    self._toggle_enabled()
                elif item[0] == "toggle_pin":
                    self.overlay.toggle_pin()
                elif item[0] == "save_entry":
                    self._save_current()
                elif item[0] == "hotkey_recorded":
                    self._handle_hotkey_recorded(item[1], item[2])
                elif item[0] == "runtime_error":
                    self.status_sub_var.set(item[1])
        except queue.Empty:
            pass
        self.overlay.tick()
        self.root.after(50, self._poll_queue)

    def _save_current(self):
        """Handle the "save" hotkey: persist the overlay's currently pinned
        entry to study_words.db. current_entry() itself enforces "must be
        pinned first" (see OverlayWindow.current_entry in hover_translate.py),
        so pressing save on an unpinned/hidden popup is silently a no-op --
        that's intentional, not a bug, matching the app's "pin, then save"
        workflow."""
        entry = self.overlay.current_entry()
        if entry is None:
            # not pinned/visible -- nothing to save (matches the "pin first" rule)
            return
        try:
            ht.save_entry_to_db(entry)
        except Exception:
            ht.log.exception("failed to save study entry")
            self.status_sub_var.set(
                "The word could not be saved. Details were written to the log."
            )
            return
        self._update_stats()
        if self._active_page == "saved" and self.list_view.winfo_ismapped():
            self._refresh_saved_list()

    # ------------------------------------------------------------------ close

    def _on_close(self):
        """WM_DELETE_WINDOW handler (the X button / Alt+F4) -- this is the
        one place responsible for the app's core promise that the hover
        overlay only runs while the window is open. Stops the dwell worker
        and the global hotkey listener, closes the study DB connection, then
        tears the window down. Every step is independently try/except'd (and
        logged on failure) so one step failing can never skip the ones after
        it -- a hung listener, for example, must not prevent the DB from
        closing or the window from actually closing."""
        self.translator.stop()
        if (
            self.dwell_thread.is_alive()
            and threading.current_thread() is not self.dwell_thread
        ):
            self.dwell_thread.join(timeout=2.0)
        try:
            self.listener.stop()
        except Exception:
            ht.log.exception("hotkey listener did not stop cleanly")
        try:
            self.listener.join(timeout=2.0)
        except Exception:
            ht.log.exception("hotkey listener could not be joined")
        try:
            self.conn.close()
        except Exception:
            ht.log.exception("study database did not close cleanly")
        self.root.destroy()

    def _report_callback_exception(self, exc_type, exc_value, traceback):
        """Keep Tk callback failures visible and diagnosable in windowed builds."""
        ht.log.error(
            "Tk callback failed",
            exc_info=(exc_type, exc_value, traceback),
        )
        messagebox.showerror(
            "Japanese Hover Translator",
            "Something went wrong, but the app is still running.\n\n"
            f"Technical details were saved to:\n{ht.LOG_PATH}",
            parent=self.root,
        )

    def run(self):
        """Enter the Tk event loop. Blocks until the window closes (see
        _on_close), at which point the process exits since no non-daemon
        threads remain."""
        self.root.mainloop()


if __name__ == "__main__":
    # If HoverTranslator's constructor can't find a usable OCR backend or the
    # bundled offline translation model, it raises before any window is ever
    # shown -- fall back to a plain messagebox (on a throwaway hidden root)
    # so the failure is visible even in a --windowed/no-console packaged
    # build, where there'd otherwise be nothing but a silent process exit.
    try:
        DashboardApp().run()
    except (ht.OcrSetupError, ht.TranslationSetupError) as exc:
        ht.log.error("application setup failed: %s", exc)
        error_root = tk.Tk()
        error_root.withdraw()
        messagebox.showerror(
            "Japanese Hover Translator setup",
            f"{exc}\n\nTechnical details: {ht.LOG_PATH}",
            parent=error_root,
        )
        error_root.destroy()
    except Exception:
        ht.log.exception("application startup failed")
        error_root = tk.Tk()
        error_root.withdraw()
        messagebox.showerror(
            "Japanese Hover Translator",
            "The app could not start. No study data was intentionally removed.\n\n"
            f"Technical details were saved to:\n{ht.LOG_PATH}",
            parent=error_root,
        )
        error_root.destroy()
