import { useRef, useState } from 'react';
import type { CategoryFieldDef, CategoryFieldType, CategoryRule, CategoryTypeMeta, Email } from '../App';
import { FALLBACK_CATEGORY_TYPE_META, CategoryItemCard } from '../App';
import { CategoryFieldValue } from './fields/CategoryFieldValue';

// The in-progress schema being designed/edited. Same shape as what the backend persists, minus the
// server-assigned id/isBuiltIn/schemaVersion/automations (automations has no UI in this pass — see
// FEATURE_ROADMAP.md, the data model just reserves room for it).
export interface DraftSchema {
  label: string;
  icon: string;
  classifierDescription: string;
  fields: CategoryFieldDef[];
  matchKeys: string[];
  keyMode: 'OR' | 'AND';
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

interface CategoryWizardProps {
  mode: 'create' | 'replace';
  existingCategoryType?: CategoryTypeMeta;
  emails: Email[];
  apiBaseUrl: string;
  getAuthToken: () => Promise<string | null>;
  onClose: () => void;
  onSaved: (categoryType: CategoryTypeMeta) => void;
  // Example-email selection lives in App.tsx (not local state here) because picking them happens via
  // the full-inbox EmailSelectionPicker overlay, which is a sibling of this component, not a child —
  // both need to read/write the same selection.
  selectedReferenceIds: string[];
  onOpenEmailPicker: () => void;
  onRemoveReferenceId: (emailId: string) => void;
}

const EMPTY_DRAFT: DraftSchema = {
  label: '', icon: '🏷️', classifierDescription: '', fields: [], matchKeys: [], keyMode: 'OR',
  titleTemplate: '', primaryDateField: '', cardFields: [], completionRule: null, atRiskRule: null,
};

function toDraft(existing: CategoryTypeMeta): DraftSchema {
  return {
    label: existing.label, icon: existing.icon, classifierDescription: existing.classifierDescription,
    fields: existing.fields, matchKeys: existing.matchKeys, keyMode: existing.keyMode ?? 'OR',
    titleTemplate: existing.titleTemplate,
    primaryDateField: existing.primaryDateField, cardFields: existing.cardFields,
    completionRule: existing.completionRule, atRiskRule: existing.atRiskRule,
  };
}

const FIELD_TYPE_LABELS: Record<CategoryFieldType, string> = {
  string: 'Text', number: 'Number', date: 'Date', enum: 'Status', boolean: 'Yes / No',
};

// Turns the matching-key config into one plain-language line — this and describeRule() below are what
// let the review stage show "the big picture" without the user ever seeing raw matchKeys/keyMode JSON.
function describeKey(draft: DraftSchema): string {
  if (draft.matchKeys.length === 0) {
    return 'No matching key set — every matching email will become its own card.';
  }
  const fieldLabel = (key: string) => draft.fields.find(f => f.key === key)?.label ?? key;
  const labels = draft.matchKeys.map(fieldLabel);
  if (labels.length === 1) return `Matches by: ${labels[0]}`;
  const joiner = draft.keyMode === 'AND' ? ' + ' : ' or ';
  const modeNote = draft.keyMode === 'AND' ? ' — all must match together' : ' — any one is enough';
  return `Matches by: ${labels.join(joiner)}${modeNote}`;
}

function describeRule(rule: CategoryRule, fields: CategoryFieldDef[]): string {
  const fieldLabel = (key?: string) => (key && fields.find(f => f.key === key)?.label) || key || '';
  if (rule.type === 'date_passed') return `${fieldLabel(rule.dateField)} has passed`;
  if (rule.type === 'field_equals') return `${fieldLabel(rule.field)} is ${(rule.values || []).join(' or ')}`;
  return `${fieldLabel(rule.dateField)} has passed without ${fieldLabel(rule.field)} reaching ${(rule.values || []).join(' or ')}`;
}

type Stage = 'describe' | 'reference' | 'loading' | 'review';

// Multi-stage, AI-assisted Category Wizard: describe → optionally reference real emails → AI proposes
// a draft schema (with a per-example-email preview) → review (mostly read-only "big picture": name,
// icon, the matching key and completion/at-risk rule in plain language, the field list) → refine via a
// follow-up AI prompt in a loop → approve. Deliberately has almost no direct field-editing UI — name and
// icon are the only two things a user sets by hand; everything else (fields, the key, rules, formats)
// goes through the AI refine box so the user decides the big picture and the AI handles the small print.
export default function CategoryWizard({
  mode, existingCategoryType, emails, apiBaseUrl, getAuthToken, onClose, onSaved,
  selectedReferenceIds, onOpenEmailPicker, onRemoveReferenceId,
}: CategoryWizardProps) {
  const [stage, setStage] = useState<Stage>(mode === 'replace' ? 'review' : 'describe');
  const [description, setDescription] = useState('');
  const [draft, setDraft] = useState<DraftSchema>(existingCategoryType ? toDraft(existingCategoryType) : EMPTY_DRAFT);
  const [referenceEmails, setReferenceEmails] = useState<ReferenceEmailExtraction[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [expandedPreviewId, setExpandedPreviewId] = useState<string | null>(null);
  const [refineInstruction, setRefineInstruction] = useState('');
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // Belt-and-suspenders against a double-submit creating duplicate categories: a ref is checked and set
  // synchronously, before any `await`, so a second click can't slip through in the gap between the
  // click firing and React committing the `saving` state update that disables the button.
  const submittingRef = useRef(false);

  const updateDraft = (updater: (d: DraftSchema) => DraftSchema) => setDraft(prev => updater(prev));

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
      // validation.warnings is a superset of the plain sanitizer warnings — it also includes the
      // Wizard's gate checks (no reliable key, no completion/at-risk rule), surfaced the same way so
      // the user sees them inline without a separate "are you sure" step (see SMART_CATEGORIES_DESIGN.md
      // — this is a soft block: nothing here prevents Approve, it's purely informational).
      setWarnings(data.validation?.warnings ?? data.warnings ?? []);
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
      setWarnings(data.validation?.warnings ?? data.warnings ?? []);
      setRefineInstruction('');
    } catch (error) {
      console.error('Error refining category draft:', error);
      setErrorMessage('Could not apply that change. Please try again.');
    } finally {
      setGenerating(false);
    }
  };

  const approve = async () => {
    if (submittingRef.current) return; // already in flight — ignore a repeat click/Enter entirely
    submittingRef.current = true;
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
      onSaved(data.categoryType); // unmounts this component on success — no need to reset submittingRef
    } catch (error) {
      console.error('Error saving category type:', error);
      setErrorMessage('Could not save this category. Please try again.');
      submittingRef.current = false; // only re-arm on failure, so a genuine retry after an error works
    } finally {
      setSaving(false);
    }
  };

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
              Describe what you want to track — the more specific, the better the first draft. Try to mention:
            </p>
            <ul className="wizard-tips-list">
              <li>What kind of emails — e.g. "event tickets: concerts, shows, sports matches"</li>
              <li>What makes one thing the same across emails — an ID, or a combination like "event name + date"</li>
              <li>When it should count as done — a status reaching some value, or a date passing</li>
            </ul>
            <textarea
              className="wizard-textarea"
              rows={4}
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder='e.g. "Event tickets I get by email — concerts, shows, sports matches. One card per event, matched by event name + date. Mark it done once the event date has passed. Show location and ticket/seat details."'
            />
            <div className="wizard-stage-actions">
              <button className="btn-sync" disabled={!description.trim()} onClick={() => setStage('reference')}>Next →</button>
            </div>
          </div>
        )}

        {stage === 'reference' && (
          <div className="wizard-stage">
            <p className="wizard-stage-help">
              Optionally pick up to 3 real emails as examples — this helps the AI design better fields and shows a live preview. You can skip this.
            </p>
            {selectedReferenceIds.length > 0 && (
              <div className="wizard-fields-summary">
                {selectedReferenceIds.map(id => {
                  const email = emails.find(e => e.emailId === id);
                  return (
                    <div key={id} className="wizard-field-summary-row">
                      <span className="wizard-field-summary-label">{email?.subject ?? id}</span>
                      <button className="btn-disconnect" onClick={() => onRemoveReferenceId(id)}>Remove</button>
                    </div>
                  );
                })}
              </div>
            )}
            <button className="btn-connect" onClick={onOpenEmailPicker}>
              {selectedReferenceIds.length > 0 ? '📥 Change selection' : '📥 Select from Inbox'}
            </button>
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

            <h4>Preview</h4>
            {referenceEmails.length > 0 ? (
              <>
                <p className="wizard-stage-help">Click a card to see every field it extracted, to check the extraction is accurate.</p>
                <div className="wizard-preview-grid">
                  {referenceEmails.map(re => (
                    <div key={re.emailId} className="wizard-preview-item">
                      <CategoryItemCard
                        item={{
                          itemId: re.emailId, categoryType: '__preview__',
                          fields: re.extracted, contributingEmailIds: [],
                          createdAt: '', updatedAt: '',
                          isComplete: false, isAtRisk: false,
                          manualState: null, effectiveState: 'active',
                        }}
                        meta={{ ...FALLBACK_CATEGORY_TYPE_META, ...draft, id: '__preview__', isBuiltIn: false }}
                        onClick={() => setExpandedPreviewId(prev => (prev === re.emailId ? null : re.emailId))}
                      />
                      {expandedPreviewId === re.emailId && (
                        <div className="wizard-preview-detail category-item-fields">
                          {draft.fields.map(f => {
                            const value = re.extracted[f.key];
                            return (
                              <div key={f.key} className="category-item-field">
                                <span className="category-item-field-label">{f.label}</span>
                                <span className="category-item-field-value">
                                  {value ? <CategoryFieldValue fieldDef={f} value={value} /> : <em>— not extracted —</em>}
                                </span>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <p className="wizard-stage-help">
                No example emails were used — a preview will appear once matching mail arrives after you approve.
              </p>
            )}

            <div className="wizard-details-section">
              <label className="wizard-field-label">Category name</label>
              <input className="label-text-input" value={draft.label} onChange={e => updateDraft(d => ({ ...d, label: e.target.value }))} />

              <label className="wizard-field-label">Icon</label>
              <input className="label-text-input" style={{ width: '70px' }} value={draft.icon} onChange={e => updateDraft(d => ({ ...d, icon: e.target.value }))} />

              <h4>Rules</h4>
              <p className="wizard-rule-line">🔑 {describeKey(draft)}</p>
              {draft.completionRule && <p className="wizard-rule-line">✅ Marked done once {describeRule(draft.completionRule, draft.fields)}</p>}
              {draft.atRiskRule && <p className="wizard-rule-line">⚠️ Marked at risk once {describeRule(draft.atRiskRule, draft.fields)}</p>}
              {!draft.completionRule && !draft.atRiskRule && (
                <p className="wizard-stage-help">No completion or at-risk rule — cards can still be marked done by hand.</p>
              )}

              <h4>Fields</h4>
              <div className="wizard-fields-summary">
                {draft.fields.map(f => (
                  <div key={f.key} className="wizard-field-summary-row">
                    <span className="wizard-field-summary-label">{f.label}</span>
                    <span className="wizard-field-summary-type">{FIELD_TYPE_LABELS[f.type]}</span>
                    {f.format && <span className="field-badge">{f.format}</span>}
                    {f.sticky && <span className="wizard-field-summary-tag" title="Set once, never overwritten by a later email">sticky</span>}
                  </div>
                ))}
              </div>

              <p className="wizard-stage-help">
                To change fields, the matching key, or rules, describe the change below — that's the only way to adjust these.
              </p>
              <div className="wizard-refine-row">
                <input
                  className="label-text-input label-description-input"
                  value={refineInstruction}
                  onChange={e => setRefineInstruction(e.target.value)}
                  placeholder='Ask AI to adjust, e.g. "add a field for seat number" or "match by event name only"'
                />
                <button className="btn-sync" disabled={generating || !refineInstruction.trim()} onClick={refineDraft}>
                  {generating ? 'Thinking…' : 'Ask AI'}
                </button>
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
