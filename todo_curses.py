"""A tiny persistent todo app using curses and SQLite.

Windows setup:
    py -m pip install windows-curses

Run:
    py todo_curses.py

Controls:
    Enter       Insert a new incomplete todo below the selected item
    Up/Down     Select previous/next item
    Tab         Toggle selected item between Incomplete and Complete
    Type text   Edit the selected item
    Backspace   Delete the character before the cursor
    Delete      Delete the character under the cursor
    Home/End    Move to start/end of the selected item text
    Ctrl+A/E    Move to start/end of the selected item text
    Ctrl+K      Delete from cursor to end of line
    Ctrl+D      Delete selected todo
    Alt+Left/Right  Switch lists (F5/F6 also work)
    Alt+Up/Down     Move selected todo up/down within its list
    F2/F3       Create/rename list
    F4          Move selected todo to another list
    F8          Delete the current empty list
    Esc/Ctrl+Q  Quit
"""

from __future__ import annotations

import curses
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_FILE = "todo_items.sqlite3"
INCOMPLETE = "   "
COMPLETE = " x "


@dataclass
class TodoItem:
    id: int
    text: str
    completed: bool
    position: int


@dataclass
class TodoList:
    id: int
    name: str


class TodoStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def _init_db(self) -> None:
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'todos'"
        ).fetchone()
        needs_migration = existing and "list_id" not in {
            row["name"] for row in self.conn.execute("PRAGMA table_info(todos)")
        }
        if needs_migration:
            backup_path = self.db_path.with_name(
                self.db_path.name + ".before-lists-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak"
            )
            with closing(sqlite3.connect(str(backup_path))) as backup:
                self.conn.backup(backup)
        with self.conn:
            self.conn.execute("BEGIN")
            self._create_schema()

    def _create_schema(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS lists (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL COLLATE NOCASE UNIQUE)"
        )
        self.conn.execute("INSERT INTO lists (id, name) SELECT 1, 'General' WHERE NOT EXISTS (SELECT 1 FROM lists)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL DEFAULT '',
                completed INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                list_id INTEGER NOT NULL DEFAULT 1 REFERENCES lists(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        if "list_id" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(todos)")}:
            self.conn.execute("ALTER TABLE todos ADD COLUMN list_id INTEGER NOT NULL DEFAULT 1 REFERENCES lists(id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS todos_by_list ON todos(list_id, position, id)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def load_lists(self) -> list[TodoList]:
        return [TodoList(int(row["id"]), str(row["name"]))
                for row in self.conn.execute("SELECT id, name FROM lists ORDER BY id")]

    def create_list(self, name: str) -> TodoList:
        with self.conn:
            cur = self.conn.execute("INSERT INTO lists (name) VALUES (?)", (name,))
        return TodoList(int(cur.lastrowid), name)

    def rename_list(self, list_id: int, name: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name, list_id))

    def delete_list(self, list_id: int) -> bool:
        with self.conn:
            if self.conn.execute("SELECT 1 FROM todos WHERE list_id = ?", (list_id,)).fetchone():
                return False
            if self.conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0] <= 1:
                return False
            self.conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        return True

    def active_list_id(self) -> int:
        row = self.conn.execute("SELECT value FROM settings WHERE key = 'active_list'").fetchone()
        try:
            return int(row[0]) if row else 1
        except ValueError:
            return 1

    def set_active_list(self, list_id: int) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_list', ?)", (str(list_id),))

    def move_item(self, item: TodoItem, list_id: int) -> None:
        with self.conn:
            position = self.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM todos WHERE list_id = ?", (list_id,)
            ).fetchone()[0]
            self.conn.execute("UPDATE todos SET list_id = ?, position = ?, updated_at = ? WHERE id = ?",
                              (list_id, position, _utc_now(), item.id))

    def load(self, list_id: int = 1) -> list[TodoItem]:
        rows = self.conn.execute(
            "SELECT id, text, completed, position FROM todos WHERE list_id = ? ORDER BY position, id",
            (list_id,),
        ).fetchall()
        return [
            TodoItem(
                id=int(row["id"]),
                text=str(row["text"]),
                completed=bool(row["completed"]),
                position=int(row["position"]),
            )
            for row in rows
        ]

    def insert(self, position: int, text: str = "", completed: bool = False, list_id: int = 1) -> TodoItem:
        now = _utc_now()
        cur = self.conn.execute(
            """
            INSERT INTO todos (text, completed, position, created_at, updated_at, list_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (text, int(completed), position, now, now, list_id),
        )
        self.conn.commit()
        return TodoItem(
            id=int(cur.lastrowid), text=text, completed=completed, position=position
        )

    def update_text(self, item: TodoItem) -> None:
        self.conn.execute(
            "UPDATE todos SET text = ?, updated_at = ? WHERE id = ?",
            (item.text, _utc_now(), item.id),
        )
        self.conn.commit()

    def update_completed(self, item: TodoItem) -> None:
        self.conn.execute(
            "UPDATE todos SET completed = ?, updated_at = ? WHERE id = ?",
            (int(item.completed), _utc_now(), item.id),
        )
        self.conn.commit()

    def update_positions(self, items: list[TodoItem]) -> None:
        for idx, item in enumerate(items):
            item.position = idx
            self.conn.execute(
                "UPDATE todos SET position = ?, updated_at = ? WHERE id = ?",
                (idx, _utc_now(), item.id),
            )
        self.conn.commit()

    def delete(self, item: TodoItem) -> None:
        self.conn.execute("DELETE FROM todos WHERE id = ?", (item.id,))
        self.conn.commit()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / DB_FILE


class TodoApp:
    def __init__(self, stdscr: "curses._CursesWindow", store: TodoStore) -> None:
        self.stdscr = stdscr
        self.store = store
        self.lists = store.load_lists()
        self.list_id = store.active_list_id()
        if self.list_id not in [entry.id for entry in self.lists]:
            self.list_id = self.lists[0].id
        self.items = store.load(self.list_id)
        self.view_states: dict[int, tuple[Optional[int], int, int]] = {}
        self.dialog: Optional[str] = None
        self.dialog_text = ""
        self.dialog_cursor = 0
        self.move_targets: list[TodoList] = []
        self.move_index = 0
        self.selected: Optional[int] = 0 if self.items else None
        self.cursor_col = 0
        self.scroll_top = 0
        self.message = "Press Enter to add a todo. Esc or Ctrl+Q quits."

    def run(self) -> None:
        curses.curs_set(1)
        self.stdscr.keypad(True)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
        curses.start_color()
        
        try:
            curses.use_default_colors()
            curses.assume_default_colors(-1, -1)
        except curses.error:
            pass

        self._setup_colors()

        self.stdscr.bkgd(" ", curses.color_pair(0))
        self.stdscr.refresh()

        while True:
            self._render()
            try:
                key = self._read_key()
            except KeyboardInterrupt:
                break
            if self._handle_key(key):
                break

    def _read_key(self) -> object:
        key = self.stdscr.get_wch()
        if key != "\x1b":
            return key
        # Terminals may send Alt+arrow as Escape followed by an arrow sequence.
        self.stdscr.timeout(40)
        try:
            try:
                following = self.stdscr.get_wch()
            except curses.error:
                return key
            arrows = {curses.KEY_LEFT: "alt-left", curses.KEY_RIGHT: "alt-right",
                      curses.KEY_UP: "alt-up", curses.KEY_DOWN: "alt-down"}
            if following in arrows:
                return arrows[following]
            sequence = str(following)
            if sequence in ("[", "O"):
                for _ in range(12):
                    try:
                        part = self.stdscr.get_wch()
                    except curses.error:
                        break
                    sequence += str(part)
                    if isinstance(part, str) and part.isalpha():
                        break
                if sequence in ("[D", "[1;3D", "OD"):
                    return "alt-left"
                if sequence in ("[C", "[1;3C", "OC"):
                    return "alt-right"
                if sequence in ("[A", "[1;3A", "OA"):
                    return "alt-up"
                if sequence in ("[B", "[1;3B", "OB"):
                    return "alt-down"
            return None
        finally:
            self.stdscr.timeout(-1)

    def _switch_list(self, list_id: int) -> None:
        self.view_states[self.list_id] = (self.selected, self.cursor_col, self.scroll_top)
        self.list_id = list_id
        self.items = self.store.load(list_id)
        self.selected, self.cursor_col, self.scroll_top = self.view_states.get(
            list_id, (0 if self.items else None, 0, 0)
        )
        if self.items:
            self.selected = max(0, min(self.selected or 0, len(self.items) - 1))
            self.cursor_col = min(self.cursor_col, len(self.items[self.selected].text))
        else:
            self.selected, self.cursor_col, self.scroll_top = None, 0, 0
        self._keep_selection_visible()
        self.store.set_active_list(list_id)
        self.message = next(entry.name for entry in self.lists if entry.id == list_id)

    def _cycle_list(self, delta: int) -> None:
        index = next(i for i, entry in enumerate(self.lists) if entry.id == self.list_id)
        self._switch_list(self.lists[(index + delta) % len(self.lists)].id)

    def _open_dialog(self, kind: str) -> None:
        if kind == "move":
            if self._selected_item() is None:
                self.message = "Select a todo to move."
                return
            self.move_targets = [entry for entry in self.lists if entry.id != self.list_id]
            if not self.move_targets:
                self.message = "Create another list with F2 first."
                return
            self.move_index = 0
        if kind == "delete" and (self.items or len(self.lists) == 1):
            self.message = "Only empty lists can be deleted; keep at least one list."
            return
        self.dialog = kind
        self.dialog_text = next(entry.name for entry in self.lists if entry.id == self.list_id) if kind == "rename" else ""
        self.dialog_cursor = len(self.dialog_text)

    def _handle_dialog(self, key: object) -> None:
        if key == "\x1b":
            self.dialog = None
            self.message = "Cancelled."
            return
        if key == curses.KEY_RESIZE:
            return
        if self.dialog == "move":
            if key in (curses.KEY_LEFT, curses.KEY_UP):
                self.move_index = (self.move_index - 1) % len(self.move_targets)
            elif key in (curses.KEY_RIGHT, curses.KEY_DOWN, "\t"):
                self.move_index = (self.move_index + 1) % len(self.move_targets)
        elif self.dialog in ("create", "rename"):
            if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                self.dialog_cursor = max(0, min(len(self.dialog_text), self.dialog_cursor + (-1 if key == curses.KEY_LEFT else 1)))
            elif key in (curses.KEY_HOME, "\x01"):
                self.dialog_cursor = 0
            elif key in (curses.KEY_END, "\x05"):
                self.dialog_cursor = len(self.dialog_text)
            elif key in (curses.KEY_BACKSPACE, 127, 8, "\b", "\x7f"):
                if self.dialog_cursor:
                    self.dialog_text = self.dialog_text[:self.dialog_cursor - 1] + self.dialog_text[self.dialog_cursor:]
                    self.dialog_cursor -= 1
            elif key == curses.KEY_DC:
                self.dialog_text = self.dialog_text[:self.dialog_cursor] + self.dialog_text[self.dialog_cursor + 1:]
            elif isinstance(key, str) and len(key) == 1 and key.isprintable() and len(self.dialog_text) < 40:
                self.dialog_text = self.dialog_text[:self.dialog_cursor] + key + self.dialog_text[self.dialog_cursor:]
                self.dialog_cursor += len(key)
        if key not in ("\n", "\r", curses.KEY_ENTER):
            return
        kind = self.dialog
        if kind in ("create", "rename"):
            name = self.dialog_text.strip()
            if not name:
                self.message = "List name cannot be empty."
                return
            try:
                if kind == "create":
                    entry = self.store.create_list(name)
                else:
                    self.store.rename_list(self.list_id, name)
            except sqlite3.IntegrityError:
                self.message = "A list with that name already exists."
                return
            self.lists = self.store.load_lists()
            if kind == "create":
                self._switch_list(entry.id)
            self.message = "List created." if kind == "create" else "List renamed."
        elif kind == "move":
            item = self._selected_item()
            target = self.move_targets[self.move_index]
            self.store.move_item(item, target.id)
            del self.items[self.selected]
            self.store.update_positions(self.items)
            self.selected = min(self.selected, len(self.items) - 1) if self.items else None
            self.cursor_col = len(self.items[self.selected].text) if self.items else 0
            self._keep_selection_visible()
            self.message = f"Moved todo to {target.name}."
        elif kind == "delete":
            if self.store.delete_list(self.list_id):
                self.lists = self.store.load_lists()
                old_id = self.list_id
                self._switch_list(self.lists[0].id)
                self.view_states.pop(old_id, None)
                self.message = "Deleted empty list."
        self.dialog = None

    def _setup_colors(self) -> None:
        #curses.start_color()
        self.red_pair = 1
        self.green_pair = 2
        self.dim_pair = 3
        self.help_pair = 4
        self.completed_text_pair = 5

        # Try to define a vivid lime green when the terminal supports it.
        lime_color = curses.COLOR_GREEN
        if curses.can_change_color() and curses.COLORS > 10:
            try:
                lime_color = 10
                curses.init_color(lime_color, 0, 1000, 0)
            except curses.error:
                lime_color = curses.COLOR_GREEN

        curses.init_pair(self.red_pair, curses.COLOR_RED, -1)
        curses.init_pair(self.green_pair, lime_color, -1)
        curses.init_pair(self.dim_pair, curses.COLOR_CYAN, -1)
        curses.init_pair(self.help_pair, curses.COLOR_YELLOW, -1)

        gray_color = curses.COLOR_WHITE
        if curses.COLORS > 8:
            gray_color = 8
        
        curses.init_pair(self.completed_text_pair, gray_color, -1)

    def _handle_key(self, key: object) -> bool:
        if key == "\x11":
            return True
        if self.dialog:
            self._handle_dialog(key)
            return False
        if key in ("alt-up", getattr(curses, "ALT_UP", -102)):
            self._reorder_selected(-1)
            return False
        if key in ("alt-down", getattr(curses, "ALT_DOWN", -103)):
            self._reorder_selected(1)
            return False
        if key in ("alt-left", getattr(curses, "ALT_LEFT", -100), curses.KEY_F5):
            self._cycle_list(-1)
            return False
        if key in ("alt-right", getattr(curses, "ALT_RIGHT", -101), curses.KEY_F6):
            self._cycle_list(1)
            return False
        commands = {curses.KEY_F2: "create", curses.KEY_F3: "rename",
                    curses.KEY_F4: "move", curses.KEY_F8: "delete"}
        if key in commands:
            self._open_dialog(commands[key])
            return False
        if key in ("\x1b", "\x11"):  # Esc or Ctrl+Q
            return True

        if key == curses.KEY_RESIZE:
            return False

        if key == curses.KEY_MOUSE:
            self._handle_mouse()
            return False

        if key in (curses.KEY_UP, "\x10"):  # Up or Ctrl+P
            self._move_selection(-1)
            return False

        if key in (curses.KEY_DOWN, "\x0e"):  # Down or Ctrl+N
            self._move_selection(1)
            return False
        
        if key in (curses.KEY_LEFT, "\x02"):
            self._move_cursor(-1)
            return False

        if key in (curses.KEY_RIGHT, "\x06"):
            self._move_cursor(1)
            return False

        if key in (9, "\t"):
            self._toggle_status()
            return False

        if key in ("\n", "\r", curses.KEY_ENTER):
            self._insert_below_selection()
            return False

        if key in (curses.KEY_BACKSPACE, 127, 8, "\b", "\x7f"):
            self._backspace()
            return False

        if key == curses.KEY_DC:
            self._delete_char()
            return False

        if key in (curses.KEY_HOME, "\x01"):
            self.cursor_col = 0
            return False

        if key in (curses.KEY_END, "\x05"):
            item = self._selected_item()
            self.cursor_col = len(item.text) if item else 0
            return False

        if key == "\x0b":  # Ctrl+K
            self._delete_to_end()
            return False

        if key == "\x04":  # Ctrl+D
            self._delete_selected_item()
            return False

        if isinstance(key, str) and key.isprintable():
            self._insert_text(key)
            return False

        return False

    def _handle_mouse(self) -> None:
        try:
            _mouse_id, _x, _y, _z, bstate = curses.getmouse()
        except curses.error:
            return

        scroll_up = getattr(curses, "BUTTON4_PRESSED", 0)
        scroll_down = getattr(curses, "BUTTON5_PRESSED", 0)

        if scroll_up and bstate & scroll_up:
            self._move_selection(-1)
        elif scroll_down and bstate & scroll_down:
            self._move_selection(1)

    def _header_height(self) -> int:
        return 5

    def _visible_height(self) -> int:
        height, _ = self.stdscr.getmaxyx()
        return max(1, height - self._header_height() - 1)

    def _move_selection(self, delta: int) -> None:
        if not self.items:
            self.selected = None
            self.cursor_col = 0
            return
        current = 0 if self.selected is None else self.selected
        self.selected = max(0, min(len(self.items) - 1, current + delta))
        self.cursor_col = len(self.items[self.selected].text)
        self._keep_selection_visible()

    def _reorder_selected(self, delta: int) -> None:
        if self._selected_item() is None:
            self.message = "No todo selected. Press Enter to create one."
            return
        target = self.selected + delta
        if not 0 <= target < len(self.items):
            self.message = "Already at the top of this list." if delta < 0 else "Already at the bottom of this list."
            return
        self.items[self.selected], self.items[target] = self.items[target], self.items[self.selected]
        self.selected = target
        self.store.update_positions(self.items)
        self._keep_selection_visible()
        self.message = "Moved todo up." if delta < 0 else "Moved todo down."

    def _move_cursor(self, delta: int) -> None:
        item = self._selected_item()
        max_col = len(item.text) if item else 0
        # Keep cursor position between 0 and the length of the string
        self.cursor_col = max(0, min(max_col, self.cursor_col + delta))

    def _keep_selection_visible(self) -> None:
        if self.selected is None:
            self.scroll_top = 0
            return
        visible_height = self._visible_height()
        if self.selected < self.scroll_top:
            self.scroll_top = self.selected
        elif self.selected >= self.scroll_top + visible_height:
            self.scroll_top = self.selected - visible_height + 1

    def _selected_item(self) -> Optional[TodoItem]:
        if self.selected is None or not self.items:
            return None
        if self.selected < 0 or self.selected >= len(self.items):
            return None
        return self.items[self.selected]

    def _toggle_status(self) -> None:
        item = self._selected_item()
        if item is None:
            self.message = "No todo selected. Press Enter to create one."
            return
        item.completed = not item.completed
        self.store.update_completed(item)

        if (item.completed):
            self.message = "Marked selected item as Complete."
        else:
            self.message = "Marked selected item as Incomplete."

        status = COMPLETE if item.completed else INCOMPLETE

    def _insert_below_selection(self) -> None:
        insert_at = 0 if self.selected is None else self.selected + 1
        item = self.store.insert(position=insert_at, list_id=self.list_id)
        self.items.insert(insert_at, item)
        self.store.update_positions(self.items)
        self.selected = insert_at
        self.cursor_col = 0
        self._keep_selection_visible()
        self.message = "Created a new incomplete todo. Start typing to edit it."

    def _insert_text(self, text: str) -> None:
        item = self._ensure_selected_item()
        if item is None:
            return
        #self.cursor_col = max(0, min(self.cursor_col, len(item.text)))
        #item.text = item.text[: self.cursor_col] + text + item.text[self.cursor_col :]
        #self.cursor_col += len(text)
        #self.store.update_text(item)
        #self.message = "Saved."

        # Slice and insert the character at the current cursor position
        item.text = item.text[:self.cursor_col] + text + item.text[self.cursor_col:]
        self.cursor_col += len(text)
        self.store.update_text(item)

    def _backspace(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if self.cursor_col <= 0:
            return
        self.cursor_col = min(self.cursor_col, len(item.text))
        item.text = item.text[: self.cursor_col - 1] + item.text[self.cursor_col :]
        self.cursor_col -= 1
        self.store.update_text(item)
        self.message = "Saved."

    def _delete_char(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self.cursor_col = max(0, min(self.cursor_col, len(item.text)))
        if self.cursor_col >= len(item.text):
            return
        item.text = item.text[: self.cursor_col] + item.text[self.cursor_col + 1 :]
        self.store.update_text(item)
        self.message = "Saved."

    def _delete_to_end(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self.cursor_col = max(0, min(self.cursor_col, len(item.text)))
        item.text = item.text[: self.cursor_col]
        self.store.update_text(item)
        self.message = "Saved."

    def _delete_selected_item(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self.store.delete(item)
        del self.items[self.selected]
        if not self.items:
            self.selected = None
            self.cursor_col = 0
            self.scroll_top = 0
        else:
            self.selected = min(self.selected, len(self.items) - 1)
            self.cursor_col = len(self.items[self.selected].text)
            self.store.update_positions(self.items)
            self._keep_selection_visible()
        self.message = "Deleted selected todo."

    def _ensure_selected_item(self) -> Optional[TodoItem]:
        if self._selected_item() is not None:
            return self._selected_item()
        self._insert_below_selection()
        return self._selected_item()

    def _render(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        if height < 8 or width < 30:
            self._safe_addstr(0, 0, "Terminal too small.")
            self.stdscr.refresh()
            return

        self._safe_addstr(0, 0, "SQLite Todo", curses.A_BOLD)
        db_info = f"DB: {self.store.db_path}"
        self._safe_addstr(0, max(0, width - len(db_info) - 1), db_info[: width - 1])
        self._safe_addstr(
            1,
            0,
            "Enter: New | Arrows: Navigate | Alt+Up/Down: Reorder | Tab: Toggle | Ctrl+D: Delete | Esc/Ctrl+Q: Exit",
            curses.color_pair(self.help_pair),
        )
        self._safe_addstr(2, 0,
                          "Alt+Left/Right (F5/F6): Switch | F2: New list | F3: Rename | F4: Move todo | F8: Delete empty list",
                          curses.color_pair(self.help_pair))
        self._safe_addstr(3, 0, self.message[: width - 1], curses.color_pair(self.dim_pair))
        self._draw_separator(4, width)

        self._keep_selection_visible()
        start_y = self._header_height()
        visible_height = self._visible_height()
        visible_items = self.items[self.scroll_top : self.scroll_top + visible_height]
        self._draw_footer(height, width, visible_height)

        if not self.items:
            self._safe_addstr(start_y, 0, "> [", curses.A_BOLD)
            self._safe_addstr(start_y, 3, INCOMPLETE, curses.color_pair(self.red_pair))
            self._safe_addstr(start_y, 3 + len(INCOMPLETE), "] ")
            self._safe_addstr(start_y + 1, 0, "Press Enter to create your first todo.")
            try:
                self.stdscr.move(start_y, 3 + len(INCOMPLETE) + 2)
            except curses.error:
                pass
            self._finish_render(width)
            return

        cursor_y: Optional[int] = None
        cursor_x: Optional[int] = None
        for row, item in enumerate(visible_items):
            index = self.scroll_top + row
            y = start_y + row
            is_selected = index == self.selected
            x_after_status = self._draw_item_prefix(y, item, is_selected, width)
            text_width = max(0, width - x_after_status - 1)
            text = item.text
            if not text:
                placeholder = "-"
                attr = curses.A_DIM
                if is_selected:
                    self._safe_addnstr(y, x_after_status, placeholder, text_width, attr)
                else:
                    self._safe_addnstr(y, x_after_status, placeholder, text_width, attr)
            else:
                if is_selected:
                    offset = max(0, min(self.cursor_col, len(text)) - text_width + 1)
                else:
                    offset = 0
                display_text = text[offset : offset + text_width]
                text_attr = curses.color_pair(self.completed_text_pair) if item.completed else 0
                self._safe_addnstr(y, x_after_status, display_text, text_width, text_attr)

            if is_selected:
                if text_width > 0:
                    offset = max(0, min(self.cursor_col, len(text)) - text_width + 1)
                    cursor_y = y
                    cursor_x = x_after_status + max(0, min(text_width - 1, self.cursor_col - offset))
                else:
                    cursor_y = y
                    cursor_x = max(0, width - 1)

        if cursor_y is not None and cursor_x is not None:
            try:
                self.stdscr.move(cursor_y, min(cursor_x, width - 1))
            except curses.error:
                pass
        self._finish_render(width)

    def _draw_footer(self, height: int, width: int, visible_height: int) -> None:
        footer = f"{len(self.items)} todo(s)"
        if len(self.items) > visible_height:
            first = self.scroll_top + 1
            last = min(len(self.items), self.scroll_top + visible_height)
            footer += f" | showing {first}-{last}"
        # Keep the active tab visible even with many lists or a narrow terminal.
        available = max(10, width - min(len(footer), width // 3) - 3)
        label_width = max(5, min(24, available - 4))
        labels = [f"[ {entry.name[:label_width - 4]} ]" if len(entry.name) <= label_width - 4
                  else f"[ {entry.name[:label_width - 5]}… ]" for entry in self.lists]
        active = next(i for i, entry in enumerate(self.lists) if entry.id == self.list_id)
        start = end = active
        used = len(labels[active])
        while True:
            if start > 0 and used + len(labels[start - 1]) + 1 <= available - 4:
                start -= 1
                used += len(labels[start]) + 1
            elif end < len(labels) - 1 and used + len(labels[end + 1]) + 1 <= available - 4:
                end += 1
                used += len(labels[end]) + 1
            else:
                break
        prefix = "< " if start else ""
        suffix = " >" if end < len(labels) - 1 else ""
        x = width - 1 - used - len(prefix) - len(suffix)
        self._safe_addstr(height - 1, 0, footer[:max(0, x - 1)], curses.A_DIM)
        self._safe_addstr(height - 1, x, prefix, curses.A_DIM)
        x += len(prefix)
        for index in range(start, end + 1):
            attr = curses.A_REVERSE | curses.A_BOLD if index == active else curses.A_DIM
            self._safe_addstr(height - 1, x, labels[index], attr)
            x += len(labels[index]) + 1
        self._safe_addstr(height - 1, x - 1, suffix, curses.A_DIM)

    def _finish_render(self, width: int) -> None:
        if self.dialog:
            if self.dialog in ("create", "rename"):
                prompt = "New list: " if self.dialog == "create" else "Rename list: "
                room = max(1, width - len(prompt) - 2)
                offset = max(0, self.dialog_cursor - room + 1)
                line = prompt + self.dialog_text[offset:offset + room]
                cursor_x = min(width - 2, len(prompt) + self.dialog_cursor - offset)
            elif self.dialog == "move":
                line = f"Move to: < {self.move_targets[self.move_index].name} > (Arrows: choose)"
                cursor_x = 0
            else:
                name = next(entry.name for entry in self.lists if entry.id == self.list_id)
                line = f'Delete empty list "{name}"?'
                cursor_x = 0
            self._safe_addstr(4, 0, " " * (width - 1))
            self._safe_addstr(4, 0, line, curses.color_pair(self.help_pair) | curses.A_BOLD)
            self._safe_addstr(2, 0, " " * (width - 1))
            self._safe_addstr(2, 0, "Enter: Confirm | Esc: Cancel | Ctrl+Q: Exit", curses.color_pair(self.help_pair))
            try:
                self.stdscr.move(4, cursor_x)
            except curses.error:
                pass
        self.stdscr.refresh()

    def _draw_item_prefix(self, y: int, item: TodoItem, is_selected: bool, width: int) -> int:
        selected_marker = ">" if is_selected else " "
        status = COMPLETE if item.completed else INCOMPLETE
        status_pair = self.green_pair if item.completed else self.red_pair
        x = 0
        self._safe_addstr(y, x, selected_marker, curses.A_BOLD if is_selected else 0)
        x += 2
        self._safe_addstr(y, x, "[")
        x += 1
        self._safe_addstr(y, x, status[: max(0, width - x - 1)], curses.color_pair(status_pair) | curses.A_BOLD)
        x += len(status)
        self._safe_addstr(y, x, "] ")
        x += 2
        return min(x, max(0, width - 1))

    def _draw_separator(self, y: int, width: int) -> None:
        self._safe_addstr(y, 0, "-" * max(0, width - 1), curses.A_DIM)

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            height, width = self.stdscr.getmaxyx()
            if y < 0 or y >= height or x < 0 or x >= width:
                return
            self.stdscr.addstr(y, x, text[: max(0, width - x - 1)], attr)
        except curses.error:
            pass

    def _safe_addnstr(self, y: int, x: int, text: str, max_chars: int, attr: int = 0) -> None:
        try:
            height, width = self.stdscr.getmaxyx()
            if y < 0 or y >= height or x < 0 or x >= width or max_chars <= 0:
                return
            self.stdscr.addnstr(y, x, text, min(max_chars, width - x - 1), attr)
        except curses.error:
            pass


def main(stdscr: "curses._CursesWindow") -> None:
    store = TodoStore(_db_path())
    try:
        TodoApp(stdscr, store).run()
    finally:
        store.close()


if __name__ == "__main__":
    curses.wrapper(main)
