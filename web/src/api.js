/*
 * The one place that talks to the control plane.
 *
 * Every call goes through here so that a 401 has a single meaning: the session
 * has gone, and the whole app drops back to the sign-in screen. Components
 * never see an authentication failure as an error to render.
 */

export class ApiError extends Error {
    constructor(message, status) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
    }
}

// Set by the app on mount. A module-level hook rather than a React context
// because api() is called from event handlers and effects alike, and threading
// a context through every one of them buys nothing.
let onUnauthorised = () => {};
export function setUnauthorisedHandler(fn) {
    onUnauthorised = fn;
}

export async function api(path, opts = {}) {
    const init = {
        method: opts.method || 'GET',
        credentials: 'same-origin',
        headers: {},
    };
    if (opts.body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
    }
    if (opts.signal) {
        init.signal = opts.signal;
    }

    let res;
    try {
        res = await fetch(`/api${path}`, init);
    } catch (e) {
        throw new ApiError(`Cannot reach the Regulator server: ${e.message}`, 0);
    }

    if (res.status === 401) {
        onUnauthorised();
        throw new ApiError('Session expired, please sign in again', 401);
    }
    if (res.status === 204) {
        return null;
    }

    const text = await res.text();
    let data = null;
    if (text) {
        try {
            data = JSON.parse(text);
        } catch (e) {
            data = text;
        }
    }
    if (!res.ok) {
        let detail = data && typeof data === 'object' ? data.detail || data.error || data.message : data;
        if (detail && typeof detail === 'object') {
            detail = JSON.stringify(detail);
        }
        throw new ApiError(String(detail || res.statusText || `HTTP ${res.status}`), res.status);
    }
    return data;
}

/** Fetch text rather than JSON: the savedsearches.conf export. */
export async function apiText(path) {
    const res = await fetch(`/api${path}`, { credentials: 'same-origin' });
    if (res.status === 401) {
        onUnauthorised();
        throw new ApiError('Session expired, please sign in again', 401);
    }
    if (!res.ok) {
        throw new ApiError(res.statusText || `HTTP ${res.status}`, res.status);
    }
    return res.text();
}

/** True for the one error that must never be shown to the user as a failure. */
export const isAuthError = (e) => e instanceof ApiError && e.status === 401;
