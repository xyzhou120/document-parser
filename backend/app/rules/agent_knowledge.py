"""经复核用途的种子包：提取报告事实，不推断合规性。"""

from app.schemas.rules import RulePayload

CHECKLIST = {
    "checklist_type": "gas_report_agent",
    "checklist_name": "瓦斯抽采达标评判报告（智能体试点）",
    "description": "原图工具取证；按工作面/评价单元/测点保留报告事实和不同值，不判达标。",
    "sort_order": -10,
}

BACKGROUND = (
    "依据瓦斯深度监察规则手册检查项2、3、5、7、8、11～16，将监察所需输入拆为报告事实。"
    "平台不执行手册中的合格/不合格判断、反算、公式复算或现场真实性裁定。"
    "文档可能含正文、扫描报告单、说明、审批表、设计与台账；标明来源角色和统计粒度。"
    "工作面总量、单元量、测点值、单孔参数互不替代；实测与计算、设计与实际分别提取。"
    "正文和表格同身份同值可归并为一组多证据，不同值全部保留，不选最高分或最常见值。"
    "关键词仅用于搜索；必须查看原图，OCR可错、可漏，无法确认即留空，不用领域常识补值。"
)

DIMENSION_DESCRIPTIONS = {
    "workface": "工作面/掘进对象名称，如40307工作面、22306回顺；不把第五评价单元拼入工作面名。没有明示不能推测。",
    "evaluation_unit": "评价/评判单元编号，如第五评价单元、4#；不填写整份报告标题或里程范围。",
    "measurement_point": "测点编号/名称；只有明确逐测点记录才绑定，单元总量没有测点时留空。",
    "roadway": "巷道名称或类型，如运顺、回顺；里程1700~2200m是范围而不是巷道。",
    "measurement_date": "测定/统计截止日期；不能用报告编制或审批日期代替。",
    "scope_level": "记录统计层级，如工作面合计、评价单元合计、测点、单孔；不是长度值、里程或对象名称。",
}

# 键、名称、数据类型、预期单位、类型、手册页、说明、提示、默认是否启用
FIELDS = [
    ("designed_hole_count", "报告设计钻孔数", "integer", "个", "design", 9,
     "按工作面/巷道、评价单元或钻场等报告明示粒度提取设计孔数。不把排数、钻场数、每排孔数、孔长/工程量、取样孔数当总孔数，不相乘反算。",
     ["设计钻孔", "设计孔数", "布孔", "钻孔数量"], True),
    ("actual_hole_count", "报告实际施工钻孔数", "integer", "个", "actual", 9,
     "提取报告明确的实际施工/竣工孔数，区分工作面合计和分单元/分巷道；保留补孔含否的文字。有效孔数和合计不自行计算。",
     ["实际钻孔", "施工钻孔", "竣工", "完成孔数", "实际孔数"], True),
    ("supplementary_hole_count", "补孔数量", "integer", "个", "actual", 9,
     "仅提取原文明确的补孔数量及所属范围，不因总数差额推算补孔，不把补孔孔号当数量。",
     ["补孔", "补充钻孔"], True),
    ("construction_period", "钻孔施工时间范围", "string", None, "reported", 9,
     "保留施工开始/结束日期、原文范围及所属单元，区分抽采起止、编制/审批日期，不推算。",
     ["施工时间", "开工", "施工日期", "竣工日期"], False),
    ("measurement_point_count", "效果测点数量", "integer", "个", "reported", 6,
     "只取报告明示测点数量，区分测点、取样钻孔、钻场。报告未写测点数时不得按工作面长度或钻孔数反算。",
     ["测点", "测点数量", "效果检验", "布置测点"], True),
    ("sampling_holes_per_point", "每测点取样钻孔数", "integer", "个", "reported", 6,
     "逐测点提取独立取样孔数并绑定测点；总取样数不代表每测点数，不用手册标准2孔补原文。",
     ["取样孔", "独立取样", "每个测点", "取样钻孔"], False),
    ("desorbable_gas_content_measured", "可解吸瓦斯含量实测值", "float", "m³/t", "measured", 7,
     "逐评价单元和测点提取可解吸瓦斯含量实测/测试结果。计算值、残余含量、残存含量、原始含量均不能代替；不由公式推导。",
     ["可解吸", "测试结果", "实测", "测值"], True),
    ("desorbable_gas_content_calculated", "可解吸瓦斯含量计算值", "float", "m³/t", "calculated", 7,
     "提取原文标明可解吸含量计算值；计算表列头与测定表的实测值分别记录，不自行代入计算。",
     ["可解吸", "计算值", "单元可解吸"], True),
    ("residual_gas_content_measured", "残余瓦斯含量实测值", "float", "m³/t", "measured", 19,
     "逐测点提取残余瓦斯含量实测值，绑定单元及工作面；原始/残存/可解吸含量和临界值不得替代。",
     ["残余瓦斯含量", "现场实测", "含量测定"], True),
    ("residual_gas_content_calculated", "残余瓦斯含量计算值", "float", "m³/t", "calculated", 19,
     "只提取报告已有的残余含量计算结果和对应单元，不从初始含量与抽采量自行计算。",
     ["残余瓦斯含量", "计算结果", "计算值"], True),
    ("residual_gas_pressure_measured", "残余瓦斯压力实测值", "float", "MPa", "measured", 20,
     "逐测点提取残余压力实测值及原文单位。抽采负压、原始压力、压力临界值不是残余实测压力。",
     ["残余瓦斯压力", "压力测定", "实测压力"], True),
    ("residual_gas_pressure_calculated", "残余瓦斯压力计算值", "float", "MPa", "calculated", 20,
     "只取报告明确残余压力计算结果，缺失则待确认，不用手册公式补结果，不将实测值改为计算值。",
     ["残余瓦斯压力", "计算压力", "计算值"], False),
    ("evaluation_unit_range", "评价单元及范围", "string", None, "reported", 18,
     "提取单元编号、巷道/工作面、起止里程/范围原文，单元4和单元5独立，不根据顺序猜编号。",
     ["评价单元", "评判单元", "单元划分", "评价范围"], True),
    ("extraction_duration_longest", "单元最长预抽天数", "integer", "天", "reported", 18,
     "只取单元最长抽采天数，保留对应截止日期；施工天数和最短抽采天数不是最长天数，不计算日期差。",
     ["最长", "预抽时间", "抽采天数"], False),
    ("extraction_duration_shortest", "单元最短预抽天数", "integer", "天", "reported", 18,
     "只取单元最短预抽天数，日期和最长天数不替代；同一报告多个截止期分别绑定。",
     ["最短", "预抽时间", "抽采天数"], False),
    ("extraction_time_difference_coefficient", "报告预抽时间差异系数", "float", "%", "calculated", 18,
     "只取报告中已有系数，不代入公式计算，不把手册30%标准当报告结果；原单位和原始精度保留。",
     ["差异系数", "预抽时间"], False),
    ("residual_gas_content_process", "残余瓦斯含量计算过程原文", "string", None, "process", 19,
     "摘录报告公式、参数代入和运算步骤的实际原文，可多处；仅写结论不可补成完整过程，不判断公式正确性。",
     ["残余瓦斯含量", "计算公式", "计算过程"], False),
    ("residual_gas_pressure_process", "残余瓦斯压力计算过程原文", "string", None, "process", 20,
     "摘录报告压力公式和代入步骤；没有公式不自行引用规范公式。",
     ["残余瓦斯压力", "计算过程", "计算公式"], False),
    ("reported_desorbable_threshold", "报告可解吸含量临界值及日产量对应", "string", None, "threshold", 7,
     "只摘录文档明示临界值、单位、日产量及对应关系，不根据手册查表推算，不以规范阈值补报告。",
     ["可解吸", "临界值", "日产量"], False),
    ("borehole_parameters_design", "设计钻孔参数原文", "string", None, "design", 11,
     "提取孔深/间距/倾角/孔径及单位、设计对象，不把施工值或总工程量当单孔设计参数。",
     ["设计", "孔深", "间距", "倾角", "孔径"], False),
    ("borehole_parameters_actual", "实际钻孔参数原文", "string", None, "actual", 11,
     "提取报告施工孔深/间距/倾角/孔径，保持对应行身份，不推断与设计一致。",
     ["施工", "实际", "孔深", "间距", "孔径"], False),
    ("permeability_measures", "增透措施设计和施工参数原文", "string", None, "reported", 12,
     "提取冲孔/压裂/割缝等措施、设计和实际参数原文，分清角色和对象，不判断是否符合设计。",
     ["增透", "冲孔", "压裂", "割缝"], False),
    ("evaluation_parameters", "评判参数及来源原文", "string", None, "reported", 15,
     "提取工作面长度、推进长度、煤厚、容重、储量、原始含量等参数及单位和来源；同名参数不同对象不混。",
     ["工作面长度", "煤厚", "容重", "储量", "原始瓦斯含量"], False),
    ("basic_condition_descriptions", "基础条件评判描述", "string", None, "presence", 16,
     "摘录系统/计划/设计/验收/制度/能力/计量/测试条件实际描述，不能从目录出现推断正文存在，更不判内容完整合规。",
     ["基础条件", "自评价", "计量", "测试条件"], False),
    ("effective_control_range", "钻孔有效控制范围原文", "string", None, "reported", 16,
     "提取控制边界、范围、图号和说明原文；不根据图形自动计算面积或认定覆盖合规。",
     ["有效控制", "控制范围", "控制边缘"], False),
    ("uniformity_evaluation_description", "布孔均匀程度评价描述", "string", None, "presence", 16,
     "摘录报告已有评价描述和参数，不自行判断均匀程度。",
     ["均匀", "布孔", "钻孔间距"], False),
    ("gas_emission_measured", "实测瓦斯涌出量", "string", None, "measured", 16,
     "提取实测涌出量数值、量纲和范围，区分m³/min流量与m³累计量，不把抽采流量当涌出量。",
     ["实测涌出", "瓦斯涌出量"], False),
    ("reported_conclusion_scope", "报告结论及适用范围原文", "string", None, "reported", 16,
     "摘录报告结论和原文适用范围；结论中的合格只是引文，不是平台结论；不判断扩大范围。",
     ["结论", "评判范围", "达标"], False),
    ("extraction_system_parameters", "抽采系统能力与泵参数原文", "string", None, "reported", 17,
     "逐泵站/泵提取运行/备用泵能力、管网、浓度、抽采量、负压和大气压力；保留单位，不用手册0.1MPa补值，不算装机能力要求。",
     ["泵站", "备用泵", "装机", "管网", "抽采浓度", "负压"], False),
]


def agent_seed_rules() -> list[dict]:
    rules = []
    for index, (key, name, datatype, unit, kind, page, instruction, hints, enabled) in enumerate(FIELDS):
        dimension_keys = ["workface", "evaluation_unit", "measurement_point", "roadway", "measurement_date", "scope_level"]
        schema = {k: {"required": k == "workface", "repeatable": True,
                      "description": DIMENSION_DESCRIPTIONS[k]} for k in dimension_keys}
        rule = {
            "checklist_type": CHECKLIST["checklist_type"], "field_key": key,
            "field_name_cn": name, "field_description": instruction, "data_type": datatype,
            "unit": unit, "value_kind": kind, "multi_instance": True,
            "required": enabled, "enabled": enabled, "priority": max(1, 100 - index),
            "dimension_schema": schema,
            "strategy": {"methods": ["agent_extract"], "agent_extract": {
                "background": BACKGROUND, "instructions": instruction, "synonyms": hints,
                "negative_examples": ["不得把规范、样例或领域常见数值填写成当前材料实报值", "不得从目录标题推断正文完整", "数字相同不能证明属于同一测点"],
                "identity_dimensions": dimension_keys,
                "evidence_requirements": ["value", "unit", "column_header", "row_header", "scope"],
                "source_roles": ["report_body", "report_sheet", "approval", "statement", "design", "ledger"],
                "manual_references": [f"瓦斯深度监察规则手册-规范化0930.pdf，PDF第{page}页"],
            }},
        }
        rules.append(RulePayload.model_validate(rule).model_dump())
    return rules
