# Bambu Lab

LayerNexus connects to **Bambu Lab** 3D printers via the [Bambu Lab Cloud API](https://github.com/coelacant1/Bambu-Lab-Cloud-API). You can upload G-code, start and cancel prints, and monitor progress — all from the LayerNexus dashboard, just like with Klipper printers.

!!! info "No Extra Containers Needed"
    Unlike OrcaSlicer or Spoolman, Bambu Lab integration does not require an additional Docker container. Communication goes through the Bambu Lab Cloud API (HTTPS), MQTT (for print control), and optionally local FTP (for faster uploads).

---

## Supported Printers

LayerNexus works with any Bambu Lab printer registered to your Bambu Lab Cloud account:

- **X1 Series** — X1, X1 Carbon, X1E
- **P1 Series** — P1P, P1S
- **A1 Series** — A1, A1 Mini

---

## Prerequisites

Before connecting a Bambu Lab printer, you need:

1. A **Bambu Lab Cloud account** (the same one you use in Bambu Studio or Bambu Handy)
2. Your printer must be **registered** to your Bambu Lab account (set up via Bambu Handy or Bambu Studio)
3. Your printer must be **powered on and connected** to the internet

!!! tip "LAN IP Address (Optional)"
    If your printer is on the same network as LayerNexus, you can optionally provide its local IP address for **faster G-code uploads** via FTP. Without a LAN IP, uploads go through the Bambu Lab Cloud (slower but works from anywhere).

---

## Connecting Your Account

LayerNexus uses a **3-step wizard** to connect your Bambu Lab account. You need the **Operator** or **Admin** role to set up printers.

### Step 1: Log In

1. Go to **Bambu Lab Accounts** in the navigation bar (or click **Connect Bambu Lab** on the Printers page).
2. Click **Connect Account**.
3. Enter your Bambu Lab Cloud **email** and **password**.
4. Select your **region** (Global or China).
5. Click **Send Verification Code**.

!!! note "Your Password Is Never Stored"
    LayerNexus uses your password only to trigger the 2FA verification email. It is never saved to the database.

### Step 2: Verify with 2FA Code

1. Check your email for a **6-digit verification code** from Bambu Lab.
2. Enter the code in the verification form.
3. Click **Verify**.

!!! warning
    The verification code expires after a few minutes. If it expires, go back and request a new code.

### Step 3: Select Your Printer

1. LayerNexus retrieves a list of printers registered to your account.
2. Select the printer you want to connect.
3. Optionally enter the printer's **LAN IP address** for faster uploads (you can find this in your printer's network settings or your router's device list).
4. Click **Connect Printer**.

LayerNexus creates a **printer profile** for the selected device. You can find it on the **Printers** page alongside your Klipper printers.

!!! tip "Multiple Printers"
    If you have multiple Bambu Lab printers on the same account, run the wizard again to add each one. They will all share the same Cloud account credentials.

---

## How It Works

### G-code Upload

When you upload G-code to a Bambu Lab printer, LayerNexus uses one of two methods:

| Method | When Used | Speed |
|---|---|---|
| **LAN FTP** | When a LAN IP address is configured | :material-speedometer: Fast |
| **Cloud Upload** | When no LAN IP is configured | :material-cloud-upload: Slower |

LAN FTP uploads the file directly to the printer over your local network. Cloud upload sends the file through Bambu Lab's servers, which is slower but works even when LayerNexus and the printer are on different networks.

### Print Control

Print commands (start, cancel) are sent via **MQTT** through the Bambu Lab Cloud broker. This works regardless of whether a LAN IP is configured.

### Status Monitoring

LayerNexus queries print status via MQTT (with a Cloud API fallback). The status is normalized to the same format used by Klipper printers:

| Bambu Lab State | LayerNexus Status |
|---|---|
| RUNNING | Printing |
| PAUSE | Printing (paused) |
| FINISH | Completed |
| FAILED | Failed |
| IDLE | Idle |

---

## Managing Accounts

### Viewing Connected Accounts

Go to **Bambu Lab Accounts** in the navigation bar to see all connected accounts with:

- Account email and region
- Token expiry time
- Connected printers

### Token Refresh

Bambu Lab Cloud tokens expire after approximately **24 hours**. When a token expires:

1. Go to **Bambu Lab Accounts**.
2. Click **Refresh Token** on the expired account.
3. Complete the login and 2FA verification again.

!!! info
    LayerNexus shows the token expiry time on the accounts page so you can proactively refresh before it expires.

### Disconnecting an Account

1. Go to **Bambu Lab Accounts**.
2. Click **Disconnect** on the account you want to remove.
3. Confirm the action.

!!! warning
    Disconnecting an account does not delete the associated printer profiles, but those printers will no longer be able to connect until you re-authenticate.

---

## Security

- **Passwords** are never stored — they are only used during the login flow to trigger 2FA
- **Cloud tokens** are encrypted at rest using AES (Fernet) derived from Django's `SECRET_KEY`
- **Communication** with Bambu Lab Cloud uses HTTPS (API) and TLS (MQTT on port 8883)
- All Bambu Lab management views require the **Operator** or **Admin** role

---

## Troubleshooting

**"No printers found" in Step 3?**

- Make sure your printer is registered to your Bambu Lab account (check in Bambu Handy or Bambu Studio)
- Verify the printer is powered on and connected to the internet

**Token expired / "Account inactive"?**

- Bambu Lab tokens expire after ~24 hours. Click **Refresh Token** to re-authenticate
- If refresh fails, try disconnecting and reconnecting the account

**Upload fails with LAN IP?**

- Verify the IP address is correct (check your printer's screen under Network settings)
- Make sure the LayerNexus Docker container can reach the printer's IP on your local network
- Try removing the LAN IP to fall back to Cloud upload

**Print doesn't start?**

- Check that the printer is online and not already printing
- Verify the Cloud token hasn't expired (check **Bambu Lab Accounts**)
- Check the LayerNexus logs for MQTT connection errors

**Can I use this without internet?**

- No. Bambu Lab integration requires internet access for the Cloud API and MQTT broker. Even with a LAN IP configured, the MQTT broker (for print commands) runs in the Bambu Lab Cloud.

---

## Next Steps

- [Klipper / Moonraker integration](moonraker.md) — for Klipper-based printers
- [Track filament with Spoolman](spoolman.md)
- [Manage the print queue](../user-guide/printing.md)
