from PySide6.QtGui import QFont

ACCENT = '#4A6FA5'
TEXT = '#1C2028'
MUTED = '#5F6672'


def apply(app):
    app.setStyle('Fusion')
    app.setFont(QFont('Arial', 12))
    app.setStyleSheet('''
        QWidget { background: #FFFFFF; color: #1C2028; }
        QLabel { background: transparent; }
        QLabel#muted { color: #5F6672; font-size: 11px; }
        QLabel#heading { font-size: 20px; font-weight: 600; }
        QLabel#section { color: #5F6672; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
        QLabel#badge { background: #F4F5F7; color: #4A6FA5; border: 1px solid #DDE0E6;
            border-radius: 5px; padding: 5px 9px; font-size: 11px; font-weight: 600; }
        QLabel#notice { background: #F4F5F7; color: #5F6672; border: 1px solid #DDE0E6;
            border-radius: 6px; padding: 8px; font-size: 11px; }
        QFrame#panel { border: 1px solid #DDE0E6; }
        QPushButton, QToolButton { border: 1px solid #CDD1D9; border-radius: 6px;
            padding: 6px 12px; background: white; font-size: 11px; min-height: 17px; }
        QPushButton:hover, QToolButton:hover { border-color: #4A6FA5; background: #EDF0F5; }
        QToolButton { padding-right: 20px; }
        QPushButton:disabled { color: #9CA0A8; }
        QPushButton#primary { background: #4A6FA5; color: white; border-color: #4A6FA5; }
        QPushButton#primary:hover { background: #3D5E8C; }
        QPushButton#primary:disabled { background: #CFD7E4; border-color: #CFD7E4; color: white; }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { border: 1px solid #CDD1D9; border-radius: 5px;
            padding: 6px 8px; min-height: 17px; selection-background-color: #4A6FA5; }
        QLineEdit:focus, QComboBox:focus { border-color: #4A6FA5; }
        QComboBox { padding-right: 25px; }
        QComboBox QAbstractItemView { selection-background-color: #D6E4F7; selection-color: #1C2028; }
        QTreeWidget, QTableWidget, QTableView { border: none; outline: none; background: white;
            alternate-background-color: #F4F5F7; selection-background-color: #D6E4F7;
            selection-color: #1C2028; font-size: 11px; gridline-color: transparent; }
        QTreeWidget::item { padding: 7px 3px; }
        QTableWidget::item, QTableView::item { padding: 4px 6px; }
        QHeaderView::section { background: #E8EAF0; color: #5F6672; font-size: 10px;
            font-weight: 600; border: none; border-bottom: 1px solid #DDE0E6; padding: 8px 6px; }
        QTabWidget::pane { border: none; border-top: 1px solid #DDE0E6; }
        QTabBar::tab { background: white; color: #5F6672; padding: 10px 16px;
            border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { color: #4A6FA5; font-weight: 600; border-bottom-color: #4A6FA5; }
        QTabBar::tab:hover { background: #EDF0F5; }
        QPlainTextEdit { border: none; background: #FAFBFC; padding: 8px; }
        QSplitter::handle { background: #DDE0E6; }
        QScrollArea { border: none; }
        QScrollBar:vertical { background: transparent; width: 10px; }
        QScrollBar::handle:vertical { background: #CDD1D9; border-radius: 4px; min-height: 25px; margin: 2px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QStatusBar { border-top: 1px solid #DDE0E6; color: #5F6672; padding: 3px; font-size: 11px; }
        QMenu { border: 1px solid #DDE0E6; }
        QMenu::item { padding: 6px 22px; }
        QMenu::item:selected { background: #D6E4F7; }
    ''')
