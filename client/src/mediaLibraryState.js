export function mediaLibraryView({ loading, error, items }) {
  if (loading && items.length === 0) return 'loading'
  if (error && items.length === 0) return 'error'
  if (items.length === 0) return 'empty'
  return 'list'
}

export function mediaStatusLabel(status, activeTaskType) {
  if (activeTaskType === 'ai') return '分析中'
  if (activeTaskType) return '转录中'
  return (
    {
      COMPLETED: '就绪',
      PROCESSING: '处理中',
      FAILED: '失败',
    }[status] || '排队中'
  )
}
