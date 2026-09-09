/**
 * Copy text, and say honestly whether it worked.
 *
 * The clipboard API refuses outside a secure context, which is exactly where
 * an internal control plane on plain http lives, so the failure path matters
 * as much as the success one.
 */
export async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (e) {
        // eslint-disable-next-line no-console
        console.log(text);
        return false;
    }
}
