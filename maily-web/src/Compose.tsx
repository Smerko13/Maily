import { useEffect, useRef, useState } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';

type Provider = 'gmail' | 'outlook';

interface ConnectedAccount {
  email: string;
  provider: Provider;
}

export interface ComposeSeed {
  mode: 'new' | 'reply' | 'replyAll' | 'forward';
  senderEmail?: string;
  provider?: Provider;
  to?: string[];
  cc?: string[];
  subject?: string;
  body?: string;
  threadId?: string;
  inReplyTo?: string;
  originalMessageId?: string;
  draftContext?: { subject: string; summary?: string; content?: string };
}

interface ComposeProps {
  accounts: ConnectedAccount[];
  contacts: string[];
  signature: string;
  seed?: ComposeSeed;
  onSent: () => void;
  onCancel: () => void;
}

interface SavedDraft {
  senderEmail: string;
  to: string;
  cc: string;
  bcc: string;
  subject: string;
  body: string;
  replyTo: string;
}

const DRAFT_KEY = 'mailyComposeDraft';
const MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024;

function parseRecipients(value: string): string[] {
  return [...new Set(value.split(/[;,]/).map(item => item.trim()).filter(Boolean))];
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function savedDraft(): Partial<SavedDraft> {
  try {
    return JSON.parse(localStorage.getItem(DRAFT_KEY) || '{}');
  } catch {
    return {};
  }
}

export default function Compose({ accounts, contacts, signature, seed, onSent, onCancel }: ComposeProps) {
  const restored = seed ? {} : savedDraft();
  const initialAccount = accounts.find(account => account.email === seed?.senderEmail) || accounts[0];
  const [senderEmail, setSenderEmail] = useState(seed?.senderEmail || restored.senderEmail || initialAccount?.email || '');
  const [to, setTo] = useState(seed?.to?.join(', ') || restored.to || '');
  const [cc, setCc] = useState(seed?.cc?.join(', ') || restored.cc || '');
  const [bcc, setBcc] = useState(restored.bcc || '');
  const [subject, setSubject] = useState(seed?.subject || restored.subject || '');
  const [body, setBody] = useState(seed?.body || restored.body || '');
  const [replyTo, setReplyTo] = useState(restored.replyTo || '');
  const [attachments, setAttachments] = useState<File[]>([]);
  const [showCopyFields, setShowCopyFields] = useState(Boolean(seed?.cc?.length));
  const [showReplyTo, setShowReplyTo] = useState(Boolean(restored.replyTo));
  const [sending, setSending] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [drafting, setDrafting] = useState(false);
  const [error, setError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const selectedAccount = accounts.find(account => account.email === senderEmail) || initialAccount;
  const attachmentBytes = attachments.reduce((total, file) => total + file.size, 0);

  useEffect(() => {
    const draft: SavedDraft = { senderEmail, to, cc, bcc, subject, body, replyTo };
    localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  }, [senderEmail, to, cc, bcc, subject, body, replyTo]);

  const addAttachments = (files: FileList | null) => {
    if (!files) return;
    const next = [...attachments, ...Array.from(files)];
    if (next.length > 10) {
      setError('You can attach up to 10 files.');
      return;
    }
    if (next.reduce((total, file) => total + file.size, 0) > MAX_ATTACHMENT_BYTES) {
      setError('Attachments must total 3 MB or less.');
      return;
    }
    setError('');
    setAttachments(next);
  };

  const generateDraft = async () => {
    if (!aiPrompt.trim() && !seed?.draftContext) {
      setError('Describe the email you want the AI to draft.');
      return;
    }
    setDrafting(true);
    setError('');
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');
      const payload = aiPrompt.trim()
        ? { prompt: aiPrompt.trim() }
        : seed?.draftContext;
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/draft`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || data.message || 'Draft generation failed');
      setBody(data.draft || '');
    } catch (draftError) {
      setError(draftError instanceof Error ? draftError.message : 'Draft generation failed');
    } finally {
      setDrafting(false);
    }
  };

  const send = async () => {
    if (!selectedAccount || !parseRecipients(to).length || !subject.trim() || !body.trim()) {
      setError('Choose a sender and complete To, Subject, and Body.');
      return;
    }
    setSending(true);
    setError('');
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString();
      if (!token) throw new Error('No auth token available');
      const encodedAttachments = await Promise.all(attachments.map(async file => ({
        filename: file.name,
        mimeType: file.type || 'application/octet-stream',
        content: await fileToBase64(file),
      })));
      const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/send`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          senderEmail: selectedAccount.email,
          provider: selectedAccount.provider,
          to: parseRecipients(to),
          cc: parseRecipients(cc),
          bcc: parseRecipients(bcc),
          subject: subject.trim(),
          body: body.trim(),
          replyTo: replyTo.trim(),
          attachments: encodedAttachments,
          threadId: seed?.threadId,
          inReplyTo: seed?.inReplyTo,
          originalMessageId: seed?.originalMessageId,
          mode: seed?.mode || 'new',
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || data.message || 'Email could not be sent');
      localStorage.removeItem(DRAFT_KEY);
      onSent();
    } catch (sendError) {
      setError(sendError instanceof Error ? sendError.message : 'Email could not be sent');
    } finally {
      setSending(false);
    }
  };

  if (!accounts.length) {
    return (
      <div className="compose-empty">
        <h2>Connect an email account first</h2>
        <p>Sending requires a Gmail or Outlook account with send permission.</p>
        <button className="btn-sync" onClick={onCancel}>Back</button>
      </div>
    );
  }

  return (
    <div className="compose-shell">
      <div className="compose-header">
        <div>
          <span className="compose-kicker">{seed?.mode && seed.mode !== 'new' ? seed.mode : 'New message'}</span>
          <h1>{seed?.mode === 'replyAll' ? 'Reply all' : seed?.mode === 'forward' ? 'Forward' : seed?.mode === 'reply' ? 'Reply' : 'Compose'}</h1>
        </div>
        <button className="compose-close" onClick={onCancel} aria-label="Close compose">×</button>
      </div>

      <div className="compose-fields">
        <label>
          <span>From</span>
          <select value={senderEmail} onChange={event => setSenderEmail(event.target.value)}>
            {accounts.map(account => <option key={`${account.provider}-${account.email}`} value={account.email}>{account.email} · {account.provider}</option>)}
          </select>
        </label>
        <label>
          <span>To</span>
          <input value={to} onChange={event => setTo(event.target.value)} list="compose-contacts" placeholder="name@example.com" />
        </label>
        <datalist id="compose-contacts">{contacts.map(contact => <option value={contact} key={contact} />)}</datalist>

        <div className="compose-field-actions">
          <button onClick={() => setShowCopyFields(value => !value)}>Cc/Bcc</button>
          <button onClick={() => setShowReplyTo(value => !value)}>Reply-To</button>
        </div>

        {showCopyFields && (
          <div className="compose-copy-grid">
            <label><span>Cc</span><input value={cc} onChange={event => setCc(event.target.value)} list="compose-contacts" /></label>
            <label><span>Bcc</span><input value={bcc} onChange={event => setBcc(event.target.value)} list="compose-contacts" /></label>
          </div>
        )}
        {showReplyTo && <label><span>Reply-To</span><input value={replyTo} onChange={event => setReplyTo(event.target.value)} placeholder="replies@example.com" /></label>}
        <label><span>Subject</span><input value={subject} onChange={event => setSubject(event.target.value)} placeholder="Subject" /></label>
      </div>

      <div className="compose-ai-row">
        <input value={aiPrompt} onChange={event => setAiPrompt(event.target.value)} placeholder={seed?.draftContext ? 'Optional: give the AI extra reply instructions' : 'Ask AI to draft this email'} />
        <button onClick={generateDraft} disabled={drafting}>{drafting ? 'Drafting…' : 'Draft with AI'}</button>
      </div>

      <textarea className="compose-body" value={body} onChange={event => setBody(event.target.value)} placeholder="Write your message…" />
      {signature && <p className="compose-signature-note">Your saved signature will be appended when this message is sent.</p>}

      {attachments.length > 0 && (
        <div className="compose-attachments">
          {attachments.map((file, index) => (
            <span key={`${file.name}-${index}`}>{file.name}<button onClick={() => setAttachments(current => current.filter((_, itemIndex) => itemIndex !== index))} aria-label={`Remove ${file.name}`}>×</button></span>
          ))}
        </div>
      )}
      {error && <div className="compose-error">{error}</div>}

      <div className="compose-footer">
        <button type="button" className="compose-attach" onClick={() => fileInputRef.current?.click()}>
          Attach files
        </button>
        <input
          ref={fileInputRef}
          className="compose-file-input"
          type="file"
          multiple
          onChange={event => {
            addAttachments(event.target.files);
            event.target.value = '';
          }}
        />
        <span className="compose-attachment-status" aria-live="polite">
          {attachments.length > 0
            ? `${attachments.length} file${attachments.length === 1 ? '' : 's'} · ${formatAttachmentSize(attachmentBytes)}`
            : 'No files attached'}
        </span>
        <span className="compose-save-state">Draft saved automatically</span>
        <button className="compose-send" onClick={send} disabled={sending}>{sending ? 'Sending…' : 'Send'}</button>
      </div>
    </div>
  );
}