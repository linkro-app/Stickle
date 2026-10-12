"""The list of notes in the Stickle window: every note not deleted, shown or
hidden, and the trash, narrowed down by a search as it is typed.

It only reads the notes; opening, hiding, deleting, restoring and emptying go
through the note manager, as they do from a note's own window. Emptying from
the trash cannot be undone, so it is asked for first; deleting is not, as the
trash keeps the note.
"""

from collections.abc import Callable
from typing import override

from PySide6.QtCore import QDateTime, QEvent, QLocale, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.labels import category_item_icon, mark_item_icon, mark_name
from stickle.app.notes import NoteManager
from stickle.app.palette import swatch_icon
from stickle.core.colors import DEFAULT_COLOR, PALETTE
from stickle.core.labels import Category, Mark
from stickle.core.markdown import note_title
from stickle.core.note import Note

NOTE_ID = Qt.ItemDataRole.UserRole
ALL, SHOWN, HIDDEN, TRASH = "all", "shown", "hidden", "trash"
# Not ids: no category or mark is stored with these.
ANY, NONE = "*any*", "*none*"
# A note's text is stored a second after typing stops; the list follows a moment later.
REFRESH_DELAY_MS = 300
# Typing on narrows the list once the keys pause, not on every letter.
SEARCH_DELAY_MS = 150


def _ask(parent: QWidget, question: str) -> bool:
    answer = QMessageBox.question(
        parent,
        "Stickle",
        question,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


confirm: Callable[[QWidget, str], bool] = _ask  # replaced in tests


def deleted_on(note: Note) -> str:
    """The day the note was deleted, in the user's own way of writing dates."""
    moment = QDateTime.fromString(note.deleted_at or "", Qt.DateFormat.ISODateWithMs)
    return QLocale().toString(moment.toLocalTime().date(), QLocale.FormatType.ShortFormat)


class NoteList(QWidget):
    def __init__(self, notes: NoteManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._notes = notes
        self._categories: list[Category] = []
        self._marks: list[Mark] = []
        self.label = QLabel(self)
        self.filter_box = QComboBox(self)
        for key in (ALL, SHOWN, HIDDEN, TRASH):
            self.filter_box.addItem("", key)
        self.filter_box.currentIndexChanged.connect(self.refresh)
        # What the notes are about, and how they are marked: filled from what exists.
        self.category_box = QComboBox(self)
        self.category_box.currentIndexChanged.connect(self.refresh)
        self.mark_box = QComboBox(self)
        self.mark_box.currentIndexChanged.connect(self.refresh)
        # Ctrl+1 to Ctrl+9: a category, in their order; Ctrl+0: every note again.
        self.category_shortcuts: list[QShortcut] = []
        for digit in range(10):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{digit}"), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(lambda digit=digit: self.choose_category(digit))
            self.category_shortcuts.append(shortcut)
        # Text being composed by an input method is not searched until it is
        # committed: results would flicker with every jamo of a Korean syllable.
        self.search_box = QLineEdit(self)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.installEventFilter(self)
        self._search_soon = QTimer(self)
        self._search_soon.setSingleShot(True)
        self._search_soon.setInterval(SEARCH_DELAY_MS)
        self._search_soon.timeout.connect(self.refresh)
        self.search_box.textChanged.connect(self._search_soon.start)
        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.find_shortcut.activated.connect(self.start_search)
        self.list = QListWidget(self)
        self.label.setBuddy(self.list)
        self.list.itemActivated.connect(self._activate)  # double-click (Enter: eventFilter)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu_at)
        self.list.installEventFilter(self)
        self.empty_button = QPushButton(self)
        self.empty_button.clicked.connect(self.empty_trash)

        self._refresh_soon = QTimer(self)
        self._refresh_soon.setSingleShot(True)
        self._refresh_soon.setInterval(REFRESH_DELAY_MS)
        self._refresh_soon.timeout.connect(self.refresh)
        notes.changed.connect(self.refresh)
        notes.note_saved.connect(self._refresh_soon.start)

        top = QHBoxLayout()
        top.addWidget(self.label, 1)
        top.addWidget(self.filter_box)
        narrow = QHBoxLayout()
        narrow.addWidget(self.category_box, 1)
        narrow.addWidget(self.mark_box, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addLayout(narrow)
        layout.addWidget(self.search_box)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty_button)
        self.retranslate()

    def tab_order(self) -> list[QWidget]:
        """Its parts as they read, for the window's Tab order."""
        return [
            self.filter_box,
            self.category_box,
            self.mark_box,
            self.search_box,
            self.list,
            self.empty_button,
        ]

    @property
    def in_trash(self) -> bool:
        return self.filter_box.currentData() == TRASH

    def retranslate(self) -> None:
        self.label.setText(self.tr("&Notes"))
        self.list.setAccessibleName(self.tr("Notes"))
        self.filter_box.setAccessibleName(self.tr("Show"))
        self.filter_box.setItemText(0, self.tr("All notes"))
        self.filter_box.setItemText(1, self.tr("Notes on screen"))
        self.filter_box.setItemText(2, self.tr("Hidden notes"))
        self.filter_box.setItemText(3, self.tr("Trash"))
        self.category_box.setAccessibleName(self.tr("Category"))
        self.category_box.setAccessibleDescription(
            self.tr("Ctrl+1 to Ctrl+9 choose a category, Ctrl+0 every note.")
        )
        self.mark_box.setAccessibleName(self.tr("Mark"))
        self.search_box.setPlaceholderText(self.tr("Search notes"))
        self.search_box.setAccessibleName(self.tr("Search notes"))
        self.search_box.setAccessibleDescription(
            self.tr("The list keeps only notes containing this text. Down goes to the list.")
        )
        self.empty_button.setText(self.tr("Empty the trash…"))
        self.refresh()

    def _fill_filters(self) -> None:
        """The categories and marks there are now, keeping what was chosen if it
        still exists."""
        self._categories, self._marks = self._notes.label_choices()
        background = self.category_box.palette().base().color()
        ink = self.mark_box.palette().text().color()
        none = QIcon()
        boxes = (
            (
                self.category_box,
                [(ANY, self.tr("All categories"), none), (NONE, self.tr("No category"), none)]
                + [(c.id, c.name, category_item_icon(c, background)) for c in self._categories],
            ),
            (
                self.mark_box,
                [(ANY, self.tr("All marks"), none)]
                + [(m.id, mark_name(m), mark_item_icon(m, ink)) for m in self._marks],
            ),
        )
        for box, choices in boxes:
            chosen = box.currentData()
            box.blockSignals(True)
            box.clear()
            for key, text, icon in choices:
                box.addItem(icon, text, key)
            box.setCurrentIndex(max(0, box.findData(chosen)))
            box.blockSignals(False)

    def choose_category(self, digit: int) -> None:
        """Ctrl+digit: the digit-th category (if there is one), or every note for 0."""
        if digit == 0:
            self.category_box.setCurrentIndex(0)
        elif digit <= len(self._categories):
            index = self.category_box.findData(self._categories[digit - 1].id)
            self.category_box.setCurrentIndex(index)

    def _wanted(self, note: Note) -> bool:
        shown = self.filter_box.currentData()
        category = self.category_box.currentData()
        mark = self.mark_box.currentData()
        if category == NONE and note.label is not None:
            return False
        if category not in (ANY, NONE) and note.label != category:
            return False
        if mark != ANY and mark not in note.marks:
            return False
        if shown in (ALL, TRASH):
            return True
        return note.hidden if shown == HIDDEN else not note.hidden

    def _labels(self, note: Note) -> list[str]:
        """The note's category and marks, by name."""
        names = [c.name for c in self._categories if c.id == note.label]
        return names + [mark_name(m) for m in self._marks if m.id in note.marks]

    def _text(self, note: Note) -> str:
        title = note_title(note.body) or self.tr("(empty note)")
        if self.in_trash:
            deleted = self.tr("deleted %1").replace("%1", deleted_on(note))
            return " · ".join([title, *self._labels(note), deleted])
        states: list[str] = self._labels(note)
        if note.hidden:
            states.append(self.tr("hidden"))
        if not note.always_on_top:
            states.append(self.tr("not on top"))
        if note.locked:
            states.append(self.tr("locked"))
        return " · ".join([title, *states])

    @property
    def search_term(self) -> str:
        return self.search_box.text().strip()

    def start_search(self) -> None:
        self.search_box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_box.selectAll()

    def refresh(self) -> None:
        """Show the notes as they are stored now, keeping the one selected."""
        self._search_soon.stop()
        self._fill_filters()
        selected = self.selected_id()
        self.list.clear()
        if self.in_trash:
            trash = self._notes.trash_notes()
            notes = [note for note in trash if self._wanted(note)]
            self.list.setAccessibleDescription(
                self.tr("Enter brings the note back; Delete empties it from the trash for good.")
            )
        else:
            trash: list[Note] = []
            notes = [note for note in self._notes.listed_notes() if self._wanted(note)]
            self.list.setAccessibleDescription(
                self.tr("Enter opens the note, Delete deletes it; more in the context menu.")
            )
        term = self.search_term
        if term:
            found = self._notes.matching(term)
            notes = [note for note in notes if note.id in found]
        for note in notes:
            item = QListWidgetItem(self._text(note))
            item.setIcon(swatch_icon(note.color if note.color in PALETTE else DEFAULT_COLOR))
            item.setData(NOTE_ID, note.id)
            self.list.addItem(item)
            if note.id == selected:
                self.list.setCurrentItem(item)
        if not notes:
            if term:
                text = self.tr("No notes match")
            elif self.in_trash:
                text = self.tr("The trash is empty")
            else:
                text = self.tr("No notes here")
            empty = QListWidgetItem(text)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
        elif self.list.currentRow() < 0:
            self.list.setCurrentRow(0)
        self.empty_button.setVisible(self.in_trash)
        self.empty_button.setEnabled(bool(trash))  # the whole trash, whatever is searched

    def selected_id(self) -> str | None:
        row = self.list.currentRow()
        note_id = self.list.item(row).data(NOTE_ID) if row >= 0 else None
        return note_id if isinstance(note_id, str) else None

    def _activate(self, item: QListWidgetItem) -> None:
        note_id = item.data(NOTE_ID)
        if not isinstance(note_id, str):
            return
        if self.in_trash:
            self._notes.restore_note(note_id)
        else:
            self._notes.open_note(note_id)

    def _remove(self, note_id: str) -> None:
        """Delete key: into the trash, or out of it for good once confirmed."""
        if not self.in_trash:
            self._notes.delete_note(note_id)
            return
        question = self.tr("Empty this note from the trash? This cannot be undone.")
        if confirm(self, question):
            self._notes.purge_note(note_id)

    def empty_trash(self) -> None:
        question = self.tr("Empty the trash? Its notes will be gone for good.")
        if confirm(self, question):
            self._notes.empty_trash()

    def _search_key(self, key: int) -> bool:
        """Down or Enter take the results; Escape clears the search. Whether handled."""
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._search_soon.isActive():
                self.refresh()  # typed too quickly for the list to have caught up
            self.list.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        if key == Qt.Key.Key_Escape and self.search_box.text():
            self.search_box.clear()
            self.refresh()
            return True
        return False

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return super().eventFilter(watched, event)
        if watched is self.search_box and self._search_key(event.key()):
            return True
        if watched is self.list and event.key() == Qt.Key.Key_Delete:
            note_id = self.selected_id()
            if note_id is not None:
                self._remove(note_id)
            return True
        if watched is self.list and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Here rather than through itemActivated: list views on macOS take
            # Cmd+O for that, not Enter.
            if self.list.currentRow() >= 0:
                self._activate(self.list.currentItem())
            return True
        return super().eventFilter(watched, event)

    def _menu_at(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is None or not isinstance(item.data(NOTE_ID), str):
            return
        self.list.setCurrentItem(item)
        self.menu_for(item).popup(self.list.viewport().mapToGlobal(position))

    def _add(self, menu: QMenu, text: str, action: Callable[[], object]) -> None:
        item = QAction(text, menu)
        item.triggered.connect(action)
        menu.addAction(item)

    def menu_for(self, item: QListWidgetItem) -> QMenu:
        """What can be done to the note on this row."""
        note_id = str(item.data(NOTE_ID))
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if self.in_trash:
            self._add(menu, self.tr("Restore"), lambda: self._notes.restore_note(note_id))
            menu.addSeparator()
            self._add(menu, self.tr("Empty from the trash…"), lambda: self._remove(note_id))
            return menu
        note = next((n for n in self._notes.listed_notes() if n.id == note_id), None)
        if note is not None and note.hidden:
            self._add(menu, self.tr("Show"), lambda: self._notes.open_note(note_id))
        else:
            self._add(menu, self.tr("Open"), lambda: self._notes.open_note(note_id))
            self._add(menu, self.tr("Hide"), lambda: self._notes.hide_note(note_id))
        menu.addSeparator()
        self._add(menu, self.tr("Delete"), lambda: self._notes.delete_note(note_id))
        return menu
