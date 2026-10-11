"""Managing categories and marks: making, renaming, recolouring, giving another
icon, reordering and deleting them.

Deleting asks first. With notes in a category, the user chooses between taking the
category off them, which cannot be undone and is said so, and moving them to
the trash with it, asked once more with how many go, hidden and locked ones
included; the trash keeps them for a year. A mark deleted only comes off its notes,
which cannot be undone either and is said so.
"""

from collections.abc import Callable
from typing import Literal, override

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stickle.app import category_dialog
from stickle.app import notes as notes_module
from stickle.app.category_dialog import next_dot_color
from stickle.app.category_icons import category_icon
from stickle.app.labels import dot_color, icon_name, mark_icon, mark_name
from stickle.app.notes import NoteManager
from stickle.app.palette import color_name, swatch_icon
from stickle.core.colors import PALETTE
from stickle.core.labels import BUILT_IN_MARKS, MARK_ICONS, NAME_LENGTH, Category, Mark
from stickle.data.labels import CategoryNameError, MarkNameError

LABEL_ID = Qt.ItemDataRole.UserRole

type Removal = Literal["category", "notes"] | None


def _ask_how(parent: QWidget, question: str, category_only: str, with_notes: str) -> Removal:
    box = QMessageBox(QMessageBox.Icon.Warning, "Stickle", question, parent=parent)
    only = box.addButton(category_only, QMessageBox.ButtonRole.DestructiveRole)
    notes = box.addButton(with_notes, QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    clicked = box.clickedButton()
    return "category" if clicked is only else "notes" if clicked is notes else None


def _ask(parent: QWidget, question: str) -> bool:
    answer = QMessageBox.question(
        parent,
        "Stickle",
        question,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


# Replaced in tests.
ask_how: Callable[[QWidget, str, str, str], Removal] = _ask_how
confirm: Callable[[QWidget, str], bool] = _ask


class CategoryPage(QWidget):
    """The categories tab: a list and what can be done to the one chosen."""

    def __init__(self, notes: NoteManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._notes = notes

        self.label = QLabel(self)
        self.list = QListWidget(self)
        self.label.setBuddy(self.list)
        self.list.setEditTriggers(
            QListWidget.EditTrigger.EditKeyPressed | QListWidget.EditTrigger.DoubleClicked
        )
        self.list.itemChanged.connect(self._renamed)
        self.list.currentRowChanged.connect(self._enable)
        self.list.installEventFilter(self)
        self.problem = QLabel(self)
        self.problem.setWordWrap(True)
        self.problem.hide()

        self.new_button = QPushButton(self)
        self.new_button.clicked.connect(self.new_category)
        self.rename_button = QPushButton(self)
        self.rename_button.clicked.connect(self._rename)
        self.color_button = QPushButton(self)
        self.color_menu = QMenu(self.color_button)
        for key in PALETTE:
            action = self.color_menu.addAction(swatch_icon(key), "")
            action.setData(key)
            action.triggered.connect(lambda _=False, key=key: self._recolor(key))
        self.color_button.setMenu(self.color_menu)
        self.icon_button = QPushButton(self)
        self.icon_button.clicked.connect(self._choose_icon)
        self.up_button = QPushButton(self)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button = QPushButton(self)
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.delete_button = QPushButton(self)
        self.delete_button.clicked.connect(self.delete)
        for keys, step in (("Alt+Up", -1), ("Alt+Down", 1)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda step=step: self.move_selected(step))

        side = QVBoxLayout()
        for button in (
            self.new_button,
            self.rename_button,
            self.color_button,
            self.icon_button,
            self.up_button,
            self.down_button,
            self.delete_button,
        ):
            side.addWidget(button)
        side.addStretch()
        body = QHBoxLayout()
        body.addWidget(self.list, 1)
        body.addLayout(side)
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addLayout(body)
        layout.addWidget(self.problem)

        notes.changed.connect(self.refresh)
        self.retranslate()

    def retranslate(self) -> None:
        self.label.setText(self.tr("&Categories"))
        self.list.setAccessibleName(self.tr("Categories"))
        self.list.setAccessibleDescription(
            self.tr("F2 renames, Alt+Up and Alt+Down move, Delete deletes.")
        )
        self.new_button.setText(self.tr("&New…"))
        self.rename_button.setText(self.tr("&Rename"))
        self.color_button.setText(self.tr("C&olor"))
        self.icon_button.setText(self.tr("&Icon…"))
        self.up_button.setText(self.tr("Move &up"))
        self.down_button.setText(self.tr("Move do&wn"))
        self.delete_button.setText(self.tr("&Delete…"))
        for action in self.color_menu.actions():
            action.setText(color_name(str(action.data())))
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def categories(self) -> list[Category]:
        return self._notes.label_choices()[0]

    def selected(self) -> Category | None:
        row = self.list.currentRow()
        chosen = self.list.item(row).data(LABEL_ID) if row >= 0 else None
        return next((c for c in self.categories() if c.id == chosen), None)

    def refresh(self, keep: str | None = None) -> None:
        """The categories as they are now, the selected one (or keep) staying selected."""
        current = self.selected()
        keep = keep or (current.id if current is not None else None)
        self.list.blockSignals(True)
        self.list.clear()
        for category in self.categories():
            item = QListWidgetItem(
                category_icon(category.icon, dot_color(category.color)), category.name
            )
            item.setData(LABEL_ID, category.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.list.addItem(item)
            if category.id == keep:
                self.list.setCurrentItem(item)
        if self.list.count() and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._enable()

    def _enable(self) -> None:
        chosen = self.selected() is not None
        row = self.list.currentRow()
        for button in (self.rename_button, self.color_button, self.icon_button, self.delete_button):
            button.setEnabled(chosen)
        self.up_button.setEnabled(chosen and row > 0)
        self.down_button.setEnabled(chosen and row < self.list.count() - 1)

    def _show_problem(self, text: str) -> None:
        self.problem.setText(text)
        self.problem.setVisible(bool(text))

    def new_category(self) -> None:
        dialog = notes_module.new_category_dialog(
            self, self._notes.create_category, next_dot_color(len(self.categories()))
        )
        if dialog.created is not None:
            self.refresh(keep=dialog.created.id)
            self.list.setFocus(Qt.FocusReason.OtherFocusReason)  # on with the keyboard

    def _rename(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.editItem(self.list.item(row))

    def _renamed(self, item: QListWidgetItem) -> None:
        category_id = str(item.data(LABEL_ID))
        try:
            self._notes.rename_category(category_id, item.text())
        except CategoryNameError:
            self._show_problem(self.tr("There is already a category with this name."))
            self.refresh(keep=category_id)  # back to the name it has
            return
        self._show_problem("")
        self.refresh(keep=category_id)

    def _recolor(self, color: str) -> None:
        category = self.selected()
        if category is not None:
            self._notes.set_category_color(category.id, color)

    def _choose_icon(self) -> None:
        category = self.selected()
        if category is None:
            return
        icon = category_dialog.choose_icon(self, category.icon, category.color)
        if icon is not None:
            self._notes.set_category_icon(category.id, icon)
        self.list.setFocus(Qt.FocusReason.OtherFocusReason)  # on with the keyboard

    def move_selected(self, step: int) -> None:
        category = self.selected()
        if category is not None:
            self._notes.move_category(category.id, step)
            self.refresh(keep=category.id)

    def delete(self) -> None:
        """Asks first: see the module's description."""
        category = self.selected()
        if category is None:
            return
        inside = self._notes.notes_in_category(category.id)
        if not inside:
            question = self.tr("Delete the category “%1”? No note has it.").replace(
                "%1", category.name
            )
            if confirm(self, question):
                self._notes.remove_category(category.id, with_notes=False)
            return
        question = self.tr(
            "%n note(s) will lose the category “%1”. This cannot be undone. "
            "The notes themselves stay as they are.",
            "",
            len(inside),
        ).replace("%1", category.name)
        how = ask_how(
            self,
            question,
            self.tr("Delete the category only"),
            self.tr("Move the notes to the trash too…"),
        )
        if how == "category":
            self._notes.remove_category(category.id, with_notes=False)
            return
        if how != "notes":
            return
        hidden = sum(note.hidden for note in inside)
        locked = sum(note.locked for note in inside)
        question = self.tr(
            "Move %n note(s) to the trash with the category “%1”? "
            "They can be brought back from the trash for a year.",
            "",
            len(inside),
        ).replace("%1", category.name)
        if hidden or locked:
            question += "\n\n" + self.tr(
                "Among them: %1 hidden, %2 locked.", "numbers of notes"
            ).replace("%1", str(hidden)).replace("%2", str(locked))
        if confirm(self, question):
            self._notes.remove_category(category.id, with_notes=True)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.list
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Delete
            and self.list.state() != QListWidget.State.EditingState
        ):
            self.delete()
            return True
        return super().eventFilter(watched, event)


class MarkPage(QWidget):
    """The marks tab: as the categories tab, with an icon instead of a colour."""

    def __init__(self, notes: NoteManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._notes = notes

        self.label = QLabel(self)
        self.list = QListWidget(self)
        self.label.setBuddy(self.list)
        self.list.setEditTriggers(
            QListWidget.EditTrigger.EditKeyPressed | QListWidget.EditTrigger.DoubleClicked
        )
        self.list.itemChanged.connect(self._renamed)
        self.list.currentRowChanged.connect(self._enable)
        self.list.installEventFilter(self)
        self.problem = QLabel(self)
        self.problem.setWordWrap(True)
        self.problem.hide()

        self.new_button = QPushButton(self)
        self.new_button.clicked.connect(self.new_mark)
        self.rename_button = QPushButton(self)
        self.rename_button.clicked.connect(self._rename)
        self.icon_button = QPushButton(self)
        self.icon_menu = QMenu(self.icon_button)
        for icon in MARK_ICONS:
            action = self.icon_menu.addAction(mark_icon(icon), "")
            action.setData(icon)
            action.triggered.connect(lambda _=False, icon=icon: self.set_icon(icon))
        self.icon_button.setMenu(self.icon_menu)
        self.up_button = QPushButton(self)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button = QPushButton(self)
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.delete_button = QPushButton(self)
        self.delete_button.clicked.connect(self.delete)
        for keys, step in (("Alt+Up", -1), ("Alt+Down", 1)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda step=step: self.move_selected(step))

        side = QVBoxLayout()
        for button in (
            self.new_button,
            self.rename_button,
            self.icon_button,
            self.up_button,
            self.down_button,
            self.delete_button,
        ):
            side.addWidget(button)
        side.addStretch()
        body = QHBoxLayout()
        body.addWidget(self.list, 1)
        body.addLayout(side)
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addLayout(body)
        layout.addWidget(self.problem)

        notes.changed.connect(self.refresh)
        self.retranslate()

    def retranslate(self) -> None:
        self.label.setText(self.tr("&Marks"))
        self.list.setAccessibleName(self.tr("Marks"))
        self.list.setAccessibleDescription(
            self.tr(
                "F2 renames (an empty name gives a built-in mark its own back), "
                "Alt+Up and Alt+Down move, Delete deletes."
            )
        )
        self.new_button.setText(self.tr("&New…"))
        self.rename_button.setText(self.tr("&Rename"))
        self.icon_button.setText(self.tr("&Icon"))
        self.up_button.setText(self.tr("Move &up"))
        self.down_button.setText(self.tr("Move do&wn"))
        self.delete_button.setText(self.tr("&Delete…"))
        for action in self.icon_menu.actions():
            action.setText(icon_name(str(action.data())))
        self.refresh()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def marks(self) -> list[Mark]:
        return self._notes.label_choices()[1]

    def selected(self) -> Mark | None:
        row = self.list.currentRow()
        chosen = self.list.item(row).data(LABEL_ID) if row >= 0 else None
        return next((m for m in self.marks() if m.id == chosen), None)

    def refresh(self, keep: str | None = None) -> None:
        current = self.selected()
        keep = keep or (current.id if current is not None else None)
        self.list.blockSignals(True)
        self.list.clear()
        for mark in self.marks():
            item = QListWidgetItem(mark_icon(mark.icon), mark_name(mark))
            item.setData(LABEL_ID, mark.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.list.addItem(item)
            if mark.id == keep:
                self.list.setCurrentItem(item)
        if self.list.count() and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._enable()

    def _enable(self) -> None:
        chosen = self.selected() is not None
        row = self.list.currentRow()
        for button in (self.rename_button, self.icon_button, self.delete_button):
            button.setEnabled(chosen)
        self.up_button.setEnabled(chosen and row > 0)
        self.down_button.setEnabled(chosen and row < self.list.count() - 1)

    def _show_problem(self, text: str) -> None:
        self.problem.setText(text)
        self.problem.setVisible(bool(text))

    def new_mark(self) -> None:
        dialog = new_mark_dialog(self, self._notes.create_mark)
        if dialog.created is not None:
            self.refresh(keep=dialog.created.id)
            self.list.setFocus(Qt.FocusReason.OtherFocusReason)  # on with the keyboard

    def _rename(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.editItem(self.list.item(row))

    def _renamed(self, item: QListWidgetItem) -> None:
        mark_id = str(item.data(LABEL_ID))
        try:
            self._notes.rename_mark(mark_id, item.text())
        except MarkNameError:
            self._show_problem(
                self.tr("There is already a mark with this name, or none was given.")
            )
            self.refresh(keep=mark_id)
            return
        self._show_problem("")
        self.refresh(keep=mark_id)

    def set_icon(self, icon: str) -> None:
        mark = self.selected()
        if mark is not None:
            self._notes.set_mark_icon(mark.id, icon)
            self.refresh(keep=mark.id)

    def move_selected(self, step: int) -> None:
        mark = self.selected()
        if mark is not None:
            self._notes.move_mark(mark.id, step)
            self.refresh(keep=mark.id)

    def delete(self) -> None:
        """Asks first, saying it cannot be undone when notes have the mark."""
        mark = self.selected()
        if mark is None:
            return
        name = mark_name(mark)
        having = self._notes.notes_with_mark(mark.id)
        if having:
            question = self.tr(
                "%n note(s) will lose the mark “%1”. This cannot be undone. "
                "The notes themselves stay as they are.",
                "",
                len(having),
            ).replace("%1", name)
        else:
            question = self.tr("Delete the mark “%1”? No note has it.").replace("%1", name)
        if confirm(self, question):
            self._notes.remove_mark(mark.id)

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.list
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Delete
            and self.list.state() != QListWidget.State.EditingState
        ):
            self.delete()
            return True
        return super().eventFilter(watched, event)


class NewMarkDialog(QDialog):
    """A name and an icon. create makes the mark, or raises MarkNameError, shown
    here while the dialog stays open."""

    def __init__(self, parent: QWidget | None, create: Callable[[str, str], Mark]) -> None:
        super().__init__(parent)
        self._create = create
        self.created: Mark | None = None
        self.setMinimumWidth(320)
        self.name = QLineEdit(self)
        self.name.setMaxLength(NAME_LENGTH)
        self.icon = QComboBox(self)
        # The built-in marks' icons last: a new mark is most likely something else.
        built_in = len(BUILT_IN_MARKS)
        for icon in (*MARK_ICONS[built_in:], *MARK_ICONS[:built_in]):
            self.icon.addItem(mark_icon(icon), "", icon)
        self.problem = QLabel(self)
        self.problem.setWordWrap(True)
        self.problem.hide()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.name.textChanged.connect(self._name_changed)
        self.name_label = QLabel(self)
        self.name_label.setBuddy(self.name)
        self.icon_label = QLabel(self)
        self.icon_label.setBuddy(self.icon)
        form = QFormLayout()
        form.addRow(self.name_label, self.name)
        form.addRow(self.icon_label, self.icon)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        self.retranslate()
        self._name_changed("")

    @property
    def ok_button(self) -> QPushButton:
        button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert button is not None
        return button

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("New mark"))
        self.name_label.setText(self.tr("&Name:"))
        self.icon_label.setText(self.tr("&Icon:"))
        for index in range(self.icon.count()):
            self.icon.setItemText(index, icon_name(str(self.icon.itemData(index))))

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _name_changed(self, text: str) -> None:
        self.ok_button.setEnabled(bool(text.strip()))
        self.problem.hide()

    @override
    def accept(self) -> None:
        try:
            self.created = self._create(self.name.text(), str(self.icon.currentData()))
        except MarkNameError:
            self.problem.setText(self.tr("There is already a mark with this name."))
            self.problem.show()
            self.name.setFocus()
            self.name.selectAll()
            return
        super().accept()


def _ask_for_mark(parent: QWidget, create: Callable[[str, str], Mark]) -> NewMarkDialog:
    dialog = NewMarkDialog(parent, create)
    dialog.exec()
    return dialog


# Asks, and returns once answered: the dialog's created is the new mark, if any.
new_mark_dialog: Callable[[QWidget, Callable[[str, str], Mark]], NewMarkDialog] = (
    _ask_for_mark  # replaced in tests
)


class LabelManager(QDialog):
    """Categories and marks, a tab each."""

    def __init__(self, notes: NoteManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(380)
        self.tabs = QTabWidget(self)
        self.categories = CategoryPage(notes, self.tabs)
        self.marks = MarkPage(notes, self.tabs)
        self.tabs.addTab(self.categories, "")
        self.tabs.addTab(self.marks, "")
        self.close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.close_buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.close_buttons)
        self.retranslate()

    def retranslate(self) -> None:
        title = self.tr("Categories and marks")
        self.setWindowTitle(title)
        self.tabs.setAccessibleName(title)
        self.tabs.tabBar().setAccessibleName(title)
        self.tabs.setAccessibleDescription(self.tr("Ctrl+Tab goes to the other tab."))
        self.tabs.setTabText(0, self.tr("Categories"))
        self.tabs.setTabText(1, self.tr("Marks"))

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
