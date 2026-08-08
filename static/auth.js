// MetaMask sign-in.
//
// The nonce is issued by the server and held in the session; it is never sent
// back in the request body, so it cannot be replayed. A valid signature proves
// control of the wallet -- the server still decides whether that wallet is
// approved for access.

document.addEventListener('DOMContentLoaded', function () {
    'use strict';

    var connectBtn = document.getElementById('connectMetamaskBtn');
    var statusEl = document.getElementById('authStatus');
    if (!connectBtn) {
        return;
    }

    function setStatus(message, tone) {
        if (!statusEl) {
            return;
        }
        statusEl.textContent = message || '';
        statusEl.className = 'mt-3 small' + (tone ? ' text-' + tone : '');
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function setBusy(busy, label) {
        connectBtn.disabled = busy;
        connectBtn.textContent = busy ? label : 'Sign in with MetaMask';
    }

    async function signIn(account) {
        setBusy(true, 'Requesting challenge...');
        setStatus('');

        var nonceResponse = await fetch('/get_nonce_to_sign/' + encodeURIComponent(account));
        var nonceData = await nonceResponse.json();
        if (!nonceResponse.ok || !nonceData.nonce) {
            setStatus(nonceData.error || 'Could not start sign-in. Please try again.', 'danger');
            setBusy(false);
            return;
        }

        setBusy(true, 'Waiting for signature...');
        var signature = await window.ethereum.request({
            method: 'personal_sign',
            params: [nonceData.nonce, account]
        });

        setBusy(true, 'Verifying...');
        var verifyResponse = await fetch('/verify_signature', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken()
            },
            body: JSON.stringify({
                signed_message: signature,
                wallet_address: account
            })
        });

        var verifyData = await verifyResponse.json();
        if (verifyResponse.ok && verifyData.success) {
            setStatus('Signed in. Redirecting...', 'success');
            window.location.assign('/');
            return;
        }

        setStatus(verifyData.error || 'Sign-in failed.', 'danger');
        setBusy(false);
    }

    connectBtn.addEventListener('click', async function () {
        if (typeof window.ethereum === 'undefined') {
            setStatus('MetaMask was not detected. Install it to sign in.', 'danger');
            return;
        }

        try {
            var accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
            if (!accounts || accounts.length === 0) {
                setStatus('No wallet account was made available.', 'danger');
                return;
            }
            await signIn(accounts[0]);
        } catch (error) {
            // 4001 is the EIP-1193 code for a user-rejected request.
            if (error && error.code === 4001) {
                setStatus('Sign-in cancelled.', 'muted');
            } else {
                console.error('Sign-in failed:', error);
                setStatus('Sign-in could not be completed. Please try again.', 'danger');
            }
            setBusy(false);
        }
    });
});
