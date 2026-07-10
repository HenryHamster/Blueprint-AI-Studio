export declare function diffPngs(
  beforePath: string,
  afterPath: string,
  diffOutPath: string,
): Promise<{ change_pct: number; changed_pixels: number; total_pixels: number }>;
