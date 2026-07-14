import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: ReactNode
  render: (row: T) => ReactNode
  align?: 'start' | 'end' | 'center'
  className?: string
}

export function DataTable<T>({ columns, rows, rowKey, label }: { columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string | number; label: string }) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <caption className="sr-only">{label}</caption>
        <thead><tr>{columns.map((column) => <th key={column.key} scope="col" className={column.className} style={{ textAlign: column.align }}>{column.header}</th>)}</tr></thead>
        <tbody>{rows.map((row) => <tr key={rowKey(row)}>{columns.map((column) => <td key={column.key} className={column.className} style={{ textAlign: column.align }} data-label={typeof column.header === 'string' ? column.header : undefined}>{column.render(row)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}
