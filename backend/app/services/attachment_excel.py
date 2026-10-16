"""只读 OOXML 适配器：读取模板行，交给统一附件数据校验。"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from app.services.attachment_input import (
    AttachmentInputError as ExcelError,
    COLUMNS,
    normalize_period,
    validate_attachment_data as validate_row,
)

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def _safe_xml(data: bytes):
    try:
        return ET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise ExcelError('不接受 XML 实体或 DTD') from exc


def _xml_events(parser):
    try:
        yield from parser
    except DefusedXmlException as exc:
        raise ExcelError('不接受 XML 实体或 DTD') from exc
    except ET.ParseError as exc:
        raise ExcelError('工作表 XML 无效') from exc


def iter_excel(data: bytes, *, max_rows=100000, max_expanded_bytes=200 * 1024 * 1024):
    """按行产出；ZIP/XML 大小有界，工作表用 iterparse，不加载整个行集。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExcelError('文件不是有效的 .xlsx') from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > 3000 or len({x.filename for x in entries}) != len(entries):
            raise ExcelError('Excel ZIP 成员过多或重复')
        if sum(x.file_size for x in entries) > max_expanded_bytes:
            raise ExcelError('Excel 解压大小超过限制')
        for info in entries:
            if info.flag_bits & 1 or '..' in info.filename.split('/') or info.filename.startswith('/'):
                raise ExcelError('不接受加密或异常路径的 Excel')
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = _safe_xml(archive.read('xl/sharedStrings.xml'))
            for si in root.findall(NS + 'si'):
                strings.append(''.join(t.text or '' for t in si.iter(NS + 't')))
        workbook = _safe_xml(archive.read('xl/workbook.xml'))
        relationships = _safe_xml(archive.read('xl/_rels/workbook.xml.rels'))
        rels = {x.attrib['Id']: x.attrib['Target'] for x in relationships
                if x.attrib.get('TargetMode') != 'External'}
        total = 0
        for sheet in workbook.findall(NS + 'sheets/' + NS + 'sheet'):
            target = rels.get(sheet.attrib.get(REL + 'id'))
            if not target:
                raise ExcelError('工作表引用无效')
            path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
            if not path.startswith('xl/worksheets/'):
                raise ExcelError('工作表路径无效')
            # 限制已在 ZIP 元信息检查；禁止 XML 实体后才交给流式解析器。
            xml = archive.read(path)
            header = None
            parser = ET.iterparse(io.BytesIO(xml), events=('start', 'end'),
                                  forbid_dtd=True, forbid_entities=True, forbid_external=True)
            sheet_data = None
            for event, element in _xml_events(parser):
                if event == 'start' and element.tag == NS + 'sheetData':
                    sheet_data = element
                if event != 'end' or element.tag != NS + 'row':
                    continue
                values, numeric, formulas = {}, set(), set()
                for cell in element.findall(NS + 'c'):
                    ref = re.match(r'([A-Z]+)', cell.attrib.get('r', ''))
                    if not ref:
                        raise ExcelError('单元格坐标无效')
                    col = ref[1]
                    if len(col) > 2:
                        raise ExcelError('模板列数超过限制')
                    t = cell.attrib.get('t')
                    value = cell.findtext(NS + 'v') or ''
                    if t == 's':
                        try:
                            value = strings[int(value)]
                        except (ValueError, IndexError) as exc:
                            raise ExcelError('共享字符串索引无效') from exc
                    elif t == 'inlineStr':
                        value = ''.join(x.text or '' for x in cell.iter(NS + 't'))
                    elif t in {None, 'n'} and value:
                        numeric.add(col)
                    if cell.find(NS + 'f') is not None:
                        formulas.add(col)
                    if len(value) > 10000:
                        raise ExcelError('单元格内容过长')
                    values[col] = value
                if not any(v.strip() for v in values.values()):
                    element.clear()
                    continue
                if header is None:
                    header = {col: val.strip() for col, val in values.items()}
                    if len(set(header.values())) != len(header) or set(header.values()) != set(COLUMNS):
                        raise ExcelError('表头必须包含模板的 10 个列名，且不能重复')
                else:
                    total += 1
                    if total > max_rows:
                        raise ExcelError('Excel 行数超过限制')
                    raw = {header[col]: val for col, val in values.items() if col in header}
                    normalized, errors = validate_row(raw,
                        numeric_ids=[header[c] for c in numeric if header.get(c) in {'mine_code', 'file_name_id', 'attachment_id'}],
                        formula_columns=[header[c] for c in formulas if c in header])
                    yield sheet.attrib['name'], int(element.attrib['r']), raw, normalized, errors
                element.clear()
                if sheet_data is not None:
                    sheet_data.clear()
        if total == 0:
            raise ExcelError('Excel 没有附件数据')
