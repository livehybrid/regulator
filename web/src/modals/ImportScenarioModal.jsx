/*
 * Turn a savedsearches.conf into a scenario.
 *
 * Preview before create, always: it shows which searches would take part and,
 * more usefully, why the rest would not.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Checkbox from '@splunk/react-ui/Checkbox';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Number from '@splunk/react-ui/Number';
import Select from '@splunk/react-ui/Select';
import Text from '@splunk/react-ui/Text';
import TextArea from '@splunk/react-ui/TextArea';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import SavedSearchTable from '../components/SavedSearchTable';
import { Grid2, Note, SectionHeading } from '../components/primitives';
import { api } from '../api';
import { fmtInt } from '../format';
import { useNotify } from '../Notifications';

const slug = (s) =>
    String(s || 'imported')
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 60) || 'imported';

export default function ImportScenarioModal({ prefill, suggestedName, onClose, onCreated }) {
    const [form, setForm] = useState({
        name: slug(suggestedName),
        savedsearches: prefill || '',
        load_model: 'closed',
        virtual_users: 10,
        duration_s: 600,
        think_median_s: 30,
        index: 'main',
        schedule_start: '',
        time_from_saved: 'derived',
        only_enabled: true,
        only_scheduled: false,
        allow_side_effects: false,
    });
    const [preview, setPreview] = useState(null);
    const notify = useNotify();

    const set = (k) => (e, data) => setForm((f) => ({ ...f, [k]: data.value }));
    const toggle = (k) => () => setForm((f) => ({ ...f, [k]: !f[k] }));

    const body = () => ({
        ...form,
        schedule_start: form.schedule_start.trim() || null,
        // A schedule-model run only makes sense over scheduled searches, so it
        // forces the filter rather than silently producing an empty scenario.
        only_scheduled: form.only_scheduled || form.load_model === 'schedule',
    });

    const doPreview = async () => {
        const rows = await api('/scenarios/preview', { method: 'POST', body: body() });
        setPreview(rows || []);
    };

    const doCreate = async () => {
        const created = await api('/scenarios', { method: 'POST', body: body() });
        notify(
            `Scenario ${created.name} created with ${fmtInt((created.saved_selected || []).length)} searches`,
            'success'
        );
        onCreated(created);
    };

    const included = preview ? preview.filter((r) => !r.skipped_reason).length : 0;

    return (
        <Dialog
            title="Create a scenario from saved searches"
            width="1080px"
            onClose={onClose}
            footer={
                <>
                    <BusyButton label="Preview" onClick={doPreview} />
                    <BusyButton appearance="primary" label="Create scenario" onClick={doCreate} />
                </>
            }
        >
            <Note>
                Paste a savedsearches.conf (any app&apos;s default/ or local/ file), or use the one
                loaded from a target. Regulator keeps the file as it is and runs the searches from
                it.
            </Note>

            <Grid2>
                <ControlGroup label="Scenario name" labelPosition="top">
                    <Text value={form.name} onChange={set('name')} />
                </ControlGroup>
                <ControlGroup
                    label="Load model"
                    labelPosition="top"
                    help={
                        form.load_model === 'closed'
                            ? 'Virtual users sampling the searches, weighted by how often each is scheduled.'
                            : "Every scheduled search fires on its own cron, as Splunk's scheduler would."
                    }
                >
                    <Select value={form.load_model} onChange={set('load_model')}>
                        <Select.Option label="closed: virtual users" value="closed" />
                        <Select.Option label="schedule: the cron itself" value="schedule" />
                    </Select>
                </ControlGroup>
                <ControlGroup label="Virtual users (closed model)" labelPosition="top">
                    <Number value={form.virtual_users} min={1} onChange={set('virtual_users')} />
                </ControlGroup>
                <ControlGroup label="Duration in seconds" labelPosition="top">
                    <Number value={form.duration_s} min={1} onChange={set('duration_s')} />
                </ControlGroup>
                <ControlGroup label="Median think time in seconds" labelPosition="top">
                    <Number value={form.think_median_s} min={0} onChange={set('think_median_s')} />
                </ControlGroup>
                <ControlGroup
                    label="Corpus index"
                    labelPosition="top"
                    help="Used by the empty-corpus check before a run starts."
                >
                    <Text value={form.index} onChange={set('index')} />
                </ControlGroup>
                <ControlGroup
                    label="Virtual clock start HH:MM"
                    labelPosition="top"
                    help="Schedule model only. Blank starts from now."
                >
                    <Text value={form.schedule_start} onChange={set('schedule_start')} />
                </ControlGroup>
                <ControlGroup
                    label="Time ranges"
                    labelPosition="top"
                    help={
                        form.time_from_saved === 'derived'
                            ? "Keep each search's window, rolled with jitter so the cache is not what is measured."
                            : 'Pass -24h@h and friends straight through, exactly as the scheduler runs them.'
                    }
                >
                    <Select value={form.time_from_saved} onChange={set('time_from_saved')}>
                        <Select.Option label="derived: roll the window" value="derived" />
                        <Select.Option label="as saved: pass through" value="as_saved" />
                    </Select>
                </ControlGroup>
            </Grid2>

            <Checkbox checked={form.only_enabled} onChange={toggle('only_enabled')}>
                Only enabled searches
            </Checkbox>
            <Checkbox checked={form.only_scheduled} onChange={toggle('only_scheduled')}>
                Only scheduled searches
            </Checkbox>
            <Checkbox checked={form.allow_side_effects} onChange={toggle('allow_side_effects')}>
                Allow searches that write somewhere (collect, outputlookup, sendemail). Off unless
                you mean it: a load test replays a search hundreds of times
            </Checkbox>

            <ControlGroup label="savedsearches.conf" labelPosition="top">
                <TextArea
                    value={form.savedsearches}
                    rowsMax={14}
                    spellCheck={false}
                    onChange={set('savedsearches')}
                />
            </ControlGroup>

            {preview && (
                <>
                    <SectionHeading>
                        {fmtInt(included)} of {fmtInt(preview.length)} searches would take part
                    </SectionHeading>
                    <SavedSearchTable rows={preview} />
                </>
            )}
        </Dialog>
    );
}

ImportScenarioModal.propTypes = {
    prefill: PropTypes.string,
    suggestedName: PropTypes.string,
    onClose: PropTypes.func.isRequired,
    onCreated: PropTypes.func.isRequired,
};
