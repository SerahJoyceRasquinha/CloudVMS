# Gatehouse — Cloud Video Management System

Campus-gate video management with AI detection: live camera views, recording, unique
entry/exit counts for people and vehicles, and intrusion / restricted-area alerts with video
evidence, all in a web dashboard.

![Dashboard](docs/screenshots/dashboard.png)

## Run it

1. Download the project (**Code → Download ZIP**) and extract it.
2. Double-click **`RUN_VMS.bat`**.

That's it. The launcher checks what your PC already has and installs only what's missing
(Python 3.12, required libraries, the AI model, Google Chrome). Then it starts the server and
opens the dashboard in a new Chrome tab at <http://localhost:8000>.

> **First run:** it needs internet and takes several minutes (about 1 GB of downloads). Windows
> may ask for permission once. Later runs start in seconds.

**Sign in:** the first admin password is shown in the launcher window and saved in
`data\initial_admin_password.txt`.

**Stop:** close the window named *Cloud VMS server*.

<sub>Linux / macOS: run `./run_vms.sh` instead (Ctrl+C to stop).</sub>

## More

Features, architecture, datasets and cloud deployment: [docs/DETAILS.md](docs/DETAILS.md)
