interface SettingsAdvancedSectionProps {
  healthCheckUrlsInput: string;
  onHealthCheckUrlsChange: (value: string) => void;
  onUpdateHealthCheckUrls: (urls: string[]) => void;
}

const KEYBOARD_SHORTCUTS: [string, string][] = [
  ['Ctrl+K', 'Command palette'],
  ['1', 'Strategic chat'],
  ['2', 'Pipeline'],
  ['3', 'Project update'],
  ['4', 'Runtime'],
  ['/', 'Focus search'],
  ['N', 'New task'],
  ['R', 'Refresh'],
  ['S', 'Settings'],
  ['A', 'Archive (pipeline)'],
  ['F', 'Fullscreen (in drawer)'],
  ['Esc', 'Close modal'],
];

const SETTINGS_EXPORT_KEYS = ['viewMode', 'refreshInterval', 'pageSize', 'defaultTaskType', 'notifyOnComplete', 'soundOnComplete', 'archivePageSize'];

export function SettingsAdvancedSection({ healthCheckUrlsInput, onHealthCheckUrlsChange, onUpdateHealthCheckUrls }: SettingsAdvancedSectionProps) {
  return (
    <div className="space-y-6">
      {/* Health Check URLs */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-1">Health Check URLs</h3>
        <p className="text-ui-md text-neutral-600 mb-3">One URL per line. Probed with HTTP GET after each smoketest.</p>
        <textarea
          value={healthCheckUrlsInput}
          onChange={e => onHealthCheckUrlsChange(e.target.value)}
          placeholder="https://example.com/health"
          rows={4}
          className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono resize-y workbench-focus"
        />
        <button
          onClick={() => onUpdateHealthCheckUrls(healthCheckUrlsInput.split('\n').filter(Boolean))}
          className="mt-2 inline-flex items-center justify-center min-h-[44px] bg-workbench-panel hover:bg-workbench-hover text-neutral-300 rounded-lg px-4 py-2 text-xs transition-colors"
        >
          Save URLs
        </button>
      </div>

      {/* Export / Import */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Export / Import</h3>
        <div className="flex gap-3">
          <button
            onClick={() => {
              const settings: Record<string, unknown> = {};
              SETTINGS_EXPORT_KEYS.forEach(k => {
                const val = localStorage.getItem(`blueprint-admin:${k}`);
                if (val !== null) settings[k] = JSON.parse(val);
              });
              const blob = new Blob([JSON.stringify(settings, null, 2)], { type: 'application/json' });
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = 'blueprint-settings.json';
              a.click();
              URL.revokeObjectURL(url);
            }}
            className="inline-flex items-center justify-center min-h-[44px] bg-workbench-panel hover:bg-workbench-hover text-neutral-300 text-xs py-2 px-4 rounded-lg transition-colors border border-neutral-700"
          >
            Export Settings
          </button>
          <button
            onClick={() => {
              const input = document.createElement('input');
              input.type = 'file';
              input.accept = '.json,application/json';
              input.onchange = (e) => {
                const file = (e.target as HTMLInputElement).files?.[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = (ev) => {
                  try {
                    const settings = JSON.parse(ev.target?.result as string);
                    SETTINGS_EXPORT_KEYS.forEach(k => {
                      if (k in settings) localStorage.setItem(`blueprint-admin:${k}`, JSON.stringify(settings[k]));
                    });
                    window.location.reload();
                  } catch { alert('Invalid settings file'); }
                };
                reader.readAsText(file);
              };
              input.click();
            }}
            className="inline-flex items-center justify-center min-h-[44px] bg-workbench-panel hover:bg-workbench-hover text-neutral-300 text-xs py-2 px-4 rounded-lg transition-colors border border-neutral-700"
          >
            Import Settings
          </button>
        </div>
      </div>

      {/* Keyboard Shortcuts */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Keyboard Shortcuts</h3>
        <div className="bg-workbench-base rounded-lg border border-neutral-800 p-4">
          <div className="grid grid-cols-2 gap-x-8 gap-y-2">
            {KEYBOARD_SHORTCUTS.map(([key, label]) => (
              <div key={key} className="flex items-center justify-between">
                <span className="text-xs text-neutral-500">{label}</span>
                <kbd className="text-ui-md font-mono bg-workbench-panel border border-neutral-700 text-neutral-300 px-2 py-0.5 rounded ml-4">{key}</kbd>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
