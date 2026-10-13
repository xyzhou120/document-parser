import api from "./api";
export interface WorkspaceRun {
  parse_run_id: string;
  parent_parse_run_id?: string | null;
  status: string;
  run_mode?: string;
  created_at: string;
  fields_extracted: number;
  error_message?: string | null;
}
export interface ContextRevision {
  context_id: string;
  document_id: string;
  revision: number;
  subtype: string;
  context: Record<string, unknown>;
  actor: string;
  reason: string;
  created_at: string;
}
export interface DocumentRelation {
  relation_id: string;
  source_document_id: string;
  target_document_id: string;
  relation_type: string;
  source_role: string;
  actor: string;
  reason: string;
}
export interface ImpactPlan {
  suggested_mode: string;
  parent_parse_run_id?: string | null;
  full_reasons: string[];
  added_fields: string[];
  removed_fields: string[];
  rerun_fields: string[];
  inherited_fields: string[];
  configuration_changes: string[];
  changed_fields: Array<{
    field_key: string;
    category: string;
    changed_properties: string[];
  }>;
  note: string;
}
export interface RelationHistory {
  revision: number;
  action: string;
  actor: string;
  reason: string;
}
export interface RelatedFactResult {
  sources: Array<{
    document_id: string;
    file_name: string;
    source_role: string;
    parse_run_id?: string | null;
    facts: unknown[];
  }>;
  same_identity_candidates: Array<{
    identity: Record<string, unknown>;
    candidates: Array<Record<string, unknown>>;
    different_values: boolean;
  }>;
  unmatched_facts: unknown[];
  note: string;
}
export interface Decision {
  actor: string;
  reason: string;
  expected_revision: number;
}
export const documentRuns = (
  id: string,
): Promise<{ file_name: string; mine_name: string; items: WorkspaceRun[] }> =>
  api.get("/v1/documents/" + id + "/runs");
export const documentContext = (
  id: string,
): Promise<{ current: ContextRevision | null; history: ContextRevision[] }> =>
  api.get("/v1/documents/" + id + "/context");
export const saveDocumentContext = (
  id: string,
  body: Decision & { subtype: string; context: Record<string, unknown> },
) => api.post("/v1/documents/" + id + "/context", body);
export const documentImpact = (id: string): Promise<ImpactPlan> =>
  api.get("/v1/documents/" + id + "/impact-plan");
export const documentRelations = (
  id: string,
): Promise<{
  revision: number;
  items: DocumentRelation[];
  history: RelationHistory[];
}> => api.get("/v1/documents/" + id + "/relations");
export const linkDocument = (
  id: string,
  body: Decision & {
    target_document_id: string;
    source_role: string;
    relation_type: string;
  },
) => api.post("/v1/documents/" + id + "/relations", body);
export const unlinkDocument = (id: string, relation: string, body: Decision) =>
  api.delete("/v1/documents/" + id + "/relations/" + relation, { data: body });
export const relatedDocumentFacts = (id: string): Promise<RelatedFactResult> =>
  api.get("/v1/documents/" + id + "/related-facts");
