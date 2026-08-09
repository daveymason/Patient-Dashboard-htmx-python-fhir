document.addEventListener('DOMContentLoaded', function () {
    'use strict';

    var testButton = document.getElementById('testProviderBtn');
    var resultDiv = document.getElementById('providerTestResult');
    if (!testButton || !resultDiv) {
        return;
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    // Built as DOM nodes rather than an innerHTML string: model IDs come back
    // from the provider, and concatenating them into markup would make a
    // hostile or malformed ID an injection point on an admin-only page.
    function render(data) {
        resultDiv.textContent = '';

        var line = document.createElement('div');
        line.className = data.ok ? 'text-success' : 'text-danger';
        line.textContent = data.message || 'Unexpected response.';
        resultDiv.appendChild(line);

        if (data.suggestions && data.suggestions.length) {
            var hint = document.createElement('div');
            hint.className = 'form-text mt-2';
            hint.textContent = 'Available on this account:';
            resultDiv.appendChild(hint);

            var list = document.createElement('ul');
            list.className = 'form-text mb-0 ps-3';
            data.suggestions.forEach(function (id) {
                var item = document.createElement('li');
                var code = document.createElement('code');
                code.textContent = id;
                item.appendChild(code);
                list.appendChild(item);
            });
            resultDiv.appendChild(list);
        }
    }

    function setPlainMessage(message, tone) {
        resultDiv.textContent = '';
        var line = document.createElement('div');
        line.className = tone ? 'text-' + tone : '';
        line.textContent = message;
        resultDiv.appendChild(line);
    }

    testButton.addEventListener('click', async function () {
        testButton.disabled = true;
        setPlainMessage('Checking...', 'muted');

        try {
            var response = await fetch('/settings/ai/test', {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken() }
            });
            render(await response.json());
        } catch (error) {
            console.error('Provider check failed:', error);
            setPlainMessage(
                'The provider could not be checked. Check your connection and try again.',
                'danger'
            );
        } finally {
            testButton.disabled = false;
        }
    });
});
