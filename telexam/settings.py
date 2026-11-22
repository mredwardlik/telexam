"""Settings from config.env, overridden by environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

KEYS = ('TOKEN', 'ADMINS', 'DATABASE')
TOKEN_PATTERN = re.compile(r'^\d+:[\w-]{30,}$')


class ConfigError(Exception):
    """The configuration is missing or wrong."""


@dataclass(frozen=True)
class Settings:
    token: str = ''
    admins: frozenset[int] = field(default_factory=frozenset)
    database: Path = Path('telexam.db')


def parse_admins(value: str) -> frozenset[int]:
    """Telegram ids separated by commas or spaces.

    >>> sorted(parse_admins('123, 456 789,'))
    [123, 456, 789]
    """
    ids = [part for part in re.split(r'[\s,;]+', value or '') if part]
    try:
        return frozenset(int(part) for part in ids)
    except ValueError:
        raise ConfigError(f'ADMINS must be Telegram user ids separated by commas, not "{value}". '
                          'Your id can be found with @userinfobot.') from None


def load_settings(path: str | os.PathLike = 'config.env', environ: Mapping[str, str] | None = None,
                  need_token: bool = True) -> Settings:
    """Read the settings. DATABASE in the file is relative to the file itself.

    Every key can also be set as an environment variable: ``TOKEN=... telexam``.
    """
    environ = os.environ if environ is None else environ
    path = Path(path)
    values: dict[str, str | None] = dict(dotenv_values(path)) if path.is_file() else {}
    values.update({key: environ[key] for key in KEYS if environ.get(key)})

    token = (values.get('TOKEN') or '').strip()
    if need_token and not TOKEN_PATTERN.match(token):
        where = f'"{path}"' if path.is_file() else f'"{path}" (not found, copy config.example.env)'
        raise ConfigError(f'TOKEN in {where} is not set. Get a token from @BotFather, it looks like 123456789:AAH...')
    database = Path(values.get('DATABASE') or 'telexam.db')
    if not database.is_absolute():
        database = (path.resolve().parent if path.is_file() else Path.cwd()) / database
    return Settings(token=token, admins=parse_admins(values.get('ADMINS') or ''), database=database)
