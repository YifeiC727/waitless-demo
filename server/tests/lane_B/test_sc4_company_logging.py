import copy
import logging
import logging.config
import re

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from tests.company._live_harness import company_settings
from waitless.company.app import run_company
from waitless.company.scripted import AGENT_MODE_ENV


def test_company_handler_uses_default_stream(monkeypatch, mode_env, engine_config, company_dir, tmp_path):
    calls = []
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: calls.append(kw))
    settings = company_settings(mode_env('live-sim', company=True), engine_config, company_dir, tmp_path)
    monkeypatch.setenv(AGENT_MODE_ENV, 'scripted')
    assert run_company(settings) == 0
    handler = calls[-1]['log_config']['handlers']['waitless']
    assert 'stream' not in handler


def test_company_logging(monkeypatch, capsys, mode_env, engine_config, company_dir, tmp_path):
    original = copy.deepcopy(LOGGING_CONFIG)
    calls = []
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: calls.append(kw))
    settings = company_settings(mode_env('live-sim', company=True), engine_config, company_dir, tmp_path)
    monkeypatch.setenv(AGENT_MODE_ENV, 'scripted')
    assert run_company(settings) == 0
    config = calls[-1]['log_config']
    assert config['loggers']['waitless.company'] == {
        'handlers': ['waitless'], 'level': 'INFO', 'propagate': False}
    assert LOGGING_CONFIG == original
    for name in original['loggers']:
        assert config['loggers'][name] == original['loggers'][name]
    for category in ('handlers', 'formatters'):
        for name, value in original[category].items():
            assert config[category][name] == value
    monkeypatch.delenv(AGENT_MODE_ENV)
    assert run_company(settings) == 0
    assert calls[-1]['log_config'] == config

    # dictConfig also disables existing unrelated loggers. Restore all logger
    # state so the regression suite's caplog and other libraries are unaffected.
    names = set(logging.root.manager.loggerDict) | {
        'waitless.company', 'waitless.company.scripted', 'waitless.company.carrier',
        'uvicorn', 'uvicorn.error', 'uvicorn.access', 'botocore.credentials'}
    loggers = [logging.getLogger(name) for name in names]
    saved = [(logger, logger.handlers[:], logger.level, logger.propagate, logger.disabled)
             for logger in loggers]
    try:
        logging.config.dictConfig(config)
        logging.getLogger('waitless.company.scripted').info(
            'scripted reply end step=agent intent=open_answer')
        logging.getLogger('waitless.company.carrier').info(
            'scripted line step=agent intent=open_answer voice=Joanna')
        logging.getLogger('botocore.credentials').info('other library')
        err = capsys.readouterr().err
        prefix = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} INFO waitless\.company\.'
        assert re.search(prefix + r'scripted scripted reply end step=agent', err)
        assert re.search(prefix + r'carrier scripted line step=agent', err)
        assert 'other library' not in err
    finally:
        for logger, handlers, level, propagate, disabled in saved:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
