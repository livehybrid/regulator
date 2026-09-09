import React from 'react';
import PropTypes from 'prop-types';
import P from '@splunk/react-ui/Paragraph';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import CacheView from '../components/CacheView';
import Dialog from '../components/Dialog';
import { copyText } from '../clipboard';
import { useNotify } from '../Notifications';

export default function CacheModal({ cache, loading, onClose }) {
    const notify = useNotify();
    const copy = async () => {
        const ok = await copyText(JSON.stringify(cache, null, 2));
        notify(
            ok ? 'Cache JSON copied to the clipboard' : 'The clipboard refused; the JSON is in the browser console',
            ok ? 'success' : 'warning'
        );
    };

    return (
        <Dialog
            title="SmartStore cache"
            onClose={onClose}
            footer={!loading && <BusyButton appearance="primary" label="Copy JSON" onClick={copy} />}
        >
            {loading ? (
                <P>
                    <WaitSpinner /> Reading the cache from the indexers&hellip;
                </P>
            ) : (
                <CacheView cache={cache} />
            )}
        </Dialog>
    );
}

CacheModal.propTypes = {
    cache: PropTypes.object,
    loading: PropTypes.bool,
    onClose: PropTypes.func.isRequired,
};
