"""密碼鎖（tools/encrypt_pages.py）：密文可用正確密碼解回原文、不殘留明文、沒密碼就拒絕部署。"""
import base64
import json
import os
import re

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tools import encrypt_pages as E

HTML = '<!DOCTYPE html><html><head><title>t</title></head><body><span data-sig="buy">🟢 #1</span> 2412 停利</body></html>'


def _payload(shell):
    return json.loads(re.search(r'var P = (\{.*?\});', shell).group(1))


def _decrypt(shell, password):
    p = _payload(shell)
    key = E.derive_key(password, base64.b64decode(p['salt']))
    return AESGCM(key).decrypt(base64.b64decode(p['iv']), base64.b64decode(p['ct']), None).decode()


def test_roundtrip_and_no_plaintext(tmp_path, monkeypatch):
    (tmp_path / 'a.html').write_text(HTML, encoding='utf-8')
    (tmp_path / 'b.html').write_text(HTML, encoding='utf-8')
    monkeypatch.setenv('PAGE_PASSWORD', 'correct-horse')
    assert E.main(str(tmp_path)) == 0
    a = (tmp_path / 'a.html').read_text(encoding='utf-8')
    b = (tmp_path / 'b.html').read_text(encoding='utf-8')
    for s in (a, b):
        assert 'data-sig' not in s and '2412' not in s and '停利' not in s
        assert 'noindex' in s
        assert _decrypt(s, 'correct-horse') == HTML
    # 全站同 salt（一次解鎖跨頁通用），每頁 IV 不同
    assert _payload(a)['salt'] == _payload(b)['salt']
    assert _payload(a)['iv'] != _payload(b)['iv']
    assert (tmp_path / 'robots.txt').read_text().strip().endswith('Disallow: /')
    with pytest.raises(InvalidTag):
        _decrypt(a, 'wrong-password')


@pytest.mark.parametrize('pw', ['', 'short'])
def test_refuses_missing_or_short_password(tmp_path, monkeypatch, pw):
    (tmp_path / 'a.html').write_text(HTML, encoding='utf-8')
    monkeypatch.setenv('PAGE_PASSWORD', pw)
    assert E.main(str(tmp_path)) == 1
    assert (tmp_path / 'a.html').read_text(encoding='utf-8') == HTML   # 原檔不動


def test_deploy_workflow_encrypts_before_push():
    wf = open(os.path.join(os.path.dirname(__file__), '.github/workflows/deploy_pages.yml'),
              encoding='utf-8').read()
    assert 'tools/encrypt_pages.py' in wf
    assert 'secrets.PAGE_PASSWORD' in wf
    assert wf.index('python tools/encrypt_pages.py _site') < wf.index('git push --force')
