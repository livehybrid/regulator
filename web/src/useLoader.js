/*
 * Load-once-and-reload, the shape every list page in this console needs.
 *
 * Returns the data, whether the first load is still running, the error if it
 * failed, and a reload function. A 401 is deliberately not an error: the app
 * shell has already dropped to the sign-in screen by the time it gets here, so
 * rendering "not authenticated" in the middle of the page would be noise.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { isAuthError } from './api';

export default function useLoader(fn, deps = []) {
    const [state, setState] = useState({ data: null, error: null, loading: true });
    // Bumped on every reload so a slow response from a superseded call cannot
    // overwrite a newer one.
    const generation = useRef(0);

    const load = useCallback(async () => {
        generation.current += 1;
        const mine = generation.current;
        setState((s) => ({ ...s, loading: true }));
        try {
            const data = await fn();
            if (generation.current === mine) {
                setState({ data, error: null, loading: false });
            }
        } catch (e) {
            if (generation.current !== mine) {
                return;
            }
            setState({ data: null, error: isAuthError(e) ? null : e.message, loading: false });
        }
        // fn is intentionally not a dependency: callers pass an inline arrow,
        // and depending on it would reload on every render.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, deps);

    useEffect(() => {
        load();
    }, [load]);

    return { ...state, reload: load };
}
