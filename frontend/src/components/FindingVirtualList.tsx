import {useState, type UIEvent} from "react";

export function FindingVirtualList({items}: {items: {id: string}[]}) {
  const windowSize = 80;
  const [start, setStart] = useState(0);
  const visible = items.slice(start, start + windowSize);
  const scroll = (event: UIEvent<HTMLDivElement>) => {
    const requested = Math.floor(event.currentTarget.scrollTop / 48);
    setStart(Math.min(Math.max(0, requested), Math.max(0, items.length - windowSize)));
  };
  return <><div className="virtual-controls" aria-label="Finding window controls">
    <button type="button" disabled={start === 0} onClick={() => setStart(Math.max(0, start - windowSize))}>Previous findings</button>
    <span aria-live="polite">Rows {items.length === 0 ? 0 : start + 1}–{Math.min(items.length, start + windowSize)} of {items.length}</span>
    <button type="button" disabled={start + windowSize >= items.length} onClick={() => setStart(Math.min(items.length - windowSize, start + windowSize))}>Next findings</button>
  </div><div className="virtual-list" role="grid" aria-label="Findings" aria-rowcount={items.length} tabIndex={0} onScroll={scroll}>
    <div style={{height: Math.max(items.length * 48, 48)}}>
      {visible.map((item, index) => <a className="finding-row" role="row" aria-rowindex={start + index + 1} style={{transform: `translateY(${(start + index) * 48}px)`}} key={item.id} href={`/findings/${item.id}`}>{item.id}</a>)}
    </div>
  </div></>;
}
