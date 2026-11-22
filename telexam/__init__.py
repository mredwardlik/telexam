"""A Telegram bot for timed multiple-choice tests, with results in SQLite."""

from .exam import Attempt, Exam, ExamError, Item, Test, TestFormatError, load_test_file, parse_test
from .settings import ConfigError, Settings, load_settings

__version__ = '1.0.0'
__all__ = ['Attempt', 'ConfigError', 'Exam', 'ExamError', 'Item', 'Settings', 'Test', 'TestFormatError',
           'load_settings', 'load_test_file', 'parse_test', '__version__']
