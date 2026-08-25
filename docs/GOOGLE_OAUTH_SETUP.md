# Google OAuth Setup (per-user Gmail / Calendar / Drive)

This enables the **Connections** section in Settings, where each user connects
their own Google account. It powers send-as-user email (Phase 1), two-way
Calendar sync (Phase 2), and Drive attach/share (Phase 3).

The code is deployed and inert until the three server env vars below are set.
Until then, `GET /google/oauth/start` returns 503 and outbound mail keeps using
the firm SES/Gmail fallback.

## 1. Google Cloud Console

1. **Create/choose a project** (the same one that already holds the Google Maps
   keys is fine).
2. **Enable APIs** (APIs & Services → Library): **Gmail API**, **Google Calendar
   API**, **Google Drive API**.
3. **OAuth consent screen**:
   - User type: **Internal** if every user is in your Google Workspace;
     otherwise **External**.
   - Add scopes as they ship (start with Phase 1):
     - Phase 1: `openid`, `email`, `https://www.googleapis.com/auth/gmail.send`
     - Phase 2 (Calendar): `https://www.googleapis.com/auth/calendar`
     - Phase 3 (Drive): `https://www.googleapis.com/auth/drive.file`
   - While the app is in **Testing**, add each user as a **Test user** or they
     can't consent. Publish to remove that limit (sensitive scopes like
     `gmail.send`/`calendar` require Google verification for external apps;
     `drive.file` is not a restricted scope, which keeps review lighter).
4. **Credentials → Create credentials → OAuth client ID → Web application**:
   - Authorized redirect URIs:
     - Prod: `https://api.qualifiedcommercial.com/api/v1/google/oauth/callback`
     - Local: `http://localhost:8000/api/v1/google/oauth/callback`
   - Copy the **Client ID** and **Client secret**.

## 2. Backend env vars

Set on the server (and locally for testing):

```
GMAIL_OAUTH_CLIENT_ID=<client id>
GMAIL_OAUTH_CLIENT_SECRET=<client secret>
GOOGLE_OAUTH_REDIRECT_URI=https://api.qualifiedcommercial.com/api/v1/google/oauth/callback
```

(`GMAIL_OAUTH_CLIENT_ID` / `GMAIL_OAUTH_CLIENT_SECRET` already exist in config;
`GOOGLE_OAUTH_REDIRECT_URI` is new. Restart the backend after setting them.)

Token encryption reuses the existing provider-secret path: in prod, set
`PROVIDER_SECRETS_KMS_KEY_ID` (already used for Maps keys) so refresh tokens are
KMS-encrypted; otherwise they fall back to Fernet.

## 3. Verify

1. Log in, go to **Settings → Connections → Connect Google**, consent.
2. You return to `/settings?section=connections&connected=1`; the Gmail row
   shows **On** and your Google email appears.
3. DB check: one row in `google_accounts` for your user with a non-null
   `encrypted_refresh_token` and `gmail_connected = true`.
4. Send-as-user: approve a lender-send `EmailDraft` on a loan you own → the
   message lands in your Gmail **Sent** folder, `From:` is you. Disconnect →
   the same send falls back to firm SES.

## Notes / limits

- SES fallback only actually delivers once `SES_FROM_ADDRESS` is set (SES is
  dormant by default); until then a non-connected send no-ops.
- The firm Gmail service account (inbound lender poller) is unchanged — per-user
  OAuth is additive.
- Scopes are requested incrementally: connecting Calendar/Drive later re-runs
  consent with `include_granted_scopes=true` and merges scopes onto the same row.
