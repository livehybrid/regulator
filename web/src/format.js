/*
 * Number, byte, duration and timestamp formatting.
 *
 * Ported unchanged from the hand-written console apart from the HTML escaping,
 * which React does. These are shared with the run pages and the tables, so a
 * p95 reads the same everywhere: "1.24 s" in one place and "1240ms" in another
 * is how two people end up comparing different numbers.
 */

// An en dash for "no value", so a missing figure is visibly missing rather
// than silently rendering as zero.
export const DASH = '–';

export const isBlank = (v) => v === null || v === undefined || Number.isNaN(v);

export function fmtInt(v) {
    return isBlank(v) ? DASH : Math.round(v).toLocaleString('en-GB');
}

export function fmtNum(v, dp = 2) {
    return isBlank(v) ? DASH : Number(v).toFixed(dp);
}

export function fmtPct(v, dp = 1) {
    return isBlank(v) ? DASH : `${Number(v).toFixed(dp)}%`;
}

export function fmtMs(v) {
    if (isBlank(v)) {
        return DASH;
    }
    if (v >= 10000) {
        return `${(v / 1000).toFixed(1)} s`;
    }
    if (v >= 1000) {
        return `${(v / 1000).toFixed(2)} s`;
    }
    if (v >= 10) {
        return `${Math.round(v).toLocaleString('en-GB')} ms`;
    }
    return `${Number(v).toFixed(1)} ms`;
}

export function fmtBytes(b) {
    if (isBlank(b)) {
        return DASH;
    }
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0;
    let n = Number(b);
    while (n >= 1024 && i < units.length - 1) {
        n /= 1024;
        i += 1;
    }
    return `${i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}

export function fmtDur(s) {
    if (isBlank(s)) {
        return DASH;
    }
    if (s < 90) {
        return `${Number(s).toFixed(s < 10 ? 1 : 0)} s`;
    }
    const m = Math.floor(s / 60);
    const r = Math.round(s % 60);
    if (m < 60) {
        return `${m}m ${r}s`;
    }
    return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function toDate(v) {
    if (v === null || v === undefined || v === '') {
        return null;
    }
    // Epoch seconds from the API, epoch millis from anything that has already
    // been through a JS Date. 1e11 seconds is the year 5138, so anything above
    // it is milliseconds.
    const d = typeof v === 'number' ? new Date(v * (v > 1e11 ? 1 : 1000)) : new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
}

/** "4m ago" and friends, with the absolute time kept for the title attribute. */
export function relativeTime(v) {
    const d = toDate(v);
    if (!d) {
        return { text: DASH, title: '' };
    }
    const diff = (Date.now() - d.getTime()) / 1000;
    let text;
    if (diff < 60) {
        text = `${Math.max(0, Math.round(diff))}s ago`;
    } else if (diff < 3600) {
        text = `${Math.round(diff / 60)}m ago`;
    } else if (diff < 86400) {
        text = `${Math.round(diff / 3600)}h ago`;
    } else {
        text = `${Math.round(diff / 86400)}d ago`;
    }
    return { text, title: d.toLocaleString('en-GB') };
}
