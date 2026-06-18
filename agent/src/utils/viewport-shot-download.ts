import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';

export interface ViewportShotEntry {
  name: string;
}

export interface DownloadViewportShotsOptions {
  entries: ViewportShotEntry[];
  shotDir: string;
  orchestratorUrl: string;
  projectId: string;
  sourceTaskId: string;
  /** Maximum number of screenshots to download (default: 8) */
  limit?: number;
  onLog?: (msg: string) => void;
  onError?: (name: string) => void;
}

/**
 * Downloads viewport screenshots from the orchestrator into a local directory.
 *
 * Sanitizes each entry name with path.basename() to prevent path traversal
 * (a crafted name like ../../etc/evil could otherwise escape shotDir).
 *
 * Returns the list of local file paths that were successfully downloaded.
 */
export function downloadViewportShots(opts: DownloadViewportShotsOptions): string[] {
  const { entries, shotDir, orchestratorUrl, projectId, sourceTaskId, limit = 8 } = opts;
  const downloaded: string[] = [];

  try { fs.mkdirSync(shotDir, { recursive: true }); } catch { /* ignore */ }

  for (const entry of entries.slice(0, limit)) {
    const rawName = typeof entry.name === 'string' ? entry.name : '';
    if (!rawName) continue;
    const safeName = path.basename(rawName);
    if (!safeName) continue;
    const downloadUrl =
      `${orchestratorUrl}/api/admin/viewport-shots/download-url` +
      `?project_id=${encodeURIComponent(projectId)}` +
      `&task_id=${encodeURIComponent(sourceTaskId)}` +
      `&name=${encodeURIComponent(rawName)}`;
    const dest = `${shotDir}/${safeName}.png`;
    try {
      execFileSync('curl', ['-sfL', '--max-time', '30', '-o', dest, downloadUrl]);
      downloaded.push(dest);
    } catch {
      opts.onError?.(rawName);
    }
  }

  return downloaded;
}
