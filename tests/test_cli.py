import pytest
from conftest import ROOT

from telexam import load_settings
from telexam.cli import main
from telexam.settings import ConfigError


def run(tmp_path, *argv):
    return main(['-c', str(tmp_path / 'none.env'), '--db', str(tmp_path / 'cli.db'), *argv])


def test_add_list_results_delete(tmp_path, capsys):
    examples = [str(path) for path in sorted((ROOT / 'examples').glob('*.json'))]
    assert run(tmp_path, 'add', *examples) == 0
    assert 'added "Общие знания", 10 questions' in capsys.readouterr().out
    assert run(tmp_path, 'add', examples[0]) == 1
    assert 'already exists' in capsys.readouterr().err
    assert run(tmp_path, 'add', '--replace', examples[0]) == 0
    assert 'updated' in capsys.readouterr().out
    run(tmp_path, 'list')
    assert 'Python: основы' in capsys.readouterr().out
    run(tmp_path, 'results')
    assert 'No results yet' in capsys.readouterr().out
    assert run(tmp_path, 'results', '--csv', str(tmp_path / 'out.csv')) == 0
    assert (tmp_path / 'out.csv').read_text('utf-8-sig').startswith('test;user id')
    assert run(tmp_path, 'delete', 'общие знания') == 0
    assert 'is hidden' in capsys.readouterr().out
    run(tmp_path, 'list')
    assert 'Общие знания' not in capsys.readouterr().out
    assert run(tmp_path, 'delete', 'nothing') == 1


def test_running_needs_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv('TOKEN', raising=False)
    assert run(tmp_path) == 2
    assert run(tmp_path, 'check') == 2


def test_settings(tmp_path):
    config = tmp_path / 'config.env'
    config.write_text('TOKEN=123456789:' + 'B' * 35 + '\nADMINS=1, 2\nDATABASE=data/exam.db\n', 'utf-8')
    settings = load_settings(config, environ={})
    assert settings.admins == {1, 2}
    assert settings.database == tmp_path / 'data' / 'exam.db'
    assert load_settings(config, environ={'ADMINS': '7'}).admins == {7}
    with pytest.raises(ConfigError, match='ADMINS'):
        load_settings(config, environ={'ADMINS': 'me'})
    config.write_text('TOKEN=wrong\n', 'utf-8')
    with pytest.raises(ConfigError, match='TOKEN'):
        load_settings(config, environ={})
    assert load_settings(config, environ={}, need_token=False).token == 'wrong'


def test_check(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('TOKEN', '123456789:' + 'C' * 35)
    assert run(tmp_path, 'check') == 0
    assert 'Tests (0): none' in capsys.readouterr().out
