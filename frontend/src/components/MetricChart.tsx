export function MetricChart({values, label}: {values: number[]; label: string}) {
  const points = values.map((value, index) => `${index * 32},${100 - Math.min(100, value)}`).join(" ");
  return <figure><figcaption>{label}</figcaption><svg viewBox="0 0 320 110" role="img" aria-label={`${label} chart`}><polyline points={points} fill="none" stroke="currentColor" strokeWidth="3"/></svg><table><caption>{label} numeric values</caption><tbody><tr>{values.map((value, index) => <td key={index}>{value}</td>)}</tr></tbody></table></figure>;
}
