import React from 'react';
import PropTypes from 'prop-types';
import Code from '@splunk/react-ui/Code';
import P from '@splunk/react-ui/Paragraph';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import Dialog from '../components/Dialog';
import { Muted, SectionHeading } from '../components/primitives';

export default function FleetLogsModal({ runId, logs, loading, onClose }) {
    const chunks = (logs && logs.chunks) || [];
    return (
        <Dialog title={`Worker logs for run ${runId}`} width="1080px" onClose={onClose}>
            {loading ? (
                <P>
                    <WaitSpinner /> Collecting logs from the fleet&hellip;
                </P>
            ) : chunks.length ? (
                chunks.map((c) => (
                    <div key={`${c.kind}:${c.id}`}>
                        <SectionHeading>
                            {c.group} ({c.kind} {c.id})
                        </SectionHeading>
                        <Code
                            value={c.text || '(nothing)'}
                            language="plaintext"
                            containerAppearance="section"
                            lineWrap
                        />
                    </div>
                ))
            ) : (
                <Muted>No logs were available.</Muted>
            )}
        </Dialog>
    );
}

FleetLogsModal.propTypes = {
    runId: PropTypes.any,
    logs: PropTypes.object,
    loading: PropTypes.bool,
    onClose: PropTypes.func.isRequired,
};
