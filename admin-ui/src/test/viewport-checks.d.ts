export declare const ESLINT_VIEWPORT_PATTERN: string;
export declare const EXEMPT_FROM_VIEWPORT_TEST: Set<string>;
export declare function checkRequiredComponentAssertions(): string[];
export declare function checkAllComponentsHaveTests(): string[];
export declare function check375pxOverflow(): string[];
export declare function checkBackendPatterns(): string[];
export declare function checkBackendEslintPattern(): string[];
export declare function checkBackendExemptionMirror(): string[];
export declare const SINGLE_OPT_OUT_HEADROOM: number;
export declare function classifyEscapeHatchOverage(
  count: number,
  budget: number | undefined,
  headroom?: number,
): 'ok' | 'warn' | 'violation';
export declare function checkEscapeHatchBudget(): string[];
