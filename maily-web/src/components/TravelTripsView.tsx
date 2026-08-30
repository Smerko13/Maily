import { useEffect, useState } from 'react';
import type { CategoryItem, CategoryTypeMeta, TravelTrip } from '../App';
import { CategoryItemCard } from '../App';

interface TravelTripsViewProps {
  meta: CategoryTypeMeta;
  items: CategoryItem[]; // all Travel category items (active + done), already loaded by the caller
  apiBaseUrl: string;
  getAuthToken: () => Promise<string | null>;
  onOpenItem: (itemId: string) => void;
  onBack: () => void;
}

const WAITING_LIST_ID = '__waiting__';

function parseDateOnly(d: string): number {
  return new Date(d).getTime();
}

// A travel item belongs to a trip if its startDate falls inside [trip.startDate, trip.endDate] —
// computed here, never stored, so editing a trip's dates re-groups items with no migration. The end
// boundary is treated as end-of-day so an item on the last day (e.g. a late flight) still counts.
function tripForItem(item: CategoryItem, trips: TravelTrip[]): TravelTrip | null {
  const startRaw = item.fields.startDate;
  if (!startRaw) return null;
  const t = parseDateOnly(startRaw);
  if (Number.isNaN(t)) return null;
  return trips.find(trip => {
    const tripStart = parseDateOnly(trip.startDate);
    const tripEndExclusive = parseDateOnly(trip.endDate) + 24 * 60 * 60 * 1000;
    return t >= tripStart && t < tripEndExclusive;
  }) ?? null;
}

function splitActiveDone(items: CategoryItem[], meta: CategoryTypeMeta) {
  const active = [...items.filter(i => i.effectiveState === 'active')].sort((a, b) =>
    (a.fields[meta.primaryDateField] || '').localeCompare(b.fields[meta.primaryDateField] || '')
  );
  const done = items.filter(i => i.effectiveState === 'done');
  return { active, done };
}

// The Travel category's one extra ability on top of every other category: grouping cards into
// user-created "trip" wrappers by date range, with an implicit "waiting for a trip" bucket for cards
// that don't fall inside any trip yet. Everything else — the card itself, clicking into its detail —
// is identical to any other smart category (see CategoryItemCard/onOpenItem, unchanged).
export default function TravelTripsView({ meta, items, apiBaseUrl, getAuthToken, onOpenItem, onBack }: TravelTripsViewProps) {
  const [trips, setTrips] = useState<TravelTrip[]>([]);
  const [tripsLoading, setTripsLoading] = useState(true);
  const [openedTripId, setOpenedTripId] = useState<string | null>(null);
  const [showNewTripForm, setShowNewTripForm] = useState(false);
  const [newTripName, setNewTripName] = useState('');
  const [newTripStart, setNewTripStart] = useState('');
  const [newTripEnd, setNewTripEnd] = useState('');
  const [creating, setCreating] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const loadTrips = async () => {
    setTripsLoading(true);
    try {
      const token = await getAuthToken();
      if (!token) throw new Error('No auth token available');
      const response = await fetch(`${apiBaseUrl}/travel-trips`, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      const data = await response.json();
      setTrips(data.trips || []);
    } catch (error) {
      console.error('Error loading trips:', error);
    } finally {
      setTripsLoading(false);
    }
  };

  useEffect(() => { loadTrips(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const createTrip = async () => {
    if (!newTripName.trim() || !newTripStart || !newTripEnd) return;
    setErrorMessage(null);
    setCreating(true);
    try {
      const token = await getAuthToken();
      if (!token) throw new Error('No auth token available');
      const response = await fetch(`${apiBaseUrl}/travel-trips`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newTripName.trim(), startDate: newTripStart, endDate: newTripEnd }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || `HTTP error! status: ${response.status}`);
      }
      setNewTripName(''); setNewTripStart(''); setNewTripEnd(''); setShowNewTripForm(false);
      await loadTrips();
    } catch (error) {
      console.error('Error creating trip:', error);
      setErrorMessage(error instanceof Error ? error.message : 'Could not create this trip. Please try again.');
    } finally {
      setCreating(false);
    }
  };

  const deleteTrip = async (tripId: string) => {
    if (!window.confirm('Delete this trip? Its cards move back to "Waiting for a trip" — nothing about them is deleted.')) return;
    try {
      const token = await getAuthToken();
      if (!token) throw new Error('No auth token available');
      await fetch(`${apiBaseUrl}/travel-trips`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ tripId }),
      });
      setOpenedTripId(null);
      await loadTrips();
    } catch (error) {
      console.error('Error deleting trip:', error);
    }
  };

  const waitingItems = items.filter(item => !tripForItem(item, trips));

  if (openedTripId) {
    const trip = openedTripId === WAITING_LIST_ID ? null : trips.find(t => t.tripId === openedTripId);
    const tripItems = openedTripId === WAITING_LIST_ID
      ? waitingItems
      : items.filter(item => tripForItem(item, trips)?.tripId === openedTripId);
    const { active, done } = splitActiveDone(tripItems, meta);
    return (
      <>
        <header className="tab-header">
          <button className="btn-back" onClick={() => setOpenedTripId(null)}>← Back to Trips</button>
          <h1>{meta.icon} {trip ? trip.name : '⏳ Waiting for a trip'}</h1>
        </header>
        <div className="tab-body">
          {trip && (
            <p className="wizard-stage-help">
              {trip.startDate} – {trip.endDate}
              {' · '}<button className="btn-disconnect" onClick={() => deleteTrip(trip.tripId)}>Delete trip</button>
            </p>
          )}
          <div className="category-item-grid">
            {active.map(item => <CategoryItemCard key={item.itemId} item={item} meta={meta} onClick={() => onOpenItem(item.itemId)} />)}
          </div>
          {active.length === 0 && done.length === 0 && (
            <div className="empty-inbox">
              <div className="empty-inbox-icon">{meta.icon}</div>
              <p>No cards here yet.</p>
            </div>
          )}
          {done.length > 0 && (
            <details className="category-completed-section">
              <summary>Completed ({done.length})</summary>
              <div className="category-item-grid">
                {done.map(item => <CategoryItemCard key={item.itemId} item={item} meta={meta} onClick={() => onOpenItem(item.itemId)} />)}
              </div>
            </details>
          )}
        </div>
      </>
    );
  }

  return (
    <>
      <header className="tab-header">
        <button className="btn-back" onClick={onBack}>← Back to Smart Categories</button>
        <div style={{ display: 'flex', gap: '0.75rem' }}>
          <button className="btn-connect" onClick={() => setShowNewTripForm(prev => !prev)}>+ New Trip</button>
        </div>
      </header>
      <div className="tab-body">
        <h1 style={{ margin: '0 0 16px' }}>{meta.icon} {meta.label}</h1>
        {errorMessage && <p className="wizard-warning">⚠️ {errorMessage}</p>}
        {showNewTripForm && (
          <div className="wizard-details-section" style={{ marginBottom: '24px' }}>
            <label className="wizard-field-label">Trip name</label>
            <input className="label-text-input" value={newTripName} onChange={e => setNewTripName(e.target.value)} placeholder="e.g. Italy 2026" />
            <label className="wizard-field-label">Dates</label>
            <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
              <input type="date" className="label-text-input" value={newTripStart} onChange={e => setNewTripStart(e.target.value)} />
              <span>to</span>
              <input type="date" className="label-text-input" value={newTripEnd} onChange={e => setNewTripEnd(e.target.value)} />
            </div>
            <div className="wizard-stage-actions">
              <button className="btn-disconnect" onClick={() => setShowNewTripForm(false)}>Cancel</button>
              <button className="btn-connect" disabled={creating || !newTripName.trim() || !newTripStart || !newTripEnd} onClick={createTrip}>
                {creating ? 'Creating…' : 'Create Trip'}
              </button>
            </div>
          </div>
        )}

        {tripsLoading ? (
          <div className="category-item-grid">
            {[1, 2].map(i => (
              <div key={i} className="skeleton-email-item">
                <div className="skeleton skeleton-row medium" />
                <div className="skeleton skeleton-row full" />
              </div>
            ))}
          </div>
        ) : (
          <>
            {trips.length === 0 && waitingItems.length === 0 && (
              <p className="wizard-stage-help">No trips yet — create one to start grouping your travel cards, e.g. "Italy 2026".</p>
            )}
            {/* One compact wrapper card per trip — not the individual cards inside it. Click it to
                drill into the same active/done grid every other category drill-in uses. */}
            <div className="category-item-grid">
              {trips.map(trip => {
                const tripItems = items.filter(item => tripForItem(item, trips)?.tripId === trip.tripId);
                return (
                  <div key={trip.tripId} className="category-item-card" onClick={() => setOpenedTripId(trip.tripId)}>
                    <div className="category-item-card-header">
                      <span className="category-item-card-title">🧳 {trip.name}</span>
                    </div>
                    <p className="category-item-card-line">📅 {trip.startDate} – {trip.endDate}</p>
                    <p className="category-item-card-line">{tripItems.length} card{tripItems.length === 1 ? '' : 's'}</p>
                  </div>
                );
              })}
              {waitingItems.length > 0 && (
                <div className="category-item-card" onClick={() => setOpenedTripId(WAITING_LIST_ID)}>
                  <div className="category-item-card-header">
                    <span className="category-item-card-title">⏳ Waiting for a trip</span>
                  </div>
                  <p className="category-item-card-line">{waitingItems.length} card{waitingItems.length === 1 ? '' : 's'}</p>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </>
  );
}
