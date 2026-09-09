/*
 * A Button that stays disabled, with a spinner, until its handler settles.
 *
 * Half the actions in this console call a Splunk instance: a target report can
 * take the best part of a minute, and an eviction longer. Without this the
 * only feedback is a page that appears to have ignored the click, and the
 * operator clicks again, which is how a cache gets evicted twice.
 */
import React, { useCallback, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import { isAuthError } from '../api';
import { useNotify } from '../Notifications';

export default function BusyButton({ onClick, label, disabled, elementRef, ...rest }) {
    const [busy, setBusy] = useState(false);
    const mounted = useRef(true);
    const notify = useNotify();

    const handle = useCallback(
        async (event, data) => {
            if (busy) {
                return;
            }
            setBusy(true);
            try {
                await onClick(event, data);
            } catch (e) {
                // A 401 has already sent the app back to the sign-in screen.
                if (!isAuthError(e)) {
                    notify(e.message, 'error');
                }
            } finally {
                if (mounted.current) {
                    setBusy(false);
                }
            }
        },
        [busy, notify, onClick]
    );

    React.useEffect(
        () => () => {
            mounted.current = false;
        },
        []
    );

    return (
        <Button
            {...rest}
            elementRef={elementRef}
            disabled={disabled || busy}
            label={label}
            icon={busy ? <WaitSpinner /> : rest.icon}
            onClick={handle}
        />
    );
}

BusyButton.propTypes = {
    onClick: PropTypes.func.isRequired,
    label: PropTypes.node,
    disabled: PropTypes.bool,
    elementRef: PropTypes.any,
    icon: PropTypes.node,
};
