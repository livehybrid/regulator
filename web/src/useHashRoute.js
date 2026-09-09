/*
 * Routing.
 *
 * A hash route, not a router library: the control plane serves one HTML file
 * from "/" and nothing else, so a path-based router would 404 on refresh
 * unless the server grew a catch-all, and Splunk UI has no routing component
 * to use instead. Twenty lines here beats a dependency and a server change.
 */
import { useCallback, useEffect, useState } from 'react';

const read = () => (window.location.hash || '').replace(/^#/, '') || 'targets';

export function useHashRoute() {
    const [route, setRoute] = useState(read);

    useEffect(() => {
        const onChange = () => setRoute(read());
        window.addEventListener('hashchange', onChange);
        return () => window.removeEventListener('hashchange', onChange);
    }, []);

    const navigate = useCallback((next) => {
        window.location.hash = `#${next}`;
    }, []);

    return [route, navigate];
}

/** Splits "run/17" into { page: "run", id: "17" }, decoding the id. */
export function parseRoute(route) {
    const slash = route.indexOf('/');
    if (slash < 0) {
        return { page: route, id: null };
    }
    return { page: route.slice(0, slash), id: decodeURIComponent(route.slice(slash + 1)) };
}
