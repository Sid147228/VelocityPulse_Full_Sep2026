# Microsoft Entra ID authentication setup

VelocityPulse now uses Microsoft Entra ID (Microsoft identity platform) instead of the previous local/static username form.

## 1. Create an app registration

In the Microsoft Entra admin center:

1. Open **App registrations** and choose **New registration**.
2. Name it, for example, **VelocityPulse**.
3. Choose the supported account type appropriate for your organisation.
4. Under **Redirect URI**, choose **Web** and add:
   `http://localhost:5000/auth/callback`
5. Register the application.

For a deployed environment, add its HTTPS callback as another Web redirect URI, for example:
`https://velocitypulse.example.com/auth/callback`

## 2. Create a client secret

Under **Certificates & secrets**, create a client secret.

Copy the **secret value** immediately. Do not commit it to GitHub.

## 3. API permissions

Under **API permissions**, add Microsoft Graph delegated permission:

- `User.Read`

This is used for the sign-in scope. VelocityPulse currently stores only identity claims required for the local session; it does not persist a Microsoft access token.

## 4. Configure the local environment

Copy:

`.env.example` -> `.env`

Then set:

- `FLASK_SECRET_KEY`
- `MICROSOFT_CLIENT_ID`
- `MICROSOFT_CLIENT_SECRET`
- `MICROSOFT_TENANT_ID`

For organisation-only access, use the Directory (tenant) ID rather than `common`.

The callback URI in `.env` must exactly match a Web redirect URI in the Entra app registration.

## 5. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

## 6. Start VelocityPulse

```powershell
python app.py
```

Open:

`http://localhost:5000`

Unauthenticated users will be redirected to `/login`. Selecting **Sign in with Microsoft** starts the Microsoft authorization-code flow and returns to `/auth/callback`.

## Security behaviour

- Application routes require an authenticated Microsoft session.
- Socket.IO connections reject unauthenticated sessions.
- The old arbitrary username/password form has been removed.
- Microsoft passwords are never sent to or stored by VelocityPulse.
- Client ID, secret, tenant ID and Flask secret are external configuration.
- `.env` is ignored by Git and must remain local.
- Sign out clears the VelocityPulse session and redirects through the Microsoft logout endpoint.

## Production notes

Before deploying:

- Serve the app only over HTTPS.
- Set `SESSION_COOKIE_SECURE=true`.
- Use a persistent, high-entropy `FLASK_SECRET_KEY`.
- Configure the production callback and post-logout URLs in both Entra ID and the environment.
- Prefer a tenant-specific `MICROSOFT_TENANT_ID` if access should be restricted to your organisation.
- Keep client secrets in a secrets manager rather than a plaintext file.
