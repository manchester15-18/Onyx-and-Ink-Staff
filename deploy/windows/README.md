# Move Onyx & Ink Staff to Windows

Use Docker Desktop with its WSL 2 backend, Git for Windows, and an encrypted USB drive. Docker runs the same Linux environment used for the cloud deployment, so the Python dependencies and background inbox monitor behave consistently.

1. On Windows, install Docker Desktop, enable **Start Docker Desktop when you sign in**, enable its WSL 2 backend, and install Git for Windows. In Windows power settings, set the Ethernet-connected laptop to stay awake while plugged in; closing the lid or sleep pauses inbox monitoring and autonomous staff.
2. Clone `https://github.com/manchester15-18/Onyx-and-Ink-Staff.git` to a short local path such as `C:\OnyxAndInkStaff`.
3. On the Mac, stop autonomous staff and the inbox monitor from the dashboard after active work finishes. Use an encrypted removable drive, then run `deploy/windows/export-private-data.sh` with that drive's path. This copies private configuration, Google authorizations, inbox state, reports, chats, uploads, and activity records. None of those files are put in GitHub.
4. On Windows, attach the encrypted drive and run `deploy\windows\import-private-data.ps1 -SourceFolder <migration-folder>`. Use the actual `Onyx-Ink-Private-Migration-...` folder created on the drive.
5. In `C:\OnyxAndInkStaff\.env`, set `ONYX_REQUIRE_LOGIN=true`, temporarily set a new `DASHBOARD_PASSWORD`, and set `ONYX_ALLOWED_HOSTS` to the Windows Ethernet IP with port `8765`, for example `192.168.1.40:8765`. Start with `deploy\windows\start.ps1`.
6. Open `http://localhost:8765` on Windows. Confirm the inbox, reports, and Settings page work. Then remove `DASHBOARD_PASSWORD` from `.env` and run `deploy\windows\start.ps1` again; only its salted verifier remains in `work\wifi-access.json`.
7. Confirm a new inbox refresh and a dashboard chat reply before disabling the Mac copy. Keep the Mac offline from autonomous staff during the test so both machines never monitor or send as the same aliases simultaneously.

The Docker container restarts after Docker Desktop restarts. The `work` and `reports` folders stay on the Windows disk, outside the container. Use the existing GitHub backup for code; create a separate encrypted backup of those two private folders.
