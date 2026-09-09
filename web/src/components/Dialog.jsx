/*
 * A Splunk Modal with the plumbing every dialog in this console repeats.
 *
 * Splunk UI requires a Modal to return focus to whatever opened it, and most
 * of these are opened from a row's action menu rather than from a button this
 * component can hold a ref to. So the element that had focus at the moment the
 * dialog mounted is captured and focused again on close, which is the same
 * outcome by a different route.
 */
import React, { useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Modal from '@splunk/react-ui/Modal';

export default function Dialog({
    title,
    onClose,
    children,
    footer,
    closeLabel = 'Close',
    width = '760px',
    // A dialog holding a form must not vanish on a stray click outside it.
    dismissable = false,
}) {
    const opener = useRef(null);

    useEffect(() => {
        opener.current = document.activeElement;
    }, []);

    const returnFocus = () => {
        const el = opener.current;
        if (el && typeof el.focus === 'function' && document.contains(el)) {
            el.focus();
        }
    };

    return (
        <Modal
            open
            onRequestClose={onClose}
            returnFocus={returnFocus}
            closeOnClickAway={dismissable}
            style={{ width: `min(${width}, 94vw)` }}
        >
            {/* The close button belongs to Modal, not to its header: Modal
                renders it because it was given onRequestClose above. Passing
                the handler here too puts an unknown prop on a DOM node. */}
            <Modal.Header title={title} />
            <Modal.Body style={{ maxHeight: 'min(70vh, 720px)' }}>{children}</Modal.Body>
            <Modal.Footer>
                <Button appearance="secondary" onClick={onClose} label={closeLabel} />
                {footer}
            </Modal.Footer>
        </Modal>
    );
}

Dialog.propTypes = {
    title: PropTypes.node,
    onClose: PropTypes.func.isRequired,
    children: PropTypes.node,
    footer: PropTypes.node,
    closeLabel: PropTypes.string,
    width: PropTypes.string,
    dismissable: PropTypes.bool,
};
