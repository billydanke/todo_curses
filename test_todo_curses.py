import curses
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from todo_curses import TodoApp, TodoStore


class Screen:
    def __init__(self, keys=(), width=100):
        self.keys = iter(keys)
        self.width = width
        self.draws = []
        self.timeouts = []

    def getmaxyx(self):
        return 24, self.width

    def erase(self):
        self.draws = []

    def addstr(self, y, x, text, attr=0):
        assert 0 <= x < self.width
        assert x + len(text) < self.width
        self.draws.append((y, x, text, attr))

    def addnstr(self, y, x, text, count, attr=0):
        self.addstr(y, x, text[:count], attr)

    def move(self, y, x):
        assert 0 <= y < 24 and 0 <= x < self.width

    def refresh(self):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        try:
            return next(self.keys)
        except StopIteration:
            raise curses.error()


class ListsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "todos.sqlite3"
        self.store = None

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def open_store(self):
        self.store = TodoStore(self.path)
        return self.store

    def app(self):
        app = TodoApp(Screen(), self.open_store())
        app.red_pair, app.green_pair, app.dim_pair = 1, 2, 3
        app.help_pair, app.completed_text_pair = 4, 5
        return app

    def enter_name(self, app, key, name):
        app._handle_key(key)
        for char in name:
            app._handle_key(char)
        app._handle_key("\n")

    def test_migration_preserves_items_and_creates_one_backup(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, "
                         "completed INTEGER NOT NULL, position INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.executemany("INSERT INTO todos VALUES (?, ?, ?, ?, ?, ?)",
                             [(7, "Keep me", 1, 0, "created", "updated"), (8, "", 0, 1, "c", "u")])
        store = self.open_store()
        items = store.load()
        self.assertEqual([(i.id, i.text, i.completed, i.position) for i in items],
                         [(7, "Keep me", True, 0), (8, "", False, 1)])
        self.assertEqual(store.conn.execute("SELECT created_at, updated_at FROM todos WHERE id = 7").fetchone()[:],
                         ("created", "updated"))
        backups = list(self.path.parent.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0], 2)
            self.assertNotIn("list_id", [row[1] for row in conn.execute("PRAGMA table_info(todos)")])
        store.close()
        self.open_store()
        self.assertEqual(len(list(self.path.parent.glob("*.bak"))), 1)

    def test_store_list_isolation_move_and_persistence(self):
        store = self.open_store()
        work = store.create_list("Work")
        original = store.insert(0, "Original", True)
        other = store.insert(0, "Other", list_id=work.id)
        store.move_item(original, work.id)
        self.assertEqual(store.load(), [])
        self.assertEqual([i.id for i in store.load(work.id)], [other.id, original.id])
        self.assertTrue(store.load(work.id)[1].completed)
        self.assertFalse(store.delete_list(work.id))
        store.set_active_list(work.id)
        self.assertTrue(store.delete_list(1))
        store.close()
        store = self.open_store()
        self.assertEqual(store.active_list_id(), work.id)
        self.assertEqual([entry.name for entry in store.load_lists()], ["Work"])
        self.assertFalse(store.delete_list(work.id))

    def test_app_create_edit_toggle_move_and_delete_list(self):
        app = self.app()
        app._handle_key("A")
        app._handle_key("\t")
        original_id = app.items[0].id
        self.enter_name(app, curses.KEY_F2, "Work")
        work_id = app.list_id
        self.assertEqual(app.items, [])
        app._handle_key("B")
        app._handle_key(curses.KEY_F5)
        self.assertEqual(app.items[0].text, "A")
        app._handle_key(curses.KEY_F4)
        app._handle_key("\n")
        self.assertEqual(app.items, [])
        app._handle_key(curses.KEY_F8)
        app._handle_key("\n")
        self.assertEqual(app.list_id, work_id)
        self.assertEqual([i.text for i in app.items], ["B", "A"])
        self.assertEqual(app.items[1].id, original_id)
        self.assertTrue(app.items[1].completed)
        app._handle_key("\x04")
        self.assertEqual([i.text for i in self.store.load(work_id)], ["A"])

    def test_dialog_validation_cancel_and_rename(self):
        app = self.app()
        self.enter_name(app, curses.KEY_F2, " ")
        self.assertEqual(app.dialog, "create")
        self.assertIn("empty", app.message)
        app._handle_key("\x1b")
        self.enter_name(app, curses.KEY_F2, "general")
        self.assertEqual(app.dialog, "create")
        self.assertEqual(len(self.store.load_lists()), 1)
        app._handle_key("\x1b")
        app._handle_key(curses.KEY_F3)
        app._handle_key(curses.KEY_HOME)
        for _ in "General":
            app._handle_key(curses.KEY_DC)
        for char in "Personal":
            app._handle_key(char)
        app._handle_key("\n")
        self.assertEqual(self.store.load_lists()[0].name, "Personal")
        self.assertEqual(app.items, [])
        app._handle_key(curses.KEY_F8)
        self.assertIsNone(app.dialog)

    def test_selection_scroll_and_active_list_restored(self):
        app = self.app()
        for index in range(30):
            app._insert_below_selection()
            app._insert_text(str(index))
        saved = app.selected, app.cursor_col, app.scroll_top
        self.enter_name(app, curses.KEY_F2, "Work")
        app._handle_key(curses.KEY_F5)
        self.assertEqual((app.selected, app.cursor_col, app.scroll_top), saved)
        app._handle_key(curses.KEY_F6)
        reopened = TodoApp(Screen(), self.store)
        self.assertEqual(reopened.list_id, app.list_id)

    def test_alt_keys_and_escape_sequences(self):
        app = self.app()
        self.enter_name(app, curses.KEY_F2, "Work")
        work_id = app.list_id
        app._handle_key(getattr(curses, "ALT_LEFT", "alt-left"))
        self.assertEqual(app.list_id, 1)
        app._handle_key(getattr(curses, "ALT_RIGHT", "alt-right"))
        self.assertEqual(app.list_id, work_id)
        for sequence, expected in [(["\x1b", "[", "1", ";", "3", "D"], "alt-left"),
                                   (["\x1b", curses.KEY_RIGHT], "alt-right"),
                                   (["\x1b", curses.KEY_UP], "alt-up"),
                                   (["\x1b", curses.KEY_DOWN], "alt-down"),
                                   (list("\x1b[1;3A"), "alt-up"),
                                   (list("\x1b[1;3B"), "alt-down"),
                                   (list("\x1b[A"), "alt-up"),
                                   (list("\x1b[B"), "alt-down"),
                                   (list("\x1bOA"), "alt-up"),
                                   (list("\x1bOB"), "alt-down"),
                                   (["\x1b"], "\x1b"), (["\x1b", "z"], None)]:
            app.stdscr = Screen(sequence)
            self.assertEqual(app._read_key(), expected)
            self.assertEqual(app.stdscr.timeouts[-1], -1)
        self.assertTrue(app._handle_key("\x1b"))

    def test_reorder_preserves_item_and_other_list_and_persists(self):
        app = self.app()
        other = self.store.create_list("Other")
        other_item = self.store.insert(0, "Other list task", list_id=other.id)
        for text in ("First", "Second", "Third"):
            app._insert_below_selection()
            app._insert_text(text)
        app._toggle_status()
        selected_id = app._selected_item().id
        app.cursor_col = 2
        app._handle_key(getattr(curses, "ALT_UP", "alt-up"))
        self.assertEqual([i.text for i in app.items], ["First", "Third", "Second"])
        self.assertEqual(app.selected, 1)
        self.assertEqual(app._selected_item().id, selected_id)
        self.assertTrue(app._selected_item().completed)
        self.assertEqual(app.cursor_col, 2)
        app._handle_key(getattr(curses, "ALT_DOWN", "alt-down"))
        app._handle_key("alt-up")
        self.store.close()
        store = self.open_store()
        self.assertEqual([i.text for i in store.load(app.list_id)], ["First", "Third", "Second"])
        self.assertEqual([i.position for i in store.load(app.list_id)], [0, 1, 2])
        self.assertEqual(store.load(other.id), [other_item])

    def test_reorder_boundaries_and_dialogs_do_not_change_items(self):
        app = self.app()
        app._handle_key("alt-up")
        app._handle_key("alt-down")
        self.assertEqual(app.items, [])
        self.assertIsNone(app.selected)
        app._insert_text("Only task")
        for key in ("alt-up", "alt-down"):
            with patch.object(self.store, "update_positions") as save:
                app._handle_key(key)
                save.assert_not_called()
            self.assertEqual(app.selected, 0)
        app._insert_below_selection()
        app._insert_text("Second task")
        app._open_dialog("rename")
        original_name = app.dialog_text
        app._handle_key("alt-up")
        self.assertEqual([i.text for i in app.items], ["Only task", "Second task"])
        self.assertEqual(app.dialog, "rename")
        self.assertEqual(app.dialog_text, original_name)

    def test_reorder_scroll_follows_selected_item(self):
        app = self.app()
        for index in range(25):
            app._insert_below_selection()
            app._insert_text(str(index))
        app.selected = app.scroll_top = 5
        selected_id = app._selected_item().id
        app._handle_key("alt-up")
        self.assertEqual((app.selected, app.scroll_top), (4, 4))
        self.assertEqual(app._selected_item().id, selected_id)
        for _ in range(20):
            app._handle_key("alt-down")
        self.assertEqual(app.selected, 24)
        self.assertEqual(app._selected_item().id, selected_id)
        self.assertLess(app.selected, app.scroll_top + app._visible_height())

    def test_footer_and_dialog_rendering_at_narrow_widths(self):
        app = self.app()
        for index in range(12):
            self.enter_name(app, curses.KEY_F2, f"Category {index} with a long name")
        for width in (30, 55, 100):
            app.stdscr = Screen(width=width)
            with patch("curses.color_pair", return_value=0):
                app._render()
                footer = [draw for draw in app.stdscr.draws if draw[0] == 23]
                self.assertTrue(any(draw[3] & curses.A_REVERSE for draw in footer))
                self.assertTrue(any(draw[2].endswith(" ]") for draw in footer))
                app._handle_key(curses.KEY_F3)
                app._render()
                self.assertTrue(any("Rename list:" in draw[2] for draw in app.stdscr.draws))
                app._handle_key("\x1b")


if __name__ == "__main__":
    unittest.main()
