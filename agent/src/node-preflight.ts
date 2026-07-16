// Node version preflight — checked before any startup work so an incompatible
// runtime fails immediately with an actionable message rather than an opaque
// runtime error deep in the startup sequence.

export const SUPPORTED_NODE_RANGE = '>=18.0.0';
export const RECOMMENDED_NODE_MAJOR = 20;

/**
 * Parse "vMAJOR.MINOR.PATCH" → major integer, or NaN if the string is not
 * a recognisable Node version string.
 */
function parseMajor(versionString: string): number {
  const m = versionString.match(/^v?(\d+)\./);
  return m ? parseInt(m[1], 10) : NaN;
}

/**
 * Return an error message string if `versionString` does not satisfy
 * SUPPORTED_NODE_RANGE, or null if the version is acceptable.
 *
 * Exported for unit testing; callers that want the enforced behaviour
 * should call enforceNodeVersion() instead.
 */
export function checkNodeVersion(versionString: string = process.version): string | null {
  const major = parseMajor(versionString);

  if (isNaN(major)) {
    return (
      `Blueprint runner could not parse the Node.js version string "${versionString}".\n` +
      `Ensure you are running Node.js ${SUPPORTED_NODE_RANGE} ` +
      `(recommended: v${RECOMMENDED_NODE_MAJOR}.x).\n\n` +
      `To install a supported version via nvm:\n` +
      `  nvm install ${RECOMMENDED_NODE_MAJOR}\n` +
      `  nvm use ${RECOMMENDED_NODE_MAJOR}\n`
    );
  }

  if (major < 18) {
    return (
      `Blueprint runner requires Node.js ${SUPPORTED_NODE_RANGE} ` +
      `(found ${versionString}; recommended: v${RECOMMENDED_NODE_MAJOR}.x).\n\n` +
      `To install a supported version via nvm:\n` +
      `  nvm install ${RECOMMENDED_NODE_MAJOR}\n` +
      `  nvm use ${RECOMMENDED_NODE_MAJOR}\n\n` +
      `Then re-run: npm start`
    );
  }

  return null;
}

/**
 * Check the Node version and exit with a clear error message if it is not
 * supported. Call this before any async startup work.
 */
export function enforceNodeVersion(): void {
  const err = checkNodeVersion();
  if (err) {
    process.stderr.write(`\nError: unsupported Node.js version.\n${err}\n`);
    process.exit(1);
  }
}
