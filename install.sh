#!/bin/bash

# Ensure script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run this script with sudo: sudo ./install.sh"
  exit 1
fi

# Detect the real user who ran sudo
REAL_USER=${SUDO_USER:-$USER}
if [ "$REAL_USER" = "root" ]; then
  echo "Warning: You are installing as root. The sc-deploy-hub service will run under the root account."
fi

WORKSPACE_DIR=$(pwd)
echo "========================================================"
echo "Installing sc-deploy-hub in: $WORKSPACE_DIR"
echo "Target service user account: $REAL_USER"
echo "========================================================"

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
  echo "Creating Python virtual environment (.venv)..."
  sudo -u "$REAL_USER" python3 -m venv .venv
  if [ $? -ne 0 ]; then
    echo "Error: Failed to create virtual environment. Please install python3-venv (e.g. apt install python3-venv or similar for your distro)."
    exit 1
  fi
fi

# Install requirements
echo "Upgrading pip and installing requirements..."
sudo -u "$REAL_USER" .venv/bin/pip install --upgrade pip
sudo -u "$REAL_USER" .venv/bin/pip install -r requirements.txt
if [ $? -ne 0 ]; then
  echo "Error: Failed to install Python dependencies."
  exit 1
fi

# Create sudoers configuration for passwordless systemctl control commands
SUDOERS_FILE="/etc/sudoers.d/sc-deploy-hub"
echo "Configuring passwordless sudo for systemctl commands..."
echo "$REAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start *, /usr/bin/systemctl stop *, /usr/bin/systemctl restart *, /usr/bin/systemctl status *" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"
echo "Sudoers file written to $SUDOERS_FILE with permission 0440"

echo "========================================================"
echo "Installation Successful!"
echo "You can now start sc-deploy-hub with:"
echo "  .venv/bin/uvicorn sc_deploy_hub.main:app --host 0.0.0.0 --port 8000"
echo "========================================================"
