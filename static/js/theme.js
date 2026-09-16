// Run before styles load so the saved theme is applied before the first paint.
(() => {
    const storageKey = 'bms-theme';
    const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
    let preference;
    const validTheme = value => value === 'dark' || value === 'light';
    try {
        preference = localStorage.getItem(storageKey);
    } catch (_) {
        // The toggle still works when browser storage is unavailable.
    }

    function applyTheme() {
        const theme = validTheme(preference) ? preference : (systemTheme.matches ? 'dark' : 'light');
        document.documentElement.dataset.bsTheme = theme;
        const toggle = document.getElementById('themeToggle');
        if (toggle) {
            // The icon already shows the mode the button switches to, so the
            // button is not given an 'active' fill on top of it.
            const next = theme === 'dark' ? 'light' : 'dark';
            toggle.setAttribute('aria-pressed', String(theme === 'dark'));
            toggle.setAttribute('aria-label', `Switch to ${next} mode`);
            toggle.setAttribute('title', `Switch to ${next} mode`);
        }
        document.dispatchEvent(new Event('themechange'));
    }

    applyTheme();
    systemTheme.addEventListener('change', () => {
        if (!validTheme(preference)) applyTheme();
    });
    window.addEventListener('storage', event => {
        if (event.key === storageKey || event.key === null) {
            preference = event.newValue;
            applyTheme();
        }
    });
    document.addEventListener('DOMContentLoaded', () => {
        applyTheme();
        document.getElementById('themeToggle')?.addEventListener('click', () => {
            preference = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
            try {
                localStorage.setItem(storageKey, preference);
            } catch (_) {
                // Keep the preference for this page even if it cannot be saved.
            }
            applyTheme();
        });
    });
})();
