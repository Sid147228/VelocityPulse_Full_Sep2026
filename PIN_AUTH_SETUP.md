# VelocityPulse PIN authentication setup

VelocityPulse uses a local work-email + 6-digit PIN login flow.

## How it works

- First-time users open `/register`.
- They register a display name, work email and personal 6-digit PIN.
- VelocityPulse stores only a salted scrypt hash of the PIN in SQLite.
- Users then sign in with work email + PIN.
- After repeated failed attempts, the account is temporarily locked.
- Sessions expire after a configurable period of inactivity.
- Socket.IO/live-progress connections require the same authenticated session.

## 1. Configure .env

Copy:

`.env.example` -> `.env`

At minimum set stable values for:

```env
FLASK_SECRET_KEY=<long-random-value>
PIN_PEPPER=<different-long-random-value>
PIN_REGISTRATION_CODE=<organisation-registration-code>
```

Recommended organisation restriction:

```env
PIN_ALLOWED_EMAIL_DOMAINS=company.com
```

Multiple domains can be comma separated:

```env
PIN_ALLOWED_EMAIL_DOMAINS=company.com,subsidiary.co.uk
```

Security controls:

```env
PIN_MAX_FAILED_ATTEMPTS=5
PIN_LOCKOUT_MINUTES=15
PIN_SESSION_MINUTES=480
```

For HTTPS deployments:

```env
SESSION_COOKIE_SECURE=true
```

## 2. Generate secrets

PowerShell:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Run it twice and use different values for `FLASK_SECRET_KEY` and `PIN_PEPPER`.

Use another random value for `PIN_REGISTRATION_CODE`.

Do not commit `.env`.

## 3. Install and run

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open:

`http://localhost:5000`

## 4. First-time registration

Open **Register your PIN** from the login page.

Enter:

- Name
- Work email
- Organisation registration code, when configured
- Personal 6-digit PIN
- PIN confirmation

After registration the user is signed in automatically.

The local database is created automatically at:

`instance/velocitypulse_auth.db`

It is ignored by Git.

## Login protection

Default behaviour:

- 6-digit numeric PIN
- scrypt password hashing
- optional server-side PIN pepper
- 5 failed attempts before lockout
- 15-minute lockout
- 8-hour inactivity timeout
- session cookie is HttpOnly and SameSite=Lax
- authenticated Socket.IO connections only

## Important production notes

A six-digit PIN has limited entropy. The lockout controls, registration code and network access controls are therefore important.

For organisation deployment:

1. Serve VelocityPulse over HTTPS.
2. Set `SESSION_COOKIE_SECURE=true`.
3. Set a strong `FLASK_SECRET_KEY`.
4. Set a strong, stable `PIN_PEPPER`.
5. Set `PIN_REGISTRATION_CODE`.
6. Restrict `PIN_ALLOWED_EMAIL_DOMAINS`.
7. Back up the SQLite auth database securely.
8. Keep the application behind the organisation network/VPN/reverse proxy where possible.
9. Do not share PINs between users.

Changing `PIN_PEPPER` after users have registered invalidates existing PIN hashes, so treat it as a persistent secret.
