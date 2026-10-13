import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Empty, Input, Segmented, Select, Space, Tag, message as toast } from 'antd'
import { ReloadOutlined, SendOutlined } from '@ant-design/icons'
import axios from 'axios'
import { apiError } from '@/services/api'
import { askAssistant, compareAssistantMessage, createAssistantSession, getDocumentCard, getAssistantLocations, getAssistantMessage, getAssistantStatus, requestDocumentCard, resumeAssistantMessage, submitAssistantFeedback, type AssistantComparison, type AssistantSession, type DocumentCard } from '@/services/assistant'
import type { AssistantCitation, AssistantMessage, AssistantStatus, FactEvidence } from '@/types/api'

const running = new Set(['queued', 'pending', 'running', 'retrying', 'yielded', 'waiting_service'])
const stateNames: Record<string, string> = { queued: '等待处理', pending: '等待处理', running: '正在检索', retrying: '等待重试', yielded: '自动续排', waiting_service: '等待服务恢复', waiting_quota: '等待外部额度', completed: '本次回答已完成', done: '本次回答已完成', succeeded: '本次回答已完成', partial: '回答需要核对', failed: '回答失败', failed_terminal: '回答失败，需处理', superseded: '引用版本已失效', blocked: '当前不可回答' }

function failureText(failure: unknown, fallback: string) {
  const detail = axios.isAxiosError(failure) ? failure.response?.data?.detail : undefined
  return detail && typeof detail === 'object' && typeof detail.reason === 'string' ? detail.reason : apiError(failure, fallback)
}

function key(citation: AssistantCitation, index: number) { return citation.citation_id || `${citation.chunk_id || citation.block_id || 'citation'}-${index}` }

export function citationEvidence(citation: AssistantCitation, id: string): FactEvidence {
  const ref = citation.block_ref || citation.table_ref || {}
  const page = citation.page_number ?? ref.page_number ?? ref.physical_page
  return { evidence_id: id, page_number: typeof page === 'number' ? page : null,
    bbox: citation.bbox || ref.bbox as FactEvidence['bbox'],
    coordinate_system: citation.coordinate_system || ref.coordinate_system as string | undefined,
    coordinate_origin: citation.coordinate_origin || ref.coordinate_origin as string | undefined,
    page_rotation: citation.page_rotation, image_width: citation.image_width, image_height: citation.image_height,
    quote: citation.quote, role: 'value', location_precision: citation.bbox || ref.bbox ? 'region' : 'page' }
}

export default function AssistantPanel({ documentId, parseRunId, onCitations }: {
  documentId: string; parseRunId?: string; onCitations: (evidence: FactEvidence[], selected?: FactEvidence) => void
}) {
  const [status, setStatus] = useState<AssistantStatus>()
  const [session, setSession] = useState<AssistantSession>()
  const [messages, setMessages] = useState<AssistantMessage[]>([])
  const [question, setQuestion] = useState('')
  const [mode, setMode] = useState<'answer' | 'all_locations'>('answer')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [unavailable, setUnavailable] = useState(false)
  const [locations, setLocations] = useState<Record<string, AssistantCitation[]>>({})
  const [cursors, setCursors] = useState<Record<string, string | null>>({})
  const [pollId, setPollId] = useState('')
  const [card, setCard] = useState<DocumentCard>()
  const [cardLoading, setCardLoading] = useState(false)
  const [cardError, setCardError] = useState('')
  const [comparison, setComparison] = useState<AssistantComparison>()
  const [compareView, setCompareView] = useState<'automatic' | 'human'>('automatic')
  const [compareBusy, setCompareBusy] = useState('')
  const [feedbackTarget, setFeedbackTarget] = useState<AssistantMessage>()
  const [feedbackType, setFeedbackType] = useState('wrong_value')
  const [feedbackText, setFeedbackText] = useState('')
  const [feedbackReason, setFeedbackReason] = useState('')
  const [feedbackActor, setFeedbackActor] = useState('')
  const [feedbackCitationId, setFeedbackCitationId] = useState('')
  const [cardExpanded, setCardExpanded] = useState(false)
  const requestSequence = useRef(0)
  const callback = useRef(onCitations)
  callback.current = onCitations
  const submitKey = useRef<{ input: string; requestId: string }>()
  const sessionKey = useRef(crypto.randomUUID())
  const cardKey = useRef(crypto.randomUUID())
  const feedbackKey = useRef<{ input: string; requestId: string }>()
  const messageList = useRef<HTMLDivElement>(null)
  const canAsk = status?.can_ask && status.answerer_status !== 'unavailable'

  useEffect(() => {
    const request = ++requestSequence.current
    let disposed = false
    setLoading(true); setError(''); setUnavailable(false); setStatus(undefined)
    setSession(undefined); setMessages([]); setLocations({}); setCursors({}); setPollId(''); callback.current([])
    setCard(undefined); setCardError(''); setComparison(undefined); setFeedbackTarget(undefined); setCardExpanded(false)
    sessionKey.current = crypto.randomUUID(); cardKey.current = crypto.randomUUID(); submitKey.current = undefined
    void getAssistantStatus(documentId).then((value) => { if (!disposed && request === requestSequence.current) setStatus(value) })
      .catch((failure) => {
        if (disposed) return
        const code = axios.isAxiosError(failure) ? failure.response?.status : undefined
        setUnavailable(code === 404)
        setError(code === 404 ? '当前服务未启用文档 AI 助手，请启用后刷新。' : apiError(failure, '助手状态加载失败，请重试。'))
      }).finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [documentId, refresh])

  const putMessage = (value: AssistantMessage) => {
    setMessages((current) => current.some((item) => item.message_id === value.message_id)
      ? current.map((item) => item.message_id === value.message_id ? { ...item, ...value } : item) : [...current, value])
    if (value.next_cursor !== undefined) setCursors((current) => ({ ...current, [value.message_id]: value.next_cursor || null }))
  }

  useEffect(() => {
    if (!pollId) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const request = requestSequence.current
    const poll = async () => {
      try {
        const value = await getAssistantMessage(pollId)
        if (disposed || request !== requestSequence.current) return
        failures = 0; putMessage(value)
        if (running.has((value.execution_status || value.status || '').toLowerCase())) timer = setTimeout(poll, 3000)
        else setPollId('')
      } catch (failure) {
        if (disposed) return
        const code = axios.isAxiosError(failure) ? failure.response?.status : undefined
        setError(failureText(failure, '回答进度读取失败。'))
        if ((code == null || code >= 500 || code === 429) && ++failures <= 3) timer = setTimeout(poll, Math.min(3000 * 2 ** failures, 30000))
        else setPollId('')
      }
    }
    timer = setTimeout(poll, 1500)
    return () => { disposed = true; clearTimeout(timer) }
  }, [pollId])

  useEffect(() => { messageList.current?.scrollTo({ top: messageList.current.scrollHeight, behavior: 'auto' }) }, [messages.length])

  useEffect(() => {
    const currentCard = card
    if (!currentCard || !['queued', 'running'].includes(currentCard.status)) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const request = requestSequence.current
    const poll = async () => {
      try {
        const value = await getDocumentCard(documentId, { revision_id: currentCard.revision_id, generation_id: currentCard.generation_id })
        if (disposed || request !== requestSequence.current) return
        failures = 0; setCard(value); setCardError('')
        if (['queued', 'running'].includes(value.status)) timer = setTimeout(poll, 4000)
      } catch (failure) {
        if (disposed) return
        setCardError(failureText(failure, '资料卡进度读取失败，请刷新重试。'))
        const code = axios.isAxiosError(failure) ? failure.response?.status : undefined
        if ((code == null || code >= 500 || code === 429) && ++failures <= 3) timer = setTimeout(poll, 4000 * failures)
      }
    }
    timer = setTimeout(poll, 2000)
    return () => { disposed = true; clearTimeout(timer) }
  }, [card, documentId])

  const submit = async () => {
    const text = question.trim()
    if (!text || busy || pollId || !canAsk) return
    const request = requestSequence.current
    setBusy('ask'); setError('')
    try {
      const activeSession = session || await createAssistantSession(documentId, sessionKey.current)
      if (request !== requestSequence.current) return
      setSession(activeSession)
      const parentId = messages[messages.length - 1]?.message_id
      const input = JSON.stringify({ text, mode, parent: parentId })
      if (submitKey.current?.input !== input) submitKey.current = { input, requestId: crypto.randomUUID() }
      const value = await askAssistant(activeSession.session_id, { request_id: submitKey.current.requestId, question: text, mode, parent_message_id: parentId })
      if (request !== requestSequence.current) return
      putMessage({ ...value, question: value.question || text, mode: value.mode || mode }); setQuestion(''); submitKey.current = undefined
      if (running.has((value.execution_status || value.status || '').toLowerCase())) setPollId(value.message_id)
    } catch (failure) { if (request === requestSequence.current) setError(failureText(failure, '提问失败，保留了问题，可重试。')) }
    finally { if (request === requestSequence.current) setBusy('') }
  }

  const loadLocations = async (message: AssistantMessage) => {
    const request = requestSequence.current
    setBusy(message.message_id); setError('')
    try {
      const page = await getAssistantLocations(message.message_id, cursors[message.message_id])
      if (request !== requestSequence.current) return
      const incoming = page.items || page.locations || page.citations || []
      setLocations((current) => ({ ...current, [message.message_id]: [...(current[message.message_id] || []), ...incoming].filter((item, index, all) => all.findIndex((candidate) => JSON.stringify(candidate) === JSON.stringify(item)) === index) }))
      setCursors((current) => ({ ...current, [message.message_id]: page.next_cursor || null }))
      putMessage({ ...message, search_finished: page.search_finished, remaining_work: page.remaining_work, next_cursor: page.next_cursor })
    } catch (failure) { if (request === requestSequence.current) setError(failureText(failure, '引用位置加载失败，请重试。')) }
    finally { if (request === requestSequence.current) setBusy('') }
  }

  const resume = async (message: AssistantMessage) => {
    const request = requestSequence.current
    setBusy(message.message_id); setError('')
    try { const value = await resumeAssistantMessage(message.message_id); if (request === requestSequence.current) { putMessage(value); if (running.has((value.execution_status || value.status || '').toLowerCase())) setPollId(value.message_id) } }
    catch (failure) { if (request === requestSequence.current) setError(failureText(failure, '继续检索失败，请检查服务和内容版本。')) }
    finally { if (request === requestSequence.current) setBusy('') }
  }

  const versions = { revision_id: session?.revision_id || status?.revision_id, generation_id: session?.generation_id || status?.generation_id }
  const loadCard = async (force = false) => {
    if (!status?.capabilities?.card) return
    const request = requestSequence.current
    setCardLoading(true); setCardError('')
    setCardExpanded(true)
    try {
      const value = await getDocumentCard(documentId, versions)
      if (request !== requestSequence.current) return
      if (value.status === 'not_generated' || force && value.status === 'failed') {
        if (value.status === 'failed') cardKey.current = crypto.randomUUID()
        const requested = await requestDocumentCard(documentId, { request_id: cardKey.current, ...versions })
        if (request !== requestSequence.current) return
        setCard(requested)
      } else setCard(value)
    } catch (failure) { if (request === requestSequence.current) setCardError(failureText(failure, '资料卡读取失败，请检查当前内容版本。')) }
    finally { if (request === requestSequence.current) setCardLoading(false) }
  }

  const compare = async (item: AssistantMessage) => {
    if (!parseRunId || !status?.capabilities?.compare) return
    const request = requestSequence.current
    setCompareBusy(item.message_id); setError('')
    try { const value = await compareAssistantMessage(item.message_id, { parse_run_id: parseRunId, view: compareView }); if (request === requestSequence.current) setComparison(value) }
    catch (failure) { if (request === requestSequence.current) setError(failureText(failure, '并列比较失败，请检查运行与内容版本。')) }
    finally { if (request === requestSequence.current) setCompareBusy('') }
  }

  const feedback = async () => {
    if (!feedbackTarget || !feedbackText.trim() || !feedbackReason.trim() || !feedbackActor.trim()) return
    const citation = (feedbackTarget.citations || []).find((value) => value.citation_id === feedbackCitationId)
    const rawBbox = citation?.bbox
    const bbox = Array.isArray(rawBbox) ? rawBbox : rawBbox ? [rawBbox.x0, rawBbox.y0, rawBbox.x1, rawBbox.y1] : undefined
    const locator = citation?.page_number ? { page_number: citation.page_number, block_id: citation.block_id, citation_id: citation.citation_id, bbox, coordinate_system: ['PDF_POINT', 'PIXEL', 'NORMALIZED'].includes(citation.coordinate_system || '') ? citation.coordinate_system as 'PDF_POINT' | 'PIXEL' | 'NORMALIZED' : undefined, quote: citation.quote } : undefined
    setBusy('feedback'); setError('')
    try {
      const input = { expected_correction: { type: feedbackType, text: feedbackText.trim() }, reason: feedbackReason.trim(), submitted_by: feedbackActor.trim(), locator }
      const signature = JSON.stringify([feedbackTarget.message_id, input])
      if (feedbackKey.current?.input !== signature) feedbackKey.current = { input: signature, requestId: crypto.randomUUID() }
      await submitAssistantFeedback(feedbackTarget.message_id, { request_id: feedbackKey.current.requestId, ...input })
      toast.success('反馈已登记，等待离线裁决，不会直接修改事实或规则')
      setFeedbackTarget(undefined); setFeedbackText(''); setFeedbackReason(''); feedbackKey.current = undefined
    } catch (failure) { setError(failureText(failure, '反馈提交失败，请核对引用版本和定位。')) }
    finally { setBusy('') }
  }

  return <section className="assistant-panel" aria-label="文档 AI 助手">
    <div className="panel-bar"><strong>文档 AI 助手</strong><Button size="small" icon={<ReloadOutlined />} loading={loading} disabled={!!busy || !!pollId} onClick={() => setRefresh((value) => value + 1)}>重新读取版本</Button></div>
    <div className="assistant-context">
      <p>对当前附件全文提问，支持未配置提取规则的内容。回答按原文证据核对。</p>
      {status && <><Tag color={status.can_ask ? status.coverage_status === 'full' ? 'blue' : 'gold' : 'default'}>{status.can_ask ? status.coverage_status === 'full' ? '全文内容可检索' : '部分内容可检索' : '当前没有可用索引'}</Tag><small>内容版本 {session?.revision_id || status.revision_id || '—'}<br />索引版本 {session?.generation_id || status.generation_id || '—'}</small></>}
      {status?.coverage_status === 'partial' && <Alert type="warning" showIcon message="部分页面或区域尚不可读，回答和未找到结果仅适用于已可检索范围。" />}
      {status && !status.can_ask && <Alert type="info" showIcon message={status.reason || '当前附件尚无可用内容索引，请完成前处理或等待索引构建。'} />}
      {status?.answerer_status === 'unavailable' && <Alert type="warning" showIcon message="问答模型尚不可用，请检查模型服务配置。" />}
      {!!status?.risks?.length && <p className="assistant-risks">{status.risks.map(String).join('；')}</p>}
      <div className="assistant-aux-actions">
        {status?.capabilities?.card && <Button size="small" loading={cardLoading} disabled={!!busy || !!pollId} onClick={() => void loadCard(!!card)}>资料卡</Button>}
        {card && <Tag color={card.status === 'succeeded' ? 'blue' : card.status === 'invalidated' ? 'red' : 'gold'}>资料卡：{card.status === 'succeeded' ? '已生成' : card.status === 'queued' ? '排队生成，自动更新' : card.status === 'not_generated' ? '未生成' : card.status}</Tag>}
        {card && <Button type="link" size="small" onClick={() => setCardExpanded((value) => !value)}>{cardExpanded ? '收起资料卡' : '展开资料卡'}</Button>}
        {status?.capabilities?.compare && parseRunId && <Select size="small" disabled={!!compareBusy} aria-label="并列比较的运行及事实视图" value={compareView} onChange={(value) => { setCompareView(value); setComparison(undefined) }} options={[{ value: 'automatic', label: '当前运行 · 自动事实' }, { value: 'human', label: '当前运行 · 人工事实' }]} />}
      </div>
      {cardError && <Alert type="error" showIcon message={cardError} />}
      {cardExpanded && card && ['succeeded', 'historical'].includes(card.status) && <div className="assistant-card"><strong>原文资料卡</strong><p>{card.summary || '当前版本没有摘要摘录。'}</p>{!!card.topics?.length && <div className="assistant-topics">{card.topics.map((topic) => <Tag key={topic.text}>{topic.text}</Tag>)}</div>} {!!card.outline?.items?.length && <details><summary>目录（{card.outline.items.length}）</summary>{card.outline.items.map((item) => <button type="button" className="assistant-outline" key={`${item.title}-${item.page_number}`} onClick={() => { const citation = (card.citations || []).find((value) => item.citation_ids?.includes(value.citation_id || '')); if (citation) { const evidence = citationEvidence(citation, `card-${citation.citation_id}`); callback.current([evidence], evidence) } }}><span>{item.title}</span><small>第 {item.page_number || '—'} 页</small></button>)}</details>}<details><summary>资料卡引用（{card.citations?.length || 0}）</summary>{(card.citations || []).map((citation) => <button type="button" className="assistant-outline" key={citation.citation_id} disabled={!citation.page_number} onClick={() => { const refs = (card.citations || []).map((ref, index) => citationEvidence(ref, `card-${ref.citation_id || index}`)); callback.current(refs, refs.find((ref) => ref.evidence_id === `card-${citation.citation_id}`)) }}><span>{citation.quote}</span><small>第 {citation.page_number || '—'} 页</small></button>)}</details>{!!card.risks?.length && <p className="assistant-risks">{card.risks.join('；')}</p>}<small>内容 {card.revision_id || '—'} · 索引 {card.generation_id || '—'}</small></div>}
      {cardExpanded && card && ['failed', 'invalidated', 'unavailable'].includes(card.status) && <Alert type="warning" showIcon message={card.status === 'failed' ? '资料卡生成失败，点击资料卡可重试。' : card.status === 'invalidated' ? '资料卡证据已失效，请重新读取内容版本。' : '当前内容版本不可生成资料卡。'} />}
    </div>
    {error && <Alert type={unavailable ? 'info' : 'error'} showIcon message={error} />}
    <div className="assistant-messages" ref={messageList} role="log" aria-label="问答记录">
      {!messages.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={loading ? '正在读取当前附件的助手资格…' : '例如：文档有哪些钻孔记录？请列出原文位置。'} />}
      {messages.map((item) => {
        const citations = locations[item.message_id] || (item.citations || []).slice(0, 30)
        const state = (item.execution_status || item.status || 'pending').toLowerCase()
        const refs = citations.map((citation, index) => citationEvidence(citation, `${item.message_id}-${key(citation, index)}`))
        return <article className="assistant-message" key={item.message_id}>
          <p className="assistant-question"><strong>问题</strong>{item.question}</p>
          <Tag color={['failed', 'failed_terminal', 'superseded', 'blocked'].includes(state) ? 'red' : ['completed', 'done', 'succeeded'].includes(state) ? 'blue' : 'gold'}>{stateNames[state] || state}</Tag>
          <div className="assistant-answer">{item.answer || (running.has(state) ? '正在核对原文，请稍候…' : '本次未生成可核验的回答。')}</div>
          {item.error && <p className="execution-error">{typeof item.error === 'string' ? item.error : String(item.error.reason || item.error.message || item.error.code || JSON.stringify(item.error))}</p>}
          {!!item.risks?.length && <p className="assistant-risks">风险提示：{item.risks.map(String).join('；')}</p>}
          {!!citations.length && <div className="assistant-citations">{citations.map((citation, index) => <button type="button" key={`${key(citation, index)}-${index}`} disabled={!refs[index].page_number || state === 'superseded'} onClick={() => callback.current(refs, refs[index])}>
            <strong>引用 {index + 1} · 第 {refs[index].page_number || '—'} 页</strong><span>{citation.quote || '查看原文区域'}</span>{citation.risk && <span className="assistant-risks">{citation.risk}</span>}
          </button>)}</div>}
          <p className="assistant-search-state">{item.mode === 'answer' ? '普通回答仅列出支撑答案的引用。如需逐一检索，请选择“查找所有位置”。' : item.search_finished ? '范围遍历已结束，语义召回仍受识别质量影响。' : '范围遍历尚未结束，当前引用不代表全部位置。'}</p>
          <Space wrap>
            {(cursors[item.message_id] || (item.mode === 'all_locations' && locations[item.message_id] === undefined)) && <Button size="small" loading={busy === item.message_id} disabled={!!busy || !!pollId} onClick={() => void loadLocations(item)}>{locations[item.message_id] ? '加载更多已发现位置' : '按页浏览已发现位置'}</Button>}
            {!item.search_finished && !running.has(state) && state !== 'superseded' && item.mode === 'all_locations' && <Button size="small" loading={busy === item.message_id} disabled={!!busy || !!pollId} onClick={() => void resume(item)}>继续检索未完成范围</Button>}
            <Button size="small" disabled={!!busy || !!pollId} onClick={() => setPollId(item.message_id)}>刷新回答</Button>
            {status?.capabilities?.compare && parseRunId && <Button size="small" loading={compareBusy === item.message_id} disabled={!!busy || !!pollId} onClick={() => void compare(item)}>与提取结果比较</Button>}
            {status?.capabilities?.feedback && <Button size="small" disabled={!!busy || !!pollId} onClick={() => { setFeedbackTarget(item); setFeedbackCitationId(item.citations?.[0]?.citation_id || '') }}>提交反馈</Button>}
          </Space>
          <small className="assistant-version">内容 {item.revision_id || item.revision || session?.revision_id || '—'} · 索引 {item.generation_id || item.generation || session?.generation_id || '—'}</small>
        </article>
      })}
    </div>
    {comparison && <div className="assistant-compare"><div className="assistant-compare-head"><strong>显式并列比较</strong><Button type="link" size="small" onClick={() => setComparison(undefined)}>收起</Button></div><small>比较运行：{parseRunId}</small>{comparison.version_mismatch && <Alert type="warning" showIcon message="原文回答和提取事实来自不同内容版本，请分别核对。" />}{!!comparison.warnings?.length && <p className="assistant-risks">{comparison.warnings.join('；')}</p>}<div className="assistant-compare-columns"><div><b>原文回答</b><p>{comparison.source_answer.answer || '没有可核验的回答。'}</p><small>内容版本 {comparison.source_answer.revision_id}</small></div><div><b>{compareView === 'human' ? '人工事实' : '自动事实'}</b>{!comparison.fact_view.facts.length && <p>当前视图没有事实。</p>}{comparison.fact_view.facts.map((fact) => <p key={fact.fact_id}><strong>{fact.field_name || fact.field_key}：{fact.value || fact.normalized}{fact.unit ? ` ${fact.unit}` : ''}</strong><br /><small>值类型 {fact.value_kind || '未知'} · 可供下游 {fact.available_for_downstream ? '是' : '否'}</small></p>)}</div></div><small>比较为只读操作，不会写入事实或规则。</small></div>}
    {feedbackTarget && <div className="assistant-feedback"><div className="assistant-feedback-head"><strong>提交引用反馈</strong><Button type="link" size="small" onClick={() => setFeedbackTarget(undefined)}>关闭</Button></div><Select aria-label="反馈类型" value={feedbackType} onChange={setFeedbackType} options={[{ value: 'wrong_value', label: '值不正确' }, { value: 'wrong_kind', label: '值类型不正确' }, { value: 'wrong_source', label: '引用位置不正确' }, { value: 'missing', label: '漏掉原文内容' }]} /><Select aria-label="反馈绑定的原文引用" value={feedbackCitationId} onChange={setFeedbackCitationId} options={[{ value: '', label: '整条回答（无指定引用）' }, ...(feedbackTarget.citations || []).map((citation, index) => ({ value: citation.citation_id || '', label: `引用 ${index + 1} · 第 ${citation.page_number || '—'} 页 · ${(citation.quote || '').slice(0, 35)}` }))]} /><Input.TextArea aria-label="期望修正" placeholder="期望的正确内容" value={feedbackText} onChange={(event) => setFeedbackText(event.target.value)} autoSize={{ minRows: 2, maxRows: 4 }} /><Input aria-label="反馈原因" placeholder="反馈原因" value={feedbackReason} onChange={(event) => setFeedbackReason(event.target.value)} /><Input aria-label="提交人" placeholder="提交人" value={feedbackActor} onChange={(event) => setFeedbackActor(event.target.value)} /><Button type="primary" loading={busy === 'feedback'} disabled={!feedbackText.trim() || !feedbackReason.trim() || !feedbackActor.trim() || !!busy} onClick={() => void feedback()}>登记反馈</Button><small>反馈绑定当前消息引用和内容版本，只进入待裁决样本。</small></div>}
    <div className="assistant-compose">
      <Segmented size="small" value={mode} disabled={!!busy || !!pollId} options={[{ label: '回答问题', value: 'answer', disabled: status?.capabilities?.answer === false }, { label: '查找所有位置', value: 'all_locations', disabled: status?.capabilities?.all_locations === false }]} onChange={(value) => setMode(value as 'answer' | 'all_locations')} />
      <Input.TextArea aria-label="向当前附件提问" placeholder="输入问题，Ctrl / Command + Enter 发送" value={question} maxLength={20000} autoSize={{ minRows: 3, maxRows: 7 }} disabled={!canAsk || !!busy || !!pollId} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void submit() } }} />
      <div className="assistant-compose-actions"><span>{pollId ? '正在处理当前问题…' : '问题和回答仅使用当前附件内容'}</span><Button type="primary" icon={<SendOutlined />} loading={busy === 'ask'} disabled={!canAsk || !question.trim() || !!busy || !!pollId} onClick={() => void submit()}>发送问题</Button></div>
      {status && (!status.capabilities?.card || !status.capabilities?.compare || !status.capabilities?.feedback) && <p className="assistant-capabilities">当前未启用：{[!status.capabilities?.card ? '资料卡' : '', !status.capabilities?.compare ? '显式比较' : '', !status.capabilities?.feedback ? '反馈' : ''].filter(Boolean).join('、')}</p>}
    </div>
  </section>
}
