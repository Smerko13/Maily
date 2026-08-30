import type { CategoryFieldDef } from '../../App';

function formatCurrency(raw: string): string {
  const n = Number(raw);
  if (Number.isNaN(n)) return raw;
  return n.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
}

function formatPercent(raw: string): string {
  const n = Number(raw);
  return Number.isNaN(n) ? raw : `${n}%`;
}

function formatDate(raw: string): string {
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  // Only show a time-of-day when the raw value actually carried one (an ISO date with a "T...") —
  // a date-only value like "2027-05-26" shouldn't grow a spurious "12:00 AM" from local-midnight parsing.
  const hasTime = /T\d{2}:\d{2}/.test(raw);
  return hasTime
    ? d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    : d.toLocaleDateString(undefined, { dateStyle: 'medium' });
}

function formatRelativeDate(raw: string): string {
  const target = new Date(raw);
  if (Number.isNaN(target.getTime())) return raw;
  const diffDays = Math.round((target.getTime() - Date.now()) / 86_400_000);
  if (diffDays === 0) return 'today';
  if (diffDays > 0) return `in ${diffDays} day${diffDays === 1 ? '' : 's'}`;
  return `${Math.abs(diffDays)} day${diffDays === -1 ? '' : 's'} ago`;
}

// Turns a value badge-class-safe: lowercase, spaces to dashes — so e.g. "Out for delivery" becomes
// "field-badge-out-for-delivery", letting App.css style specific statuses without any per-category CSS.
function badgeClass(value: string): string {
  return `field-badge field-badge-${value.trim().toLowerCase().replace(/\s+/g, '-')}`;
}

// Plain-string form of the same formatting rules, shared with anywhere a value needs to land inside
// a larger string rather than its own element — e.g. a card's {titleTemplate} substitution, which
// otherwise showed a raw ISO value like "2027-05-26T20:30:00" (a literal "T") straight from the field.
export function formatFieldValueText(fieldDef: CategoryFieldDef, value: string): string {
  if (fieldDef.type === 'number' && fieldDef.format === 'currency') return formatCurrency(value);
  if (fieldDef.type === 'number' && fieldDef.format === 'percent') return formatPercent(value);
  if (fieldDef.type === 'date' && fieldDef.format === 'relative-date') return formatRelativeDate(value);
  if (fieldDef.type === 'date') return formatDate(value);
  if (fieldDef.type === 'boolean') return value === 'true' ? 'Yes' : 'No';
  return value;
}

// One dispatcher for every field type/format combination a category schema can declare — this is what
// lets a wizard-created field (a currency amount, a status badge, a relative date, a link) render
// appropriately with zero per-category-type frontend code, matching the schema's `type` (+ `format`).
export function CategoryFieldValue({ fieldDef, value }: { fieldDef: CategoryFieldDef; value: string }) {
  if (fieldDef.type === 'string' && fieldDef.format === 'url') {
    return (
      <a href={value} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>
        {value}
      </a>
    );
  }
  if (fieldDef.type === 'boolean') return <>{value === 'true' ? '✅ Yes' : '❌ No'}</>;
  if (fieldDef.type === 'enum') return <span className={badgeClass(value)}>{value}</span>;
  return <>{formatFieldValueText(fieldDef, value)}</>;
}
