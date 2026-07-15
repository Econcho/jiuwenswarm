const CELIA_TRACE_ID_HEADER = "x-hag-trace-id";
const CELIA_TRACE_ID_DEFAULT = "celia-memo";
function hasHeader(headers, name) {
    const needle = name.toLowerCase();
    return Object.keys(headers).some((key) => key.toLowerCase() === needle);
}
/**
 * Add memory-celia trace sandbox header without overriding user values.
 */
export function withCeliaSandboxHeaders(headers) {
    const out = { ...headers };
    if (!hasHeader(out, CELIA_TRACE_ID_HEADER)) {
        out[CELIA_TRACE_ID_HEADER] = CELIA_TRACE_ID_DEFAULT;
    }
    return out;
}
