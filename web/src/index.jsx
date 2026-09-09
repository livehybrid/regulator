import React from 'react';
import { createRoot } from 'react-dom/client';

// First, and before anything that can reach the Splunk chart library: the
// charting bundle reads Splunk Web's page globals while it is being evaluated.
import './splunk-web-globals';

import App from './App';

const container = document.getElementById('regulator-root');
container.innerHTML = '';
createRoot(container).render(<App />);
