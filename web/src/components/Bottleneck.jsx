/*
 * Where the limit was: the run's own figures, read against each other.
 *
 * The page below has a dozen separate signals. This is the reading of them:
 * which limit the run reached, if any, with the evidence beside it, what was
 * checked and found fine, and what could not be checked at all. The last two
 * are kept apart on purpose: "the indexers were not CPU-bound" and "nobody
 * could see the indexers" must not look the same.
 *
 * It is worked out by the control plane from thresholds (see bottleneck.py),
 * not by a model, and it says so.
 */
import React from 'react';
import PropTypes from 'prop-types';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import { Muted, Note, Panel, SectionHeading } from './primitives';

const Headline = styled.div`
    font-size: ${variables.fontSizeXLarge};
    font-weight: ${variables.fontWeightSemiBold};
    line-height: 1.3;
`;

const Part = styled.div`
    margin-top: ${variables.spacingMedium};
`;

const PartHeading = styled.div`
    font-weight: ${variables.fontWeightSemiBold};
    margin-bottom: ${variables.spacingXSmall};
`;

const List = styled.ul`
    margin: 0;
    padding-left: ${variables.spacingLarge};
`;

const STRENGTH = {
    strong: 'The evidence is strong',
    suggested: 'Suggested, not certain',
    none: '',
};

function Factor({ factor }) {
    return (
        <Part>
            <PartHeading>
                {factor.label}
                {!factor.named && <Muted> (below the line for naming it)</Muted>}
            </PartHeading>
            <List>
                {(factor.evidence || []).map((line) => (
                    <li key={line}>{line}</li>
                ))}
            </List>
            {factor.named && factor.advice && <Note>{factor.advice}</Note>}
        </Part>
    );
}

Factor.propTypes = { factor: PropTypes.object.isRequired };

export default function Bottleneck({ verdict }) {
    if (!verdict) {
        return null;
    }
    const factors = verdict.factors || [];
    const fine = verdict.fine || [];
    const unchecked = verdict.not_checked || [];
    return (
        <>
            <SectionHeading>Where the limit was</SectionHeading>
            <Panel>
                <Headline>{verdict.headline}</Headline>
                {STRENGTH[verdict.strength] && <Muted>{STRENGTH[verdict.strength]}.</Muted>}
                {verdict.strength === 'none' && factors.length === 0 && (
                    <Muted>
                        That is a result: this run did not push the target far enough to find one. Raise the
                        load to see what gives first.
                    </Muted>
                )}
                {factors.map((factor) => (
                    <Factor key={factor.kind} factor={factor} />
                ))}
                {verdict.context && (
                    <Part>
                        <Muted>{verdict.context}</Muted>
                    </Part>
                )}
                {fine.length > 0 && (
                    <Part>
                        <PartHeading>Checked, and not the limit</PartHeading>
                        <List>
                            {fine.map((line) => (
                                <li key={line}>{line}</li>
                            ))}
                        </List>
                    </Part>
                )}
                {unchecked.length > 0 && (
                    <Part>
                        <PartHeading>Could not be checked</PartHeading>
                        <List>
                            {unchecked.map((line) => (
                                <li key={line}>{line}</li>
                            ))}
                        </List>
                    </Part>
                )}
                <Part>
                    <Muted $small>{verdict.method}</Muted>
                </Part>
            </Panel>
        </>
    );
}

Bottleneck.propTypes = { verdict: PropTypes.object };
