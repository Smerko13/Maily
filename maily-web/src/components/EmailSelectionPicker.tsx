import { useState } from 'react';
import type { Email, LabelDef, ConnectedAccount } from '../App';
import { sortEmailsByDate } from '../App';

interface EmailSelectionPickerProps {
  title: string;
  emails: Email[];
  labels: LabelDef[];
  accounts: ConnectedAccount[];
  selectedIds: string[];
  maxSelected: number;
  onToggle: (emailId: string) => void;
  onDone: () => void;
  onCancel: () => void;
}

// Full-screen overlay that puts the user in "selection mode" over the real inbox — same search/label
// filtering the Inbox tab already has, sorted newest-first, with no cap on how many emails are browsable
// (only on how many can be selected). Used by the Category Wizard so picking example emails happens in
// the same place the user already knows how to search/filter mail, instead of a second, smaller picker
// UI duplicating that logic. Renders as a sibling overlay (not a tab swap), so whatever's underneath
// (e.g. the wizard mid-edit) stays mounted with its state intact while this is open.
export default function EmailSelectionPicker({
  title, emails, labels, accounts, selectedIds, maxSelected, onToggle, onDone, onCancel,
}: EmailSelectionPickerProps) {
  const [accountFilter, setAccountFilter] = useState('all');
  const [labelFilter, setLabelFilter] = useState('all');
  const [search, setSearch] = useState('');

  const byAccount = accountFilter === 'all' ? emails : emails.filter(e => e.providerEmail === accountFilter);
  const byLabel = labelFilter === 'all' ? byAccount : byAccount.filter(e => (e.labels || []).includes(labelFilter));
  const query = search.trim().toLowerCase();
  const visible = sortEmailsByDate(
    query
      ? byLabel.filter(e =>
          e.subject?.toLowerCase().includes(query) ||
          e.from?.toLowerCase().includes(query) ||
          e.content?.toLowerCase().includes(query) ||
          e.summary?.toLowerCase().includes(query)
        )
      : byLabel
  );

  return (
    <div className="email-picker-overlay">
      <div className="email-picker-panel">
        <header className="tab-header">
          <h1>{title}</h1>
          <span className="category-type-row-count">{selectedIds.length} / {maxSelected} selected</span>
        </header>

        {accounts.length > 1 && (
          <div className="account-filter-bar">
            <button className={`filter-tab ${accountFilter === 'all' ? 'active' : ''}`} onClick={() => setAccountFilter('all')}>All Accounts</button>
            {accounts.map(a => (
              <button key={a.email} className={`filter-tab ${accountFilter === a.email ? 'active' : ''}`} onClick={() => setAccountFilter(a.email)}>{a.email}</button>
            ))}
          </div>
        )}

        {labels.length > 0 && (
          <div className="account-filter-bar">
            <button className={`filter-tab ${labelFilter === 'all' ? 'active' : ''}`} onClick={() => setLabelFilter('all')}>All Labels</button>
            {labels.map(l => (
              <button key={l.id} className={`filter-tab ${labelFilter === l.id ? 'active' : ''}`} onClick={() => setLabelFilter(l.id)}>{l.name}</button>
            ))}
          </div>
        )}

        <div className="inbox-search-bar">
          <div className="inbox-search-input-wrap">
            <span className="inbox-search-icon">🔎</span>
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search by subject, sender, or content"
              className="inbox-search-input"
            />
            {search && <button className="inbox-search-clear" onClick={() => setSearch('')} aria-label="Clear search">×</button>}
          </div>
        </div>

        <div className="tab-body email-picker-body">
          {visible.length === 0 ? (
            <div className="empty-inbox">
              <div className="empty-inbox-icon">🔎</div>
              <p>No emails match this search/filter.</p>
            </div>
          ) : (
            <div className="email-list">
              {visible.map((e, idx) => {
                const selected = e.emailId ? selectedIds.includes(e.emailId) : false;
                const disabled = !selected && selectedIds.length >= maxSelected;
                return (
                  <div
                    key={e.emailId ?? idx}
                    className={`email-item email-item-clickable ${selected ? 'selected' : ''} ${disabled ? 'email-item-disabled' : ''}`}
                    onClick={() => { if (e.emailId && !disabled) onToggle(e.emailId); }}
                  >
                    <div className="email-item-header">
                      <strong className="email-subject">{e.subject}</strong>
                      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                        <span className="email-status">{e.receivedAt ? new Date(e.receivedAt).toLocaleString() : ''}</span>
                        {selected && <span className="status-badge status-done">✓ Selected</span>}
                      </div>
                    </div>
                    {((e.labels && e.labels.length > 0) || (e.providerLabels && e.providerLabels.length > 0)) && (
                      <div className="email-label-row">
                        {(e.labels || []).map(labelId => {
                          const def = labels.find(l => l.id === labelId);
                          return def ? <span key={labelId} className="label-chip" style={{ background: def.color }}>{def.name}</span> : null;
                        })}
                        {(e.providerLabels || []).map(name => <span key={name} className="provider-label-badge">{name}</span>)}
                      </div>
                    )}
                    {e.summary && <p className="email-summary"><strong>Summary:</strong> {e.summary}</p>}
                    <p className="email-content">{e.content}</p>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="wizard-stage-actions email-picker-actions">
          <button className="btn-disconnect" onClick={onCancel}>Cancel</button>
          <button className="btn-connect" onClick={onDone}>Done ({selectedIds.length} selected)</button>
        </div>
      </div>
    </div>
  );
}
