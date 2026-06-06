import { useState, useEffect } from 'react';
import { AlertCircle } from 'lucide-react';

// A backend SHA is only comparable when it is a real commit identifier.
// In production the deploy pipeline does not yet populate a real backend SHA,
// so /api/version reports the sentinel 'unknown' (see backend/app/main.py).
// Sentinel/empty values mean "no comparison data" — never a genuine mismatch.
function isRealSha(s: unknown): s is string {
  if (typeof s !== 'string') return false;
  const v = s.trim().toLowerCase();
  return v !== '' && v !== 'unknown';
}

export function VersionMismatchBanner() {
  const [mismatch, setMismatch] = useState(false);
  useEffect(() => {
    const expectedSha = import.meta.env.VITE_EXPECTED_BACKEND_SHA;
    if (!expectedSha || expectedSha === 'unknown') return;
    fetch('/api/version')
      .then(r => r.json())
      .then(data => { if (isRealSha(data.sha) && data.sha !== expectedSha) setMismatch(true); })
      .catch(() => {});
  }, []);

  if (!mismatch) return null;
  return (
    <div className="mx-4 mb-2 p-3 bg-yellow-900/50 border border-yellow-700 rounded-lg text-yellow-200 text-sm flex items-center gap-2">
      <AlertCircle size={16} />
      <span>Backend version mismatch — some features may not work as expected. Try refreshing the page.</span>
      <button
        onClick={() => setMismatch(false)}
        aria-label="Dismiss version mismatch warning"
        className="ml-auto text-yellow-400 hover:text-yellow-200 min-w-[44px] min-h-[44px] flex items-center justify-center"
      >&times;</button>
    </div>
  );
}
