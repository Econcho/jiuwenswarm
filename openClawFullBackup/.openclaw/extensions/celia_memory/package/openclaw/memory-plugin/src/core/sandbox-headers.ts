const CELIA_TRACE_ID_HEADER = "x-hag-trace-id";
const CELIA_TRACE_ID_DEFAULT = "celia-memo";

function hasHeader(
  headers: Record<string, string>,
  name: string,
): boolean {
  const needle = name.toLowerCase();
  return Object.keys(headers).some((key) => key.toLowerCase() === needle);
}

/**
 * Add memory-celia trace sandbox header without overriding user values.
 */
export function withCeliaSandboxHeaders(
  headers: Record<string, string>,
): Record<string, string> {
  const out: Record<string, string> = { ...headers };
  if (!hasHeader(out, CELIA_TRACE_ID_HEADER)) {
    out[CELIA_TRACE_ID_HEADER] = CELIA_TRACE_ID_DEFAULT;
  }
  return out;
}
