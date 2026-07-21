APP_STYLE = r"""
QWidget {
    color: #172033;
    font-family: "Malgun Gothic", "Segoe UI", sans-serif;
    font-size: 13px;
}

QMainWindow, QWidget#appRoot {
    background: #f4f6fa;
}

QFrame#panel, QFrame#editorCard, QFrame#blockCard, QFrame#playerCard {
    background: #ffffff;
    border: 1px solid #e3e7ef;
    border-radius: 12px;
}

QLabel#appTitle {
    font-size: 23px;
    font-weight: 700;
    color: #111827;
}

QLabel#sectionTitle {
    font-size: 16px;
    font-weight: 700;
    color: #111827;
}

QLabel#muted, QLabel#statusText {
    color: #687386;
}

QLabel#notice {
    background: #eef4ff;
    color: #315fbd;
    border: 1px solid #d8e6ff;
    border-radius: 8px;
    padding: 9px 12px;
}

QLabel#successNotice {
    background: #ecfdf3;
    color: #137a45;
    border: 1px solid #ccefdc;
    border-radius: 8px;
    padding: 8px 10px;
}

QPushButton {
    min-height: 34px;
    padding: 0 13px;
    border-radius: 8px;
    border: 1px solid #d8dee9;
    background: #ffffff;
    color: #273348;
    font-weight: 600;
}

QPushButton:hover {
    background: #f7f9fc;
    border-color: #bbc5d5;
}

QPushButton:pressed {
    background: #edf1f7;
}

QPushButton:disabled {
    color: #a4adbb;
    background: #f2f4f7;
    border-color: #e1e5eb;
}

QPushButton#primaryButton {
    color: #ffffff;
    background: #3974e8;
    border-color: #3974e8;
}

QPushButton#primaryButton:hover {
    background: #2f66d2;
}

QPushButton#dangerButton {
    color: #b42318;
    background: #fff7f6;
    border-color: #f2ccc8;
}

QLineEdit, QComboBox, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #d8dee9;
    border-radius: 8px;
    padding: 7px 9px;
    selection-background-color: #a9c5ff;
}

QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border-color: #4d82eb;
}

QListWidget, QTableWidget {
    background: #ffffff;
    border: 1px solid #e3e7ef;
    border-radius: 9px;
    gridline-color: #edf0f5;
    alternate-background-color: #fafbfc;
    selection-background-color: #e9f1ff;
    selection-color: #172033;
}

QListWidget::item {
    padding: 9px 8px;
    border-bottom: 1px solid #eff2f6;
}

QHeaderView::section {
    background: #f7f8fb;
    color: #576276;
    border: none;
    border-bottom: 1px solid #e3e7ef;
    padding: 9px 7px;
    font-weight: 700;
}

QTableWidget::item {
    padding: 7px 6px;
    border-bottom: 1px solid #eff2f6;
}

QTabWidget::pane {
    border: none;
}

QTabBar::tab {
    background: #e9edf4;
    color: #5d687a;
    border: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    min-width: 120px;
    padding: 10px 16px;
    margin-right: 3px;
}

QTabBar::tab:selected {
    background: #ffffff;
    color: #1c2c49;
    font-weight: 700;
}

QScrollArea {
    border: none;
    background: transparent;
}

QScrollBar:vertical {
    background: transparent;
    width: 11px;
    margin: 2px;
}

QScrollBar::handle:vertical {
    background: #c6ceda;
    min-height: 30px;
    border-radius: 4px;
}

QToolTip {
    color: #ffffff;
    background: #263247;
    border: none;
    padding: 5px;
}
"""
