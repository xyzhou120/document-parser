import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { apiError } from "@/services/api";
import {
  documentContext,
  documentImpact,
  documentRelations,
  documentRuns,
  linkDocument,
  relatedDocumentFacts,
  saveDocumentContext,
  unlinkDocument,
  type ContextRevision,
  type DocumentRelation,
  type ImpactPlan,
  type RelatedFactResult,
  type RelationHistory,
  type WorkspaceRun,
} from "@/services/documents";

const subtypes = [
  { value: "unknown", label: "未确定" },
  { value: "report", label: "报告" },
  { value: "report_sheet", label: "报表" },
  { value: "statement", label: "说明" },
  { value: "design", label: "设计" },
  { value: "ledger", label: "台账" },
  { value: "approval", label: "批复" },
];
const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default function DocumentWorkspace() {
  const [inputId, setInputId] = useState("");
  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [runs, setRuns] = useState<WorkspaceRun[]>([]);
  const [contextHistory, setContextHistory] = useState<ContextRevision[]>([]);
  const [revision, setRevision] = useState(0);
  const [subtype, setSubtype] = useState("unknown");
  const [context, setContext] = useState("{}");
  const [actor, setActor] = useState("");
  const [reason, setReason] = useState("");
  const [plan, setPlan] = useState<ImpactPlan | null>(null);
  const [relations, setRelations] = useState<DocumentRelation[]>([]);
  const [relationHistory, setRelationHistory] = useState<RelationHistory[]>([]);
  const [relationRevision, setRelationRevision] = useState(0);
  const [target, setTarget] = useState("");
  const [role, setRole] = useState("");
  const [relationType, setRelationType] = useState("attachment");
  const [facts, setFacts] = useState<RelatedFactResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = async (nextId: string) => {
    if (!uuidPattern.test(nextId.trim())) {
      setError("请输入有效的文档 UUID。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const normalized = nextId.trim();
      const [history, confirmed, impact, links, related] = await Promise.all([
        documentRuns(normalized),
        documentContext(normalized),
        documentImpact(normalized),
        documentRelations(normalized),
        relatedDocumentFacts(normalized),
      ]);
      setId(normalized);
      setTitle(history.file_name);
      setRuns(history.items);
      setContextHistory(confirmed.history);
      setRevision(confirmed.current?.revision || 0);
      setSubtype(confirmed.current?.subtype || "unknown");
      setContext(JSON.stringify(confirmed.current?.context || {}, null, 2));
      setPlan(impact);
      setRelations(links.items);
      setRelationHistory(links.history);
      setRelationRevision(links.revision);
      setFacts(related);
    } catch (e) {
      setError(apiError(e, "加载文档工作区失败，请检查文档编号并重试。"));
    } finally {
      setBusy(false);
    }
  };
  const decision = () => {
    if (!actor.trim() || !reason.trim())
      throw new Error("请填写操作人和变更原因。");
    return { actor: actor.trim(), reason: reason.trim() };
  };
  const saveContext = async () => {
    try {
      const parsed: unknown = JSON.parse(context);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
        throw new Error("上下文必须是 JSON 对象。");
      setBusy(true);
      await saveDocumentContext(id, {
        ...decision(),
        expected_revision: revision,
        subtype,
        context: parsed as Record<string, unknown>,
      });
      await load(id);
    } catch (e) {
      setError(
        e instanceof SyntaxError
          ? "上下文 JSON 格式有误，请检查后重试。"
          : apiError(e, e instanceof Error ? e.message : "保存失败。"),
      );
    } finally {
      setBusy(false);
    }
  };
  const addRelation = async () => {
    try {
      if (!uuidPattern.test(target.trim()) || !role.trim())
        throw new Error("请填写关联文档 UUID 和明确的来源角色。");
      setBusy(true);
      await linkDocument(id, {
        ...decision(),
        expected_revision: relationRevision,
        target_document_id: target.trim(),
        source_role: role.trim(),
        relation_type: relationType.trim(),
      });
      await load(id);
    } catch (e) {
      setError(apiError(e, e instanceof Error ? e.message : "关联失败。"));
    } finally {
      setBusy(false);
    }
  };
  const removeRelation = async (relation: DocumentRelation) => {
    try {
      setBusy(true);
      await unlinkDocument(id, relation.relation_id, {
        ...decision(),
        expected_revision: relationRevision,
      });
      await load(id);
    } catch (e) {
      setError(apiError(e, e instanceof Error ? e.message : "解除关联失败。"));
    } finally {
      setBusy(false);
    }
  };
  const historyTable = (
    <Table
      size="small"
      rowKey="parse_run_id"
      dataSource={runs}
      scroll={{ x: 850 }}
      columns={[
        {
          title: "运行编号",
          dataIndex: "parse_run_id",
          render: (value: string) => <a href={"/result/" + value}>{value}</a>,
        },
        {
          title: "状态",
          dataIndex: "status",
          render: (value: string) => (
            <Tag
              color={
                value === "FAILED"
                  ? "error"
                  : value === "COMPLETED"
                    ? "success"
                    : "processing"
              }
            >
              {value}
            </Tag>
          ),
        },
        { title: "模式", dataIndex: "run_mode" },
        { title: "字段数", dataIndex: "fields_extracted" },
        { title: "创建时间", dataIndex: "created_at" },
      ]}
    />
  );
  return (
    <div style={{ padding: 24 }}>
      <Space direction="vertical" size={20} style={{ width: "100%" }}>
        <div>
          <h1 style={{ marginBottom: 6 }}>文档工作区</h1>
          <p style={{ color: "#667085", margin: 0 }}>
            查看运行历史与规则影响，人工确认附件上下文和来源关系。
          </p>
        </div>
        <Space.Compact block>
          <Input
            aria-label="文档编号"
            placeholder="输入文档 UUID"
            value={inputId}
            onChange={(e) => setInputId(e.target.value)}
            disabled={busy}
            onPressEnter={() => void load(inputId)}
          />
          <Button
            type="primary"
            loading={busy}
            onClick={() => void load(inputId)}
          >
            加载文档
          </Button>
        </Space.Compact>
        {error && (
          <Alert
            type="error"
            showIcon
            message={error}
            closable
            onClose={() => setError("")}
          />
        )}
        {!id ? (
          <Empty description="输入文档编号查看工作区" />
        ) : (
          <>
            <Card
              title={title}
              extra={
                <Button
                  icon={<ReloadOutlined />}
                  loading={busy}
                  onClick={() => void load(id)}
                >
                  刷新
                </Button>
              }
            >
              <Tabs
                items={[
                  { key: "runs", label: "历史运行", children: historyTable },
                  {
                    key: "impact",
                    label: "增量影响预览",
                    children: plan && (
                      <Space direction="vertical" style={{ width: "100%" }}>
                        <Alert
                          type={
                            plan.suggested_mode === "FULL" ? "warning" : "info"
                          }
                          showIcon
                          message={
                            plan.suggested_mode === "FULL"
                              ? "前处理产物不可复用，建议全量提取"
                              : "前处理产物可复用，支持增量提取"
                          }
                          description={plan.note}
                        />
                        {plan.full_reasons.length > 0 && (
                          <div>原因：{plan.full_reasons.join("、")}</div>
                        )}
                        <div>
                          新增字段：{plan.added_fields.join("、") || "无"}
                          ；删除字段：{plan.removed_fields.join("、") || "无"}
                        </div>
                        <div>
                          需要重跑：{plan.rerun_fields.join("、") || "无"}
                          ；可继承：{plan.inherited_fields.join("、") || "无"}
                        </div>
                        <Table
                          rowKey="field_key"
                          size="small"
                          dataSource={plan.changed_fields}
                          columns={[
                            { title: "字段", dataIndex: "field_key" },
                            { title: "变更类型", dataIndex: "category" },
                            {
                              title: "变更属性",
                              dataIndex: "changed_properties",
                              render: (value: string[]) => value.join("、"),
                            },
                          ]}
                        />
                      </Space>
                    ),
                  },
                  {
                    key: "context",
                    label: "附件上下文",
                    children: (
                      <Space direction="vertical" style={{ width: "100%" }}>
                        <Alert
                          type="info"
                          message="人工确认的附件子类型和上下文保留独立版本，仅作来源标注；不会改写已有事实或解析快照。"
                        />
                        <label>
                          附件子类型{" "}
                          <Select
                            style={{ minWidth: 170 }}
                            value={subtype}
                            options={subtypes}
                            onChange={setSubtype}
                          />
                        </label>
                        <label style={{ width: "100%" }}>
                          上下文 JSON（可记录工作面、煤层、评价单元等维度）
                          <Input.TextArea
                            aria-label="上下文 JSON"
                            rows={8}
                            value={context}
                            onChange={(e) => setContext(e.target.value)}
                          />
                        </label>
                        <Button
                          type="primary"
                          loading={busy}
                          onClick={() => void saveContext()}
                        >
                          确认并保存第 {revision + 1} 版
                        </Button>
                        <Collapse
                          items={[
                            {
                              key: "audit",
                              label: "上下文版本记录",
                              children: (
                                <Table
                                  rowKey="context_id"
                                  size="small"
                                  dataSource={contextHistory}
                                  columns={[
                                    { title: "版本", dataIndex: "revision" },
                                    { title: "子类型", dataIndex: "subtype" },
                                    { title: "操作人", dataIndex: "actor" },
                                    { title: "原因", dataIndex: "reason" },
                                  ]}
                                />
                              ),
                            },
                          ]}
                        />
                      </Space>
                    ),
                  },
                  {
                    key: "relations",
                    label: "来源关系",
                    children: (
                      <Space direction="vertical" style={{ width: "100%" }}>
                        <Alert
                          type="info"
                          message="仅支持同矿井文档显式关联。来源角色由人工指定；解除关联须由创建该关系的文档侧操作。"
                        />
                        <Input
                          aria-label="关联文档编号"
                          placeholder="关联文档 UUID"
                          value={target}
                          onChange={(e) => setTarget(e.target.value)}
                        />
                        <Input
                          aria-label="关系来源角色"
                          placeholder="来源角色，例如：设计依据、台账记录、批复依据"
                          value={role}
                          onChange={(e) => setRole(e.target.value)}
                        />
                        <Input
                          aria-label="关系类型"
                          placeholder="关系类型"
                          value={relationType}
                          onChange={(e) => setRelationType(e.target.value)}
                        />
                        <Button
                          type="primary"
                          loading={busy}
                          onClick={() => void addRelation()}
                        >
                          建立关联
                        </Button>
                        <Table
                          rowKey="relation_id"
                          size="small"
                          dataSource={relations}
                          scroll={{ x: 750 }}
                          columns={[
                            {
                              title: "源文档",
                              dataIndex: "source_document_id",
                            },
                            {
                              title: "目标文档",
                              dataIndex: "target_document_id",
                            },
                            { title: "来源角色", dataIndex: "source_role" },
                            {
                              title: "操作",
                              render: (_: unknown, row: DocumentRelation) => (
                                <Button
                                  disabled={
                                    busy || row.source_document_id !== id
                                  }
                                  onClick={() => void removeRelation(row)}
                                >
                                  解除关联
                                </Button>
                              ),
                            },
                          ]}
                        />
                        <Collapse
                          items={[
                            {
                              key: "audit",
                              label: "关系操作记录",
                              children: (
                                <Table
                                  rowKey="revision"
                                  size="small"
                                  dataSource={relationHistory}
                                  columns={[
                                    { title: "版本", dataIndex: "revision" },
                                    { title: "动作", dataIndex: "action" },
                                    { title: "操作人", dataIndex: "actor" },
                                    { title: "原因", dataIndex: "reason" },
                                  ]}
                                />
                              ),
                            },
                          ]}
                        />
                      </Space>
                    ),
                  },
                  {
                    key: "facts",
                    label: "关联事实候选",
                    children: (
                      <Space direction="vertical" style={{ width: "100%" }}>
                        <Alert
                          type="info"
                          message={facts?.note || "仅列出候选，不裁决正确值。"}
                        />
                        <Table
                          rowKey="document_id"
                          size="small"
                          dataSource={facts?.sources || []}
                          columns={[
                            { title: "文档", dataIndex: "file_name" },
                            { title: "关系角色", dataIndex: "source_role" },
                            {
                              title: "事实数",
                              render: (
                                _: unknown,
                                row: RelatedFactResult["sources"][number],
                              ) => row.facts.length,
                            },
                            {
                              title: "运行",
                              dataIndex: "parse_run_id",
                              render: (value?: string | null) =>
                                value ? (
                                  <a href={"/result/" + value}>查看运行</a>
                                ) : (
                                  "无完成运行"
                                ),
                            },
                          ]}
                        />
                        <Collapse
                          items={(facts?.same_identity_candidates || []).map(
                            (group, index) => ({
                              key: String(index),
                              label:
                                JSON.stringify(group.identity) +
                                " · " +
                                (group.different_values
                                  ? "存在不同值"
                                  : "值一致") +
                                " · 未裁决",
                              children: (
                                <pre
                                  style={{
                                    whiteSpace: "pre-wrap",
                                    overflowWrap: "anywhere",
                                  }}
                                >
                                  {JSON.stringify(group.candidates, null, 2)}
                                </pre>
                              ),
                            }),
                          )}
                        />
                      </Space>
                    ),
                  },
                ]}
              />
            </Card>
            <Card title="变更说明">
              <Space direction="vertical" style={{ width: "100%" }}>
                <Input
                  aria-label="操作人"
                  placeholder="操作人（保存上下文或关系时必填）"
                  value={actor}
                  onChange={(e) => setActor(e.target.value)}
                />
                <Input.TextArea
                  aria-label="变更原因"
                  placeholder="确认或变更原因（必填，保留审计）"
                  rows={2}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </Space>
            </Card>
          </>
        )}
      </Space>
    </div>
  );
}
