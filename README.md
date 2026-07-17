# SC Deploy Hub
This repository contains the source code for the `sc_deploy_hub` package.

The app is a small self-hosted FastAPI dashboard and deployment controller for automating git repository updates and systemd service management via GitHub webhooks or manual triggers.

It is intended for personal or local-network use and does not include production authentication or any other extra security by default. If you expose the app beyond a trusted LAN, add authentication, TLS, and restrict access to the API endpoints.

## Environment Variables
The following environment variables can be set to configure already included automations:

| Variable | Purpose | Example |
|---|---|---|
| `DB_PATH` | Custom path to the SQLite deployments database | `./data/deployments.db` |
| `CONFIG_PATH` | Custom path to the `config.yaml` configuration file | `./config.yaml` |

## Installation
To automate installation, virtual environment setup, and configure passwordless sudo systemctl commands on Linux, run:
```bash
sudo ./install.sh
```

Alternatively, to manually install the package, run:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows use `.venv\Scripts\activate`
pip install -r requirements.txt
```

## Running
To run the application, execute:
```bash
PYTHONPATH=src python -m uvicorn sc_deploy_hub.main:app --host 0.0.0.0 --port 8000
```

Then open the dashboard at:
```text
http://127.0.0.1:8000
```

The API is available under the versioned prefix:
```text
/api/v1/webhook
/api/v1/services
/api/v1/deployments
/api/v1/config
```