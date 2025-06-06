# VPN Service

This repository contains a simple VPN management bot and supporting services.

## Admin panel

An HTTP admin panel is provided using Flask with a simple Bootstrap-based UI.
To start it, run:

```bash
python -m admin.app
```

Then open `http://localhost:5000` in your browser.

By default the panel is protected with basic HTTP authentication using the
`ADMIN_PASSWORD` environment variable (leave empty to disable auth).
