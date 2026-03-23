Google OAuth Credential Incident Response
=======================================

This document describes manual remediation steps for an exposed Google OAuth client credential.

Revoke credential
-----------------
1. Sign in to Google Cloud Console.
2. Open the affected project.
3. Go to APIs & Services -> Credentials.
4. Find the exposed OAuth 2.0 Client ID.
5. Delete it or disable it immediately.
6. If a refresh token was issued, also revoke the app from the affected Google account at https://myaccount.google.com/permissions.

Regenerate credential
---------------------
1. In Google Cloud Console, create a replacement OAuth 2.0 Client ID.
2. Download the new JSON credential.
3. Store it in `config/google/` or set `google.client_secret_path` to the exact file you want EOS to use.
4. Restart EOS.
5. Re-authorize from the Admin Panel so a fresh `data/google_token.json` is generated.

git filter-repo cleanup
-----------------------
Do not run history rewrite automatically during application startup or normal remediation.

If repository history cleanup is required, run it manually from a clean clone after rotating the credential:

git filter-repo --path-glob '*client_secret_*.json' --invert-paths
git push --force --all
git push --force --tags

Coordinate any forced push with repository administrators before executing it.
