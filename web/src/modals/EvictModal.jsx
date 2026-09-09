/*
 * Evicting part of a SmartStore cache.
 *
 * The warning is deliberately long and deliberately first. On a shared cluster
 * most of that cache belongs to other people's dashboards and scheduled
 * searches, and there is no undo beyond waiting for everything to re-localise.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Checkbox from '@splunk/react-ui/Checkbox';
import DL from '@splunk/react-ui/DefinitionList';
import Message from '@splunk/react-ui/Message';
import P from '@splunk/react-ui/Paragraph';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Mono, Muted, Note, SectionHeading } from '../components/primitives';
import { api } from '../api';
import { fmtBytes, fmtInt } from '../format';
import { useNotify } from '../Notifications';

export default function EvictModal({ targetId, cache, loading, onClose, onDone }) {
    const [selected, setSelected] = useState([]);
    const [all, setAll] = useState(false);
    const [result, setResult] = useState(null);
    const notify = useNotify();

    const names = Object.keys((cache && cache.per_index) || {});
    const toggle = (name) =>
        setSelected((s) => (s.includes(name) ? s.filter((x) => x !== name) : [...s, name]));

    const evict = async () => {
        const r = await api(`/targets/${encodeURIComponent(targetId)}/evict`, {
            method: 'POST',
            body: { indexes: selected, all_indexes: all },
        });
        setResult(r);
        notify(`Evicted ${fmtInt((r.eviction || {}).evicted)} buckets`, 'success');
        if (onDone) {
            onDone();
        }
    };

    return (
        <Dialog
            title="Evict the SmartStore cache"
            onClose={onClose}
            footer={
                <BusyButton
                    appearance="destructive"
                    label="Evict"
                    disabled={!(all || selected.length) || !!result}
                    onClick={evict}
                />
            }
        >
            <Message appearance="fill" type="warning">
                <Message.Title>Read this before you tick anything</Message.Title>
                Eviction does not destroy data: every bucket still exists in object storage. What
                it costs is time. The next searches over that data have to re-download it, so they
                will be slow, and on a shared cluster most of that cache belongs to other people&apos;s
                dashboards and scheduled searches, not to you. There is no undo beyond waiting for
                everything to re-localise.
            </Message>

            <SectionHeading>Indexes</SectionHeading>
            {loading ? (
                <P>
                    <WaitSpinner /> Reading the cache&hellip;
                </P>
            ) : names.length ? (
                names.map((n) => (
                    <Checkbox
                        key={n}
                        checked={selected.includes(n)}
                        onChange={() => toggle(n)}
                        value={n}
                    >
                        <Mono>{n}</Mono>{' '}
                        <Muted $small>
                            {fmtInt(cache.per_index[n].local_buckets)} local buckets,{' '}
                            {fmtBytes(cache.per_index[n].local_bytes)}
                        </Muted>
                    </Checkbox>
                ))
            ) : (
                <Muted>
                    This target reported no per-index cache breakdown, so pick &quot;all
                    indexes&quot; or nothing.
                </Muted>
            )}

            <SectionHeading>Or everything</SectionHeading>
            <Checkbox checked={all} onChange={() => setAll((v) => !v)} value="all">
                <strong>All indexes</strong>{' '}
                <Muted $small>(drops the whole cache on this target)</Muted>
            </Checkbox>

            {result && (
                <>
                    <SectionHeading>Result</SectionHeading>
                    <DL termWidth="180px">
                        <DL.Term>Attempted</DL.Term>
                        <DL.Description>
                            {fmtInt((result.eviction || {}).attempted)} buckets
                        </DL.Description>
                        <DL.Term>Evicted</DL.Term>
                        <DL.Description>
                            {fmtInt((result.eviction || {}).evicted)} buckets,{' '}
                            {fmtBytes((result.eviction || {}).bytes_evicted)}
                        </DL.Description>
                        <DL.Term>Refused</DL.Term>
                        <DL.Description>{fmtInt((result.eviction || {}).failed)} buckets</DL.Description>
                        <DL.Term>Local before</DL.Term>
                        <DL.Description>
                            {fmtInt((result.before || {}).local_buckets)} buckets,{' '}
                            {fmtBytes((result.before || {}).local_bytes)}
                        </DL.Description>
                        <DL.Term>Local after</DL.Term>
                        <DL.Description>
                            {fmtInt((result.after || {}).local_buckets)} buckets,{' '}
                            {fmtBytes((result.after || {}).local_bytes)}
                        </DL.Description>
                    </DL>
                    {(result.eviction || {}).failed > 0 && (
                        <Note>
                            A bucket with a live reader cannot be evicted. Refusals are correct
                            behaviour rather than a fault.
                        </Note>
                    )}
                </>
            )}
        </Dialog>
    );
}

EvictModal.propTypes = {
    targetId: PropTypes.any.isRequired,
    cache: PropTypes.object,
    loading: PropTypes.bool,
    onClose: PropTypes.func.isRequired,
    onDone: PropTypes.func,
};
