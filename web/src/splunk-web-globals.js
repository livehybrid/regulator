/*
 * Page globals that Splunk Web defines and that @splunk/visualizations expects
 * to find.
 *
 * The charting bundle underneath the visualization library is the same code
 * Splunk Web ships, and it reads a couple of things off `window` that Splunk
 * Web's page bootstrap puts there. Regulator's console is not served by Splunk
 * Web, so nothing puts them there and the bundle throws on import:
 *
 *   TypeError: window.locale_name is not a function
 *
 * These are the smallest honest definitions of them. They are set here, in a
 * module imported before anything that reaches the chart library, because the
 * bundle reads them while it is being evaluated rather than when a chart is
 * first drawn: a shim applied after the import is a shim applied too late.
 *
 * Anything already defined is left alone, so this cannot break the case where
 * a future build of the console is served from inside Splunk Web after all.
 */

// The locale used for number and date formatting in charts. Splunk Web returns
// its own underscore form ("en_GB"), and the bundle expects that shape.
if (typeof window.locale_name !== 'function') {
    window.locale_name = () => 'en_GB';
}

// Splunk Web's page configuration. Only the locale is read by anything the
// console renders; the paths exist because code that reads $C tends to assume
// the whole object rather than one key of it.
if (typeof window.$C !== 'object' || window.$C === null) {
    window.$C = {
        LOCALE: 'en-GB',
        MRSPARKLE_ROOT_PATH: '',
        SPLUNKD_PATH: '',
        BUILD_NUMBER: '',
    };
}
