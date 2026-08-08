// Shared HTMX wiring.
//
// The previous version of this file carried a 60-second client-side dashboard
// cache that could never run: it assigned xhr.onload and then cancelled the
// request via preventDefault(), so the request was never sent and the handler
// never fired. It also drove a four-step "progress" animation that started
// after the content had already arrived. Both are gone -- caching belongs on
// the server, and a progress bar that reports no progress is worse than none.

(function () {
    'use strict';

    // Attach the CSRF token to every mutating HTMX request.
    document.body.addEventListener('htmx:configRequest', function (event) {
        var method = (event.detail.verb || 'get').toLowerCase();
        if (method === 'get' || method === 'head') {
            return;
        }
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) {
            event.detail.headers['X-CSRFToken'] = meta.getAttribute('content');
        }
    });

    // Highlight the sidebar entry matching whatever was just loaded.
    document.body.addEventListener('htmx:afterSwap', function (event) {
        var config = event.detail.requestConfig;
        if (!config || !config.path) {
            return;
        }
        document.querySelectorAll('.sidebar .nav-link').forEach(function (link) {
            var target = link.getAttribute('hx-get');
            link.classList.toggle('active', Boolean(target) && config.path.indexOf(target) === 0);
        });
    });

    // Surface failures instead of leaving a panel silently blank.
    document.body.addEventListener('htmx:responseError', function (event) {
        var target = event.detail.target;
        if (target && !target.innerHTML.trim()) {
            target.innerHTML =
                '<div class="alert alert-danger mb-0">' +
                '<i class="fas fa-exclamation-triangle me-2"></i>' +
                'This section could not be loaded. Please try again.</div>';
        }
    });

    document.body.addEventListener('htmx:sendError', function (event) {
        var target = event.detail.target;
        if (target) {
            target.innerHTML =
                '<div class="alert alert-danger mb-0">' +
                '<i class="fas fa-plug me-2"></i>' +
                'Lost connection to the server. Check your network and try again.</div>';
        }
    });
})();
