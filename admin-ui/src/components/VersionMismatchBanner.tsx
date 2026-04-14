import { useState, useEffect } from 'react';
import { AlertCircle } from 'lucide-react';

export function VersionMismatchBanner() {
  const [mismatch, setMismatch] = useState(false);
  useEffect(() => {
    const expectedSha = import.meta.env.VITE_EXPECTED_BACKEND_SHA;
    if (!expectedSha) return;
    fetch('/api/version')
      .then(r => r.json())
      .then(data => { if (data.sha && data.sha !== expectedSha) setMismatch(true); })
      .catch(() => {});
  }, []);

  if (!mismatch) return null;
  return (
    <div className="mx-4 mb-2 p-3 bg-yellow-900/50 border border-yellow-700 rounded-lg text-yellow-200 text-sm flex items-center gap-2">
      <AlertCircle size={16} />
      <span>Backend version mismatch — some features may not work as expected. Try refreshing the page.</span>
      <button onClick={() => setMismatch(false)} className="ml-auto text-yellow-400 hover:text-yellow-200">&times;</button>
    </div>
  );
}
