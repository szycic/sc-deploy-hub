"""SC Deploy Hub.

A self-hosted deployment controller that listens for GitHub webhooks,
pulls repositories, runs configurable deploy steps, and restarts systemd
services — with a real-time web dashboard for monitoring and manual control.
"""