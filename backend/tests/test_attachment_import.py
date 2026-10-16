"""模板、周期、下载策略和流程完整的纯单元验收。"""
import io
import socket
import zipfile
from types import SimpleNamespace
from urllib.parse import quote
from xml.sax.saxutils import escape

import pytest

from app.services.attachment_download import DownloadError, allowed_addresses, validate_url
from app.services.attachment_excel import COLUMNS, ExcelError, iter_excel, normalize_period, validate_row
from app.services.attachment_version_service import flow_state, safe_config
from app.services.attachment_input import validate_attachment_data


def row(**overrides):
    return dict(mine_code='001', mine_name='测试煤矿', report_time='2026/04/00', update_rate='30', cycle='',
        file_name_id='C01', file_name='测试清单', attachment_id='1987654321000000001',
        attachment_name='测试.pdf', attachment_link='https://files.example:8443/a.pdf?signature=hidden') | overrides


def workbook(rows, *, numeric_id=False, formula=False):
    def cell(col, val, is_id=False):
        return f'<c r="{col}"'+('' if numeric_id and is_id else ' t="inlineStr"')+'>' + (
            '<v>'+escape(str(val))+'</v>' if numeric_id and is_id else '<is><t>'+escape(str(val))+'</t></is>') + ('<f>HYPERLINK("http://evil")</f>' if formula and is_id else '')+'</c>'
    data = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    for n, values in enumerate([dict(zip(COLUMNS,COLUMNS)), *rows],1):
        data += f'<row r="{n}">' + ''.join(cell(f'{chr(65+i)}{n}',values.get(c,''),n>1 and c=='attachment_id') for i,c in enumerate(COLUMNS))+'</row>'
    data += '</sheetData></worksheet>'
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="附件" r:id="r1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',data)
    return out.getvalue()


def test_month_placeholder_and_raw_query_granularity():
    assert normalize_period('2026/04/00',30,'')['period_key']=='2026-04'
    assert normalize_period('2026/04/00',0,'季') == dict(period_type='quarter',period_key='2026/04/00',normalization_status='RAW_ONLY')
    with pytest.raises(ExcelError): normalize_period('2026/04/00',1,'')
    with pytest.raises(ExcelError): normalize_period('2026/02/30',1,'')
    with pytest.raises(ExcelError): normalize_period('2026/04/01',30,'年')
    # 非 Excel 接入可直接提供整数频率；0 不能被当成未填写。
    normalized, errors = validate_attachment_data(row(update_rate=0, cycle='月'))
    assert not errors and normalized['update_rate'] == 0 and normalized['period_key'] == '2026-04'


def test_ids_and_formulas_are_never_evaluated():
    valid=list(iter_excel(workbook([row()])))[0]
    assert valid[3]['attachment_id']=='1987654321000000001' and not valid[4]
    assert '精度' in list(iter_excel(workbook([row()],numeric_id=True)))[0][4][0]
    assert any('公式' in e for e in list(iter_excel(workbook([row()],formula=True)))[0][4])


def test_ten_thousand_rows_and_bounds():
    values=[{**row(),'attachment_id':str(1987654321000000001+i)} for i in range(10001)]
    assert sum(1 for _ in iter_excel(workbook(values)))==10001
    with pytest.raises(ExcelError,match='行数'): list(iter_excel(workbook(values),max_rows=10000))
    with pytest.raises(ExcelError,match='解压'): list(iter_excel(workbook([row()]),max_expanded_bytes=10))


@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:pass@files.example/a','https://files.example/a#token','http://files.example\\@localhost/a'])
def test_url_rejects_unsafe_syntax(url):
    with pytest.raises(DownloadError): validate_url(url,{'https://files.example:443':{'networks':['10.1.0.0/24']}})


def test_download_origin_and_dns_are_both_constrained():
    url='https://files.example:8443/a?token=hidden'
    policy={'networks':['10.1.0.0/24']}
    assert validate_url(url,{'https://files.example:8443':policy})[1]=='https://files.example:8443'
    def resolver(ip): return lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,8443))]
    assert allowed_addresses('files.example',8443,policy,resolver=resolver('10.1.0.9'))==['10.1.0.9']
    for denied in ('127.0.0.1','169.254.169.254','10.2.0.1','8.8.8.8'):
        with pytest.raises(DownloadError): allowed_addresses('files.example',8443,policy,resolver=resolver(denied))


def test_review_and_not_found_complete_but_failed_execution_does_not():
    run=SimpleNamespace(status='RUNNING')
    jobs=[SimpleNamespace(execution_state='done',semantic_state=s) for s in ('completed','not_found','review_required')]
    assert flow_state(run,jobs)=='COMPLETED'
    jobs[0].execution_state='failed_terminal'
    assert flow_state(run,jobs)=='FAILED'
    jobs[0].execution_state='running'
    assert flow_state(run,jobs)=='RUNNING'
    jobs[0].execution_state='done'
    jobs[0].semantic_state='incomplete'
    assert flow_state(run,jobs)=='FAILED'
    jobs[0].semantic_state='completed'
    run.status='FAILED'
    assert flow_state(run,jobs)=='FAILED'
    run.status='CANCELLED'
    assert flow_state(run,jobs)=='CANCELLED'


def test_model_configuration_snapshot_omits_runtime_credentials():
    clean=safe_config({'api_key':'hidden','model':{'base_url':'https://user:pass@host:8443/v1?token=hidden'},'attachment_link':'secret'})
    assert clean=={'model':{'base_url':'https://host:8443/v1'}}


@pytest.mark.parametrize('part', ['xl/workbook.xml', 'xl/_rels/workbook.xml.rels', 'xl/sharedStrings.xml', 'xl/worksheets/sheet1.xml'])
@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_xml_entities_are_rejected_in_all_template_parts(part, encoding):
    original = zipfile.ZipFile(io.BytesIO(workbook([row()])))
    payload = ('<?xml version="1.0" encoding="' + encoding + '"?>'
               '<!DOCTYPE x [<!ENTITY x "hidden">]><x>&x;</x>').encode(encoding)
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as target:
        for name in original.namelist():
            target.writestr(name, payload if name == part else original.read(name))
        if part not in original.namelist():
            target.writestr(part, payload)
    with pytest.raises(ExcelError):
        list(iter_excel(result.getvalue()))


def test_download_deadline_interrupts_slow_body(monkeypatch):
    import time
    import threading
    import app.services.attachment_download as download
    stopped = threading.Event()
    class Socket:
        def shutdown(self, _): stopped.set()
        def close(self): stopped.set()
        def settimeout(self, _): pass
    class Response:
        status = 200
        def getheader(self, key, default=None): return default
        def read1(self, _):
            stopped.wait(2)
            return b''
    class Connection:
        def __init__(self, *a, **k): self.sock = Socket()
        def connect(self): pass
        def request(self, *a, **k): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(download, '_PinnedHTTPS', Connection)
    monkeypatch.setattr(download, 'allowed_addresses', lambda *a: ['10.1.0.2'])
    started = time.monotonic()
    with pytest.raises(DownloadError, match='DOWNLOAD_TIMEOUT'):
        download.fetch_pdf('https://files.example/a.pdf', sources={'https://files.example:443': {'networks': ['10.1.0.0/24']}}, max_bytes=1000, timeout=0.05)
    assert stopped.is_set() and time.monotonic() - started < 1


def test_unconfigured_private_https_skips_certificate_verification(monkeypatch):
    import app.services.attachment_download as download
    seen = {}

    class Connection:
        def __init__(self, *args, **kwargs):
            seen["verify"] = kwargs.get("verify")
        def connect(self):
            raise DownloadError("stop")
        def close(self):
            pass

    monkeypatch.setattr(download, "_PinnedHTTPS", Connection)
    monkeypatch.setattr(download, "allowed_addresses", lambda *args: ["172.26.10.67"])
    with pytest.raises(DownloadError, match="stop"):
        download.fetch_pdf("https://172.26.10.67:8082/a.pdf", sources={}, max_bytes=1000, timeout=5)
    assert seen["verify"] is False
    monkeypatch.setattr(download, "allowed_addresses", lambda *args: ["8.8.8.8"])
    with pytest.raises(DownloadError, match="stop"):
        download.fetch_pdf("https://files.example/a.pdf", sources={}, max_bytes=1000, timeout=5)
    assert seen["verify"] is True


def test_download_encodes_non_ascii_path(monkeypatch):
    import app.services.attachment_download as download
    seen = {}

    class Connection:
        def __init__(self, *args, **kwargs):
            pass
        def connect(self):
            self.sock = self
        def settimeout(self, _value):
            pass
        def shutdown(self, _how):
            pass
        def close(self):
            pass
        def request(self, method, target, headers=None):
            seen["target"] = target
            raise DownloadError("stop")

    monkeypatch.setattr(download, "_PinnedHTTPS", Connection)
    monkeypatch.setattr(download, "allowed_addresses", lambda *args: ["172.26.10.67"])
    with pytest.raises(DownloadError, match="stop"):
        download.fetch_pdf("https://172.26.10.67:8082/disaster-attach/报告.pdf", sources={}, max_bytes=1000, timeout=5)
    assert "报告" not in seen["target"]
    assert quote("报告", safe="") in seen["target"]


def test_development_download_needs_no_source_configuration():
    url = 'http://localhost:8080/test.pdf?signature=original'
    _, origin, host, port, policy = validate_url(url, {})
    assert (origin, host, port, policy) == ('http://localhost:8080', 'localhost', 8080, {})
    resolver = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 8080))]
    assert allowed_addresses(host, port, policy, resolver=resolver) == ['127.0.0.1']
