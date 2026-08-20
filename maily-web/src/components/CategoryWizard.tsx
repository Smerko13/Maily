import { useState } from 'react';
import type { CategoryFieldDef, CategoryFieldType, CategoryRule, CategoryTypeMeta, Email, ConnectedAccount } from '../App';
import { FALLBACK_CATEGORY_TYPE_META, CategoryItemCard } from '../App';

// The in-progress schema being designed/edited. Same shape as what the backend persists, minus the
// server-assigned id/isBuiltIn/schemaVersion/automations (automations has no UI in this pass — see
// FEATURE_ROADMAP.md, the data model just reserves room for it).
export interface DraftSchema {
  label: string;
  icon: string;
  classifierDescription: string;
  fields: CategoryFieldDef[];
  matchKeys: string[];
  titleTemplate: string;
  primaryDateField: string;
  cardFields: string[];
  completionRule: CategoryRule | null;
  atRiskRule: CategoryRule | null;
}

interface ReferenceEmailExtraction {
  emailId: string;
  extracted: Record<string, string | null>;
}

interface PreviewItem {
  fields: Record<string, string | null>;
  isComplete: boolean;
  isAtRisk: boolean;
}

interface CategoryWizardProps {
  mode: 'create' | 'replace';
  existingCategoryType?: CategoryTypeMeta;
  emails: Email[];
  accounts: ConnectedAccount[];
  apiBaseUrl: string;
  getAuthToken: () => Promise<string | null>;
  onClose: () => void;
  onSaved: (categoryType: CategoryTypeMeta) => void;
}

const EMPTY_DRAFT: DraftSchema = {
  label: '', icon: '🏷️', classifierDescription: '', fields: [], matchKeys: [],
  titleTemplate: '', primaryDateField: '', cardFields: [], completionRule: null, atRiskRule: null,
};

function toDraft(existing: CategoryTypeMeta): DraftSchema {
  return {
    label: existing.label, icon: existing.icon, classifierDescription: existing.classifierDescription,
    fields: existing.fields, matchKeys: existing.matchKeys, titleTemplate: existing.titleTemplate,
    primaryDateField: existing.primaryDateField, cardFields: existing.cardFields,
    completionRule: existing.completionRule, atRiskRule: existing.atRiskRule,
  };
}

let fieldKeyCounter = 0;
function newFieldKey(): string {
  fieldKeyCounter += 1;
  return `field${fieldKeyCounter}`;
}

const FIELD_TYPE_OPTIONS: { value: CategoryFieldType; label: string }[] = [
  { value: 'string', label: 'Text' },
  { value: 'number', label: 'Number' },
  { value: 'date', label: 'Date' },
  { value: 'enum', label: 'Status (fixed options)' },
  { value: 'boolean', label: 'Yes / No' },
];

type Stage = 'describe' | 'reference' | 'loading' | 'review';

// Multi-stage, AI-assisted Category Wizard: describe → optionally reference real emails → AI proposes
// a draft schema (with a live preview if references were given) → review/refine (manually or via a
// follow-up AI prompt) in a loop → approve. See FEATURE_ROADMAP.md Feature 5B for the full design.
// This is the first component extracted out of App.tsx — its size and nested draft-editing state
// didn't fit the house convention of one inline JSX block per tab.
export default function CategoryWizard({ mode, existingCategoryType, emails, accounts, apiBaseUrl, getAuthToken, onClose, onSaved }: CategoryWizardProps) {
  const [stage, setStage] = useState<Stage>(mode === 'replace' ? 'review' : 'describe');
  const [description, setDescription] = useState('');
  const [accountFilter, setAccountFilter] = useState('all');
  const [selectedReferenceIds, setSelectedReferenceIds] = useState<string[]>([]);
  const [draft, setDraft] = useState<DraftSchema>(existingCategoryType ? toDraft(existingCategoryType) : EMPTY_DRAFT);
  const [referenceEmails, setReferenceEmails] = useState<ReferenceEmailExtraction[]>([]);
  const [previewItem, setPreviewItem] = useState<PreviewItem | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [refineInstruction, setRefineInstruction] = useState('');
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const updateDraft = (updater: (d: DraftSchema) => DraftSchema) => setDraft(prev => updater(prev));

  const addField = () => updateDraft(d => ({ ...d, fields: [...d.fields, { key: newFieldKey(), label: 'New Field', type: 'string' }] }));
  const removeField = (key: string) => updateDraft(d => ({
    ...d,
    fields: d.fields.filter(f => f.key !== key),
    matchKeys: d.matchKeys.filter(k => k !== key),
    cardFields: d.cardFields.filter(k => k !== key),
    primaryDateField: d.primaryDateField === key ? '' : d.primaryDateField,
  }));
  const updateField = (key: string, patch: Partial<CategoryFieldDef>) => updateDraft(d => ({
    ...d, fields: d.fields.map(f => (f.key === key ? { ...f, ...patch } : f)),
  }));
  const toggleMatchKey = (key: string) => updateDraft(d => ({
    ...d, matchKeys: d.matchKeys.includes(key) ? d.matchKeys.filter(k => k !== key) : [...d.matchKeys, key].slice(-2),
  }));
  const toggleCardField = (key: string) => updateDraft(d => ({
    ...d, cardFields: d.cardFields.includes(key) ? d.cardFields.filter(k => k !== key) : [...d.cardFields, key].slice(-2),
  }));

  const callGenerate = async (body: Record<string, unknown>) => {
    const token = await getAuthToken();
    if (!token) throw new Error('No auth token available');
    const response = await fetch(`${apiBaseUrl}/category-types/generate`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
    return response.json();
  };

  const generateDraft = async () => {
    setErrorMessage(null);
    setStage('loading');
    setGenerating(true);
    try {
      const data = await callGenerate({ description, referenceEmailIds: selectedReferenceIds });
      setDraft(data.draft);
      setReferenceEmails(data.referenceEmails || []);
      setPreviewItem(data.previewItem || null);
      setWarnings(data.warnings || []);
      setStage('review');
    } catch (error) {
      console.error('Error generating category draft:', error);
      setErrorMessage('Could not generate a draft. Please try again.');
      setStage('reference');
    } finally {
      setGenerating(false);
    }
  };

  const refineDraft = async () => {
    if (!refineInstruction.trim()) return;
    setErrorMessage(null);
    setGenerating(true);
    try {
      const data = await callGenerate({ currentDraft: draft, instruction: refineInstruction, referenceEmailIds: selectedReferenceIds });
      setDraft(data.draft);
      setReferenceEmails(data.referenceEmails || []);
      setPreviewItem(data.previewItem || null);
      setWarnings(data.warnings || []);
      setRefineInstruction('');
    } catch (error) {
      console.error('Error refining category draft:', error);
      setErrorMessage('Could not apply that change. Please try again.');
    } finally {
      setGenerating(false);
    }
  };

  const approve = async () => {
    setErrorMessage(null);
    setSaving(true);
    try {
      const token = await getAuthToken();
      if (!token) throw new Error('No auth token available');
      const isReplace = mode === 'replace' && existingCategoryType;
      const response = await fetch(`${apiBaseUrl}/category-types`, {
        method: isReplace ? 'PUT' : 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(
          isReplace
            ? { categoryTypeId: existingCategoryType!.id, draft }
            : { draft, referenceEmails }
        ),
      });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      onSaved(data.categoryType);
    } catch (error) {
      console.error('Error saving category type:', error);
      setErrorMessage('Could not save this category. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const referenceCandidates = emails
    .filter(e => accountFilter === 'all' || e.providerEmail === accountFilter)
    .slice(0, 50);

  return (
    <div className="category-wizard">
      <header className="tab-header">
        <button className="btn-back" onClick={onClose}>← Cancel</button>
        <h1>{mode === 'replace' ? `Edit "${existingCategoryType?.label}"` : 'New Smart Category'}</h1>
      </header>

      <div className="tab-body">
        {errorMessage && <p className="wizard-warning">⚠️ {errorMessage}</p>}

        {stage === 'describe' && (
          <div className="wizard-stage">
            <p className="wizard-stage-help">
              Describe what you want to track — be specific about the kind of emails and what matters about them.
            </p>
            <textarea
              className="wizard-textarea"
              rows={4}
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder='e.g. "Bills I need to pay, with the due date, amount, and who it&apos;s owed to"'
            />
            <div className="wizard-stage-actions">
              <button className="btn-sync" disabled={!description.trim()} onClick={() => setStage('reference')}>Next →</button>
            </div>
          </div>
        )}

        {stage === 'reference' && (
          <div className="wizard-stage">
            <p className="wizard-stage-help">
              Optionally pick 1-2 real emails as examples — this helps the AI design better fields and shows a live preview. You can skip this.
            </p>
            {accounts.length > 1 && (
              <div className="account-filter-bar">
                <button className={`filter-tab ${accountFilter === 'all' ? 'active' : ''}`} onClick={() => setAccountFilter('all')}>All Accounts</button>
                {accounts.map(a => (
                  <button key={a.email} className={`filter-tab ${accountFilter === a.email ? 'active' : ''}`} onClick={() => setAccountFilter(a.email)}>{a.email}</button>
                ))}
              </div>
            )}
            <div className="wizard-reference-list">
              {referenceCandidates.length === 0 && <p className="wizard-stage-help">No synced emails to pick from yet.</p>}
              {referenceCandidates.map((e, idx) => {
                const selected = e.emailId ? selectedReferenceIds.includes(e.emailId) : false;
                return (
                  <div
                    key={e.emailId ?? idx}
                    className={`email-item email-item-clickable ${selected ? 'selected' : ''}`}
                    onClick={() => {
                      if (!e.emailId) return;
                      const id = e.emailId;
                      setSelectedReferenceIds(prev => (
                        selected ? prev.filter(existingId => existingId !== id) : (prev.length < 2 ? [...prev, id] : prev)
                      ));
                    }}
                  >
                    <div className="email-item-header">
                      <strong className="email-subject">{e.subject}</strong>
                      {selected && <span className="status-badge status-done">✓ Selected</span>}
                    </div>
                    <p className="email-content">{e.content}</p>
                  </div>
                );
              })}
            </div>
            <div className="wizard-stage-actions">
              <button className="btn-disconnect" onClick={() => setStage('describe')}>← Back</button>
              <button className="btn-sync" disabled={generating} onClick={generateDraft}>
                {selectedReferenceIds.length > 0 ? 'Generate with examples →' : 'Skip & Generate →'}
              </button>
            </div>
          </div>
        )}

        {stage === 'loading' && (
          <div className="wizard-stage wizard-loading">
            <div className="skeleton skeleton-row full" />
            <div className="skeleton skeleton-row full" />
            <p>✨ Designing your category…</p>
          </div>
        )}

        {stage === 'review' && (
          <div className="wizard-stage wizard-review">
            {warnings.length > 0 && (
              <div className="wizard-warnings">
                {warnings.map((w, i) => <p key={i} className="wizard-warning">⚠️ {w}</p>)}
              </div>
            )}

            <div className="wizard-review-columns">
              <div className="wizard-preview-column">
                <h4>Preview</h4>
                <CategoryItemCard
                  item={{
                    itemId: 'preview', categoryType: '__preview__',
                    fields: previewItem?.fields || {}, contributingEmailIds: [],
                    createdAt: '', updatedAt: '',
                    isComplete: previewItem?.isComplete || false, isAtRisk: previewItem?.isAtRisk || false,
                  }}
                  meta={{ ...FALLBACK_CATEGORY_TYPE_META, ...draft, id: '__preview__', isBuiltIn: false }}
                  onClick={() => {}}
                />
                {!previewItem && (
                  <p className="wizard-stage-help">
                    No preview data yet — pick a reference email next time, or approve and it&apos;ll fill in as matching mail arrives.
                  </p>
                )}
              </div>

              <div className="wizard-editor-column">
                <label className="wizard-field-label">Category name</label>
                <input className="label-text-input" value={draft.label} onChange={e => updateDraft(d => ({ ...d, label: e.target.value }))} />

                <label className="wizard-field-label">Icon</label>
                <input className="label-text-input" style={{ width: '70px' }} value={draft.icon} onChange={e => updateDraft(d => ({ ...d, icon: e.target.value }))} />

                <label className="wizard-field-label">What should Maily look for? (used to detect matching emails)</label>
                <textarea
                  className="wizard-textarea"
                  rows={2}
                  value={draft.classifierDescription}
                  onChange={e => updateDraft(d => ({ ...d, classifierDescription: e.target.value }))}
                />

                <h4>Fields</h4>
                <p className="wizard-stage-help">
                  "Unique ID" is the field (if any) that identifies one thing across multiple emails, like an order number — pick 0-2. "On card" controls what shows on the compact card (max 2).
                </p>
                {draft.fields.map(f => (
                  <div key={f.key} className="wizard-field-row">
                    <input className="label-text-input" value={f.label} onChange={e => updateField(f.key, { label: e.target.value })} placeholder="Field label" />
                    <select className="classify-menu-select" value={f.type} onChange={e => updateField(f.key, { type: e.target.value as CategoryFieldType })}>
                      {FIELD_TYPE_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                    </select>
                    {f.type === 'enum' && (
                      <input
                        className="label-text-input"
                        value={(f.values || []).join(', ')}
                        onChange={e => updateField(f.key, { values: e.target.value.split(',').map(v => v.trim()).filter(Boolean) })}
                        placeholder="Comma-separated options"
                      />
                    )}
                    <label className="wizard-field-toggle" title="Once set, this value is never overwritten by a later email">
                      <input type="checkbox" checked={!!f.sticky} onChange={e => updateField(f.key, { sticky: e.target.checked })} />
                      Sticky
                    </label>
                    <label className="wizard-field-toggle">
                      <input type="checkbox" checked={draft.cardFields.includes(f.key)} onChange={() => toggleCardField(f.key)} />
                      On card
                    </label>
                    <label className="wizard-field-toggle">
                      <input type="checkbox" checked={draft.matchKeys.includes(f.key)} onChange={() => toggleMatchKey(f.key)} />
                      Unique ID
                    </label>
                    <button className="btn-disconnect" onClick={() => removeField(f.key)}>Remove</button>
                  </div>
                ))}
                <button className="btn-connect" onClick={addField}>+ Add field</button>

                <div className="wizard-refine-row">
                  <input
                    className="label-text-input label-description-input"
                    value={refineInstruction}
                    onChange={e => setRefineInstruction(e.target.value)}
                    placeholder='Ask AI to adjust, e.g. "add a field for late fees"'
                  />
                  <button className="btn-sync" disabled={generating || !refineInstruction.trim()} onClick={refineDraft}>
                    {generating ? 'Thinking…' : 'Ask AI'}
                  </button>
                </div>
              </div>
            </div>

            <div className="wizard-stage-actions">
              <button className="btn-disconnect" onClick={onClose}>Cancel</button>
              <button className="btn-connect" disabled={saving || draft.fields.length === 0 || !draft.label.trim()} onClick={approve}>
                {saving ? 'Saving…' : mode === 'replace' ? 'Save Changes' : 'Approve & Create'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
