import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Spin } from 'antd'
import { LeftOutlined, RightOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons'
import { Document, Page, pdfjs } from 'react-pdf'
import type { PDFPageProxy } from 'pdfjs-dist'
import type { FactEvidence } from '@/types/api'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.js?url'
import 'react-pdf/dist/esm/Page/TextLayer.css'
import 'react-pdf/dist/esm/Page/AnnotationLayer.css'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

const roleNames: Record<string, string> = { value: '值', unit: '单位', header: '表头', scope: '范围', object: '对象', dimension: '维度' }
const roleColors: Record<string, string> = { value: '#087bff', unit: '#6d46b8', header: '#a56b00', scope: '#16754a', object: '#16754a', dimension: '#16754a' }

function evidenceRectangle(evidence: FactEvidence, pdfPage: PDFPageProxy, targetWidth: number) {
  const raw = evidence.bbox
  const box = Array.isArray(raw) ? raw : raw ? [raw.x0, raw.y0, raw.x1, raw.y1] : null
  if (!box || box.length !== 4 || box.some((value) => !Number.isFinite(value))) return null
  const [x0, y0, x1, y1] = box
  if (x0 >= x1 || y0 >= y1) return null
  const base = pdfPage.getViewport({ scale: 1 })
  const viewport = pdfPage.getViewport({ scale: targetWidth / base.width })
  let pdfBox: number[]
  if (evidence.coordinate_system === 'PIXEL') {
    const iw = evidence.image_width, ih = evidence.image_height
    // Pixel coordinates only describe the rendered image when its actual size is known.
    if (!iw || !ih || iw <= 0 || ih <= 0 || x0 < 0 || y0 < 0 || x1 > iw || y1 > ih) return null
    const origin = base.convertToPdfPoint(x0 * base.width / iw, y0 * base.height / ih)
    const end = base.convertToPdfPoint(x1 * base.width / iw, y1 * base.height / ih)
    pdfBox = [...origin, ...end]
  } else if (evidence.coordinate_system === 'PDF_POINT') {
    if (evidence.coordinate_origin === 'bottom_left') {
      pdfBox = box
    } else {
      // Current source parsers use PyMuPDF: top-left, crop-relative, unrotated points.
      // Explicit rotation metadata supports boxes measured from a rendered page.
      const origin = evidence.coordinate_origin
      if (pdfPage.rotate !== 0 && !origin) return null
      if (origin && !['MUPDF_UNROTATED_TOP_LEFT', 'MUPDF_ROTATED_TOP_LEFT', 'top_left'].includes(origin)) return null
      const rotation = origin === 'MUPDF_ROTATED_TOP_LEFT' ? evidence.page_rotation ?? pdfPage.rotate : evidence.coordinate_rotation ?? 0
      const source = pdfPage.getViewport({ scale: 1, rotation })
      if (x0 < 0 || y0 < 0 || x1 > source.width || y1 > source.height) return null
      pdfBox = [...source.convertToPdfPoint(x0, y0), ...source.convertToPdfPoint(x1, y1)]
    }
  } else return null
  const [rx0, ry0, rx1, ry1] = viewport.convertToViewportRectangle(pdfBox)
  const left = Math.min(rx0, rx1), top = Math.min(ry0, ry1)
  const right = Math.max(rx0, rx1), bottom = Math.max(ry0, ry1)
  // Reject out-of-page and degenerate boxes rather than inventing/clamping a location.
  if ([left, top, right, bottom].some((v) => !Number.isFinite(v)) || left < -.1 || top < -.1 || right > viewport.width + .1 || bottom > viewport.height + .1) return null
  return { left, top, width: right - left, height: bottom - top }
}

export default function PdfPreview({ url, evidencePage, evidence = [], selectedEvidenceId }: {
  url: string; evidencePage?: number; evidence?: FactEvidence[]; selectedEvidenceId?: string
}) {
  const container = useRef<HTMLDivElement>(null)
  const scrollContainer = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(350)
  const [count, setCount] = useState(0)
  const [page, setPage] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [pdfPage, setPdfPage] = useState<PDFPageProxy>()
  useEffect(() => {
    if (!container.current) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(180, entry.contentRect.width - 24)))
    observer.observe(container.current)
    return () => observer.disconnect()
  }, [])
  useEffect(() => { setPage(1); setCount(0); setZoom(1); setPdfPage(undefined) }, [url])
  useEffect(() => {
    if (evidencePage && Number.isInteger(evidencePage) && evidencePage > 0 && evidencePage <= count) setPage(evidencePage)
  }, [evidencePage, count, selectedEvidenceId])
  const pageEvidence = evidence.filter((item) => item.page_number === page)
  const rectangles = useMemo(() => pdfPage?.pageNumber === page ? evidence.filter((item) => item.page_number === page).flatMap((item, index) => {
    const rectangle = evidenceRectangle(item, pdfPage, width * zoom)
    return rectangle ? [{ evidence: item, index, rectangle }] : []
  }) : [], [evidence, pdfPage, page, width, zoom])
  useEffect(() => {
    const active = scrollContainer.current?.querySelector<HTMLElement>('[data-selected-evidence="true"]')
    if (active) active.scrollIntoView({ block: 'center', inline: 'center', behavior: 'auto' })
  }, [rectangles, selectedEvidenceId])
  return <div className="pdf-viewer" ref={container}>
    {!!pageEvidence.length && <div style={{ padding: '6px 8px', background: '#f8fbfe', fontSize: 11, color: '#526986' }} aria-live="polite">
      {pageEvidence.length} 处证据 · {rectangles.length} 个有效区域；区域框不代表字段精确位置
      {pageEvidence.some((item) => !item.bbox) && ' · 页级/文本证据仅定位到页'}
      {rectangles.length < pageEvidence.filter((item) => item.bbox).length && ' · 部分坐标无效或缺少坐标规格，未画框'}
    </div>}
    <div className="pdf-canvas-scroll" ref={scrollContainer}><Document file={url}
      onLoadSuccess={({ numPages }) => setCount(numPages)} loading={<div className="pdf-loading"><Spin /><p>加载原文…</p></div>}
      error={<div className="pdf-loading">无法预览此 PDF，<a href={url} target="_blank" rel="noreferrer">打开原文</a>后重试。</div>}
    ><div style={{ position: 'relative', width: width * zoom, flexShrink: 0 }}>
      <Page pageNumber={page} width={width * zoom} loading={<Spin />} onLoadSuccess={setPdfPage} />
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }} aria-label="原文证据高亮">
        {rectangles.map(({ evidence: item, index, rectangle }) => {
          const selected = selectedEvidenceId === (item.evidence_id || `evidence-${index}`)
          const color = roleColors[item.role || 'value'] || '#087bff'
          const precision = item.location_precision || item.location_status || item.scope || 'region'
          return <div key={`${item.evidence_id || index}-${item.role}`} data-selected-evidence={selected} style={{ position: 'absolute', ...rectangle,
            border: `${selected ? 3 : 2}px ${precision === 'region' ? 'dashed' : 'solid'} ${color}`, background: `${color}${selected ? '25' : '14'}` }}>
            <span style={{ position: 'absolute', top: rectangle.top < 20 ? 0 : -18, left: -2, color: '#fff', background: color, fontSize: 10, padding: '1px 3px', whiteSpace: 'nowrap' }}>
              {roleNames[item.role || 'value'] || item.role} · {precision === 'region' ? '取证区域' : precision === 'page' ? '页级' : '原文区域'}
            </span>
          </div>
        })}
      </div>
    </div></Document></div>
    <div className="pdf-controls"><Button size="small" aria-label="上一页" icon={<LeftOutlined />} disabled={page <= 1} onClick={() => setPage((value) => value - 1)} /><span>{count ? `${page} / ${count}` : '— / —'}</span><Button size="small" aria-label="下一页" icon={<RightOutlined />} disabled={page >= count} onClick={() => setPage((value) => value + 1)} /><span className="pdf-control-divider" /><Button size="small" aria-label="缩小文档" icon={<MinusOutlined />} disabled={zoom <= .6} onClick={() => setZoom((value) => Math.max(.6, value - .2))} /><span>{Math.round(zoom * 100)}%</span><Button size="small" aria-label="放大文档" icon={<PlusOutlined />} disabled={zoom >= 2} onClick={() => setZoom((value) => Math.min(2, value + .2))} /></div>
  </div>
}
