Place Google OAuth client JSON credentials in this directory.

Allowed patterns:
- `config/google/*.json` when using the default discovery behavior.
- An explicit `google.client_secret_path` value in `config.json` pointing to a specific JSON file.

Security notes:
- Do not commit real credential JSON files.
- Revoke and replace the credential immediately if it was exposed.
