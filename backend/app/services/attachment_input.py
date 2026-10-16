"""统一附件业务数据：Excel 和后续接入器共用字段校验与周期规范化。"""
import calendar
import re
from urllib.parse import urlsplit

COLUMNS = ('mine_code', 'mine_name', 'report_time', 'update_rate', 'cycle', 'file_name_id',
           'file_name', 'attachment_id', 'attachment_name', 'attachment_link')
RATE_TYPES = {1: 'day', 7: 'week', 10: 'ten_day', 30: 'month', 90: 'quarter', 180: 'half_year', 360: 'year'}
CYCLE_TYPES = dict(zip(('日', '周', '旬', '月', '季', '半年', '年'), RATE_TYPES.values()))


class AttachmentInputError(ValueError):
    pass


def normalize_period(raw: str, rate: int, cycle: str) -> dict:
    kind = RATE_TYPES.get(rate) if rate else CYCLE_TYPES.get(cycle)
    if kind is None:
        raise AttachmentInputError('上报频率编码或 cycle 无效')
    if cycle and CYCLE_TYPES.get(cycle) != kind:
        raise AttachmentInputError('update_rate 与 cycle 的频率不一致')
    match = re.fullmatch(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?: 00:00:00)?', raw)
    if not match:
        # 不猜测 Excel 序列日期和上游自定义周期；原值仍可登记和精确查询。
        return dict(period_type=kind, period_key=raw, normalization_status='RAW_ONLY')
    year, month, day = map(int, match.groups())
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        raise AttachmentInputError('所属周期年月无效')
    if day == 0 and kind in {'day', 'week', 'ten_day'}:
        raise AttachmentInputError('日/周/旬周期不能使用 00 日期')
    if day and day > calendar.monthrange(year, month)[1]:
        raise AttachmentInputError('所属周期日期无效')
    if kind == 'month':
        key = f'{year:04}-{month:02}'
    elif kind == 'year':
        key = f'{year:04}'
    elif kind in {'quarter', 'half_year', 'week', 'ten_day'}:
        # 尚无上游季度/半年锚点及跨年周定义，保留原值，不自行造 Q1/W01。
        return dict(period_type=kind, period_key=raw, normalization_status='RAW_ONLY')
    else:
        key = f'{year:04}-{month:02}-{day:02}'
    return dict(period_type=kind, period_key=key, normalization_status='NORMALIZED')


def validate_attachment_data(raw: dict, *, numeric_ids=(), formula_columns=()) -> tuple[dict, list[str]]:
    row = {key: str(raw[key] if raw.get(key) is not None else '').strip() for key in COLUMNS}
    errors = []
    for key in COLUMNS:
        if key != 'cycle' and not row[key]:
            errors.append(f'{key} 不能为空')
    limits = {'mine_code': 100, 'mine_name': 200, 'report_time': 100, 'cycle': 20,
              'file_name_id': 200, 'file_name': 500, 'attachment_id': 200, 'attachment_name': 500,
              'attachment_link': 4000, 'update_rate': 10}
    for key, limit in limits.items():
        if len(row[key]) > limit:
            errors.append(f'{key} 超过长度限制')
    for key in numeric_ids:
        value = row.get(key, '')
        if not re.fullmatch(r'\d{1,15}', value):
            errors.append(f'{key} 必须存为文本，数值型长 ID 已有精度丢失风险')
    if formula_columns:
        errors.append('不接受公式单元格：' + ', '.join(formula_columns))
    try:
        rate = int(row['update_rate'])
        if rate not in {0, *RATE_TYPES}:
            raise ValueError()
        row['update_rate'] = rate
        row.update(normalize_period(row['report_time'], rate, row['cycle']))
    except (ValueError, AttachmentInputError) as exc:
        errors.append(str(exc) if isinstance(exc, AttachmentInputError) else 'update_rate 编码无效')
    try:
        url = urlsplit(row['attachment_link'])
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError()
        _ = url.port
        if any(ord(c) < 32 or c in '\\' for c in row['attachment_link']):
            raise ValueError()
    except ValueError:
        errors.append('附件链接必须是无用户凭据的 HTTP(S) URL')
    row['format'] = row['attachment_name'].rsplit('.', 1)[-1].lower() if '.' in row['attachment_name'] else 'unknown'
    return row, errors

