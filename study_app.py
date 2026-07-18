"""
Legacy compatibility entry point for Japanese Hover Translator.

The original standalone shuffle-flashcard interface remains below for source-history
reference, but running this file now opens the primary dashboard so review answers
always go through the SM-2 scheduler.

Run:
    python study_app.py
"""

import os
import random
import sqlite3
import tkinter as tk
from tkinter import ttk

STUDY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "study_words.db")

BG_COLOR = "#14141f"
PANEL_COLOR = "#1c1c2b"
ACCENT_COLOR = "#7dd3fc"
TEXT_COLOR = "#f5f5f5"
DIM_TEXT_COLOR = "#9ca3af"
TRANSLATION_COLOR = "#ffe066"
DICT_FORM_COLOR = "#a7f3d0"
LEARNED_COLOR = "#4ade80"

JAPANESE_FONT = ("Yu Gothic UI", 20)
DETAIL_FONT = ("Segoe UI", 12)
SMALL_FONT = ("Segoe UI", 9)


def init_db():
    conn = sqlite3.connect(STUDY_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            surface_text TEXT NOT NULL,
            translation TEXT,
            dict_forms TEXT,
            saved_at TEXT NOT NULL,
            learned INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


class StudyApp:
    """Retained for source-history reference only -- do not instantiate.

    This was the original standalone flashcard interface. It writes a plain
    learned=1 flag directly (see _answer_study_card below) instead of going
    through the SM-2 scheduler in spaced_repetition.py, which is now the only
    schedule saved_words rows are supposed to have. Running it against the
    current database would corrupt that schedule for every card it touches.
    __init__ refuses to run for exactly that reason; the rest of the class
    body is deliberately left in place as-is rather than deleted.
    """

    def __init__(self, root):
        raise RuntimeError(
            "StudyApp is retained for reference only and must not be run -- "
            "it writes a legacy learned flag that bypasses SM-2 scheduling "
            "and would corrupt study_words.db. Run dashboard_app.py instead."
        )
        self.root.title("Japanese Study List")
        self.root.configure(bg=BG_COLOR)
        self.root.geometry("820x520")

        self.conn = init_db()
        self.study_queue = []
        self.study_index = 0
        self.study_revealed = False

        self._build_style()
        self._build_layout()
        self.refresh_list()

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview", background=PANEL_COLOR, fieldbackground=PANEL_COLOR,
            foreground=TEXT_COLOR, rowheight=28, borderwidth=0, font=DETAIL_FONT,
        )
        style.configure(
            "Treeview.Heading", background=BG_COLOR, foreground=DIM_TEXT_COLOR,
            borderwidth=0, font=SMALL_FONT,
        )
        style.map("Treeview", background=[("selected", "#2a2a40")])

    def _build_layout(self):
        top_bar = tk.Frame(self.root, bg=BG_COLOR)
        top_bar.pack(fill="x", padx=10, pady=(10, 4))

        tk.Label(
            top_bar, text="Saved words", bg=BG_COLOR, fg=TEXT_COLOR,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        self.show_learned_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            top_bar, text="Show learned", variable=self.show_learned_var,
            command=self.refresh_list, bg=BG_COLOR, fg=TEXT_COLOR,
            selectcolor=PANEL_COLOR, activebackground=BG_COLOR, activeforeground=TEXT_COLOR,
            font=SMALL_FONT,
        ).pack(side="left", padx=12)

        tk.Button(
            top_bar, text="Study mode", command=self.start_study_mode,
            bg=ACCENT_COLOR, fg="#0a0a12", font=("Segoe UI", 10, "bold"),
            relief="flat", padx=10, pady=4,
        ).pack(side="right")

        # --- list view ---
        # Parented directly to root (no intermediate wrapper frame) so that
        # pack_forget() on this single widget in start_study_mode actually
        # frees its space -- an earlier version wrapped this in a separate
        # always-packed "body" frame, which kept claiming the space even
        # after list_frame itself was forgotten, leaving a blank gap with
        # the study-mode flashcard stacked below it instead of replacing it.
        self.list_frame = tk.Frame(self.root, bg=BG_COLOR)
        self.list_frame.pack(fill="both", expand=True, padx=10, pady=6)

        columns = ("word", "translation", "saved_at", "learned")
        self.tree = ttk.Treeview(self.list_frame, columns=columns, show="headings")
        self.tree.heading("word", text="Word / Phrase")
        self.tree.heading("translation", text="Translation")
        self.tree.heading("saved_at", text="Saved")
        self.tree.heading("learned", text="Learned")
        self.tree.column("word", width=180)
        self.tree.column("translation", width=320)
        self.tree.column("saved_at", width=140)
        self.tree.column("learned", width=70, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        scrollbar = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="left", fill="y")

        detail_panel = tk.Frame(self.list_frame, bg=PANEL_COLOR, width=280)
        detail_panel.pack(side="left", fill="y", padx=(10, 0))
        detail_panel.pack_propagate(False)

        self.detail_word = tk.Label(
            detail_panel, text="", bg=PANEL_COLOR, fg=TEXT_COLOR,
            font=JAPANESE_FONT, wraplength=260, justify="left",
        )
        self.detail_word.pack(anchor="w", padx=12, pady=(16, 6))

        self.detail_translation = tk.Label(
            detail_panel, text="", bg=PANEL_COLOR, fg=TRANSLATION_COLOR,
            font=(DETAIL_FONT[0], DETAIL_FONT[1], "italic"), wraplength=260, justify="left",
        )
        self.detail_translation.pack(anchor="w", padx=12, pady=(0, 10))

        tk.Label(
            detail_panel, text="Dictionary forms", bg=PANEL_COLOR, fg=DIM_TEXT_COLOR,
            font=SMALL_FONT,
        ).pack(anchor="w", padx=12)
        self.detail_dict_forms = tk.Label(
            detail_panel, text="", bg=PANEL_COLOR, fg=DICT_FORM_COLOR,
            font=SMALL_FONT, wraplength=260, justify="left",
        )
        self.detail_dict_forms.pack(anchor="w", padx=12, pady=(0, 14))

        self.detail_saved_at = tk.Label(
            detail_panel, text="", bg=PANEL_COLOR, fg=DIM_TEXT_COLOR, font=SMALL_FONT,
        )
        self.detail_saved_at.pack(anchor="w", padx=12)

        btn_row = tk.Frame(detail_panel, bg=PANEL_COLOR)
        btn_row.pack(anchor="w", padx=12, pady=14, fill="x")

        self.learned_btn = tk.Button(
            btn_row, text="Mark learned", command=self._toggle_learned,
            bg=LEARNED_COLOR, fg="#0a0a12", relief="flat", font=SMALL_FONT, padx=8, pady=4,
        )
        self.learned_btn.pack(fill="x", pady=(0, 6))

        tk.Button(
            btn_row, text="Delete", command=self._delete_selected,
            bg="#f87171", fg="#0a0a12", relief="flat", font=SMALL_FONT, padx=8, pady=4,
        ).pack(fill="x")

        self.selected_id = None

        # --- study mode view (built lazily, swapped in over list_frame) ---
        self.study_frame = None

        self.status_label = tk.Label(
            self.root, text=f"Database: {STUDY_DB_PATH}", bg=BG_COLOR, fg=DIM_TEXT_COLOR,
            font=SMALL_FONT, anchor="w",
        )
        self.status_label.pack(fill="x", padx=10, pady=(0, 8))

    # ---------------- list view ----------------

    def refresh_list(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        query = "SELECT id, surface_text, translation, saved_at, learned FROM saved_words"
        if not self.show_learned_var.get():
            query += " WHERE learned = 0"
        query += " ORDER BY saved_at DESC"
        for row_id, word, translation, saved_at, learned in self.conn.execute(query):
            self.tree.insert(
                "", "end", iid=str(row_id),
                values=(word, translation or "", saved_at, "✓" if learned else ""),
            )

    def _on_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = int(selection[0])
        row = self.conn.execute(
            "SELECT surface_text, translation, dict_forms, saved_at, learned"
            " FROM saved_words WHERE id = ?",
            (self.selected_id,),
        ).fetchone()
        if not row:
            return
        surface_text, translation, dict_forms, saved_at, learned = row
        self.detail_word.config(text=surface_text)
        self.detail_translation.config(text=translation or "")
        self.detail_dict_forms.config(text=dict_forms or "(none)")
        self.detail_saved_at.config(text=f"Saved {saved_at}")
        self.learned_btn.config(text="Mark not learned" if learned else "Mark learned")

    def _toggle_learned(self):
        if self.selected_id is None:
            return
        self.conn.execute(
            "UPDATE saved_words SET learned = 1 - learned WHERE id = ?", (self.selected_id,)
        )
        self.conn.commit()
        self.refresh_list()

    def _delete_selected(self):
        if self.selected_id is None:
            return
        self.conn.execute("DELETE FROM saved_words WHERE id = ?", (self.selected_id,))
        self.conn.commit()
        self.selected_id = None
        self.detail_word.config(text="")
        self.detail_translation.config(text="")
        self.detail_dict_forms.config(text="")
        self.detail_saved_at.config(text="")
        self.refresh_list()

    # ---------------- study (flashcard) mode ----------------

    def start_study_mode(self):
        rows = self.conn.execute(
            "SELECT id, surface_text, translation, dict_forms FROM saved_words WHERE learned = 0"
        ).fetchall()
        if not rows:
            self.status_label.config(text="No unlearned words to study -- save some from the hover overlay first (F10 then F11).")
            return
        random.shuffle(rows)
        self.study_queue = rows
        self.study_index = 0
        self.study_revealed = False
        self.list_frame.pack_forget()
        self._build_study_frame()
        self.study_frame.pack(fill="both", expand=True)
        self._show_study_card()

    def _build_study_frame(self):
        if self.study_frame is not None:
            self.study_frame.destroy()
        self.study_frame = tk.Frame(self.root, bg=BG_COLOR)

        self.study_progress = tk.Label(
            self.study_frame, text="", bg=BG_COLOR, fg=DIM_TEXT_COLOR, font=SMALL_FONT,
        )
        self.study_progress.pack(pady=(20, 10))

        card = tk.Frame(self.study_frame, bg=PANEL_COLOR)
        card.pack(pady=10)

        self.study_word = tk.Label(
            card, text="", bg=PANEL_COLOR, fg=TEXT_COLOR, font=("Yu Gothic UI", 32),
            wraplength=600, justify="center", padx=40, pady=30,
        )
        self.study_word.pack()

        self.study_reveal_frame = tk.Frame(card, bg=PANEL_COLOR)
        self.study_translation = tk.Label(
            self.study_reveal_frame, text="", bg=PANEL_COLOR, fg=TRANSLATION_COLOR,
            font=(DETAIL_FONT[0], 16, "italic"), wraplength=600, justify="center",
        )
        self.study_translation.pack(pady=(0, 8))
        self.study_dict_forms = tk.Label(
            self.study_reveal_frame, text="", bg=PANEL_COLOR, fg=DICT_FORM_COLOR,
            font=SMALL_FONT, wraplength=600, justify="center",
        )
        self.study_dict_forms.pack(pady=(0, 20))

        btn_row = tk.Frame(self.study_frame, bg=BG_COLOR)
        btn_row.pack(pady=16)

        self.reveal_btn = tk.Button(
            btn_row, text="Reveal (Space)", command=self._reveal_study_card,
            bg=ACCENT_COLOR, fg="#0a0a12", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=16, pady=8,
        )
        self.reveal_btn.pack(side="left", padx=6)

        self.got_it_btn = tk.Button(
            btn_row, text="Got it ✓", command=lambda: self._answer_study_card(True),
            bg=LEARNED_COLOR, fg="#0a0a12", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=16, pady=8, state="disabled",
        )
        self.got_it_btn.pack(side="left", padx=6)

        self.still_learning_btn = tk.Button(
            btn_row, text="Still learning", command=lambda: self._answer_study_card(False),
            bg="#f59e0b", fg="#0a0a12", font=("Segoe UI", 11, "bold"),
            relief="flat", padx=16, pady=8, state="disabled",
        )
        self.still_learning_btn.pack(side="left", padx=6)

        tk.Button(
            self.study_frame, text="← Back to list", command=self._exit_study_mode,
            bg=BG_COLOR, fg=DIM_TEXT_COLOR, relief="flat", font=SMALL_FONT,
        ).pack(pady=(10, 0))

        self.root.bind("<space>", lambda _e: self._reveal_study_card())

    def _show_study_card(self):
        if self.study_index >= len(self.study_queue):
            self.status_label.config(text="Study session complete.")
            self._exit_study_mode()
            return
        _id, surface_text, _translation, _dict_forms = self.study_queue[self.study_index]
        self.study_word.config(text=surface_text)
        self.study_reveal_frame.pack_forget()
        self.study_revealed = False
        self.reveal_btn.config(state="normal")
        self.got_it_btn.config(state="disabled")
        self.still_learning_btn.config(state="disabled")
        self.study_progress.config(
            text=f"Card {self.study_index + 1} of {len(self.study_queue)}"
        )

    def _reveal_study_card(self):
        if self.study_frame is None or not self.study_frame.winfo_ismapped() or self.study_revealed:
            return
        _id, _surface_text, translation, dict_forms = self.study_queue[self.study_index]
        self.study_translation.config(text=translation or "")
        self.study_dict_forms.config(text=dict_forms or "")
        self.study_reveal_frame.pack()
        self.study_revealed = True
        self.reveal_btn.config(state="disabled")
        self.got_it_btn.config(state="normal")
        self.still_learning_btn.config(state="normal")

    def _answer_study_card(self, got_it):
        row_id = self.study_queue[self.study_index][0]
        if got_it:
            self.conn.execute("UPDATE saved_words SET learned = 1 WHERE id = ?", (row_id,))
            self.conn.commit()
        self.study_index += 1
        self._show_study_card()

    def _exit_study_mode(self):
        self.root.unbind("<space>")
        if self.study_frame is not None:
            self.study_frame.pack_forget()
        self.list_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.refresh_list()


if __name__ == "__main__":
    from dashboard_app import DashboardApp

    DashboardApp().run()
