/*
 * Page-level feedback.
 *
 * The old console used toasts in the bottom-right corner. Splunk UI deprecates
 * that pattern outright (accessibility: a message that removes itself is a
 * message a screen-reader user, or anyone who looked away, never received), so
 * this is a MessageBar at the top of the page instead.
 *
 * Splunk's guidance also says not to stack MessageBars, because stacking
 * breaks the one-to-one relationship between a message and the action that
 * caused it. So only the newest is rendered, and older unread ones are counted
 * next to it rather than piling up down the page.
 */
import React, { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import MessageBar from '@splunk/react-ui/MessageBar';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

const NotifyContext = createContext(() => {});

const Bar = styled.div`
    position: sticky;
    top: 0;
    z-index: 10;
    padding: ${variables.spacingSmall} ${variables.spacingLarge} 0;
    background-color: ${variables.backgroundColorPage};
`;

const Count = styled.span`
    margin-left: ${variables.spacingSmall};
    color: ${variables.contentColorMuted};
    font-size: ${variables.fontSizeSmall};
`;

// A success confirmation is the one kind that may reasonably disappear: the
// state it reports is visible in the page behind it. Errors stay until read.
const AUTO_DISMISS_MS = 8000;

export function NotificationProvider({ children }) {
    const [queue, setQueue] = useState([]);
    const nextId = useRef(1);

    const dismiss = useCallback((id) => {
        setQueue((q) => q.filter((m) => m.id !== id));
    }, []);

    const notify = useCallback(
        (message, type = 'error') => {
            const id = nextId.current;
            nextId.current += 1;
            const entry = { id, message: String(message), type };
            // Cap the queue: a run that fails every two seconds must not grow
            // an unbounded list of identical messages in memory.
            setQueue((q) => [...q.slice(-9), entry]);
            if (type === 'success' || type === 'info') {
                setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
            }
            return id;
        },
        [dismiss]
    );

    const value = useMemo(() => notify, [notify]);
    const current = queue[queue.length - 1];
    const older = queue.length - 1;

    return (
        <NotifyContext.Provider value={value}>
            {current && (
                <Bar>
                    <MessageBar
                        type={current.type}
                        aria-label="Regulator notification"
                        onRequestClose={() => dismiss(current.id)}
                    >
                        {current.message}
                        {older > 0 && <Count>and {older} earlier message(s)</Count>}
                    </MessageBar>
                </Bar>
            )}
            {children}
        </NotifyContext.Provider>
    );
}

NotificationProvider.propTypes = {
    children: PropTypes.node,
};

export const useNotify = () => useContext(NotifyContext);
