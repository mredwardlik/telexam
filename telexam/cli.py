"""``telexam`` runs the bot; ``telexam add | list | results | delete | check`` manage the tests from a terminal."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .exam import Exam, ExamError, load_test_file, to_csv_rows
from .settings import ConfigError, Settings, load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='telexam',
        description='A Telegram bot for timed multiple-choice tests. Without a command it runs the bot.',
        epilog='Settings are read from config.env (see config.example.env) or environment variables.',
    )
    parser.add_argument('-c', '--config', default='config.env', help='settings file (default: config.env)')
    parser.add_argument('--db', type=Path, help='database file instead of DATABASE from the settings')
    parser.add_argument('-v', '--verbose', action='store_true', help='log every request to Telegram')
    parser.add_argument('-V', '--version', action='version', version=f'telexam {__version__}')
    commands = parser.add_subparsers(dest='command', metavar='COMMAND')
    commands.add_parser('run', help='run the bot (the default)')
    commands.add_parser('check', help='check the settings and the tests, then exit')
    add = commands.add_parser('add', help='add tests from JSON files')
    add.add_argument('files', nargs='+', type=Path, metavar='FILE')
    add.add_argument('-r', '--replace', action='store_true', help='update tests that already exist')
    commands.add_parser('list', help='list the tests')
    results = commands.add_parser('results', help='show results, or save them for Excel')
    results.add_argument('test', nargs='?', help='only this test')
    results.add_argument('--csv', type=Path, metavar='FILE', help='save to a CSV file')
    delete = commands.add_parser('delete', help='hide a test from the list, its results stay')
    delete.add_argument('test')
    return parser


def open_exam(args, settings: Settings) -> Exam:
    path = args.db or settings.database
    return Exam(path)


def cmd_add(exam: Exam, args) -> int:
    code = 0
    for path in args.files:
        try:
            data = load_test_file(path)
            existed = exam.find_test(data['name']) is not None
            test = exam.add_test(data, replace=args.replace)
        except (OSError, ExamError) as error:
            print(f'{path}: {error}', file=sys.stderr)
            code = 1
            continue
        verb = 'updated' if existed else 'added'
        print(f'{path}: {verb} "{test.name}", {len(test.questions)} questions, {test.minutes} min')
    return code


def cmd_list(exam: Exam, args) -> int:
    tests = exam.tests()
    if not tests:
        print('No tests yet. Add one: telexam add examples/general.json')
        return 0
    results = exam.results()
    width = max(len(test.name) for test in tests)
    for test in tests:
        taken = sum(1 for attempt in results if attempt.test_id == test.id)
        print(f'{test.name:<{width}}  {len(test.questions):>3} questions  {test.minutes:>3} min  '
              f'{test.points} point(s)  {taken} attempt(s)')
    return 0


def cmd_results(exam: Exam, args) -> int:
    test = None
    if args.test:
        test = exam.find_test(args.test)
        if test is None:
            print(f'No test named "{args.test}"', file=sys.stderr)
            return 1
    rows = to_csv_rows(exam.results(test.id if test else None))
    if args.csv:
        with args.csv.open('w', encoding='utf-8-sig', newline='') as file:
            csv.writer(file, delimiter=';').writerows(rows)
        print(f'Saved {len(rows) - 1} results to {args.csv}')
        return 0
    if len(rows) == 1:
        print('No results yet.')
        return 0
    columns = [0, 2, 3, 5, 6, 7, 9]
    widths = [max(len(row[column]) for row in rows) for column in columns]
    for row in rows:
        print('  '.join(row[column].ljust(width) for column, width in zip(columns, widths)).rstrip())
    return 0


def cmd_delete(exam: Exam, args) -> int:
    test = exam.find_test(args.test)
    if test is None:
        print(f'No test named "{args.test}"', file=sys.stderr)
        return 1
    exam.archive_test(test.id)
    print(f'"{test.name}" is hidden. Add the same file again to bring it back.')
    return 0


def use_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, 'encoding', None) or '').lower().replace('-', '')
        if encoding != 'utf8' and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')


def main(argv: Sequence[str] | None = None) -> int:
    use_utf8()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')
    log = logging.getLogger('telexam')
    command = args.command or 'run'
    try:
        settings = load_settings(args.config, need_token=command in ('run', 'check'))
    except ConfigError as error:
        log.error('%s', error)
        return 2

    exam = open_exam(args, settings)
    try:
        if command in ('add', 'list', 'results', 'delete'):
            return {'add': cmd_add, 'list': cmd_list, 'results': cmd_results, 'delete': cmd_delete}[command](exam, args)
        if not settings.admins:
            log.warning('ADMINS is empty: nobody can add tests from Telegram or see the results there')
        if command == 'check':
            tests = exam.tests()
            print(f'Database: {args.db or settings.database}')
            print(f'Tests ({len(tests)}): {", ".join(test.name for test in tests) or "none"}')
            print(f'Admins: {len(settings.admins)}')
            return 0
        from telebot.apihelper import ApiTelegramException

        from .bot import ExamBot

        bot = ExamBot(settings, exam)
        if not exam.tests():
            log.warning('There are no tests yet. Add one: telexam add examples/general.json')
        try:
            bot.run()
        except ApiTelegramException as error:
            if error.error_code == 401:
                log.error('Telegram doesn\'t accept the token. Check TOKEN in "%s".', args.config)
                return 2
            raise
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        exam.close()
